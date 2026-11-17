"""Score a recorded capture buffer (the app's segment folder + segments.jsonl).

    python -I score_stream.py MODEL.tflite STREAM_DIR OUT.npz

STREAM_DIR holds ``segments.jsonl`` (the capture worker's events) and ``segments/``.
Segments are fed in order through StreamScorer, exactly as the live buffer would feed
them, each anchored at its Twitch ``date`` converted to chat time the way
argus/capture/buffer.py does it (``date + clock_offset``, clock_offset = the smallest
``recv_end - segment end``). Contiguous segments are decoded several at a time (one
ffmpeg call per CHUNK segments) to keep the offline run cheap; the rows are identical.

Output: t (chat-time epoch seconds of each 1 s row), scores (rows x KEEP), keep, rms.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scorer as S  # noqa: E402

CHUNK = 15


def load_segments(stream_dir: Path) -> list[dict]:
    segs: dict[int, dict] = {}
    for line in (stream_dir / "segments.jsonl").read_text().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("t") == "seg" and not ev.get("ad"):
            segs[int(ev["num"])] = ev
        elif ev.get("t") == "meta" and int(ev.get("num", -1)) in segs:
            s = segs[int(ev["num"])]
            if ev.get("date") is not None:
                s["date"] = ev["date"]
            if ev.get("dur"):
                s["dur"] = ev["dur"]
    out = [s for s in sorted(segs.values(), key=lambda s: s["num"])
           if s.get("date") is not None and (stream_dir / "segments" / s["file"]).exists()]
    return out


def main() -> None:
    model_path, stream_dir, dst = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    segs = load_segments(stream_dir)
    if not segs:
        print(f"{stream_dir}: no segments")
        return
    offset = min(s["recv_end"] - (s["date"] + s["dur"]) for s in segs)
    sc = S.StreamScorer(S.Yamnet(model_path))
    # group into contiguous runs, then chunks
    runs: list[list[dict]] = [[segs[0]]]
    for a, b in zip(segs, segs[1:]):
        if b["num"] == a["num"] + 1 and abs(b["date"] - (a["date"] + a["dur"])) < 0.5:
            runs[-1].append(b)
        else:
            runs.append([b])
    for run in runs:
        first = True
        for i in range(0, len(run), CHUNK):
            part = run[i:i + CHUNK]
            files = "|".join(str(stream_dir / "segments" / s["file"]) for s in part)
            wav = np.frombuffer(subprocess.run(
                ["/usr/bin/ffmpeg", "-nostdin", "-v", "error", "-i", "concat:" + files, "-vn", "-ac", "1",
                 "-ar", str(S.SR), "-f", "f32le", "-"], capture_output=True, check=True).stdout, np.float32)
            sc.feed_audio(wav, part[0]["date"] + offset, gap=first)
            first = False
    t = np.array([r[0] for r in sc.rows])
    np.savez_compressed(dst, t=t, scores=np.array([r[1] for r in sc.rows]), keep=np.array(S.KEEP),
                        rms=np.array([r[2] for r in sc.rows], np.float32), clock_offset=offset)
    print(f"{stream_dir.name}: {len(segs)} segments in {len(runs)} run(s), {len(t)} s scored, "
          f"clock_offset {offset:+.2f} s")


if __name__ == "__main__":
    main()
