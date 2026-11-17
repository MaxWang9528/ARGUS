"""A stand-in capture worker for Recorder tests: speaks the same JSON-lines protocol
as ``python -m livewatcher.capture.recorder worker`` but makes up small segments."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path


def emit(ev: dict) -> None:
    sys.stdout.write(json.dumps(ev) + "\n")
    sys.stdout.flush()


def watch_stdin() -> None:
    while sys.stdin.buffer.read(1024):
        pass
    os._exit(0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--seg-dur", type=float, default=0.2)
    ap.add_argument("--count", type=int, default=0, help="segments before a clean end (0 = forever)")
    ap.add_argument("--crash-after", type=int, default=0, help="exit 1 after this many segments")
    ap.add_argument("--gap-at", type=int, default=0, help="skip this segment as an ad")
    ap.add_argument("--silent", action="store_true", help="start, then never say anything")
    args = ap.parse_args()
    threading.Thread(target=watch_stdin, daemon=True).start()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    emit({"t": "start", "quality": "720p60", "low_latency": True, "pid": os.getpid()})
    print("INFO fake worker up", file=sys.stderr, flush=True)
    if args.silent:
        time.sleep(3600)
    date = time.time() - 0.05
    i = 0
    while True:
        i += 1
        time.sleep(args.seg_dur)
        meta = {"num": 1000 + i, "date": date, "dur": args.seg_dur, "ad": False, "prefetch": True}
        date += args.seg_dur
        if i == args.gap_at:
            emit({"t": "skip", **meta, "ad": True, "reason": "ad"})
        else:
            name = f"seg_{i:06d}.ts"
            (out / name).write_bytes(b"\x47" + bytes(187))
            emit({"t": "seg", "file": name, "bytes": 188, "recv_end": time.time(), **meta})
        if args.crash_after and i >= args.crash_after:
            print("ERROR fake worker crashed", file=sys.stderr, flush=True)
            return 1
        if args.count and i >= args.count:
            emit({"t": "end", "reason": "stream ended"})
            return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
