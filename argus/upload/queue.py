"""The upload queue: uploads ``queued`` clips one at a time, to one of the TikTok
accounts, within each account's limits. See "Accounts" in docs/CONTRACTS.md.

- **Which clip:** manual ("clip now") clips first, then the highest ``clip.score``, then
  the oldest. A clip that has been queued for more than ``tiktok.max_queue_age_h`` hours
  (0 = never) is ``dropped``. The age counts from when the queue saw the clip become
  ``queued`` (a hand approval restarts it); clips already queued when the app starts
  count from the start.
- **Which account:** the enabled accounts with a free slot are the candidates: fewer
  than ``max_uploads_per_day`` real uploads in the last 24 h and at least
  ``min_minutes_between_uploads`` since that account's last one. With
  ``tiktok.cycle_accounts`` the next candidate after the account used last (round
  robin, dry runs included), otherwise the first candidate in list order. With no
  candidate the clip stays ``queued`` and ``waiting_reason`` says why.
- **Dry runs** (``tiktok.dry_run``, read before each attempt) don't count and aren't
  limited, but pick an account the same way, so every account's login gets tested.
- ``ok -> uploaded``, ``dry_run -> dry_run_ok``, ``transient`` -> retried up to 2 times
  with a pause, then ``failed``; ``blocked -> failed`` (never retried) and a pause:
  - a logged-out profile or a CAPTCHA pauses **that account** only (the others go on);
  - anything else blocked (TikTok's page changed, the browser can't start, an unclear
    state after Post) pauses **the whole queue**, because it would hit every account.
  The whole-queue pause lifts when a clip is approved by hand or the config changes.
  An account's pause lifts when its login is checked and found good or the config
  changes; a hand approval lifts it only when every enabled account is paused (with
  review on, every clip is approved by hand, so it would otherwise never hold).
- A clip is never left in ``uploading`` (the dashboard can't act on that status):
  interrupted uploads become ``failed`` with a "check TikTok" note.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..core.config import Config, ConfigStore, TikTokAccount
from ..core.db import Database
from ..core.events import EventBus, Topic
from ..core.interfaces import Uploader
from ..core.models import Clip, ClipStatus, UploadResult
from .login import profile_busy
from .tiktok_browser import blocked_scope, check_video_file

log = logging.getLogger("argus.upload")

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


# ---- accounts: limits and choice (pure functions of config, database and time) ------------
@dataclass
class AccountSlot:
    """One account's upload budget at a given moment."""

    account: TikTokAccount
    uploads_24h: int               # real uploads in the last 24 h
    last_upload_ts: float | None   # its last real upload
    next_free_ts: float | None     # when the next real upload is allowed; None = now, or
    #                                never (max_uploads_per_day = 0, see limit_reason)
    limit_reason: str | None       # why a real upload can't happen now; None = it can


def account_slot(account: TikTokAccount, db: Database, now: float) -> AccountSlot:
    times = db.upload_times_since(now - DAY_S, kind="ok", account=account.name)
    last = db.last_upload_ts(kind="ok", account=account.name)
    cap, gap = account.max_uploads_per_day, account.min_minutes_between_uploads * 60
    free_at: float | None = None
    reason: str | None = None
    if cap <= 0:
        reason = "max_uploads_per_day is 0"
    else:
        if len(times) >= cap:
            # Free once enough of the last 24 h's uploads have aged out.
            free_at = times[len(times) - cap] + DAY_S
            reason = f"daily limit reached ({len(times)} of {cap} in the last 24 h)"
        if last is not None and now - last < gap:
            free_at = max(free_at or 0.0, last + gap)
            reason = reason or (f"keeping {account.min_minutes_between_uploads} min between uploads")
        if reason and free_at is not None:
            reason += f" (next in {max(0.0, free_at - now) / 60:.0f} min)"
    return AccountSlot(account, len(times), last, free_at, reason)


def choose_account(cfg: Config, db: Database, now: float, *, dry_run: bool,
                   unavailable: dict[str, str] | None = None) -> tuple[TikTokAccount | None, str | None]:
    """The account the next upload goes to, or (None, why not).

    ``unavailable`` maps account names to a reason they can't be used now (paused,
    profile open). Dry runs ignore the limits but still rotate.
    """
    unavailable = unavailable or {}
    accounts = cfg.tiktok_accounts()
    enabled = [a for a in accounts if a.enabled]
    if not enabled:
        return None, "no TikTok account is enabled"
    reasons: list[str] = []
    free: set[str] = set()
    for a in enabled:
        why = unavailable.get(a.name)
        if why is None and not dry_run:
            why = account_slot(a, db, now).limit_reason
        if why is None:
            free.add(a.name)
        else:
            reasons.append(f"account {a.name}: {why}")
    if not free:
        return None, "; ".join(reasons)
    order = accounts
    if cfg.tiktok.cycle_accounts:
        names = [a.name for a in accounts]
        last = db.last_upload_account(names)
        if last is not None:
            i = names.index(last) + 1
            order = accounts[i:] + accounts[:i]
    return next(a for a in order if a.name in free), None


def _stable(reason: str | None) -> str:
    """A reason without its countdown, to log it only when it really changes."""
    return re.sub(r" \(next in [^)]*\)", "", reason or "")


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
        self._paused: str | None = None              # whole-queue pause, after a blocked result
        self.account_paused: dict[str, str] = {}     # account name -> why it is paused
        self.waiting_reason: str | None = None       # set while no account can take the clip
        self.uploading_account: str | None = None    # the account an upload is running on
        # Last known login state per account from uploads: name -> (logged in, when).
        self.login_seen: dict[str, tuple[bool, float]] = {}
        self._transient: dict[str, int] = {}         # clip id -> transient failures so far
        self._last_status: dict[str, ClipStatus] = {}
        self._queued_since: dict[str, float] = {}    # clip id -> when it became queued
        self._manual: dict[str, bool] = {}           # clip id -> made with "clip now"
        self._started = clock()
        # Subscribe now, so no approval or config change is missed before run() starts.
        self._sub = bus.subscribe(Topic.CLIP, Topic.CONFIG_CHANGED, maxsize=1000)
        for clip in db.list_clips(status=list(_MANUAL_FROM), limit=100_000):
            self._last_status[clip.id] = clip.status

    # ---- state the dashboard reads -------------------------------------------------------
    @property
    def paused_reason(self) -> str | None:
        """Why uploads are paused: the whole queue, or some accounts. None = not paused."""
        if self._paused:
            return self._paused
        if self.account_paused:
            return "; ".join(f"account {n} paused: {r}" for n, r in self.account_paused.items())
        return None

    def unavailable(self, cfg: Config) -> dict[str, str]:
        """Accounts that can't take an upload now, whatever their limits, and why."""
        out = {name: f"paused ({why})" for name, why in self.account_paused.items()}
        for a in cfg.tiktok_accounts():
            if a.name not in out and a.name != self.uploading_account and profile_busy(a):
                out[a.name] = "its browser profile is open (login window or login check)"
        return out

    def next_account(self) -> tuple[TikTokAccount | None, str | None]:
        """The account the next upload would go to right now, or (None, why not)."""
        cfg = self.store.get()
        return choose_account(cfg, self.db, self._clock(), dry_run=cfg.tiktok.dry_run,
                              unavailable=self.unavailable(cfg))

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
        clips = self._live_queue()
        if self._paused:
            return False
        if not clips:
            self._set_waiting(None)
            return False
        return await self._handle(clips[0])

    # ---- which clip ----------------------------------------------------------------------
    def _live_queue(self) -> list[Clip]:
        """The queued clips, best first, after dropping the expired ones."""
        cfg = self.store.get()
        now = self._clock()
        max_s = cfg.tiktok.max_queue_age_h * 3600
        keep = []
        for clip in self.db.list_clips(status=ClipStatus.QUEUED, oldest_first=True, limit=100_000):
            since = self._queued_since.setdefault(clip.id, max(clip.created_at, self._started))
            if max_s and now - since > max_s:
                hours = f"{cfg.tiktok.max_queue_age_h:g}"
                self._finish(clip, ClipStatus.DROPPED, f"expired: no upload slot within {hours} h")
                log.info("clip %s dropped: no upload slot within %s h", clip.id, hours,
                         extra={"streamer": clip.streamer})
                continue
            keep.append(clip)
        keep.sort(key=lambda c: (not self.is_manual(c), -c.score, c.created_at))
        return keep

    def is_manual(self, clip: Clip) -> bool:
        """True for a "clip now" clip (its peak's source is manual)."""
        if clip.id not in self._manual:
            manual = False
            if clip.peak_id:
                peaks = self.db.list_peaks(streamer=clip.streamer, since=clip.window_start - 3600,
                                           limit=100_000)
                manual = any(p.id == clip.peak_id and p.source == "manual" for p in peaks)
            self._manual[clip.id] = manual
        return self._manual[clip.id]

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
            if payload.status is ClipStatus.QUEUED:
                if prev in _MANUAL_FROM:
                    self._transient.pop(payload.id, None)
                    self._queued_since[payload.id] = self._clock()
                    self._resume_on_approval(f"clip {payload.id} was approved")
                else:
                    self._queued_since.setdefault(payload.id, self._clock())
            elif payload.status is not ClipStatus.UPLOADING:
                self._queued_since.pop(payload.id, None)
            if payload.status is ClipStatus.UPLOADED:
                self._last_status.pop(payload.id, None)
                self._manual.pop(payload.id, None)
            else:
                self._last_status[payload.id] = payload.status

    def pause(self, reason: str) -> None:
        """Pause the whole queue."""
        # Approvals and config changes from before this point are old news: drain
        # them first, so only a later one resumes the queue.
        self._drain_events()
        self._paused = reason
        log.warning("upload queue paused: %s. It resumes when you approve a clip again or "
                    "change the settings.", reason)

    def pause_account(self, name: str, reason: str) -> None:
        """Pause one account; the others keep uploading."""
        self._drain_events()
        self.account_paused[name] = reason
        log.warning("TikTok account %s paused: %s. It resumes when its login checks out (Check "
                    "or Log in on the dashboard) or you change the settings.", name, reason)

    def resume_account(self, name: str, why: str = "its login checked out") -> None:
        if self.account_paused.pop(name, None) is not None:
            log.info("TikTok account %s resumed (%s)", name, why)

    def note_login(self, name: str, logged_in: bool) -> None:
        """A login check's result (from the dashboard). A good login lifts the pause."""
        self.login_seen[name] = (logged_in, self._clock())
        if logged_in:
            self.resume_account(name)

    def _resume_on_approval(self, why: str) -> None:
        """A hand approval: lifts the whole-queue pause, and the account pauses only
        when no enabled account is left to upload to."""
        if self._paused:
            log.info("upload queue resumed (%s)", why)
            self._paused = None
        enabled = {a.name for a in self.store.get().tiktok_accounts() if a.enabled}
        if enabled and enabled <= set(self.account_paused):
            for name in list(self.account_paused):
                self.resume_account(name, why)

    def _resume(self, why: str) -> None:
        if self._paused:
            log.info("upload queue resumed (%s)", why)
            self._paused = None
        for name in list(self.account_paused):
            self.resume_account(name, why)

    # ---- limits (kept for callers of the single-account API) -----------------------------
    def limit_reason(self, cfg: Config, now: float) -> str | None:
        """Why a real upload can't happen at ``now`` on any account, or None if it can."""
        account, reason = choose_account(cfg, self.db, now, dry_run=False,
                                         unavailable=self.unavailable(cfg))
        return None if account else reason

    def _set_waiting(self, reason: str | None) -> None:
        if reason and _stable(reason) != _stable(self.waiting_reason):
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
            cfg = self.store.get()
            dry_run = cfg.tiktok.dry_run
            account, reason = choose_account(cfg, self.db, self._clock(), dry_run=dry_run,
                                             unavailable=self.unavailable(cfg))
            if account is None:
                self._set_waiting(reason)
                return attempted
            self._set_waiting(None)

            result = await self._attempt(clip, dry_run, account)
            attempted = True
            where = f"account {account.name}"
            if result.kind == "ok":
                self._finish(clip, ClipStatus.UPLOADED, "")
                log.info("clip %s posted to TikTok (%s)", clip.id, where, extra=extra)
                return True
            if result.kind == "dry_run":
                self._finish(clip, ClipStatus.DRY_RUN_OK, "")
                log.info("clip %s dry run reached the Post button (%s)", clip.id, where, extra=extra)
                return True
            if result.kind == "blocked":
                error = result.error or "no reason given"
                self._finish(clip, ClipStatus.FAILED, result.error or "blocked")
                if blocked_scope(error) == "account":
                    if not re.search(r"captcha|verif", error, re.IGNORECASE):  # i.e. logged out
                        self.login_seen[account.name] = (False, self._clock())
                    self.pause_account(account.name, f"clip {clip.id} was blocked: {error}")
                else:
                    self.pause(f"clip {clip.id} was blocked on {where}: {error}")
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
            if fresh is None or fresh.status is not ClipStatus.QUEUED or self._paused:
                return True  # rejected or changed meanwhile
            clip = fresh

    async def _attempt(self, clip: Clip, dry_run: bool, account: TikTokAccount) -> UploadResult:
        clip.status, clip.account = ClipStatus.UPLOADING, account.name
        self._save(clip)
        self.uploading_account = account.name
        try:
            result = await self.uploader.upload(clip, clip.caption, dry_run, account)
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
        finally:
            self.uploading_account = None
        self.db.add_upload(clip.id, result.kind, result.error, ts=self._clock(), account=account.name)
        if result.kind in ("ok", "dry_run"):
            self.login_seen[account.name] = (True, self._clock())
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
