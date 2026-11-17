"""``TunedTracker``: a copy of ``argus.detect.peak.PeakTracker._close`` with hook
points for the variants tried here. With the default ``P`` it behaves exactly like
the app's tracker (checked by ``evaluate.py --selftest``).

Variant knobs (class attribute ``P``; make a variant with ``variant(name, **knobs)``):
  pre_far, pre_near  local "pre-window" baseline: median of the history rates whose
                     bin ends in [end - pre_far, end - pre_near]. 0 = off.
  pre_mode           "max": baseline = max(long baseline, pre-window median)
                     "only": baseline = pre-window median
  poisson_k          spread >= poisson_k * sqrt(baseline / smooth_s): the counting noise
                     of a smoothed rate, so slow chats don't get a huge z from noise. 0 = off.
  jump_s, jump_z     the rate must have risen by jump_z * spread within the last jump_s
                     seconds (rate - min over that span). 0 = off.
  jump_frac          ...and by at least this share of (rate - baseline): most of the excess
                     is new, not a slow climb. 0 = off.
  pre_stat           "median" or "mean" of the pre-window
  plateau_reject     drop a peak that hits max_clip_s without falling back (a plateau).
  new_share_min      drop a peak where under this share of the peak's messages come from
                     chatters not seen in the previous 10 min. 0 = off.
  spam_share_max     drop a peak where over this share of messages are copies (same
                     normalised text, 4+ words or 25+ chars, from 3+ chatters). 1 = off.
Each sample also gets ``margin``: how far the start test is from firing (>= 1 fires),
the minimum over its criteria of value / threshold.
"""

from __future__ import annotations

import math
import re
import statistics
from collections import deque

from common import PeakTracker
from argus.core.models import ChatMessage, PeakEvent, RateSample
from argus.detect import peak as pk

DEFAULTS = dict(pre_far=0.0, pre_near=0.0, pre_mode="max", pre_stat="median", jump_frac=0.0, poisson_k=0.0, jump_s=0.0, jump_z=0.0,
                plateau_reject=False, new_share_min=0.0, spam_share_max=1.0)

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", text.lower()).strip()


class TunedTracker(PeakTracker):
    P: dict = DEFAULTS

    def _reset(self) -> None:
        super()._reset()
        self._rates: deque[tuple[float, float]] = deque()  # (end, rate) every bin, for jump
        self._seen: dict[str, float] = {}                  # chatter -> last message ts
        self._new_in_bin: dict[int, int] = {}
        self._texts: dict[int, list[tuple[str, str]]] = {}
        self._act_new = 0
        self._act_texts: list[tuple[str, str]] = []
        self.margins: list[float] = []

    def feed(self, msg: ChatMessage, det) -> bool:
        ok = super().feed(msg, det)
        if ok:
            idx = math.floor(msg.ts / self._bin_s)
            idx = max(idx, self._next_idx)
            key = pk._chatter_key(msg)
            last = self._seen.get(key)
            if last is None or msg.ts - last > 600:
                self._new_in_bin[idx] = self._new_in_bin.get(idx, 0) + 1
            self._seen[key] = msg.ts
            self._texts.setdefault(idx, []).append((key, msg.text))
        return ok

    # ---- baseline --------------------------------------------------------------
    def _baseline2(self, end: float, bin_s: float, det) -> tuple[float, float]:
        P = self.P
        base, spread = self._baseline(end, bin_s, det.recent_baseline_s)
        if P["pre_far"] > 0 and self._history:
            pre = [r for ts, r in self._history if end - P["pre_far"] < ts <= end - P["pre_near"]]
            if len(pre) * bin_s >= (P["pre_far"] - P["pre_near"]) / 2:
                pb = statistics.median(pre) if P["pre_stat"] == "median" else statistics.fmean(pre)
                if P["pre_mode"] == "only" or pb > base:
                    base = pb
        if P["poisson_k"] > 0:
            spread = max(spread, P["poisson_k"] * math.sqrt(max(base, 0.05) / max(det.smooth_s, bin_s)))
        return base, spread

    def _spam_share(self, texts: list[tuple[str, str]]) -> float:
        by: dict[str, set] = {}
        for k, t in texts:
            n = _norm(t)
            if len(n) >= 25 or n.count(" ") >= 3:
                by.setdefault(n, set()).add(k)
        if not texts:
            return 0.0
        return sum(1 for k, t in texts if len(by.get(_norm(t), ())) >= 3) / len(texts)

    # ---- the copy of PeakTracker._close, with hooks ------------------------------
    def _close(self, idx, bin_s, det):
        P = self.P
        b = self._open.pop(idx, None) or pk._Bin()
        new_here = self._new_in_bin.pop(idx, 0)
        texts_here = self._texts.pop(idx, [])
        cap = max(1, math.ceil(pk.MAX_PER_USER_PER_S * bin_s))
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
        if not hasattr(self, "_recent_extra"):
            self._recent_extra = deque()
        self._recent_extra.append((end, new_here, texts_here))
        while self._recent_extra and self._recent_extra[0][0] <= end - det.smooth_s + 1e-9:
            self._recent_extra.popleft()

        self._rates.append((end, rate))
        while self._rates and self._rates[0][0] <= end - max(P["jump_s"], 1) - 1e-9:
            self._rates.popleft()

        while self._history and self._history[0][0] <= end - det.baseline_window_s:
            self._history.popleft()
        if self._active is not None:
            base, spread = self._active.baseline, self._active.spread
        else:
            base, spread = self._baseline2(end, bin_s, det)
        z = (rate - base) / spread
        threshold = max(base * det.multiplier, base + det.z_threshold * spread, det.min_msgs_per_sec)

        self._track_gap(not b.users, end, base, bin_s, det)
        held = self._in_gap or end < self._hold_until

        self._rise.append((start, rate > base + spread))
        while self._rise and self._rise[0][0] < start - pk.MAX_RISE_LOOKBACK_S:
            self._rise.popleft()

        # margin: >= 1 means every start criterion passes
        crit = [rate / max(base * det.multiplier, 1e-6), z / det.z_threshold,
                rate / max(det.min_msgs_per_sec, 1e-6)]
        if P["jump_s"] > 0:
            lo = min(r for _, r in self._rates)
            if P["jump_z"] > 0:
                crit.append((rate - lo) / (P["jump_z"] * spread))
            if P["jump_frac"] > 0:
                crit.append((rate - lo) / max(1e-6, P["jump_frac"] * (rate - base)) if rate > base else 0.0)
        margin = min(crit)
        self.margins.append(margin if users >= det.min_unique_chatters else min(margin, 0.0))

        peak: PeakEvent | None = None
        if self._active is None:
            cooled = self._last_end is None or end - self._last_end >= det.cooldown_s
            warm = self._bins_closed * bin_s >= pk.MIN_HISTORY_S
            if warm and cooled and not held and margin > 1 and users >= det.min_unique_chatters:
                self._active = pk._Active(ts_start=self._rise_start(start), ts_peak=end, peak_rate=rate,
                                          baseline=base, spread=spread)
                for _, rb in self._recent:
                    self._active.add(rb, users)
                self._act_new = sum(n for _, n, _ in self._recent_extra)
                self._act_texts = [t for _, _, ts_ in self._recent_extra for t in ts_]
        else:
            a = self._active
            a.add(b, users)
            self._act_new += new_here
            self._act_texts += texts_here
            if rate > a.peak_rate:
                a.peak_rate, a.ts_peak = rate, end
            end_level = max(a.baseline * det.end_ratio, a.baseline + a.spread)
            capped = end >= a.ts_start + det.max_clip_s
            if rate < end_level or capped:
                ts_end = min(end, a.ts_start + det.max_clip_s)
                score = a.peak_rate / max(a.baseline, pk.MIN_SPREAD)
                self._active = None
                sustained = ts_end - a.ts_start
                n = max(1, a.msgs)
                keep = sustained >= det.min_sustain_s - 1e-9
                why = "short"
                if keep and P["plateau_reject"] and capped and rate >= end_level:
                    keep, why = False, "plateau"
                if keep and P["new_share_min"] > 0 and self._act_new / n < P["new_share_min"]:
                    keep, why = False, "regulars"
                if keep and P["spam_share_max"] < 1 and self._spam_share(self._act_texts) > P["spam_share_max"]:
                    keep, why = False, "spam"
                if keep:
                    reaction, answer = a.reactions / n, a.answers / n
                    rank = score * math.sqrt(a.chatters) * pk.text_factor(reaction, answer)
                    peak = PeakEvent(streamer=self.streamer, ts_start=a.ts_start, ts_peak=a.ts_peak,
                                     ts_end=ts_end, score=score, rank=rank, reaction=reaction,
                                     chatters=a.chatters)
                    self._last_end = ts_end
                elif why != "short":
                    self._last_end = ts_end  # a rejected real burst still gets its cooldown
                self.rejected = getattr(self, "rejected", 0) + (why != "short" and not keep)

        if self._active is None and peak is None:
            if rate > 0:
                self._silent_since = None
            elif self._silent_since is None:
                self._silent_since = start
            elif end - self._silent_since >= pk.DORMANT_AFTER_S:
                self._history.clear()
                self._bins_closed = 0
            if not held:
                self._history.append((end, rate))
                if self._empty_run:
                    self._empty_in_hist += 1

        return RateSample(streamer=self.streamer, ts=end, rate=rate, baseline=base,
                          threshold=threshold, z=z, unique_chatters=users), peak


def variant(name: str, **knobs) -> type[TunedTracker]:
    bad = set(knobs) - set(DEFAULTS)
    if bad:
        raise ValueError(bad)
    return type(name, (TunedTracker,), {"P": {**DEFAULTS, **knobs}})
