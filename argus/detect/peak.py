"""Chat peak detection.

``PeakTracker`` holds the maths for one streamer. It has no clock: feed it chat
messages (binned by their own ``ts``) and call ``tick(now, det)`` to close every
bin that ends at or before ``now``. Each closed bin gives one ``RateSample``;
a finished peak gives one ``PeakEvent``. Tests and ``replay`` drive it directly.

``ChatPeakDetector`` is the app component: it subscribes to ``Topic.CHAT``, ticks
the trackers on the wall clock and publishes ``Topic.RATE`` and ``Topic.PEAK``.

The signal:
- messages are counted in ``bin_s`` bins (one account counts for at most
  ``MAX_PER_USER_PER_S`` per second, so a lone spammer can't make a peak) and the
  rate is the trailing mean over ``smooth_s`` (messages per second);
- the baseline is the rolling median of that rate over ``baseline_window_s``,
  leaving out bins inside a peak; the spread is the MAD scaled to a standard
  deviation, never below ``MIN_SPREAD`` (integer counts make the MAD exactly 0
  in quiet chats);
- ``z = (rate - baseline) / spread``;
- if the median of the last ``RECENT_BASELINE_S`` is higher, it is the baseline
  instead (a step to a new chat level is not a peak), and after
  ``DORMANT_AFTER_S`` of total silence the 60 s warm-up starts again.
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from ..core.config import ConfigStore, Detection
from ..core.events import EventBus, Topic
from ..core.models import ChatMessage, PeakEvent, RateSample

log = logging.getLogger("argus.detect")

MAD_TO_SD = 1.4826        # MAD of a normal distribution * this = its standard deviation
MIN_SPREAD = 0.25         # msgs/s; floor for the spread so silent chat can't give huge z
MIN_HISTORY_S = 60.0      # no peak can start before this much history
MAX_RISE_LOOKBACK_S = 20.0  # ts_start is at most this far before the bin that triggered
MAX_PER_USER_PER_S = 2.0  # one user counts for at most this many msgs/s (stops a lone spammer)
RECENT_BASELINE_S = 120.0  # a short-window median that lifts the baseline after a level step
DORMANT_AFTER_S = 120.0   # this long with no chat (offline, disconnected) restarts the warm-up


def effective_bin_s(det: Detection) -> float:
    return float(det.bin_s) if det.bin_s > 0 else 1.0


def is_counted(msg: ChatMessage, det: Detection) -> bool:
    """False for bot commands (``!...``) and for users in ``ignore_users``."""
    if msg.text.lstrip().startswith("!"):
        return False
    ignored = {u.lower() for u in det.ignore_users}
    return msg.user.lower() not in ignored


def _chatter_key(msg: ChatMessage) -> str:
    return msg.user_id or msg.user.lower()


@dataclass
class _Bin:
    users: dict[str, int] = field(default_factory=dict)  # chatter -> messages
    count: int = 0  # set when the bin closes, with each user's share capped


@dataclass
class _Active:
    ts_start: float
    ts_peak: float
    peak_rate: float
    baseline: float
    spread: float


class PeakTracker:
    """Peak detection for one streamer, driven by message timestamps and ticks."""

    def __init__(self, streamer: str) -> None:
        self.streamer = streamer.lower()
        self._bin_s: float | None = None
        self._reset()

    def _reset(self) -> None:
        self._open: dict[int, _Bin] = {}           # bin index -> counts not yet closed
        self._next_idx: int | None = None          # index of the next bin to close
        self._recent: deque[tuple[float, _Bin]] = deque()        # closed bins in the smoothing window
        self._history: deque[tuple[float, float]] = deque()      # (ts, rate) for the baseline
        self._rise: deque[tuple[float, bool]] = deque()          # (bin start, above baseline + spread)
        self._bins_closed = 0
        self._silent_since: float | None = None
        self._active: _Active | None = None
        self._last_end: float | None = None

    @property
    def in_peak(self) -> bool:
        return self._active is not None

    # ---- input -----------------------------------------------------------------
    def _check_bin_s(self, det: Detection) -> float:
        bin_s = effective_bin_s(det)
        if self._bin_s != bin_s:
            if self._bin_s is not None:
                log.info("bin_s changed to %s, restarting detection", bin_s,
                         extra={"streamer": self.streamer})
            self._bin_s = bin_s
            self._reset()
        return bin_s

    def feed(self, msg: ChatMessage, det: Detection) -> bool:
        """Count one message. Returns False if it was filtered out."""
        if not is_counted(msg, det):
            return False
        bin_s = self._check_bin_s(det)
        idx = math.floor(msg.ts / bin_s)
        if self._next_idx is None:
            self._next_idx = idx
        idx = max(idx, self._next_idx)  # a late message goes into the oldest open bin
        b = self._open.setdefault(idx, _Bin())
        key = _chatter_key(msg)
        b.users[key] = b.users.get(key, 0) + 1
        return True

    # ---- clock -----------------------------------------------------------------
    def tick(self, now: float, det: Detection) -> tuple[list[RateSample], list[PeakEvent]]:
        """Close every bin that ends at or before ``now``."""
        bin_s = self._check_bin_s(det)
        last = math.floor(now / bin_s + 1e-9) - 1   # last bin index that has fully ended
        if self._next_idx is None:
            self._next_idx = last
        if last - self._next_idx + 1 > det.baseline_window_s / bin_s + 1:
            # A long silent gap (replay of a log with holes): history is stale anyway.
            self._reset()
            self._bin_s = bin_s
            self._next_idx = last
        samples: list[RateSample] = []
        peaks: list[PeakEvent] = []
        while self._next_idx <= last:
            idx = self._next_idx
            self._next_idx += 1
            sample, peak = self._close(idx, bin_s, det)
            samples.append(sample)
            if peak is not None:
                peaks.append(peak)
        return samples, peaks

    # ---- maths -----------------------------------------------------------------
    @staticmethod
    def _median_mad(rates: list[float]) -> tuple[float, float]:
        base = statistics.median(rates)
        return base, statistics.median(abs(r - base) for r in rates)

    def _baseline(self, end: float, bin_s: float) -> tuple[float, float]:
        """Median and spread of the history. When the median of the last
        RECENT_BASELINE_S is higher (chat stepped up to a new level, e.g. the
        stream went live), that one is used, so a level change isn't a peak."""
        if not self._history:
            return 0.0, MIN_SPREAD
        base, mad = self._median_mad([r for _, r in self._history])
        recent = [r for ts, r in self._history if ts > end - RECENT_BASELINE_S]
        if len(recent) * bin_s >= RECENT_BASELINE_S / 4:
            r_base, r_mad = self._median_mad(recent)
            if r_base > base:
                base, mad = r_base, r_mad
        return base, max(mad * MAD_TO_SD, MIN_SPREAD)

    def _close(self, idx: int, bin_s: float, det: Detection) -> tuple[RateSample, PeakEvent | None]:
        b = self._open.pop(idx, None) or _Bin()
        cap = max(1, math.ceil(MAX_PER_USER_PER_S * bin_s))
        b.count = sum(min(n, cap) for n in b.users.values())
        start = idx * bin_s
        end = start + bin_s
        self._bins_closed += 1

        self._recent.append((end, b))
        while self._recent and self._recent[0][0] <= end - det.smooth_s + 1e-9:
            self._recent.popleft()
        smooth = max(det.smooth_s, bin_s)
        rate = sum(x.count for _, x in self._recent) / smooth
        users = len(set().union(*(x.users for _, x in self._recent)))

        while self._history and self._history[0][0] <= end - det.baseline_window_s:
            self._history.popleft()
        if self._active is not None:
            base, spread = self._active.baseline, self._active.spread
        else:
            base, spread = self._baseline(end, bin_s)
        z = (rate - base) / spread
        threshold = max(base * det.multiplier, base + det.z_threshold * spread, det.min_msgs_per_sec)

        self._rise.append((start, rate > base + spread))
        while self._rise and self._rise[0][0] < start - MAX_RISE_LOOKBACK_S:
            self._rise.popleft()

        peak: PeakEvent | None = None
        if self._active is None:
            cooled = self._last_end is None or end - self._last_end >= det.cooldown_s
            warm = self._bins_closed * bin_s >= MIN_HISTORY_S
            if (warm and cooled and rate > base * det.multiplier and z > det.z_threshold
                    and rate > det.min_msgs_per_sec and users >= det.min_unique_chatters):
                self._active = _Active(ts_start=self._rise_start(start), ts_peak=end, peak_rate=rate,
                                       baseline=base, spread=spread)
                log.debug("peak started: rate %.1f, baseline %.1f, z %.1f", rate, base, z,
                          extra={"streamer": self.streamer})
        else:
            a = self._active
            if rate > a.peak_rate:
                a.peak_rate, a.ts_peak = rate, end
            end_level = max(a.baseline * det.end_ratio, a.baseline + a.spread)
            if rate < end_level or end >= a.ts_start + det.max_clip_s:
                ts_end = min(end, a.ts_start + det.max_clip_s)
                score = a.peak_rate / max(a.baseline, MIN_SPREAD)
                self._active = None
                # How long chat stayed raised, from where it began rising to where it fell
                # back. Measured on real chat, lone bursts come out at 6-7 s and reactions
                # at 10 s and up.
                sustained = ts_end - a.ts_start
                if sustained >= det.min_sustain_s - 1e-9:
                    peak = PeakEvent(streamer=self.streamer, ts_start=a.ts_start, ts_peak=a.ts_peak,
                                     ts_end=ts_end, score=score)
                    self._last_end = ts_end
                else:
                    # A spike, not sustained chatter: a lone burst lasts about smooth_s
                    # seconds after smoothing. Dropped, and no cooldown, so a real peak
                    # right after it still counts.
                    log.info("spike ignored: chat stayed up %.0f s (score %.1f), needs %.0f s",
                             sustained, score, det.min_sustain_s, extra={"streamer": self.streamer})

        if self._active is None and peak is None:
            if rate > 0:
                self._silent_since = None
            elif self._silent_since is None:
                self._silent_since = start
            elif end - self._silent_since >= DORMANT_AFTER_S:
                # Chat has been dead for a while (offline or disconnected): whatever
                # comes next is a new level, so warm up again from scratch.
                self._history.clear()
                self._bins_closed = 0
            self._history.append((end, rate))  # bins inside a peak stay out of the baseline

        sample = RateSample(streamer=self.streamer, ts=end, rate=rate, baseline=base,
                            threshold=threshold, z=z, unique_chatters=users)
        return sample, peak

    def _rise_start(self, start: float) -> float:
        """Start of the run of bins above baseline + spread that ends at this bin."""
        ts = start
        floor = self._last_end if self._last_end is not None else -math.inf
        for bin_start, above in reversed(self._rise):
            if not above or bin_start < floor:
                break
            ts = bin_start
        return ts


class ChatPeakDetector:
    """Publishes a RateSample per enabled streamer every bin and a PeakEvent when
    a peak ends. Thresholds are read from the store on every bin."""

    def __init__(self, *, store: ConfigStore, bus: EventBus,
                 clock: Callable[[], float] = time.time) -> None:
        self.store, self.bus, self.clock = store, bus, clock
        self.trackers: dict[str, PeakTracker] = {}

    def handle(self, msg: ChatMessage) -> None:
        name = msg.streamer.lower()
        tracker = self.trackers.get(name)
        if tracker is None:
            s = self.store.get().streamer(name)
            if s is None or not s.enabled:
                return
            tracker = self.trackers[name] = PeakTracker(name)
        tracker.feed(msg, self.store.get().detection_for(name))

    def tick(self, now: float) -> None:
        cfg = self.store.get()
        enabled = [s.name.lower() for s in cfg.enabled_streamers()]
        for name in list(self.trackers):
            if name not in enabled:
                del self.trackers[name]
        for name in enabled:
            tracker = self.trackers.setdefault(name, PeakTracker(name))
            samples, peaks = tracker.tick(now, cfg.detection_for(name))
            for s in samples:
                self.bus.publish(Topic.RATE, s)
            for p in peaks:
                log.info("chat peak: score %.1f, %.0f s long", p.score, p.ts_end - p.ts_start,
                         extra={"streamer": name})
                self.bus.publish(Topic.PEAK, p)

    def _bin_s(self) -> float:
        cfg = self.store.get()
        names = [s.name for s in cfg.enabled_streamers()]
        return min((effective_bin_s(cfg.detection_for(n)) for n in names),
                   default=effective_bin_s(cfg.detection))

    async def run(self) -> None:
        sub = self.bus.subscribe(Topic.CHAT)
        try:
            bin_s = self._bin_s()
            next_tick = (math.floor(self.clock() / bin_s) + 1) * bin_s
            while True:
                timeout = next_tick - self.clock()
                if timeout > 0:
                    try:
                        _, msg = await asyncio.wait_for(sub.get(), timeout)
                        self.handle(msg)
                    except TimeoutError:
                        pass
                    if next_tick > self.clock():
                        continue
                # Drain what has already arrived so it lands in its bin, then tick.
                while not sub.queue.empty():
                    self.handle(sub.queue.get_nowait()[1])
                self.tick(next_tick)
                bin_s = self._bin_s()
                next_tick = (math.floor(max(self.clock(), next_tick) / bin_s) + 1) * bin_s
        finally:
            sub.close()
