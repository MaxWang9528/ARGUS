"""The orchestrator: builds every component and runs the clip pipeline.

FROZEN for branch agents. Real modules are loaded by the names in ``_REAL``; any
that can't be imported fall back to the fake, so each branch's work is picked up
as soon as it is merged.

The dashboard (branch E) gets one ``App`` object and uses:
    app.store, app.bus, app.db, app.recorder, app.renderer,
    app.clips (ClipService), app.rate_history(), app.recent_logs(), app.components
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import shutil
import time
from collections import deque
from pathlib import Path
from typing import Any

from .core import fakes
from .core.config import LAYOUTS, ConfigStore
from .core.db import Database
from .core.events import EventBus, Topic
from .core.interfaces import WindowUnavailable
from .core.models import Clip, ClipStatus, LogLine, PeakEvent, RateSample
from .detect.judge import MomentJudge
from .detect.speech import SpeechCheck
from .discover.service import StreamDiscovery

log = logging.getLogger("argus.app")

# component -> (module, class name, fake class)
_REAL: dict[str, tuple[str, str, type]] = {
    "chat": ("argus.chat.twitch_irc", "TwitchChatSource", fakes.FakeChatSource),
    "detector": ("argus.detect.peak", "ChatPeakDetector", fakes.FakePeakDetector),
    "recorder": ("argus.capture.recorder", "Recorder", fakes.FakeRecorder),
    "cutter": ("argus.capture.cutter", "Cutter", fakes.FakeCutter),
    "renderer": ("argus.render.renderer", "FfmpegRenderer", fakes.FakeRenderer),
    "uploader": ("argus.upload.tiktok_browser", "TikTokUploader", fakes.FakeUploader),
    "upload_queue": ("argus.upload.queue", "ClipUploadQueue", fakes.FakeUploadQueue),
}


class ClipError(Exception):
    """A dashboard action that can't be done (unknown clip, wrong status, ...)."""


class BusLogHandler(logging.Handler):
    """Forwards ``logging`` records from ``argus.*`` to Topic.LOG.

    Use ``logging.getLogger("argus.<area>")``. Pass ``extra={"streamer": name}``
    to tag a line with a streamer.
    """

    def __init__(self, bus: EventBus, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(logging.DEBUG)
        self.bus, self.loop = bus, loop

    def emit(self, record: logging.LogRecord) -> None:
        level = record.levelname.lower()
        line = LogLine(
            ts=record.created, level=level if level in ("debug", "info", "warning", "error") else "error",
            source=record.name.removeprefix("argus."), streamer=getattr(record, "streamer", ""),
            message=record.getMessage())
        try:
            self.loop.call_soon_threadsafe(self.bus.publish, Topic.LOG, line)
        except RuntimeError:
            pass  # loop closed


class ClipService:
    """Turns peaks into clips and carries out the dashboard's clip actions."""

    WAIT_GRACE_S = 120.0  # how long past window_end to wait for the buffer

    def __init__(self, app: App) -> None:
        self.app = app
        self._tasks: set[asyncio.Task] = set()

    # ---- helpers -------------------------------------------------------------
    def _save(self, clip: Clip) -> None:
        self.app.db.save_clip(clip)
        # A copy: the clip object keeps changing, and subscribers read events later.
        self.app.bus.publish(Topic.CLIP, clip.model_copy(deep=True))

    def _get(self, clip_id: str) -> Clip:
        clip = self.app.db.get_clip(clip_id)
        if clip is None:
            raise ClipError(f"no clip with id {clip_id}")
        return clip

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- pipeline ------------------------------------------------------------
    def recover(self) -> None:
        """Clips left half-made by a previous run (ARGUS stopped mid-way). Nothing is
        working on them any more, so they would sit in "being made" forever.
        - waiting/cutting: the buffer is wiped at start-up, so they can't be cut: dropped.
        - rendering with the raw cut on disk: rendered again."""
        stuck = self.app.db.list_clips(
            status=[ClipStatus.WAITING, ClipStatus.CUTTING, ClipStatus.RENDERING], limit=10_000)
        for clip in stuck:
            if clip.status is ClipStatus.RENDERING and clip.raw_path and Path(clip.raw_path).exists():
                log.info("clip %s: ARGUS was stopped while it rendered; rendering again", clip.id,
                         extra={"streamer": clip.streamer})
                self._spawn(self._resume_render(clip))
                continue
            clip.status = ClipStatus.DROPPED
            clip.error = "interrupted: ARGUS was stopped before this clip was cut"
            self._save(clip)
            log.warning("clip %s dropped: %s", clip.id, clip.error, extra={"streamer": clip.streamer})

    async def _resume_render(self, clip: Clip) -> None:
        try:
            await self._render(clip, clip.active_layout)
            auto = self.app.store.get().general.auto_upload
            clip.status = ClipStatus.QUEUED if auto else ClipStatus.PENDING_REVIEW
            self._save(clip)
            self.app.save_clip_rates(clip)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            clip.status, clip.error = ClipStatus.FAILED, f"{type(exc).__name__}: {exc}"
            self._save(clip)
            log.exception("clip %s failed", clip.id, extra={"streamer": clip.streamer})

    async def run(self) -> None:
        self.recover()
        with self.app.bus.subscribe(Topic.PEAK) as sub:
            async for _, peak in sub:
                if peak.source == "auto":
                    self.on_peak(peak)

    def on_peak(self, peak: PeakEvent) -> Clip | None:
        cfg = self.app.store.get()
        det = cfg.detection_for(peak.streamer)
        self.app.db.save_peak(peak)
        status = self.app.recorder.status(peak.streamer)
        if not status.live:
            log.info("peak ignored: streamer is offline (checking again now)", extra={"streamer": peak.streamer})
            recheck = getattr(self.app.recorder, "request_live_check", None)  # not in the protocol
            if recheck is not None:
                recheck()
            return None
        if not status.watched:
            log.info("peak ignored: outside max_streamers, not recorded", extra={"streamer": peak.streamer})
            return None
        if peak.source == "auto" and not cfg.general.auto_clip:
            log.info("peak ignored: auto-clip is off", extra={"streamer": peak.streamer})
            return None
        if peak.source == "manual":
            start = peak.ts_start - det.manual_preroll_s
            end = peak.ts_end + det.manual_postroll_s
        else:
            # Short and centred on the moment: a little before chat started rising, and
            # after it, until chat calms down but no longer than max_after_peak_s past
            # the busiest second (chat can stay loud long after the moment itself).
            start = peak.ts_start - det.preroll_s
            end = min(peak.ts_end + det.postroll_s, peak.ts_peak + det.max_after_peak_s)
        end = min(max(end, start + 1.0), start + det.max_clip_s)
        if status.buffer_start is not None and start < status.buffer_start - 1.0 and peak.source == "auto":
            log.info("peak ignored: buffer doesn't cover the pre-roll yet", extra={"streamer": peak.streamer})
            return None
        clip = Clip(
            streamer=peak.streamer, peak_id=peak.id, window_start=start, window_end=end,
            active_layout=cfg.layout_for(peak.streamer).layout,
            caption=cfg.caption_for(peak.streamer, status.category), score=peak.score,
            rank=peak.rank)
        self._save(clip)
        self._spawn(self._process(clip, speech=peak.source == "auto"))
        return clip

    async def _process(self, clip: Clip, speech: bool = False) -> None:
        """Wait for the window, cut it, then render it while the speech check (auto clips
        only) transcribes the raw cut and the moment judge reads it. A failed speech check or
        judge never fails the clip."""
        app = self.app
        try:
            deadline = clip.window_end + self.WAIT_GRACE_S
            while not app.recorder.window_ready(clip.streamer, clip.window_end):
                if time.time() > deadline:
                    raise WindowUnavailable("the buffer never reached the end of the window")
                await asyncio.sleep(1.0)
            clip.status = ClipStatus.CUTTING
            self._save(clip)
            clip.raw_path = str(await app.cutter.cut(
                clip.streamer, clip.window_start, clip.window_end, clip.id))
            check = asyncio.create_task(self._checks(clip)) if speech else None
            try:
                await self._render(clip, clip.active_layout)
            except BaseException:
                if check is not None:
                    check.cancel()
                raise
            drop = await check if check is not None else None
            if drop:
                # Kept with its render: approving it still sends it to the upload queue.
                clip.status, clip.error = ClipStatus.DROPPED, drop
                self._save(clip)
                log.info("clip %s dropped: %s", clip.id, drop, extra={"streamer": clip.streamer})
                app.save_clip_rates(clip)
                return
            auto = app.store.get().general.auto_upload
            clip.status = ClipStatus.QUEUED if auto else ClipStatus.PENDING_REVIEW
            self._save(clip)
            log.info("clip %s ready (%s)", clip.id, clip.status.value, extra={"streamer": clip.streamer})
            app.save_clip_rates(clip)
        except WindowUnavailable as exc:
            clip.status, clip.error = ClipStatus.DROPPED, str(exc)
            self._save(clip)
            log.warning("clip %s dropped: %s", clip.id, exc, extra={"streamer": clip.streamer})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            clip.status, clip.error = ClipStatus.FAILED, f"{type(exc).__name__}: {exc}"
            self._save(clip)
            log.exception("clip %s failed", clip.id, extra={"streamer": clip.streamer})

    async def _checks(self, clip: Clip) -> str | None:
        """Auto clips: the speech check, then the moment judge (it reads the transcript).
        Returns a drop reason or None. Neither ever raises except on cancel."""
        drop = await self.app.speech.check(clip)
        if drop:
            return drop
        try:
            category = self.app.recorder.status(clip.streamer).category
        except Exception:  # noqa: BLE001 - only context for the judge
            category = ""
        return await self.app.judge.judge(clip, category)

    async def _render(self, clip: Clip, layout: str) -> None:
        clip.status = ClipStatus.RENDERING
        self._save(clip)
        layout_cfg = self.app.store.get().layout_for(clip.streamer, layout)
        clip.renders[layout] = str(await self.app.renderer.render(clip, layout_cfg))
        clip.active_layout = layout

    # ---- actions the dashboard calls ----------------------------------------
    def clip_now(self, streamer: str) -> Clip:
        """Clip a streamer by hand: manual_preroll_s before now to manual_postroll_s after."""
        now = time.time()
        peak = PeakEvent(streamer=streamer.lower(), ts_start=now, ts_peak=now, ts_end=now, source="manual")
        clip = self.on_peak(peak)
        if clip is None:
            raise ClipError(f"{streamer} is not live")
        self.app.bus.publish(Topic.PEAK, peak)
        return clip

    def approve(self, clip_id: str) -> Clip:
        """Send a reviewed clip to the upload queue. Also retries a failed upload."""
        clip = self._get(clip_id)
        # DROPPED covers clips that expired in the upload queue; one with a render can go back.
        if clip.status not in (ClipStatus.PENDING_REVIEW, ClipStatus.DRY_RUN_OK, ClipStatus.FAILED,
                               ClipStatus.REJECTED, ClipStatus.DROPPED):
            raise ClipError(f"clip is {clip.status.value}, can't approve it")
        if not clip.out_path:
            raise ClipError("clip has no rendered file")
        clip.status, clip.error = ClipStatus.QUEUED, ""
        self._save(clip)
        return clip

    upload_now = approve

    def reject(self, clip_id: str) -> Clip:
        clip = self._get(clip_id)
        if clip.status in (ClipStatus.UPLOADING, ClipStatus.UPLOADED):
            raise ClipError(f"clip is {clip.status.value}, can't reject it")
        clip.status = ClipStatus.REJECTED
        self._save(clip)
        return clip

    def set_caption(self, clip_id: str, caption: str) -> Clip:
        clip = self._get(clip_id)
        clip.caption = caption
        self._save(clip)
        return clip

    async def rerender(self, clip_id: str, layout: str) -> Clip:
        """Render the clip in another layout and make that the one to upload."""
        clip = self._get(clip_id)
        if layout not in LAYOUTS:
            raise ClipError(f"unknown layout {layout}")
        if not clip.raw_path:
            raise ClipError("clip has no raw file yet")
        if clip.status in (ClipStatus.WAITING, ClipStatus.CUTTING, ClipStatus.RENDERING,
                           ClipStatus.QUEUED, ClipStatus.UPLOADING):
            raise ClipError(f"clip is {clip.status.value}, can't re-render now")
        previous = clip.status
        try:
            await self._render(clip, layout)
            clip.status = previous
        except Exception as exc:  # noqa: BLE001
            clip.status, clip.error = ClipStatus.FAILED, f"{type(exc).__name__}: {exc}"
        self._save(clip)
        return clip


class App:
    def __init__(self, store: ConfigStore, *, demo: bool = False, serve: bool = True) -> None:
        self.store = store
        self.bus = EventBus() if store.bus is None else store.bus
        store.bus = self.bus
        self.demo, self.serve = demo, serve
        cfg = store.get()
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(cfg.data_dir / "argus.db")
        self.components: dict[str, str] = {}  # component -> "real" or "fake"
        common = {"store": store, "bus": self.bus}
        self.chat = self._make("chat", **common)
        self.detector = self._make("detector", **common)
        self.recorder = self._make("recorder", **common)
        self.cutter = self._make("cutter", recorder=self.recorder, **common)
        self.renderer = self._make("renderer", **common)
        self.uploader = self._make("uploader", **common)
        self.upload_queue = self._make("upload_queue", db=self.db, uploader=self.uploader, **common)
        # The speech check uses faster-whisper when it is installed; the demo uses a fake.
        self.speech = SpeechCheck(store, transcriber=fakes.FakeTranscriber() if demo else None)
        # Off by default; needs the anthropic package and a credential.
        self.judge = MomentJudge(store)
        # Stream discovery (off by default): adds the top live channels to the watch list.
        self.discovery = StreamDiscovery(store=store, bus=self.bus)
        checker = getattr(self.recorder, "live_checker", None)
        if checker is not None and hasattr(checker, "hints"):
            checker.hints = self.discovery.live_hint  # discovered channels aren't checked twice
        self.clips = ClipService(self)
        self._rates: dict[str, deque[RateSample]] = {}
        self._logs: deque[LogLine] = deque(maxlen=500)

    _REAL_IN_DEMO = ("upload_queue",)

    def _make(self, name: str, **kwargs: Any) -> Any:
        module, cls_name, fake = _REAL[name]
        # The real upload queue is pure logic (limits, ranking, account rotation); in the
        # demo it drives the fake uploader, so the Accounts page behaves for real.
        if not self.demo or name in self._REAL_IN_DEMO:
            try:
                cls = getattr(importlib.import_module(module), cls_name)
                self.components[name] = "real"
                return cls(**kwargs)
            except (ImportError, AttributeError) as exc:
                log.warning("%s: using the fake (%s)", name, exc)
        self.components[name] = "fake"
        return fake(**kwargs)

    # ---- data the dashboard reads -------------------------------------------
    # ---- chat rate for one clip (the dashboard replays it with the video) ----
    CLIP_RATES_PAD_S = 5.0

    def _clip_rates_path(self, clip_id: str) -> Path:
        return self.store.get().data_dir / "clips" / "rates" / f"{clip_id}.json"

    def _window_rates(self, clip: Clip) -> list[RateSample]:
        lo, hi = clip.window_start - self.CLIP_RATES_PAD_S, clip.window_end + self.CLIP_RATES_PAD_S
        return [r for r in self.rate_history(clip.streamer) if lo <= r.ts <= hi]

    def save_clip_rates(self, clip: Clip) -> None:
        """Keep the clip window's rate samples: the in-memory history only covers 15 min."""
        rates = self._window_rates(clip)
        if not rates:
            return
        path = self._clip_rates_path(clip.id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("[" + ",".join(r.model_dump_json() for r in rates) + "]", encoding="utf-8")
        except OSError as exc:
            log.warning("could not save chat rates for clip %s: %s", clip.id, exc)

    def clip_rates(self, clip: Clip) -> list[RateSample]:
        """Rate samples covering the clip window (padded 5 s each side), oldest first."""
        path = self._clip_rates_path(clip.id)
        try:
            return [RateSample.model_validate(r) for r in json.loads(path.read_text(encoding="utf-8"))]
        except FileNotFoundError:
            return self._window_rates(clip)
        except (OSError, ValueError) as exc:
            log.warning("bad chat rate file for clip %s: %s", clip.id, exc)
            return self._window_rates(clip)

    def rate_history(self, streamer: str) -> list[RateSample]:
        """Up to the last 15 minutes of rate samples."""
        return list(self._rates.get(streamer.lower(), ()))

    def recent_logs(self) -> list[LogLine]:
        return list(self._logs)

    async def _collect(self) -> None:
        with self.bus.subscribe(Topic.RATE, Topic.LOG) as sub:
            async for topic, payload in sub:
                if topic is Topic.RATE:
                    self._rates.setdefault(payload.streamer, deque(maxlen=900)).append(payload)
                else:
                    self._logs.append(payload)

    async def _serve(self) -> None:
        try:
            create_app = importlib.import_module("argus.api.server").create_app
        except (ImportError, AttributeError) as exc:
            log.warning("dashboard not available (%s)", exc)
            await asyncio.Event().wait()
            return
        import uvicorn

        g = self.store.get().general
        server = uvicorn.Server(uvicorn.Config(
            create_app(self), host=g.dashboard_host, port=g.dashboard_port, log_level="warning"))
        print(f"Dashboard: http://{g.dashboard_host}:{g.dashboard_port}")
        task = asyncio.create_task(server.serve())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancelling serve() directly makes uvicorn's lifespan print a traceback.
            server.should_exit = True
            await task
            raise

    # ---- retention -----------------------------------------------------------
    RETENTION_EVERY_S = 3600.0
    _BUSY = (ClipStatus.WAITING, ClipStatus.CUTTING, ClipStatus.RENDERING,
             ClipStatus.QUEUED, ClipStatus.UPLOADING)

    def cleanup(self, now: float | None = None) -> int:
        """Delete clip files older than ``general.retention_days`` (0 keeps everything).

        A clip in progress keeps its files. The clip row stays in the database with its
        file paths cleared, so the history (and the peak log's links) survive. Leftover
        cut scratch folders older than a day go too. Returns the number of files deleted.
        """
        cfg = self.store.get()
        days = cfg.general.retention_days
        if days <= 0:
            return 0
        now = time.time() if now is None else now
        cutoff = now - days * 86400
        clips_dir = (cfg.data_dir / "clips").resolve()
        removed = 0

        for clip in self.db.list_clips(limit=1_000_000, oldest_first=True):
            if clip.created_at >= cutoff:
                break
            if clip.status in self._BUSY or not (clip.raw_path or clip.renders or clip.debug_screenshot):
                continue
            removed += self._delete_clip_files(clip)
            clip.raw_path, clip.renders, clip.debug_screenshot = None, {}, None
            self.db.save_clip(clip)
            self.bus.publish(Topic.CLIP, clip.model_copy(deep=True))
        tmp = clips_dir / "tmp"
        if tmp.is_dir():
            for d in tmp.iterdir():
                if d.stat().st_mtime < now - 86400:
                    shutil.rmtree(d, ignore_errors=True)
        if removed:
            log.info("retention: deleted %d clip files older than %d days", removed, days)
        return removed

    def _delete_clip_files(self, clip: Clip) -> int:
        """Delete a clip's raw, rendered, screenshot and chat-rate files. Never touches
        anything outside data_dir. Returns how many files were deleted."""
        root = self.store.get().data_dir.resolve()
        removed = 0
        for path in (clip.raw_path, *clip.renders.values(), clip.debug_screenshot,
                     str(self._clip_rates_path(clip.id))):
            if not path:
                continue
            p = Path(path).resolve()
            if not p.is_relative_to(root):
                continue
            try:
                p.unlink()
                removed += 1
            except FileNotFoundError:
                pass
        return removed

    # ---- purge (dashboard) ------------------------------------------------------
    def delete_clip(self, clip_id: str) -> int:
        """Delete one clip: its files and its row. Clips still being made or uploaded
        can't be deleted (ClipError). Returns the number of files deleted."""
        clip = self.db.get_clip(clip_id)
        if clip is None:
            raise ClipError(f"no clip with id {clip_id}")
        if clip.status in self._BUSY:
            raise ClipError(f"clip is {clip.status.value}; wait for it to finish or reject it first")
        files = self._delete_clip_files(clip)
        self.db.delete_clip(clip_id)
        log.info("clip %s deleted (%d files)", clip_id, files, extra={"streamer": clip.streamer})
        return files

    def purge_clips(self, statuses: list[str] | None = None, older_than_h: float = 0.0) -> dict[str, int]:
        """Delete every clip in ``statuses`` (None = every status that isn't in progress)
        and older than ``older_than_h`` hours (0 = any age): files and rows. Clips being
        made, queued or uploading are always kept."""
        wanted = None if statuses is None else {ClipStatus(s) for s in statuses}
        cutoff = time.time() - older_than_h * 3600 if older_than_h > 0 else None
        clips = files = kept = 0
        for clip in self.db.list_clips(limit=1_000_000):
            if wanted is not None and clip.status not in wanted:
                continue
            if cutoff is not None and clip.created_at > cutoff:
                continue
            if clip.status in self._BUSY:
                kept += 1
                continue
            files += self._delete_clip_files(clip)
            self.db.delete_clip(clip.id)
            clips += 1
        if clips:
            log.info("purged %d clips (%d files)", clips, files)
        return {"clips": clips, "files": files, "kept_in_progress": kept}

    async def _retention(self) -> None:
        while True:
            try:
                self.cleanup()  # a few unlinks; publishes on the bus, so stays on the loop
            except Exception:
                log.exception("retention cleanup failed")
            await asyncio.sleep(self.RETENTION_EVERY_S)

    async def run(self) -> None:
        handler = BusLogHandler(self.bus, asyncio.get_running_loop())
        root = logging.getLogger("argus")
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        jobs = [self._collect(), self.clips.run(), self.chat.run(), self.detector.run(),
                self.recorder.run(), self.upload_queue.run(), self._retention(), self.discovery.run()]
        if self.serve:
            jobs.append(self._serve())
        try:
            async with asyncio.TaskGroup() as tg:
                for job in jobs:
                    tg.create_task(job)
        finally:
            root.removeHandler(handler)
            self.db.close()
