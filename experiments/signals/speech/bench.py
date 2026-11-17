"""Cost benchmarks.

  python -I bench.py rtf <stream_dir> --models tiny.en base.en small.en --secs 120
      CPU seconds per audio second (own process + ffmpeg), rolling 10 s chunks, 1 thread.
  python -I bench.py concurrent <stream_dir>... --streams 12 --model base.en --secs 120
      N streams through one shared model (num_workers=N, one Python thread per
      stream, 1 CTranslate2 thread each): wall time vs audio time = can N keep up?
  python -I bench.py confirm <stream_dir> --model small.en --threads 4 --window 30
      latency to transcribe one candidate window (the confirm-only mode).
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import speech  # noqa: E402


def cpu_now() -> float:
    s = resource.getrusage(resource.RUSAGE_SELF)
    c = resource.getrusage(resource.RUSAGE_CHILDREN)
    return s.ru_utime + s.ru_stime + c.ru_utime + c.ru_stime


def seg_files(d: str, start: int, n: int) -> list[str]:
    segs = [json.loads(x) for x in open(os.path.join(d, "segments.jsonl")) if '"seg"' in x]
    segs = [s for s in segs if not s.get("ad")][start:start + n]
    return [os.path.join(d, "segments", s["file"]) for s in segs]


def run_stream(model, files: list[str], words_out: list | None = None) -> float:
    """Transcribe like the live worker: 5-segment chunks every 4 segments."""
    audio_s = 0.0
    for i in range(0, len(files) - 4, 4):
        a = speech.decode_segments(files[i:i + 5])
        w = speech.transcribe_chunk(model, a, 0.0, (0.0, 1e9))
        loud = speech.loudness_per_s(a)
        if words_out is not None:
            words_out.extend(w)
        audio_s += 8.0
        del loud
    return audio_s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["rtf", "concurrent", "confirm"])
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--models", nargs="+", default=["tiny.en", "base.en", "small.en"])
    ap.add_argument("--model", default="base.en")
    ap.add_argument("--threads", type=int, default=1)
    ap.add_argument("--streams", type=int, default=12)
    ap.add_argument("--secs", type=float, default=120)
    ap.add_argument("--start", type=int, default=100, help="first segment index")
    ap.add_argument("--window", type=float, default=30)
    ap.add_argument("--root", default=None)
    a = ap.parse_args()
    nseg = int(a.secs / 2) + 1

    if a.mode == "rtf":
        for m in a.models:
            model = speech.load_model(m, 1, a.root)
            c0, w0 = cpu_now(), time.perf_counter()
            audio = sum(run_stream(model, seg_files(d, a.start, nseg)) for d in a.dirs)
            c, w = cpu_now() - c0, time.perf_counter() - w0
            print(f"{m:9s} audio {audio:5.0f} s  cpu-RTF {c / audio:.3f}  wall-RTF {w / audio:.3f}")
    elif a.mode == "concurrent":
        from faster_whisper import WhisperModel
        model = WhisperModel(a.model, device="cpu", compute_type="int8", cpu_threads=1,
                             num_workers=a.streams, download_root=a.root)
        jobs = [seg_files(a.dirs[i % len(a.dirs)], a.start + 30 * (i // len(a.dirs)), nseg)
                for i in range(a.streams)]
        done: list[float] = []
        c0, w0 = cpu_now(), time.perf_counter()
        th = [threading.Thread(target=lambda f=f: done.append(run_stream(model, f))) for f in jobs]
        for t in th:
            t.start()
        for t in th:
            t.join()
        c, w = cpu_now() - c0, time.perf_counter() - w0
        per = sum(done) / len(done)
        print(f"{a.streams} streams x {per:.0f} s ({a.model}): wall {w:.0f} s "
              f"-> {'keeps up' if w < per else 'FALLS BEHIND'} (wall/audio {w / per:.2f}), "
              f"cpu {c:.0f} s = {c / w:.1f} cores busy, cpu-RTF/stream {c / sum(done):.3f}")
    else:
        model = speech.load_model(a.model, a.threads, a.root)
        files = seg_files(a.dirs[0], a.start, int(a.window / 2))
        for rep in range(3):
            c0, w0 = cpu_now(), time.perf_counter()
            au = speech.decode_segments(files)
            w = speech.transcribe_chunk(model, au, 0.0, (0.0, 1e9))
            print(f"confirm {a.window:.0f} s window, {a.model} x{a.threads} thr: "
                  f"{time.perf_counter() - w0:.1f} s wall, {cpu_now() - c0:.1f} s cpu, {len(w)} words")
    return 0


if __name__ == "__main__":
    sys.exit(main())
