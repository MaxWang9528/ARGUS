"""The rolling buffer index: which segments are on disk, what time they cover, where
the holes are, and the HLS playlist that lists them.

Time model
----------
Each segment is one of Twitch's own HLS segments (about 2 s of MPEG-TS that starts on
a keyframe), written to disk unchanged by the capture worker (see ``recorder.py``).
Twitch stamps every segment with ``#EXT-X-PROGRAM-DATE-TIME``: the moment it was
produced, on Twitch's clock. That date is the anchor. It does not depend on when we
happened to download the segment, so the burst of old segments a client gets at
start-up (or a slow download) does not shift anything.

Chat time is our local ``time.time()``. To turn a segment date into chat time we add
``clock_offset``: the smallest ``(local time the last byte arrived) - (segment end on
Twitch's clock)`` over recent segments. With low latency on, segments are streamed
while Twitch produces them, so this minimum is the local clock's error against
Twitch's plus the fastest delivery (measured ~0.3 s). It corrects a drifting local
clock (WSL2 is prone to that) without an NTP query. What viewers see later than that,
and how long chat takes to react, is ``capture.sync_offset_s`` (applied by callers).

So: ``chat_time = segment.date + offset_in_segment + clock_offset``.

Gaps
----
A gap is a stretch of Twitch time with no segment: ad segments that were filtered out,
a segment that failed to download, or a pause. A segment has a gap before it when the
worker reported a skipped segment just before it, when Twitch's sequence number jumps,
or (as a backup) when its date starts later than the previous segment ended. Gaps are
computed from the segments on every query, so a prefetch segment whose real date
arrives later (``meta`` event) is re-judged, and a gap disappears when it scrolls out.
"""

from __future__ import annotations

import itertools
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..core.interfaces import WindowUnavailable

GAP_TOLERANCE_S = 0.5      # a date jump bigger than this is a hole
OFFSET_WINDOW = 150        # segments used for the clock-offset minimum (~5 min)


@dataclass
class Segment:
    idx: int               # our own running number (playlist media sequence)
    num: int               # Twitch's media sequence number
    file: str              # file name inside the buffer folder
    date: float            # start, Twitch clock (UTC epoch seconds)
    duration: float
    recv_end: float        # local time.time() when the last byte was written
    size: int = 0
    ad: bool = False
    prefetch: bool = False
    gap_reason: str = ""   # set when the worker skipped a segment (or numbers jump) before this one

    @property
    def end(self) -> float:
        return self.date + self.duration


@dataclass
class Gap:
    start: float           # Twitch clock
    end: float
    reason: str = "missing"


@dataclass
class BufferIndex:
    """In-memory index of one streamer's buffer. Fed with worker events (``apply``)."""

    streamer: str = ""
    segments: deque[Segment] = field(default_factory=deque)
    skipped: dict[int, str] = field(default_factory=dict)  # Twitch num -> reason (recent)
    _offsets: deque[float] = field(default_factory=lambda: deque(maxlen=OFFSET_WINDOW))
    _by_num: dict[int, Segment] = field(default_factory=dict)
    _next_idx: int = 0
    _dropped_discontinuities: int = 0
    _pending_reason: str = ""

    # ---- feeding -------------------------------------------------------------
    def apply(self, ev: dict[str, Any]) -> Segment | Gap | None:
        """Apply one worker event. Returns the new Segment, a new Gap, or None."""
        kind = ev.get("t")
        if kind == "seg":
            return self.add_segment(
                num=int(ev["num"]), file=str(ev["file"]), date=ev.get("date"),
                duration=float(ev["dur"]), recv_end=float(ev["recv_end"]),
                size=int(ev.get("bytes", 0)), ad=bool(ev.get("ad", False)),
                prefetch=bool(ev.get("prefetch", False)))
        if kind == "meta":
            self.update_meta(int(ev["num"]), ev.get("date"), ev.get("dur"))
        elif kind == "skip":
            self.skipped[int(ev["num"])] = str(ev.get("reason", "missing"))
            self._pending_reason = self._pending_reason or str(ev.get("reason", "missing"))
            if len(self.skipped) > 500:
                for k in sorted(self.skipped)[:250]:
                    del self.skipped[k]
        return None

    def add_segment(self, *, num: int, file: str, date: float | None, duration: float,
                    recv_end: float, size: int = 0, ad: bool = False,
                    prefetch: bool = False) -> Segment | Gap:
        last = self.segments[-1] if self.segments else None
        if date is None:
            # No PROGRAM-DATE-TIME: chain onto the previous segment if it is the next
            # number, else estimate from the arrival time.
            if last is not None and num == last.num + 1:
                date = last.end
            else:
                date = recv_end - duration - self.clock_offset
        seg = Segment(idx=self._next_idx, num=num, file=file, date=float(date), duration=duration,
                      recv_end=recv_end, size=size, ad=ad, prefetch=prefetch)
        self._next_idx += 1
        if last is not None:
            if seg.date - last.end < -max(GAP_TOLERANCE_S, last.duration):
                # The timeline went backwards: Twitch restarted the encoder. Start over.
                self.reset()
                last = None
            elif self._pending_reason:
                seg.gap_reason = self._pending_reason
            elif num > last.num + 1:
                seg.gap_reason = "missing"
        self._pending_reason = ""
        self.segments.append(seg)
        self._by_num[num] = seg
        self._offsets.append(recv_end - seg.end)
        gap = self._gap_between(last, seg) if last is not None else None
        return gap or seg

    def update_meta(self, num: int, date: float | None, duration: float | None) -> None:
        """Real date/duration for a segment that was first seen as a prefetch entry."""
        seg = self._by_num.get(num)
        if seg is None:
            return
        if date is not None:
            seg.date = float(date)
        if duration is not None and duration > 0:
            seg.duration = float(duration)
        # Recompute this segment's offset sample (it is the most recent ones that change).
        self._offsets = deque((s.recv_end - s.end for s in list(self.segments)[-OFFSET_WINDOW:]),
                              maxlen=OFFSET_WINDOW)

    def reset(self) -> list[Segment]:
        """Forget everything. Returns the segments that were dropped."""
        dropped = list(self.segments)
        self.segments.clear()
        self._by_num.clear()
        self._pending_reason = ""
        self._offsets.clear()
        self._dropped_discontinuities = 0
        return dropped

    def trim(self, keep_s: float) -> list[Segment]:
        """Drop the oldest segments so about ``keep_s`` seconds stay. Returns them."""
        removed: list[Segment] = []
        if not self.segments:
            return removed
        newest_end = self.segments[-1].end
        while len(self.segments) > 1 and self.segments[1].date <= newest_end - keep_s:
            seg = self.segments.popleft()
            # The gap before the new first segment is no longer inside the buffer.
            if self._gap_between(seg, self.segments[0]) is not None:
                self._dropped_discontinuities += 1
            self.segments[0].gap_reason = ""
            self._by_num.pop(seg.num, None)
            removed.append(seg)
        return removed

    # ---- gaps ----------------------------------------------------------------
    @staticmethod
    def _gap_between(a: Segment, b: Segment) -> Gap | None:
        jump = b.date - a.end
        if b.gap_reason:
            return Gap(start=a.end, end=max(b.date, a.end), reason=b.gap_reason)
        if jump > GAP_TOLERANCE_S:
            return Gap(start=a.end, end=b.date, reason="date jump")
        return None

    @property
    def gaps(self) -> list[Gap]:
        """Holes inside the buffer, oldest first (Twitch clock)."""
        return [g for a, b in itertools.pairwise(self.segments)
                if (g := self._gap_between(a, b)) is not None]

    # ---- time ------------------------------------------------------------------
    @property
    def clock_offset(self) -> float:
        """Local clock minus Twitch clock, plus the fastest delivery seen (seconds)."""
        return min(self._offsets) if self._offsets else 0.0

    def to_chat(self, twitch_ts: float) -> float:
        return twitch_ts + self.clock_offset

    def to_twitch(self, chat_ts: float) -> float:
        return chat_ts - self.clock_offset

    @property
    def start(self) -> float | None:
        """Chat time of the oldest buffered frame (before any sync offset)."""
        return self.to_chat(self.segments[0].date) if self.segments else None

    @property
    def end(self) -> float | None:
        """Chat time where the newest closed segment ends."""
        return self.to_chat(self.segments[-1].end) if self.segments else None

    def gaps_chat(self) -> list[tuple[float, float, str]]:
        return [(self.to_chat(g.start), self.to_chat(g.end), g.reason) for g in self.gaps]

    # ---- queries -------------------------------------------------------------
    def covers_until(self, chat_ts: float) -> bool:
        """True when closed segments reach chat time ``chat_ts``."""
        end = self.end
        return end is not None and end >= chat_ts

    def select(self, start_chat: float, end_chat: float) -> tuple[list[Segment], float]:
        """Segments covering buffer window [start_chat, end_chat] (chat clock, offset
        already applied), and how far into the first segment the window starts.

        Raises WindowUnavailable if the window starts before the buffer, is not fully
        recorded yet, or overlaps a gap.
        """
        if end_chat <= start_chat:
            raise WindowUnavailable("empty window")
        if not self.segments:
            raise WindowUnavailable("the buffer is empty")
        t0, t1 = self.to_twitch(start_chat), self.to_twitch(end_chat)
        first, last = self.segments[0], self.segments[-1]
        if t0 < first.date - 0.05:
            raise WindowUnavailable(
                f"window starts {first.date - t0:.1f} s before the buffer")
        if t1 > last.end + 0.05:
            raise WindowUnavailable(f"window ends {t1 - last.end:.1f} s after the buffer")
        for g in self.gaps:
            if g.start < t1 and g.end > t0:
                raise WindowUnavailable(
                    f"window overlaps a {g.end - g.start:.0f} s gap in the buffer ({g.reason})")
        chosen = [s for s in self.segments if s.end > t0 and s.date < t1]
        if not chosen:
            raise WindowUnavailable("no segments in the window")
        for a, b in itertools.pairwise(chosen):
            if self._gap_between(a, b) is not None:
                raise WindowUnavailable("window overlaps a gap in the buffer")
        return chosen, max(0.0, t0 - chosen[0].date)

    # ---- playlist ------------------------------------------------------------
    def playlist_text(self) -> str:
        """An HLS media playlist of the buffer. Dates are chat time, so a player can
        line the video up with chat and peak markers."""
        segs = list(self.segments)
        target = max((math.ceil(s.duration) for s in segs), default=2)
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{target}",
                 f"#EXT-X-MEDIA-SEQUENCE:{segs[0].idx if segs else 0}",
                 f"#EXT-X-DISCONTINUITY-SEQUENCE:{self._dropped_discontinuities}"]
        for a, s in zip([None, *segs], segs, strict=False):
            if a is not None and self._gap_between(a, s) is not None:
                lines.append("#EXT-X-DISCONTINUITY")
            lines.append("#EXT-X-PROGRAM-DATE-TIME:" + _iso(self.to_chat(s.date)))
            lines.append(f"#EXTINF:{s.duration:.3f},")
            lines.append(s.file)
        return "\n".join(lines) + "\n"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

