"""Per-second cheap video features for one recording.

    python -I extract.py VIDEO OUT.npz [--fps 4] [--width 160] [--face x,y,w,h]

One ffmpeg run decodes the video, drops it to ``--fps`` frames per second and
scales it to ``--width`` (16:9) grey frames, piped to numpy as raw bytes. Per
frame we compute:

- ``motion``: mean absolute difference from the previous frame (0-255 scale);
- ``face``: the same, inside the facecam region (fractions of the frame, as in
  a streamer's ``stack_top`` layout region), or NaN when no region is given;
- ``luma``: mean brightness;
- ``hist``: L1 distance between 32-bin luma histograms of consecutive frames
  (0-2); a scene cut is ``hist > CUT_HIST``.

Per second (bin = floor(frame_time)) we keep mean/max motion, mean/max face
motion (cut frames left out, a cut is not a reaction), the biggest brightness
jump against the previous second's mean, and the number of cuts.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

import numpy as np

CUT_HIST = 0.6     # histogram L1 distance that counts as a hard cut


def decode(path: str, fps: float, width: int, height: int, *, threads: int = 1,
           start: float | None = None, duration: float | None = None, keyframes: bool = False):
    """Grey frames (numpy) at ``fps``. ``keyframes``: decode only keyframes (Twitch: one per
    2 s segment), which skips almost all of the decoding work."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-threads", str(threads)]
    if keyframes:
        cmd += ["-skip_frame", "nokey"]
    if start is not None:
        cmd += ["-ss", str(start)]
    cmd += ["-i", path]
    if duration is not None:
        cmd += ["-t", str(duration)]
    cmd += ["-an", "-sn", "-map", "0:v:0", "-vf",
            f"fps={fps},scale={width}:{height}:flags=area,format=gray",
            "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    n = width * height
    try:
        while True:
            buf = proc.stdout.read(n)
            if len(buf) < n:
                break
            yield np.frombuffer(buf, np.uint8).reshape(height, width)
    finally:
        proc.stdout.close()
        proc.wait()


def frame_features(frames, face=None):
    """Yields (motion, face_motion, luma, hist_dist) per frame."""
    prev = prev_h = None
    sl = None
    for f in frames:
        if sl is None and face is not None:
            h, w = f.shape
            x, y, fw, fh = face
            sl = (slice(int(y * h), max(int(y * h) + 1, int((y + fh) * h))),
                  slice(int(x * w), max(int(x * w) + 1, int((x + fw) * w))))
        fi = f.astype(np.int16)
        hist = np.bincount(f.ravel() >> 3, minlength=32) / f.size
        if prev is None:
            m = fm = hd = 0.0
        else:
            d = np.abs(fi - prev)
            m = float(d.mean())
            fm = float(d[sl].mean()) if sl is not None else float("nan")
            hd = float(np.abs(hist - prev_h).sum())
        yield m, fm, float(f.mean()), hd
        prev, prev_h = fi, hist


def per_second(rows: np.ndarray, fps: float) -> dict[str, np.ndarray]:
    """rows: (n_frames, 4) at a constant fps from t=0. Returns per-second arrays."""
    sec = (np.arange(len(rows)) / fps).astype(int)
    return per_second_t(rows, sec, int(sec[-1]) + 1 if len(rows) else 0)


def segment_frames(seg_dir: str, index: str, fps: float, width: int, height: int):
    """Frames from an ARGUS buffer folder (``segments.jsonl`` + ``segments/``), one
    ffmpeg run per 2 s segment as the live app would do when a segment closes.
    Yields (twitch_clock_time, frame)."""
    import json
    import os
    for line in open(index, encoding="utf-8"):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("t") != "seg" or ev.get("ad"):
            continue
        path = os.path.join(seg_dir, ev["file"])
        if not os.path.exists(path):
            continue
        for k, f in enumerate(decode(path, fps, width, height)):
            yield ev["date"] + k / fps, f


def features_from_segments(seg_dir: str, index: str, fps: float = 4.0, width: int = 160,
                           face=None) -> dict:
    height = int(round(width * 9 / 16 / 2) * 2)
    ts, frames = [], []
    for t, f in segment_frames(seg_dir, index, fps, width, height):
        ts.append(t)
        frames.append(f)
    rows = np.array(list(frame_features(iter(frames), face)), dtype=np.float64).reshape(-1, 4)
    ts = np.array(ts)
    # re-time onto a whole-second grid from the first frame; gaps stay NaN
    t0 = np.floor(ts[0])
    rel = ts - t0
    sec = rel.astype(int)
    S = int(sec[-1]) + 1
    out = per_second_t(rows, sec, S)
    out["t0"] = t0
    return out


def per_second_t(rows: np.ndarray, sec: np.ndarray, S: int) -> dict[str, np.ndarray]:
    """Like per_second, with an explicit (sorted) second index per frame."""
    out = {k: np.full(S, np.nan) for k in
           ("motion", "motion_max", "face", "face_max", "flash", "cuts", "luma")}
    motion, face, luma, hd = rows.T
    cut = hd > CUT_HIST
    starts = np.searchsorted(sec, np.arange(S))
    ends = np.searchsorted(sec, np.arange(S), side="right")
    for s in range(S):
        a, b = starts[s], ends[s]
        if a == b:
            continue
        mo, fa, cu = motion[a:b], face[a:b], cut[a:b]
        out["motion"][s] = mo.mean()
        out["motion_max"][s] = mo.max()
        nc = fa[~cu]
        if nc.size and not np.all(np.isnan(nc)):
            out["face"][s] = np.nanmean(nc)
            out["face_max"][s] = np.nanmax(nc)
        out["cuts"][s] = cu.sum()
        out["luma"][s] = luma[a:b].mean()
    for s in range(1, S):
        a, b = starts[s], ends[s]
        if a < b and not np.isnan(out["luma"][s - 1]):
            out["flash"][s] = np.abs(luma[a:b] - out["luma"][s - 1]).max()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("out")
    ap.add_argument("--fps", type=float, default=4.0)
    ap.add_argument("--width", type=int, default=160)
    ap.add_argument("--face", default="", help="x,y,w,h fractions of the frame")
    ap.add_argument("--threads", type=int, default=1)
    a = ap.parse_args(argv)
    face = tuple(float(v) for v in a.face.split(",")) if a.face else None
    height = int(round(a.width * 9 / 16 / 2) * 2)
    t0, c0 = time.time(), time.process_time()
    rows = np.array(list(frame_features(decode(a.video, a.fps, a.width, height, threads=a.threads),
                                        face)), dtype=np.float64).reshape(-1, 4)
    feats = per_second(rows, a.fps)
    ru = __import__("resource").getrusage(__import__("resource").RUSAGE_CHILDREN)
    wall = time.time() - t0
    cpu_child = ru.ru_utime + ru.ru_stime
    cpu_self = time.process_time() - c0
    secs = len(feats["motion"])
    np.savez(a.out, **feats, frames=rows, fps=a.fps)
    print(f"{a.video}: {secs} s of video, {len(rows)} frames, wall {wall:.1f} s, "
          f"cpu ffmpeg {cpu_child:.1f} s + python {cpu_self:.1f} s "
          f"= {100 * (cpu_child + cpu_self) / max(secs, 1):.2f}% of one core", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
