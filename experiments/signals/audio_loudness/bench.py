"""Cost of the live path: N streams at once, each fed buffered HLS segments at real
time through one persistent ffmpeg (``SegmentDecoder``) -> features -> tracker.

    python -I bench.py <segment_dir> --streams 12 --segments 40 [--mode pipe|spawn]

``segment_dir`` holds ~2 s MPEG-TS segments of one rendition (e.g. the 720p one the
app buffers), fed in name order. Reports CPU (this process + ffmpeg children) as a
share of one core per stream, and latency from feeding a segment to the tracker
having the last whole second of it.
"""

from __future__ import annotations

import argparse
import os
import resource
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audio_signal as A  # noqa: E402


def seg_durations(files: list[Path]) -> list[float]:
    """Decoded audio length of each segment (what the pipe really yields per segment)."""
    return [len(A.decode(str(f))) / A.SR for f in files]


class Stream:
    def __init__(self, mode: str) -> None:
        self.fx, self.tr = A.FeatureExtractor(), A.AudioTracker()
        self.lock = threading.Lock()
        self.emitted: list[tuple[float, float]] = []   # (bin end t, wall time)
        self.mode = mode
        self.dec = A.SegmentDecoder(self.on_pcm) if mode == "pipe" else None

    def on_pcm(self, pcm: np.ndarray) -> None:
        with self.lock:
            for row in self.fx.push(pcm):
                self.tr.push(row)
                self.emitted.append((row["t"], time.monotonic()))

    def feed(self, path: Path) -> None:
        if self.dec is not None:
            self.dec.feed(path.read_bytes())
        else:  # one ffmpeg per segment
            self.on_pcm(A.decode(str(path)))

    def close(self) -> None:
        if self.dec is not None:
            self.dec.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("segdir")
    ap.add_argument("--streams", type=int, default=12)
    ap.add_argument("--segments", type=int, default=40)
    ap.add_argument("--mode", choices=["pipe", "spawn"], default="pipe")
    a = ap.parse_args()
    files = sorted(Path(a.segdir).glob("*.ts"))[: a.segments]
    durs = seg_durations(files)
    ends = np.cumsum(durs)
    streams = [Stream(a.mode) for _ in range(a.streams)]
    fed: list[float] = []
    t0, c0 = time.monotonic(), os.times()
    for i, f in enumerate(files):
        target = t0 + (ends[i - 1] if i else 0.0)
        time.sleep(max(0.0, target - time.monotonic()))
        fed.append(time.monotonic())
        for k, s in enumerate(streams):
            # stagger streams across the segment period like independent live streams
            threading.Timer(k * durs[i] / len(streams), s.feed, args=(f,)).start()
    time.sleep(durs[-1] + 3)
    for s in streams:
        s.close()
    wall = time.monotonic() - t0
    c1 = os.times()
    self_cpu = (c1.user - c0.user) + (c1.system - c0.system)
    child_cpu = (c1.children_user - c0.children_user) + (c1.children_system - c0.children_system)
    audio_s = float(ends[-1])
    lat = []
    for k, s in enumerate(streams):
        stagger = [fed[i] + k * durs[i] / len(streams) for i in range(len(files))]
        for t, w in s.emitted:
            i = int(np.searchsorted(ends, t - 1e-6))   # segment holding the end of this bin
            if i < len(files) and t >= ends[i] - 1.0:   # last whole second of segment i
                lat.append((i, w - stagger[i]))
    first = [x for i, x in lat if i < 2]
    lat = np.array([x for i, x in lat if i >= 2])   # steady state (first 2 = ffmpeg probing)
    print(f"mode {a.mode}: {a.streams} streams x {audio_s:.0f} s audio in {wall:.0f} s wall")
    print(f"CPU: python {self_cpu:.2f} s + ffmpeg {child_cpu:.2f} s = "
          f"{100 * (self_cpu + child_cpu) / audio_s / a.streams:.2f} % of one core per stream "
          f"(ffmpeg {100 * child_cpu / audio_s / a.streams:.2f} %)")
    print(f"peak RSS python {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB, "
          f"largest ffmpeg child {resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024:.0f} MB")
    if len(lat):
        print(f"latency segment fed -> its last second scored: median {np.median(lat) * 1000:.0f} ms, "
              f"p95 {np.percentile(lat, 95) * 1000:.0f} ms, max {lat.max() * 1000:.0f} ms (n={len(lat)}, steady state); "
              f"start-up {max(first, default=0) * 1000:.0f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
