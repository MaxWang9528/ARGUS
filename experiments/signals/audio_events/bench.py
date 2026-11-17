"""Cost of the audio-event signal on the app's real input: ~2 s 720p60 MPEG-TS segments.

    python -I bench.py MODEL.tflite SEG_DIR [N_STREAMS] [--pyav]

Feeds every segment in SEG_DIR (sorted) through StreamScorer.feed_segment, once per
simulated stream, all sharing one interpreter in one process (the way it would run
inside the app), and reports CPU seconds (this process + ffmpeg children) per stream
minute, wall time and peak RSS. Also times the model alone (no decode).
"""

from __future__ import annotations

import os
import resource
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scorer as S  # noqa: E402


def cpu() -> float:
    a = resource.getrusage(resource.RUSAGE_SELF)
    b = resource.getrusage(resource.RUSAGE_CHILDREN)
    return a.ru_utime + a.ru_stime + b.ru_utime + b.ru_stime


def main() -> None:
    model_path, seg_dir = sys.argv[1:3]
    n_streams = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    dec = S.decode_pyav if "--pyav" in sys.argv else S.decode
    segs = sorted(Path(seg_dir).glob("*.ts"))
    m = S.Yamnet(model_path, threads=1)
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

    # model only
    x = np.random.default_rng(0).standard_normal(S.WIN).astype(np.float32) * 0.1
    m.frame(x)
    c0, t0 = cpu(), time.perf_counter()
    for _ in range(200):
        m.frame(x)
    per_frame = (cpu() - c0) / 200
    print(f"model only: {per_frame * 1000:.2f} ms CPU/frame -> {per_frame * 2 * 60:.2f} CPU s per stream-minute (2 frames/s)")

    # decode: per-segment ffmpeg, video demuxed but not decoded
    c0, t0 = cpu(), time.perf_counter()
    audio_s = 0.0
    for _ in range(n_streams):
        sc = S.StreamScorer(m)
        ts = 0.0
        for p in segs:
            w = dec(p)
            sc.feed_audio(w, ts)
            ts += len(w) / S.SR
        audio_s += ts
    used, wall = cpu() - c0, time.perf_counter() - t0
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    child = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
    print(f"{n_streams} stream(s) x {len(segs)} segments = {audio_s / 60:.1f} stream-min: "
          f"{used:.2f} CPU s, {wall:.2f} s wall -> {used / (audio_s / 60):.2f} CPU s per stream-minute "
          f"(model {per_frame * 120:.2f} + decode/other)")
    print(f"peak RSS: python {rss:.0f} MB (after model load {rss0:.0f} MB), largest ffmpeg child {child:.0f} MB; "
          f"nproc {os.cpu_count()}")


if __name__ == "__main__":
    main()
