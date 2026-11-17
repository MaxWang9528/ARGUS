"""Per-second audio event scores from YAMNet (TFLite, CPU).

YAMNet (MobileNet-v1, AudioSet's 521 classes) takes 0.975 s of 16 kHz mono audio and
returns one score per class. We run it once per second of stream (hop 1.0 s) and keep
the classes that matter for "something just happened" moments, plus the loudness.

Two ways in:
- ``score_file(path)``: decode a whole file with ffmpeg (offline / evaluation);
- ``StreamScorer.feed_segment(path, start_ts)``: one HLS segment (~2 s MPEG-TS) at a time,
  the way the app's capture buffer writes them; it carries the leftover samples over to
  the next segment so frames line up on whole seconds of stream time.

Run with the scratch venv (numpy + ai-edge-litert):
    python -I scorer.py MODEL.tflite AUDIO_OR_TS OUT.npz
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np

SR = 16000
WIN = 15600   # YAMNet's fixed input (0.975 s)
HOP = 8000    # a frame every 0.5 s; two frames are max-pooled into one row per second

# AudioSet class indices (yamnet_class_map.csv) grouped into moment-relevant events.
GROUPS: dict[str, list[int]] = {
    "laugh":  [13, 15, 16, 17, 18],         # Laughter, Giggle, Snicker, Belly laugh, Chuckle
    "scream": [6, 7, 8, 9, 11],             # Shout, Bellow, Whoop, Yell, Screaming
    "gasp":   [39, 22, 33],                 # Gasp, Wail/moan, Groan
    "cheer":  [61, 62, 64, 65],             # Cheering, Applause, Crowd, Hubbub
    "bang":   [420, 421, 422, 424, 426, 428, 430],  # Explosion, Gunshot, Machine gun, Artillery, Fireworks, Burst, Boom
}
CONTEXT = {"speech": [0], "music": [132], "silence": [494]}
KEEP = sorted({i for v in (*GROUPS.values(), *CONTEXT.values()) for i in v})


def decode(path: str | Path, ffmpeg: str = "/usr/bin/ffmpeg") -> np.ndarray:
    """Any audio/video file -> float32 mono 16 kHz in [-1, 1]."""
    out = subprocess.run(
        [ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR),
         "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32)


def decode_pyav(path: str | Path) -> np.ndarray:
    """Same as decode() but in-process with PyAV (no ffmpeg spawn per segment)."""
    import av
    with av.open(str(path)) as f:
        st = f.streams.audio[0]
        rs = av.AudioResampler(format="flt", layout="mono", rate=SR)
        parts = [fr.to_ndarray().reshape(-1) for pk in f.demux(st) for f0 in pk.decode()
                 for fr in rs.resample(f0)]
        parts += [fr.to_ndarray().reshape(-1) for fr in rs.resample(None)]
    return np.concatenate(parts) if parts else np.zeros(0, np.float32)


class Yamnet:
    def __init__(self, model_path: str | Path, threads: int = 1):
        from ai_edge_litert.interpreter import Interpreter
        self.it = Interpreter(model_path=str(model_path), num_threads=threads)
        self.it.allocate_tensors()
        self.inp = self.it.get_input_details()[0]["index"]
        self.out = self.it.get_output_details()[0]["index"]

    def frame(self, x: np.ndarray) -> np.ndarray:
        self.it.set_tensor(self.inp, x.astype(np.float32, copy=False))
        self.it.invoke()
        return self.it.get_tensor(self.out)[0]


def frames_scores(model: Yamnet, wav: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(n_seconds, len(KEEP)) class scores and (n_seconds,) RMS loudness in dBFS.

    Row k covers stream second [k, k+1): frames start at k and k+0.5 s; scores are the max
    of the two (a short laugh or gasp is not split across frame edges), loudness the mean.
    """
    per = SR // HOP
    n = max(0, (len(wav) - WIN) // SR + 1)
    scores = np.zeros((n, len(KEEP)), np.float32)
    rms = np.zeros(n, np.float32)
    for k in range(n):
        fr, en = [], []
        for j in range(per):
            a = k * SR + j * HOP
            x = wav[a: a + WIN]
            if len(x) < WIN:
                break
            fr.append(model.frame(x)[KEEP])
            en.append(np.mean(x * x))
        scores[k] = np.max(fr, axis=0)
        rms[k] = 10 * np.log10(np.mean(en) + 1e-10)
    return scores, rms


def score_file(model: Yamnet, path: str | Path) -> dict[str, np.ndarray]:
    wav = decode(path)
    scores, rms = frames_scores(model, wav)
    return {"scores": scores, "rms": rms, "keep": np.array(KEEP), "t": np.arange(len(rms), dtype=np.float64)}


class StreamScorer:
    """Incremental scorer fed one HLS segment at a time (near real time).

    Keeps the audio from the start of the next unscored second onward; a row for second
    [t, t+1) is emitted once audio up to t + 0.5 + 0.975 s has arrived. A segment whose
    start is more than 0.5 s off the expected time (a hole in the buffer, an encoder
    restart) re-anchors the clock and drops the partial second.
    """

    def __init__(self, model: Yamnet):
        self.model = model
        self.buf = np.zeros(0, np.float32)
        self.t_next: float | None = None   # stream time of buf[0]
        self.rows: list[tuple[float, np.ndarray, float]] = []

    def feed_segment(self, path: str | Path, start_ts: float, gap: bool = False) -> int:
        return self.feed_audio(decode(path), start_ts, gap)

    def feed_audio(self, wav: np.ndarray, start_ts: float, gap: bool = False) -> int:
        if self.t_next is not None and not gap:
            expected = self.t_next + len(self.buf) / SR
            gap = abs(start_ts - expected) > 0.5
        if self.t_next is None or gap:
            self.buf, self.t_next = np.zeros(0, np.float32), start_ts
        self.buf = np.concatenate([self.buf, wav])
        done = 0
        need = (SR // HOP - 1) * HOP + WIN
        while len(self.buf) >= need:
            fr, en = [], []
            for j in range(SR // HOP):
                x = self.buf[j * HOP: j * HOP + WIN]
                fr.append(self.model.frame(x)[KEEP])
                en.append(np.mean(x * x))
            self.rows.append((self.t_next, np.max(fr, axis=0), float(10 * np.log10(np.mean(en) + 1e-10))))
            self.buf = self.buf[SR:]
            self.t_next += 1.0
            done += 1
        return done


def group_matrix(scores: np.ndarray, keep: np.ndarray) -> dict[str, np.ndarray]:
    """Per-group score per second: max over the group's classes."""
    col = {int(c): i for i, c in enumerate(keep)}
    out = {g: scores[:, [col[c] for c in cls]].max(axis=1) for g, cls in GROUPS.items()}
    for g, cls in CONTEXT.items():
        out[g] = scores[:, [col[c] for c in cls]].max(axis=1)
    return out


if __name__ == "__main__":
    model_path, src, dst = sys.argv[1:4]
    res = score_file(Yamnet(model_path), src)
    np.savez_compressed(dst, **res)
    g = group_matrix(res["scores"], res["keep"])
    print(f"{src}: {len(res['rms'])} s;", " ".join(f"{k} p99={np.percentile(v, 99):.2f}" for k, v in g.items()))
