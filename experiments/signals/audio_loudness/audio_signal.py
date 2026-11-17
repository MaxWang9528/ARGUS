"""Audio loudness / excitement signal (prototype).

Per stream, per 1 s bin, from the audio of the HLS segments the app already buffers:

- ``mom``   momentary loudness (400 ms window, max over the second), LUFS-like dB.
            The PCM is roughly K-weighted by ffmpeg (high-pass 60 Hz + high shelf
            +4 dB at 1.5 kHz) before it gets here, as in BS.1770.
- ``short`` short-term loudness (3 s window ending at the bin end).
- ``voice`` loudness of the 1-4 kHz band (where a raised / yelling voice puts its
            energy, and where speech dominates the game bed).
- ``flux``  spectral flux (half-wave rectified log-magnitude rise between 32 ms
            frames), mean over the second: sudden hits, crashes, screams.

``AudioTracker`` turns them into a z-score against the stream's own baseline the
same way ``argus.detect.peak`` does for chat: rolling median over
``baseline_window_s`` (bins inside a peak left out), MAD * 1.4826 as the spread with
a floor, and a recent-median lift so a step to a louder level (music turned up) is
not a peak. On top of the level z it scores the *rise*: loudness now minus the median
of the previous ``rise_ref_s`` seconds, in units of the spread (a sharp jump against
the immediately preceding context, which a broadcast limiter cannot flatten as much
as the absolute level).

Everything is streaming: ``FeatureExtractor.push(pcm)`` accepts any chunk size and
returns the bins that closed; ``AudioTracker.push(row)`` returns a z row and, when a
peak finishes, a candidate. ``decode`` runs ffmpeg once per file (offline) and
``SegmentDecoder`` keeps one ffmpeg per stream fed with segment bytes (live).
Only numpy is needed.
"""

from __future__ import annotations

import math
import subprocess
import threading
from collections import deque
from dataclasses import dataclass, field

import numpy as np

SR = 16000
BLOCK = SR // 10              # 100 ms blocks for the loudness windows
NFFT, HOP = 512, 256          # 32 ms frames, 16 ms hop for flux / band energy
FFMPEG = "/usr/bin/ffmpeg"
# Approximate K-weighting (BS.1770) + downmix + resample, done by ffmpeg while decoding.
AF = "highpass=f=60:poles=2,highshelf=f=1500:g=4,aresample=16000"
SILENCE_DB = -70.0
MAD_TO_SD = 1.4826


def _db(ms: np.ndarray | float) -> np.ndarray:
    return np.maximum(10.0 * np.log10(np.maximum(ms, 1e-12)) - 0.691, SILENCE_DB)


def ffmpeg_cmd(src: str = "pipe:0", fmt_in: str | None = None) -> list[str]:
    cmd = [FFMPEG, "-nostdin" if src != "pipe:0" else "-hide_banner", "-loglevel", "error"]
    if fmt_in:
        cmd += ["-f", fmt_in]
    return cmd + ["-i", src, "-vn", "-sn", "-dn", "-ac", "1", "-af", AF,
                  "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"]


def decode(path: str) -> np.ndarray:
    """Whole file -> mono float32 PCM at SR (offline use)."""
    out = subprocess.run(ffmpeg_cmd(path), capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32)


# ---------------------------------------------------------------------------- features
class FeatureExtractor:
    """Streaming per-second features. ``push`` any amount of PCM; get closed bins."""

    def __init__(self) -> None:
        self._buf = np.zeros(0, dtype=np.float32)   # PCM not yet in a full block/frame
        self._blocks: deque[float] = deque(maxlen=30)  # mean square per 100 ms (3 s)
        self._sec_blocks: list[float] = []           # momentary values in this second
        self._sec_flux: list[float] = []
        self._sec_voice: list[float] = []
        self._prev_logmag: np.ndarray | None = None
        self._frame_tail = np.zeros(NFFT - HOP, dtype=np.float32)
        self._win = np.hanning(NFFT).astype(np.float32)
        f = np.fft.rfftfreq(NFFT, 1 / SR)
        self._vband = (f >= 1000) & (f <= 4000)
        self._blocks_done = 0
        self.t = 0.0                                 # seconds of audio consumed

    def push(self, pcm: np.ndarray) -> list[dict]:
        rows: list[dict] = []
        self._buf = np.concatenate([self._buf, pcm.astype(np.float32, copy=False)])
        n = len(self._buf) // BLOCK
        if n == 0:
            return rows
        chunk, self._buf = self._buf[: n * BLOCK], self._buf[n * BLOCK:]
        # STFT over this chunk with the carried tail (frames hop exactly 256 samples).
        x = np.concatenate([self._frame_tail, chunk])
        self._frame_tail = x[-(NFFT - HOP):]
        nfr = (len(x) - NFFT) // HOP + 1
        idx = np.arange(NFFT)[None, :] + HOP * np.arange(nfr)[:, None]
        spec = np.abs(np.fft.rfft(x[idx] * self._win, axis=1))
        logmag = np.log1p(100.0 * spec)
        prev = self._prev_logmag if self._prev_logmag is not None else logmag[:1]
        d = np.diff(np.vstack([prev, logmag]), axis=0)
        flux = np.maximum(d, 0).sum(axis=1)          # one per frame
        self._prev_logmag = logmag[-1:]
        voice = (spec[:, self._vband] ** 2).sum(axis=1) / (NFFT * NFFT / 4)
        fpb = BLOCK // HOP                            # 6.25 frames per block -> use time index
        ms = (chunk.reshape(n, BLOCK) ** 2).mean(axis=1)
        for i in range(n):
            self._blocks.append(float(ms[i]))
            b = list(self._blocks)
            self._sec_blocks.append(float(np.mean(b[-4:])))
            lo, hi = int(i * fpb), max(int((i + 1) * fpb), int(i * fpb) + 1)
            self._sec_flux.extend(flux[lo:hi].tolist())
            self._sec_voice.extend(voice[lo:hi].tolist())
            self._blocks_done += 1
            if self._blocks_done % 10 == 0:
                self.t = self._blocks_done / 10
                rows.append({
                    "t": self.t,                      # bin end, seconds of audio
                    "mom": float(_db(max(self._sec_blocks))),
                    "short": float(_db(np.mean(b))),
                    "voice": float(_db(np.mean(self._sec_voice) if self._sec_voice else 0.0)),
                    "flux": float(np.mean(self._sec_flux)) if self._sec_flux else 0.0,
                })
                self._sec_blocks, self._sec_flux, self._sec_voice = [], [], []
        return rows


def features(pcm: np.ndarray, chunk_s: float = 2.0) -> list[dict]:
    """Offline convenience: feed PCM in segment-sized chunks, like the live path."""
    fx, rows, step = FeatureExtractor(), [], int(chunk_s * SR)
    for i in range(0, len(pcm), step):
        rows += fx.push(pcm[i:i + step])
    return rows


# ---------------------------------------------------------------------------- tracker
@dataclass
class AudioParams:
    baseline_window_s: float = 180.0
    recent_baseline_s: float = 60.0
    min_history_s: float = 60.0
    rise_ref_s: float = 10.0          # "rise" compares with the median of this long before
    rise_gap_s: float = 2.0           # ...ending this long before the bin
    floor_db: float = 1.5             # spread floor for the dB features
    floor_flux_rel: float = 0.05      # spread floor for flux, relative to its baseline
    w_level: float = 1.0              # score = w_level*z(mom) + w_voice*z(voice) + ...
    w_voice: float = 0.5
    w_rise: float = 1.0
    w_flux: float = 0.5
    start_score: float = 5.5          # a peak starts above this score (~5-10/h on dev samples)
    end_score: float = 1.5            # ...and ends when it falls below this
    min_len_s: float = 2.0
    max_len_s: float = 30.0
    cooldown_s: float = 30.0


@dataclass
class AudioPeak:
    t_start: float
    t_peak: float
    t_end: float
    score: float                       # max combined score
    level_z: float                     # z of momentary loudness at the peak
    rise_db: float                     # dB above the preceding context at the peak


def _median_mad(v: np.ndarray) -> tuple[float, float]:
    m = float(np.median(v))
    return m, float(np.median(np.abs(v - m)))


@dataclass
class AudioTracker:
    p: AudioParams = field(default_factory=AudioParams)

    def __post_init__(self) -> None:
        self._hist: deque[tuple[float, float, float, float]] = deque()  # t, mom, voice, flux
        self._ctx: deque[tuple[float, float]] = deque()   # t, mom (all bins, for the rise)
        self._first: float | None = None
        self._active: dict | None = None
        self._last_end = -math.inf

    def _base(self, col: int, t: float) -> tuple[float, float]:
        a = np.array([h[col] for h in self._hist])
        base, mad = _median_mad(a)
        rec = np.array([h[col] for h in self._hist if h[0] > t - self.p.recent_baseline_s])
        if len(rec) >= self.p.recent_baseline_s / 4:
            rb, rm = _median_mad(rec)
            if rb > base:
                base, mad = rb, rm
        floor = self.p.floor_db if col != 3 else max(self.p.floor_flux_rel * abs(base), 1e-6)
        return base, max(mad * MAD_TO_SD, floor)

    def push(self, row: dict) -> tuple[dict, AudioPeak | None]:
        p, t = self.p, row["t"]
        if self._first is None:
            self._first = t
        while self._hist and self._hist[0][0] <= t - p.baseline_window_s:
            self._hist.popleft()
        self._ctx.append((t, row["mom"]))
        while self._ctx and self._ctx[0][0] <= t - p.rise_ref_s - p.rise_gap_s:
            self._ctx.popleft()
        out = {"t": t, "z_level": 0.0, "z_voice": 0.0, "z_flux": 0.0, "z_rise": 0.0,
               "rise_db": 0.0, "score": 0.0}
        warm = t - self._first >= p.min_history_s and len(self._hist) >= 20
        peak = None
        if warm:
            if self._active is not None:
                bm, sm, bv, sv, bf, sf = self._active["base"]
            else:
                (bm, sm), (bv, sv), (bf, sf) = self._base(1, t), self._base(2, t), self._base(3, t)
            ctx = [m for tt, m in self._ctx if tt <= t - p.rise_gap_s]
            ref = float(np.median(ctx)) if ctx else row["mom"]
            out["z_level"] = (row["mom"] - bm) / sm
            out["z_voice"] = (row["voice"] - bv) / sv
            out["z_flux"] = (row["flux"] - bf) / sf
            out["rise_db"] = row["mom"] - max(ref, bm)  # a pause then normal talk is not a rise
            out["z_rise"] = out["rise_db"] / sm
            out["score"] = (p.w_level * max(out["z_level"], 0) + p.w_voice * max(out["z_voice"], 0)
                            + p.w_rise * max(out["z_rise"], 0) + p.w_flux * min(max(out["z_flux"], 0), 6))
            peak = self._step(t, out, (bm, sm, bv, sv, bf, sf))
        if self._active is None and peak is None:
            self._hist.append((t, row["mom"], row["voice"], row["flux"]))
        return out, peak

    def _step(self, t: float, z: dict, base: tuple) -> AudioPeak | None:
        p, a, s = self.p, self._active, z["score"]
        if a is None:
            if s > p.start_score and t - self._last_end >= p.cooldown_s:
                self._active = {"t0": t - 1, "tp": t, "s": s, "lz": z["z_level"],
                                "rise": z["rise_db"], "base": base}
            return None
        if s > a["s"]:
            a.update(tp=t, s=s, lz=z["z_level"], rise=z["rise_db"])
        if s < p.end_score or t - a["t0"] >= p.max_len_s:
            self._active = None
            if t - a["t0"] >= p.min_len_s:
                self._last_end = t
                return AudioPeak(a["t0"], a["tp"], t, a["s"], a["lz"], a["rise"])
        return None


def run_tracker(rows: list[dict], p: AudioParams | None = None) -> tuple[list[dict], list[AudioPeak]]:
    tr, zs, peaks = AudioTracker(p or AudioParams()), [], []
    for r in rows:
        z, pk = tr.push(r)
        zs.append(z)
        if pk:
            peaks.append(pk)
    return zs, peaks


def top_candidates(zs: list[dict], key: str = "score", min_sep_s: float = 30.0,
                   n: int | None = None) -> list[tuple[float, float]]:
    """Ranked (t, value) local maxima of a per-second score, at least ``min_sep_s`` apart
    (greedy non-maximum suppression). Threshold-free, for top-N-per-hour evaluation."""
    order = sorted(((z[key], z["t"]) for z in zs if z[key] > 0), reverse=True)
    picked: list[tuple[float, float]] = []
    for v, t in order:
        if all(abs(t - pt) >= min_sep_s for pt, _ in picked):
            picked.append((t, v))
            if n is not None and len(picked) >= n:
                break
    return picked


# ---------------------------------------------------------------------------- live path
class SegmentDecoder:
    """One persistent ffmpeg per stream. ``feed(segment_bytes)`` as the buffer gets a
    segment; PCM comes back on a reader thread into ``on_pcm``. MPEG-TS segments of
    one HLS rendition can be concatenated byte-wise, so ffmpeg sees a continuous
    stream; after a gap or encoder restart, ``restart()`` and re-anchor the clock."""

    def __init__(self, on_pcm) -> None:
        self.on_pcm = on_pcm
        self._start()

    def _start(self) -> None:
        self.proc = subprocess.Popen(ffmpeg_cmd("pipe:0", "mpegts"), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self._th = threading.Thread(target=self._read, daemon=True)
        self._th.start()

    def _read(self) -> None:
        out, rest = self.proc.stdout, b""
        while True:
            b = out.read(SR // 5 * 4)               # up to 200 ms of float32
            if not b:
                return
            b, n = rest + b, (len(rest) + len(b)) // 4 * 4
            b, rest = b[:n], b[n:]
            if b:
                self.on_pcm(np.frombuffer(b, dtype=np.float32))

    def feed(self, data: bytes) -> None:
        self.proc.stdin.write(data)

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        self.proc.wait(timeout=10)
        self._th.join(timeout=10)

    def restart(self) -> None:
        self.close()
        self._start()
