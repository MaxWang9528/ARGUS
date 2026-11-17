"""Speech-excitement signal: transcribe the streamer in rolling chunks and score
excitement per second.

Pipeline (all CPU, faster-whisper / CTranslate2 int8):

1. Audio is decoded to 16 kHz mono float (ffmpeg). Live, a chunk is the last few
   HLS segments of the capture buffer (``BufferIndex.select``), concatenated and
   decoded; offline, any audio file.
2. Chunks of ``CHUNK_S`` seconds, hopping ``HOP_S`` (so they overlap by
   ``CHUNK_S - HOP_S``), are transcribed with Silero VAD on and word timestamps.
   A word is kept only by the chunk that "owns" its midpoint (the middle part of
   the chunk), so the overlap never counts a word twice.
3. Per-second features: words/s, excitement phrases, swears, laughter, shouting
   (speech-band loudness), all against a rolling median/MAD baseline.
4. ``candidates`` turns the score into moments (local maxima, a minimum gap).

Run ``python -I speech.py transcribe <audio> <out.json>`` to write words + per-second
loudness, then ``score``/``evaluate.py`` work from that JSON.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import time
from dataclasses import dataclass

import numpy as np

SR = 16000
CHUNK_S = 10.0
HOP_S = 8.0

# Things people say when something happens. Game-neutral, English. Matched against
# the lower-cased text of a word and the two after it, punctuation removed. Kept to
# phrases; everyday single words ("go", "wait", "bro") are left to the repeat cue.
EXCLAIM = re.compile(r"""(?x)^(?:
      oh\ my\ (?:god|gosh|lord|days) | oh\ (?:no|shit|crap|fuck|god|wow|hell) | no\ way
    | let'?s\ (?:go+|fucking) | what\ the\ (?:fuck|hell|heck|f) | holy\ (?:shit|crap|cow|moly|fuck)
    | are\ you\ (?:kidding|serious) | you'?re\ kidding | jesus\ christ | get\ out\ of\ here
    | i\ can'?t\ believe | bro\ what | dude\ what | wtf | omg | wo+w | who+a | sheesh | ye+s+\ ye+s
    | no+\ no+ | ah+ | aa+h+ | oh+ )\b""")
SWEAR = re.compile(r"(?i)\b(?:f+u+c+k+\w*|shit\w*|damn\w*|bitch\w*|asshole|crap|"
                   r"motherf\w*|goddamn\w*|bastard)\b")
LAUGH = re.compile(r"(?i)(?:\b(?:ha){2,}h?\b|\bhe(?:he)+\b|\blol\b|[\[(](?:laugh\w*|chuckl\w*)[\])])")


def decode(path: str, start: float | None = None, dur: float | None = None) -> np.ndarray:
    """Decode audio to 16 kHz mono float32 with ffmpeg."""
    cmd = ["/usr/bin/ffmpeg", "-nostdin", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", path]
    if dur is not None:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32)


def decode_segments(files: list[str]) -> np.ndarray:
    """Live path: concatenate HLS .ts segments from the capture buffer and decode."""
    cmd = ["/usr/bin/ffmpeg", "-nostdin", "-v", "error", "-i", "concat:" + "|".join(files),
           "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    return np.frombuffer(subprocess.run(cmd, capture_output=True, check=True).stdout, np.float32)


def loudness_per_s(audio: np.ndarray) -> list[float]:
    """Speech-band (300-3400 Hz) RMS in dBFS per second: a cheap 'shouting' cue."""
    n = len(audio) // SR
    out = []
    for i in range(n):
        x = audio[i * SR:(i + 1) * SR]
        spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        f = np.fft.rfftfreq(len(x), 1 / SR)
        band = spec[(f > 300) & (f < 3400)]
        out.append(float(10 * np.log10(np.mean(band ** 2) + 1e-12)))
    return out


def load_model(name: str = "base.en", threads: int = 1, root: str | None = None):
    from faster_whisper import WhisperModel
    return WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=threads,
                        num_workers=1, download_root=root)


def transcribe_chunk(model, audio: np.ndarray, t0: float, own: tuple[float, float],
                     beam: int = 1) -> list[dict]:
    """Words of one chunk (absolute times) whose midpoint lies in ``own``."""
    segs, _ = model.transcribe(
        audio, language="en", beam_size=beam, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300}, word_timestamps=True,
        condition_on_previous_text=False, without_timestamps=False, temperature=0.0,
        hallucination_silence_threshold=2.0)
    words = []
    for s in segs:
        if s.no_speech_prob > 0.8 and s.avg_logprob < -1.0:
            continue
        if s.compression_ratio > 2.4:   # a repetition loop (music, "go go go go ...")
            continue
        run, prev = 0, None
        for w in s.words or []:
            key = re.sub(r"\W", "", w.word.lower())
            run = run + 1 if key == prev else 0
            prev = key
            if run >= 2 or w.probability < 0.15:   # keep at most two repeats in a row
                continue
            a, b = t0 + w.start, t0 + w.end
            mid = (a + b) / 2
            if own[0] <= mid < own[1]:
                words.append({"t": round(a, 2), "e": round(b, 2), "w": w.word.strip(),
                              "p": round(w.probability, 3)})
    return words


def transcribe_file(model, path: str, start: float = 0.0, dur: float | None = None,
                    chunk_s: float = CHUNK_S, hop_s: float = HOP_S) -> dict:
    """Rolling-chunk transcription of a file; returns words, loudness and timing."""
    audio = decode(path, start or None, dur)
    total = len(audio) / SR
    words, cpu = [], 0.0
    pad = (chunk_s - hop_s) / 2
    t = 0.0
    while t < total:
        a = audio[int(t * SR):int((t + chunk_s) * SR)]
        lo = 0.0 if t == 0 else t + pad
        hi = total + 1 if t + chunk_s >= total else t + chunk_s - pad
        c0 = time.process_time(); w0 = time.perf_counter()
        words += transcribe_chunk(model, a, t, (lo, hi))
        cpu += time.perf_counter() - w0
        if t + chunk_s >= total:
            break
        t += hop_s
    return {"start": start, "duration": total, "words": words,
            "loud": loudness_per_s(audio), "wall_s": cpu}


# ---------------------------------------------------------------- scoring

@dataclass
class ScoreCfg:
    smooth_s: int = 3          # features are summed over this trailing window
    baseline_s: int = 180      # rolling median/MAD window
    w_wps: float = 1.0
    w_loud: float = 1.0
    w_excl: float = 1.5
    w_swear: float = 1.0
    w_laugh: float = 1.0
    wps_active: bool = True    # v2: words/s against the speaking rate (v1: False)


def _norm(w: str) -> str:
    return re.sub(r"[^a-z' ]", "", w.lower()).strip()


def features(words: list[dict], loud: list[float], n: int) -> dict[str, np.ndarray]:
    """Per-second raw features over n seconds (word times relative to second 0)."""
    wps = np.zeros(n); ex = np.zeros(n); sw = np.zeros(n); la = np.zeros(n)
    rep = np.zeros(n); bang = np.zeros(n)
    toks = [_norm(w["w"]) for w in words]
    for i, w in enumerate(words):
        s = int(w["t"])
        if not 0 <= s < n:
            continue
        wps[s] += 1
        if SWEAR.search(w["w"]):
            sw[s] += 1
        if LAUGH.search(w["w"]):
            la[s] += 1
        if "!" in w["w"]:
            bang[s] += 1
        if EXCLAIM.match(" ".join(toks[i:i + 3])):
            ex[s] += 1
        # the same short word twice in a row within a second ("go go", "no no", "run run")
        if (i and toks[i] and toks[i] == toks[i - 1] and len(toks[i]) <= 5
                and w["t"] - words[i - 1]["t"] < 1.0):
            rep[s] += 1
    ld = np.full(n, np.nan)
    m = min(n, len(loud))
    ld[:m] = loud[:m]
    return {"wps": wps, "excl": ex, "swear": sw, "laugh": la, "rep": rep, "bang": bang, "loud": ld}


def _roll(x: np.ndarray, k: int) -> np.ndarray:
    c = np.concatenate([[0], np.cumsum(x)])
    out = np.empty(len(x))
    for i in range(len(x)):
        out[i] = c[i + 1] - c[max(0, i + 1 - k)]
    return out


def _robust_z(x: np.ndarray, win: int, floor: float, active: bool = False) -> np.ndarray:
    """Causal z against the trailing median/MAD (what a live tracker can know).
    ``active``: baseline only over seconds where x > 0 (the speaking rate while
    speaking), so talking again after a pause is not a jump."""
    z = np.zeros(len(x))
    for i in range(len(x)):
        h = x[max(0, i - win):i]
        if active:
            h = h[h > 0]
        if len(h) < 30:
            continue
        med = np.median(h)
        mad = max(np.median(np.abs(h - med)) * 1.4826, floor)
        z[i] = (x[i] - med) / mad
    return z


def _lex(x: np.ndarray, win: int, cap: float) -> np.ndarray:
    """Count over 5 s, capped, divided by 1 + this streamer's usual 5 s count (a
    streamer who swears all the time gets less for swearing)."""
    c = np.minimum(_roll(x, 5), cap)
    usual = _roll(x, win) / win * 5
    return c / (1.0 + usual)


def score(words: list[dict], loud: list[float], n: int, cfg: ScoreCfg = ScoreCfg()) -> dict:
    f = features(words, loud, n)
    k = cfg.smooth_s
    wps = _roll(f["wps"], k) / k
    # loudness: loudest second in the window, speech band; only louder than usual counts
    lf = np.where(np.isnan(f["loud"]), np.nanmedian(f["loud"]) if np.isfinite(f["loud"]).any() else -60, f["loud"])
    ld = np.array([lf[max(0, i - k + 1):i + 1].max() for i in range(n)])
    z_wps = np.clip(_robust_z(wps, cfg.baseline_s, 1.0 if cfg.wps_active else 0.5,
                                   cfg.wps_active), -2, 6)
    z_loud = np.clip(_robust_z(ld, cfg.baseline_s, 2.0), -2, 6)
    B = cfg.baseline_s
    ex = _lex(f["excl"], B, 3); sw = _lex(f["swear"], B, 3); la = _lex(f["laugh"], B, 2)
    rp = _lex(f["rep"] + f["bang"], B, 3)
    s = (cfg.w_wps * np.maximum(z_wps, 0) + cfg.w_loud * np.maximum(z_loud, 0)
         + cfg.w_excl * (ex + 0.5 * rp) + cfg.w_swear * sw + cfg.w_laugh * la)
    return {"score": s, "z_wps": z_wps, "z_loud": z_loud, "excl": ex, "swear": sw,
            "laugh": la, "rep": rp}


def candidates(s: np.ndarray, top_n: int | None = None, min_gap_s: int = 30,
               min_score: float = 0.0) -> list[tuple[int, float]]:
    """Greedy non-maximum suppression: (second, score), highest first."""
    order = np.argsort(-s)
    picked: list[tuple[int, float]] = []
    for i in order:
        if s[i] <= min_score:
            break
        if all(abs(int(i) - j) >= min_gap_s for j, _ in picked):
            picked.append((int(i), float(s[i])))
            if top_n and len(picked) >= top_n:
                break
    return picked


def text_window(words: list[dict], a: float, b: float) -> str:
    """Transcript of [a, b): the caption seed for a clip."""
    return " ".join(w["w"] for w in words if a <= w["t"] < b)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("transcribe")
    t.add_argument("audio"); t.add_argument("out")
    t.add_argument("--model", default="base.en"); t.add_argument("--threads", type=int, default=1)
    t.add_argument("--root", default=None)
    t.add_argument("--start", type=float, default=0.0); t.add_argument("--dur", type=float, default=None)
    a = ap.parse_args()
    model = load_model(a.model, a.threads, a.root)
    r = transcribe_file(model, a.audio, a.start, a.dur)
    r.update(model=a.model, threads=a.threads, rtf=r["wall_s"] / max(r["duration"], 1e-9))
    with open(a.out, "w") as fh:
        json.dump(r, fh)
    print(f"{a.audio}: {r['duration']:.0f} s, {len(r['words'])} words, "
          f"wall {r['wall_s']:.1f} s, RTF {r['rtf']:.3f} ({a.model}, {a.threads} thr)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
