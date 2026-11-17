"""Per-second audio scores -> causal "excitement" -> candidate moments.

Everything here is causal (only the past is used), so it can run live on the buffer:
- each event group (laugh, scream, gasp, cheer, bang) and the loudness get their own
  per-stream baseline: the trailing median of the last BASELINE_S seconds, spread = MAD
  scaled to a standard deviation with a floor. A stream full of gunfire or constant
  laughing tracks raises its own baseline instead of firing all the time;
- scores are compared on a log scale (YAMNet probabilities are tiny most of the time
  and saturate near 1), z is clipped to [0, Z_MAX];
- excitement = weighted sum of the group z values, then the mean over SMOOTH_S;
- candidates are runs above THRESH; one per run, at least SEP_S apart, scored by the
  run's maximum. No candidate before WARMUP_S of history.

The weights were fixed by hand before looking at any labels (vocal reactions count most,
game sounds least) and were not tuned on the evaluation streams.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

WEIGHTS = {"laugh": 1.0, "scream": 1.0, "gasp": 0.5, "cheer": 0.7, "bang": 0.3, "loud": 0.5}
BASELINE_S = 600
FLOOR = {"loud": 2.0}       # dB; others use LOG_FLOOR
LOG_FLOOR = 0.35            # natural-log units (~x1.4)
EPS = 0.01                  # log(p + EPS)
Z_MAX = 8.0
SMOOTH_S = 3
WARMUP_S = 60
THRESH = 3.0
SEP_S = 30


@dataclass
class Candidate:
    t_peak: float     # stream seconds (same base as the input rows)
    t_start: float
    t_end: float
    score: float
    top: str          # group with the largest weighted contribution at the peak


def _rolling_med_mad(x: np.ndarray, win: int, step: int = 5) -> tuple[np.ndarray, np.ndarray]:
    """Trailing median and MAD of x[t-win:t] (excluding t). Recomputed every `step` s."""
    n = len(x)
    med = np.full(n, np.nan)
    mad = np.full(n, np.nan)
    last = (np.nan, np.nan)
    for t in range(n):
        if t % step == 0 and t >= 10:
            w = x[max(0, t - win): t]
            w = w[~np.isnan(w)]
            if len(w) >= 10:
                m = np.median(w)
                last = (m, 1.4826 * np.median(np.abs(w - m)))
        med[t], mad[t] = last
    return med, mad


def group_z(groups: dict[str, np.ndarray], rms: np.ndarray) -> dict[str, np.ndarray]:
    out = {}
    for g in WEIGHTS:
        if g == "loud":
            x = rms.astype(float)
            floor = FLOOR["loud"]
        else:
            x = np.log(groups[g].astype(float) + EPS)
            floor = LOG_FLOOR
        med, mad = _rolling_med_mad(x, BASELINE_S)
        z = (x - med) / np.maximum(np.nan_to_num(mad, nan=floor), floor)
        out[g] = np.clip(np.nan_to_num(z, nan=0.0), 0, Z_MAX)
    return out


def excitement(groups: dict[str, np.ndarray], rms: np.ndarray, weights: dict[str, float] | None = None
               ) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    w = weights or WEIGHTS
    z = group_z(groups, rms)
    e = sum(w[g] * z[g] for g in w if w[g])
    k = np.ones(SMOOTH_S) / SMOOTH_S
    e = np.convolve(e, k)[: len(e)]          # trailing mean (causal)
    e[:WARMUP_S] = 0
    return e, z


def candidates(e: np.ndarray, z: dict[str, np.ndarray], t0: float = 0.0, thresh: float = THRESH,
               sep: int = SEP_S, weights: dict[str, float] | None = None) -> list[Candidate]:
    w = weights or WEIGHTS
    out: list[Candidate] = []
    n = len(e)
    t = 0
    while t < n:
        if e[t] <= thresh:
            t += 1
            continue
        a = t
        while t < n and e[t] > thresh:
            t += 1
        b = t
        p = a + int(np.argmax(e[a:b]))
        top = max(w, key=lambda g: w[g] * z[g][p])
        c = Candidate(t0 + p, t0 + a, t0 + b, float(e[p]), top)
        if out and c.t_peak - out[-1].t_peak < sep:
            if c.score > out[-1].score:
                out[-1] = c
        else:
            out.append(c)
    return out
