"""Transcribe a capture buffer (the app's HLS segment folder + its segments.jsonl
event log) the way a live worker would: every ``HOP`` segments, decode the last
``CHUNK`` segments (about 10 s) straight from the .ts files and transcribe them.

Times are written on the chat clock: ``segment.date + clock_offset`` where
``clock_offset`` is the minimum ``recv_end - segment end`` (as in
``argus/capture/buffer.py``).

  python -I buffer_stream.py <stream_dir> <out.json> [--model base.en] [--follow]

``--follow`` keeps tailing segments.jsonl like the live app would (stops after
``--idle`` seconds without a new segment). CPU seconds (own process plus ffmpeg
children) are recorded so the real-time factor is independent of other load.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import speech  # noqa: E402

CHUNK = 5   # segments per transcription chunk (~10 s)
HOP = 4     # new segments between chunks (~8 s, so ~2 s overlap)


def read_segments(path: str) -> list[dict]:
    segs = []
    with open(path) as fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue  # a line still being written
            if ev.get("t") == "seg" and not ev.get("ad") and ev.get("date"):
                segs.append(ev)
    return segs


def cpu_now() -> float:
    s = resource.getrusage(resource.RUSAGE_SELF)
    c = resource.getrusage(resource.RUSAGE_CHILDREN)
    return s.ru_utime + s.ru_stime + c.ru_utime + c.ru_stime


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir"); ap.add_argument("out")
    ap.add_argument("--model", default="base.en"); ap.add_argument("--threads", type=int, default=1)
    ap.add_argument("--root", default=None)
    ap.add_argument("--follow", action="store_true"); ap.add_argument("--idle", type=float, default=60)
    ap.add_argument("--max-s", type=float, default=None, help="stop after this much audio")
    a = ap.parse_args()

    model = speech.load_model(a.model, a.threads, a.root)
    log = os.path.join(a.dir, "segments.jsonl")
    words: list[dict] = []
    loud: dict[int, float] = {}
    prev_hi = -1.0
    pos = 0              # index of the first segment of the next chunk
    offset = None
    cpu0, wall0 = cpu_now(), time.perf_counter()
    audio_s = 0.0
    lat: list[float] = []
    last_new = time.time()
    while True:
        segs = read_segments(log)
        if segs:
            offset = min(s["recv_end"] - (s["date"] + s["dur"]) for s in segs[-150:])
        progressed = False
        while pos + CHUNK <= len(segs) or (not a.follow and pos < len(segs)):
            chunk = segs[pos:pos + CHUNK]
            # a gap inside the chunk: cut the chunk there and restart after it
            for i in range(1, len(chunk)):
                if chunk[i]["date"] - (chunk[i - 1]["date"] + chunk[i - 1]["dur"]) > 0.5:
                    chunk = chunk[:i]
                    break
            files = [os.path.join(a.dir, "segments", s["file"]) for s in chunk]
            t0 = chunk[0]["date"] + offset
            dur = sum(s["dur"] for s in chunk)
            full = len(chunk) == CHUNK
            step = HOP if full else len(chunk)
            # own from where the previous chunk stopped owning (or the chunk start after
            # a gap); up to 1 s before the end, which the next chunk re-hears in context
            lo = prev_hi if t0 <= prev_hi < t0 + dur else t0
            hi = t0 + dur - (CHUNK - HOP) * 2.0 / 2 if full else t0 + dur
            prev_hi = hi
            try:
                audio = speech.decode_segments(files)
            except Exception as e:  # noqa: BLE001 - a broken segment: skip it
                print("decode failed", files[0], e, file=sys.stderr)
                pos += step
                continue
            words += speech.transcribe_chunk(model, audio, t0, (lo, hi))
            for k, db in enumerate(speech.loudness_per_s(audio)):
                sec = int(t0 + k)
                if lo <= sec < hi:
                    loud[sec] = db
            audio_s += step * 2.0
            if a.follow:
                lat.append(time.time() - chunk[-1]["recv_end"])
            pos += step
            progressed = True
            if a.max_s and audio_s >= a.max_s:
                break
        if a.max_s and audio_s >= a.max_s:
            break
        if not a.follow:
            break
        if progressed:
            last_new = time.time()
            with open(a.out + ".part", "w") as fh:
                json.dump({"words": words}, fh)
        elif time.time() - last_new > a.idle:
            break
        time.sleep(1.0)

    cpu = cpu_now() - cpu0
    r = {"dir": a.dir, "model": a.model, "threads": a.threads, "clock_offset": offset,
         "audio_s": audio_s, "cpu_s": cpu, "wall_s": time.perf_counter() - wall0,
         "cpu_rtf": cpu / max(audio_s, 1e-9),
         "latency_p50": float(np.median(lat)) if lat else None,
         "latency_p95": float(np.percentile(lat, 95)) if lat else None,
         "words": words, "loud": {str(k): v for k, v in sorted(loud.items())}}
    with open(a.out, "w") as fh:
        json.dump(r, fh)
    print(f"{os.path.basename(a.dir.rstrip('/'))}: {audio_s:.0f} s audio, {len(words)} words, "
          f"cpu {cpu:.0f} s, cpu-RTF {r['cpu_rtf']:.3f}, lat p50 {r['latency_p50']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
