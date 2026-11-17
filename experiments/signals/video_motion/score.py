"""Turn per-second video features into z-scores and candidate moments.

Baselines are causal (only the past is used), per stream, as in the chat
detector: for every second, the median and MAD (scaled to a standard deviation)
of ``log1p(feature)`` over the trailing ``BASE_WINDOW_S`` seconds, skipping the
last ``BASE_GAP_S`` so a moment does not raise its own baseline.
"""

from __future__ import annotations

import numpy as np

BASE_WINDOW_S = 300
BASE_GAP_S = 5
MIN_HISTORY_S = 60
MAD_TO_SD = 1.4826
STEP_S = 5          # the baseline is recomputed every STEP_S seconds (cheap, smooth enough)
CUT_WINDOW_S = 10   # cut rate = cuts in the last CUT_WINDOW_S seconds

CHANNELS = ("motion", "face", "flash", "cutrate")


def robust_z(x: np.ndarray, floor: float) -> np.ndarray:
    n = len(x)
    z = np.full(n, np.nan)
    med = sd = np.nan
    for t in range(n):
        if t % STEP_S == 0:
            a, b = max(0, t - BASE_WINDOW_S), t - BASE_GAP_S
            if b - a >= MIN_HISTORY_S:
                h = x[a:b]
                h = h[~np.isnan(h)]
                if h.size >= MIN_HISTORY_S // 2:
                    med = np.median(h)
                    sd = max(MAD_TO_SD * np.median(np.abs(h - med)), floor)
        if not np.isnan(med) and not np.isnan(x[t]):
            z[t] = (x[t] - med) / sd
    return z


def channels(f: dict) -> dict[str, np.ndarray]:
    """Per-second raw channels (already transformed) from extract.py output."""
    cuts = np.nan_to_num(f["cuts"])
    cutrate = np.convolve(cuts, np.ones(CUT_WINDOW_S), mode="full")[:len(cuts)]
    return {
        "motion": np.log1p(f["motion"]),
        "face": np.log1p(f["face"]),
        "flash": np.log1p(f["flash"]),
        "cutrate": cutrate,
    }


FLOORS = {"motion": 0.1, "face": 0.1, "flash": 0.1, "cutrate": 0.5}


def zscores(f: dict) -> dict[str, np.ndarray]:
    ch = channels(f)
    return {k: robust_z(v, FLOORS[k]) for k, v in ch.items()}


def roll_max(x: np.ndarray, w: int) -> np.ndarray:
    """Trailing+leading max over a centred window of w seconds (NaN -> -inf)."""
    y = np.where(np.isnan(x), -np.inf, x)
    h = w // 2
    out = np.full_like(y, -np.inf)
    for s in range(-h, h + 1):
        out = np.maximum(out, np.roll(y, s))
    return out


def combined(z: dict[str, np.ndarray], weights: dict[str, float]) -> np.ndarray:
    """Weighted sum of positive z-scores (each clipped at 8), smoothed with a 3 s max."""
    n = len(next(iter(z.values())))
    s = np.zeros(n)
    for k, w in weights.items():
        if w and k in z and not np.all(np.isnan(z[k])):
            s += w * np.clip(np.nan_to_num(z[k], nan=0.0), 0, 8)
    return roll_max(s, 3)


def pick(score: np.ndarray, *, min_gap_s: int = 45, thresh: float = 1e-6,
         top: int | None = None) -> list[tuple[int, float]]:
    """Greedy non-maximum suppression: (second, score), best first."""
    order = np.argsort(-score)
    taken: list[tuple[int, float]] = []
    for t in order:
        v = score[t]
        if not np.isfinite(v) or v < thresh:
            break
        if all(abs(t - u) >= min_gap_s for u, _ in taken):
            taken.append((int(t), float(v)))
            if top is not None and len(taken) >= top:
                break
    return taken
