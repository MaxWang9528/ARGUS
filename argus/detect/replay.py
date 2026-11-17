"""``./lw replay <chat.jsonl>``: run a recorded chat log through the detector as
fast as possible, using the messages' own timestamps."""

from __future__ import annotations

import argparse
import time
from collections.abc import Iterable

from ..chat.chatlog import read_chat_log
from ..core.config import ConfigStore
from ..core.models import ChatMessage, PeakEvent, RateSample
from .peak import PeakTracker


def replay(messages: Iterable[ChatMessage], store: ConfigStore, streamer: str | None = None,
           ) -> tuple[list[PeakEvent], dict[str, list[RateSample]]]:
    """Feed messages (in time order) through one PeakTracker per streamer.

    Returns the peaks in the order they ended and the rate samples per streamer.
    """
    only = streamer.lower() if streamer else None
    trackers: dict[str, PeakTracker] = {}
    samples: dict[str, list[RateSample]] = {}
    peaks: list[PeakEvent] = []
    last_ts: float | None = None

    def tick(now: float) -> None:
        cfg = store.get()
        for name, tr in trackers.items():
            s, p = tr.tick(now, cfg.detection_for(name))
            samples[name].extend(s)
            peaks.extend(p)

    for msg in messages:
        name = msg.streamer.lower()
        if only and name != only:
            continue
        if name not in trackers:
            trackers[name] = PeakTracker(name)
            samples[name] = []
        tick(msg.ts)  # close every bin that ended before this message
        trackers[name].feed(msg, store.get().detection_for(name))
        last_ts = msg.ts if last_ts is None else max(last_ts, msg.ts)

    if last_ts is not None:
        # Let the log end in silence so a peak in progress can finish.
        cfg = store.get()
        tail = max((cfg.detection_for(n).smooth_s + 2 * cfg.detection_for(n).bin_s for n in trackers),
                   default=0)
        tick(last_ts + tail)
    return peaks, samples


def _clock(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts))


def main(args: argparse.Namespace) -> int:
    store: ConfigStore = getattr(args, "store", None) or ConfigStore()
    only = (getattr(args, "streamer", None) or "").lower()
    messages = [m for m in read_chat_log(args.file) if not only or m.streamer.lower() == only]
    messages.sort(key=lambda m: m.ts)
    if not messages:
        print(f"{args.file}: no chat messages" + (f" for {only}" if only else ""))
        return 0
    t0 = messages[0].ts
    peaks, samples = replay(messages, store, only or None)
    for p in sorted(peaks, key=lambda p: p.ts_start):
        print(f"{p.streamer:<16} start {_clock(p.ts_start)} UTC (+{p.ts_start - t0:7.1f} s)  "
              f"peak +{p.ts_peak - t0:7.1f} s  end +{p.ts_end - t0:7.1f} s  "
              f"({p.ts_end - p.ts_start:4.1f} s)  score {p.score:5.1f}  "
              f"rank {p.rank:6.1f}  react {p.reaction:4.0%}  chatters {p.chatters}")
    counted = sum(len(v) for v in samples.values())
    span = messages[-1].ts - t0
    print(f"{len(peaks)} peak(s) in {span / 60:.1f} min of chat, {len(messages)} messages, "
          f"{len(samples)} streamer(s), {counted} rate samples")
    return 0
