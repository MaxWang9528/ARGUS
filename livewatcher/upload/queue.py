"""The upload queue: uploads ``queued`` clips one at a time, within the rate limits.

- Oldest queued clip first. ``tiktok.dry_run`` is read right before each attempt.
- Real uploads respect ``max_uploads_per_day`` (successes in the last 24 h) and
  ``min_minutes_between_uploads`` (since the last success). A clip that has to wait
  stays ``queued``. Dry runs don't count and aren't limited.
- ``ok -> uploaded``, ``dry_run -> dry_run_ok``, ``blocked -> failed`` (no retry, and
  the whole queue pauses until a clip is approved by hand or the config changes),
  ``transient`` -> retried up to 2 times with a pause, then ``failed``.
- A clip is never left in ``uploading`` (the dashboard can't act on that status):
  interrupted uploads become ``failed`` with a "check TikTok" note.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..core.config import Config, ConfigStore
from ..core.db import Database
from ..core.events import EventBus, Topic
from ..core.interfaces import Uploader
from ..core.models import Clip, ClipStatus, UploadResult
from .tiktok_browser import check_video_file

log = logging.getLogger("livewatcher.upload")

DAY_S = 24 * 3600
INTERRUPTED = ("the app stopped while this clip was uploading; check your TikTok profile "
               "before approving it again, it may already be posted")

# A move to ``queued`` from one of these is a hand approval (ClipService.approve).
# The pipeline itself queues clips straight from ``rendering``. ClipService mutates
# and republishes one Clip object, so a late reader may see all of a new clip's
# events as ``queued``: an unknown previous status is therefore NOT a hand approval,
# and the statuses of clips that already exist are read from the database.
_MANUAL_FROM = frozenset({ClipStatus.PENDING_REVIEW, ClipStatus.DRY_RUN_OK, ClipStatus.FAILED,
                          ClipStatus.REJECTED})


class ClipUploadQueue:
    """Implements ``UploadQueue``."""

    MAX_RETRIES = 2

    def __init__(self, *, store: ConfigStore, bus: EventBus, db: Database, uploader: Uploader,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 poll_s: float = 5.0, retry_pause_s: float = 60.0) -> None:
        self.store, self.bus, self.db, self.uploader = store, bus, db, uploader
        self._clock, self._sleep = clock, sleep
        self.poll_s, self.retry_pause_s = poll_s, retry_pause_s
        self.paused_reason: str | None = None    # set after a blocked result
        self.waiting_reason: str | None = None   # set while a rate limit holds a clip
        self._transient: dict[str, int] = {}     # clip id -> transient failures so far
        self._last_status: dict[str, ClipStatus] = {}
        # Subscribe now, so no approval or config change is missed before run() starts.
        self._sub = bus.subscribe(Topic.CLIP, Topic.CONFIG_CHANGED, maxsize=1000)
        for clip in db.list_clips(status=list(_MANUAL_FROM), limit=100_000):
            self._last_status[clip.id] = clip.status

    # ---- life cycle ----------------------------------------------------------------------
    async def run(self) -> None:
        try:
            self.recover_interrupted()
            while True:
                await self.step()
                await self._sleep(self.poll_s)
        finally:
            self._sub.close()

    def recover_interrupted(self) -> None:
        """Clips left in ``uploading`` by a crash or a stop become ``failed``."""
        for clip in self.db.list_clips(status=ClipStatus.UPLOADING, limit=1000):
            clip.status, clip.error = ClipStatus.FAILED, INTERRUPTED
            self._save(clip)
            log.warning("clip %s was interrupted during an upload", clip.id, extra={"streamer": clip.streamer})

    async def step(self) -> bool:
        """Handle at most one queued clip. Returns True if an upload was attempted."""
        self._drain_events()
        if self.paused_reason:
            return False
        clips = self.db.list_clips(status=ClipStatus.QUEUED, oldest_first=True, limit=1)
        if not clips:
            self._set_waiting(None)
            return False
        return await self._handle(clips[0])

    # ---- pause / resume ------------------------------------------------------------------
    def _drain_events(self) -> None:
        while not self._sub.queue.empty():
            topic, payload = self._sub.queue.get_nowait()
            self.handle_event(topic, payload)

    def handle_event(self, topic: Topic, payload: Any) -> None:
        if topic is Topic.CONFIG_CHANGED:
            self._resume("the config changed")
        elif topic is Topic.CLIP and isinstance(payload, Clip):
            prev = self._last_status.get(payload.id)
            if payload.status is ClipStatus.QUEUED and prev in _MANUAL_FROM:
                self._transient.pop(payload.id, None)
                self._resume(f"clip {payload.id} was approved")
            if payload.status is ClipStatus.UPLOADED:
                self._last_status.pop(payload.id, None)
            else:
                self._last_status[payload.id] = payload.status

    def pause(self, reason: str) -> None:
        # Approvals and config changes from before this point are old news: drain
        # them first, so only a later one resumes the queue.
        self._drain_events()
        self.paused_reason = reason
        log.warning("upload queue paused: %s. It resumes when you approve a clip again or "
                    "change the settings.", reason)

    def _resume(self, why: str) -> None:
        if self.paused_reason:
            log.info("upload queue resumed (%s)", why)
            self.paused_reason = None

    # ---- rate limits ---------------------------------------------------------------------
    def limit_reason(self, cfg: Config, now: float) -> str | None:
        """Why a real upload can't happen at ``now``, or None if it can."""
        tk = cfg.tiktok
        done = self.db.count_uploads_since(now - DAY_S, kind="ok")
        if done >= tk.max_uploads_per_day:
            return f"daily limit reached ({done} of {tk.max_uploads_per_day} in the last 24 h)"
        last = self.db.last_upload_ts(kind="ok")
        gap = tk.min_minutes_between_uploads * 60
        if last is not None and now - last < gap:
            left = (last + gap - now) / 60
            return (f"keeping {tk.min_minutes_between_uploads} min between uploads "
                    f"(next in {left:.0f} min)")
        return None

    def _set_waiting(self, reason: str | None) -> None:
        if reason and reason.split(" (next")[0] != (self.waiting_reason or "").split(" (next")[0]:
            log.info("upload waiting: %s", reason)
        self.waiting_reason = reason

    # ---- one clip ------------------------------------------------------------------------
    async def _handle(self, clip: Clip) -> bool:
        extra = {"streamer": clip.streamer}
        problem = check_video_file(clip.out_path)
        if problem:
            # Specific to this clip, so it fails without pausing the queue.
            clip.status, clip.error = ClipStatus.FAILED, problem
            self._save(clip)
            log.warning("clip %s can't be uploaded: %s", clip.id, problem, extra=extra)
            return False

        attempted = False
        while True:
            dry_run = self.store.get().tiktok.dry_run
            if not dry_run:
                reason = self.limit_reason(self.store.get(), self._clock())
                if reason:
                    self._set_waiting(reason)
                    return attempted
            self._set_waiting(None)

            result = await self._attempt(clip, dry_run)
            attempted = True
            if result.kind == "ok":
                self._finish(clip, ClipStatus.UPLOADED, "")
                log.info("clip %s posted to TikTok", clip.id, extra=extra)
                return True
            if result.kind == "dry_run":
                self._finish(clip, ClipStatus.DRY_RUN_OK, "")
                log.info("clip %s dry run reached the Post button", clip.id, extra=extra)
                return True
            if result.kind == "blocked":
                self._finish(clip, ClipStatus.FAILED, result.error or "blocked")
                self.pause(f"clip {clip.id} was blocked: {result.error or 'no reason given'}")
                return True

            # transient
            count = self._transient.get(clip.id, 0) + 1
            self._transient[clip.id] = count
            if count > self.MAX_RETRIES:
                self._finish(clip, ClipStatus.FAILED,
                             f"{result.error or 'temporary error'} (gave up after {count} attempts)")
                log.warning("clip %s failed after %d attempts", clip.id, count, extra=extra)
                return True
            clip.status = ClipStatus.QUEUED
            clip.error = (f"{result.error or 'temporary error'}; retry {count} of {self.MAX_RETRIES} "
                          f"in {self.retry_pause_s:.0f} s")
            self._save(clip)
            log.info("clip %s: %s", clip.id, clip.error, extra=extra)
            await self._sleep(self.retry_pause_s)
            self._drain_events()
            fresh = self.db.get_clip(clip.id)
            if fresh is None or fresh.status is not ClipStatus.QUEUED or self.paused_reason:
                return True  # rejected or changed meanwhile
            clip = fresh

    async def _attempt(self, clip: Clip, dry_run: bool) -> UploadResult:
        clip.status = ClipStatus.UPLOADING
        self._save(clip)
        try:
            result = await self.uploader.upload(clip, clip.caption, dry_run)
        except asyncio.CancelledError:
            clip.status, clip.error = ClipStatus.FAILED, INTERRUPTED
            try:
                self._save(clip)
            except Exception:
                log.exception("couldn't save clip %s while stopping", clip.id)
            raise
        except Exception as exc:
            # The uploader broke its contract (it shouldn't raise). Don't retry blindly.
            log.exception("uploader raised", extra={"streamer": clip.streamer})
            result = UploadResult(kind="blocked", error=f"uploader error: {type(exc).__name__}: {exc}")
        self.db.add_upload(clip.id, result.kind, result.error, ts=self._clock())
        clip.debug_screenshot = result.screenshot
        return result

    def _finish(self, clip: Clip, status: ClipStatus, error: str) -> None:
        clip.status, clip.error = status, error
        self._transient.pop(clip.id, None)
        self._save(clip)

    def _save(self, clip: Clip) -> None:
        self.db.save_clip(clip)
        # A copy, so readers see this status even after the object changes again.
        self.bus.publish(Topic.CLIP, clip.model_copy(deep=True))
