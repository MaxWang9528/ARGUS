"""Score detector variants on the labelled dataset and the unlabelled chat logs.

  python -I evaluate.py --selftest          TunedTracker defaults == app PeakTracker
  python -I evaluate.py [variant ...]       table of the named variants (see VARIANTS)
  python -I evaluate.py --loso              leave-one-stream-out choice of each variant's
                                            sensitivity knob, scored on the held-out stream

Hits: a peak's centre (ts_peak - 4 s) inside a label's window. Recall is per event
(moment windows of the same stream within 20 s are one event). "Busy, nothing" =
label no + chat_reacted yes. Precision = moment / (moment + no) over peaks that land
on a labelled window (maybe left out); peaks on no label are counted separately.
AUC: per window, the highest start margin in [centre-15, centre+20], moment vs no.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from functools import lru_cache

from common import CHAT_LAG_S, PeakTracker, auc, load_chatlogs, load_dataset, run, user_detection
from tracker import TunedTracker, variant

RANK = {"moment": 3, "maybe": 2, "no": 1}


def det_with(**kw):
    return user_detection().model_copy(update=kw)


# name -> (tracker class, detection overrides, sensitivity knob name, grid for that knob)
CUR = dict()  # the user's config.toml as is
REL = dict(multiplier=2.5, z_threshold=3.0, min_msgs_per_sec=0.3, min_unique_chatters=3,
           cooldown_s=45, min_sustain_s=6)


def V():
    T = TunedTracker
    return {
        "current": (T, CUR, None, None),
        "relaxed": (T, REL, None, None),
        # relaxed thresholds, sensitivity swept on the multiplier
        "relaxed_m": (T, REL, "multiplier", [2.5, 3, 3.5, 4, 5, 6]),
        # A: local pre-window baseline (max with the long one)
        "pre": (variant("pre", pre_far=45, pre_near=15), REL, "multiplier", [2.5, 3, 3.5, 4, 5, 6]),
        # B: Poisson-aware spread instead of absolute floors
        "pois": (variant("pois", poisson_k=1.0), {**REL, "min_msgs_per_sec": 0.6}, "z_threshold",
                 [3, 4, 5, 6, 7, 8]),
        # C: rise within N s
        "jump": (variant("jump", jump_s=10, jump_z=4), REL, "multiplier", [2.5, 3, 3.5, 4, 5, 6]),
        # A + B
        "pre_pois": (variant("pre_pois", pre_far=45, pre_near=15, poisson_k=1.0),
                     {**REL, "min_msgs_per_sec": 0.6, "multiplier": 2.5}, "z_threshold",
                     [3, 4, 5, 6, 7, 8]),
        # A + B + C
        "pre_pois_jump": (variant("ppj", pre_far=45, pre_near=15, poisson_k=1.0, jump_s=10, jump_z=4),
                          {**REL, "min_msgs_per_sec": 0.6, "multiplier": 2.5}, "z_threshold",
                          [3, 4, 5, 6, 7, 8]),
        # filters on top of pre_pois
        "pp_plateau": (variant("ppp", pre_far=45, pre_near=15, poisson_k=1.0, plateau_reject=True),
                       {**REL, "min_msgs_per_sec": 0.6, "multiplier": 2.5}, "z_threshold",
                       [3, 4, 5, 6, 7, 8]),
        "pp_new": (variant("ppn", pre_far=45, pre_near=15, poisson_k=1.0, new_share_min=0.15),
                   {**REL, "min_msgs_per_sec": 0.6, "multiplier": 2.5}, "z_threshold",
                   [3, 4, 5, 6, 7, 8]),
        "pp_spam": (variant("pps", pre_far=45, pre_near=15, poisson_k=1.0, spam_share_max=0.3),
                    {**REL, "min_msgs_per_sec": 0.6, "multiplier": 2.5}, "z_threshold",
                    [3, 4, 5, 6, 7, 8]),
    }


def extra_variants() -> dict:
    """More variants from JSON in $CT_VARIANTS: {name: [knobs, det_overrides, knob, grid]}."""
    p = os.environ.get("CT_VARIANTS")
    if not p:
        return {}
    out = {}
    for name, (knobs, over, knob, grid) in json.load(open(p)).items():
        out[name] = (variant(name, **knobs), over, knob, grid)
    return out


@lru_cache(maxsize=1)
def data():
    return load_dataset()


@lru_cache(maxsize=1)
def logs():
    return load_chatlogs()


def score_streams(cls, det, names=None) -> dict:
    streams, labels = data()
    res = dict(peaks=0, hours=0.0, hit_ev=set(), ev=set(), lab=dict(moment=0, maybe=0, no=0, none=0),
               busy_hit=0, busy=0, pos=[], neg=[], per_stream={})
    for s in streams:
        if names is not None and s["name"] not in names:
            continue
        tr_peaks, samples = run(cls, s["msgs"], det, s["name"])
        # margins line up with samples
        tr = None
        inside = [p for p in tr_peaks if s["t0"] <= p.ts_peak <= s["t1"]]
        res["peaks"] += len(inside)
        res["hours"] += (s["t1"] - s["t0"]) / 3600
        res["per_stream"][s["name"]] = len(inside)
        labs = [lab for lab in labels if lab["stream"] == s["name"]]
        for lab in labs:
            if lab["label"] == "moment":
                res["ev"].add(lab["event"])
        hit_labs = set()
        for p in inside:
            c = p.ts_peak - CHAT_LAG_S
            on = [lab for lab in labs if lab["window_wall"][0] <= c <= lab["window_wall"][1]]
            if not on:
                res["lab"]["none"] += 1
                continue
            best = max(on, key=lambda lab: RANK[lab["label"]])
            res["lab"][best["label"]] += 1
            for lab in on:
                hit_labs.add(lab["id"])
                if lab["label"] == "moment":
                    res["hit_ev"].add(lab["event"])
        # margins per window
        ts = [x.ts for x in samples]
        margins = samples_margins.get(id(samples))
        for lab in labs:
            busy = lab["label"] == "no" and lab["chat_reacted"] == "yes"
            res["busy"] += busy
            res["busy_hit"] += busy and lab["id"] in hit_labs
            c = lab["center_wall"]
            m = [mg for t, mg in zip(ts, margins) if c - 15 <= t <= c + 20]
            mx = max(m) if m else 0.0
            if lab["label"] == "moment":
                res["pos"].append(mx)
            elif lab["label"] == "no":
                res["neg"].append(mx)
    return res


samples_margins: dict[int, list[float]] = {}
_orig_run = run


def run_m(cls, msgs, det, name="s"):
    """run() that keeps the tracker's margins next to its samples."""
    tr_holder = {}

    class C(cls):  # type: ignore[misc, valid-type]
        def __init__(self, n):
            super().__init__(n)
            tr_holder["t"] = self

    peaks, samples = _orig_run(C, msgs, det, name)
    samples_margins[id(samples)] = tr_holder["t"].margins
    return peaks, samples


run = run_m  # noqa: F811


def is_drift(p, samples) -> bool:
    """A slow climb: chat took 20 s or more to go from base + 25% of the rise to the
    peak (moments on the labelled set take a median 7 s)."""
    import bisect
    ts = [x.ts for x in samples]
    i_pk = min(len(samples) - 1, bisect.bisect_left(ts, p.ts_peak))
    i0 = bisect.bisect_left(ts, p.ts_start)
    base = samples[i0].baseline
    lvl = base + 0.25 * (samples[i_pk].rate - base)
    j = i_pk
    while j > max(0, i_pk - 300) and samples[j].rate > lvl:
        j -= 1
    return i_pk - j >= 20


def score_logs(cls, det) -> dict:
    drift_n = [0, 0]
    by_speed = {"busy": [], "slow": [], "dead": []}
    for s in logs():
        hours = (s["t1"] - s["t0"]) / 3600
        if hours < 0.25:
            continue
        mps = len(s["msgs"]) / (s["t1"] - s["t0"])
        peaks, samples = _orig_run(cls, s["msgs"], det, s["name"])
        drift_n[0] += sum(is_drift(p, samples) for p in peaks)
        drift_n[1] += len(peaks)
        kind = "busy" if mps >= 3 else "slow" if mps >= 0.4 else "dead"
        # evenness: share of 15-min blocks with at least one peak
        nb = max(1, int(hours * 4))
        blocks = {min(nb - 1, int((p.ts_peak - s["t0"]) / 900)) for p in peaks}
        by_speed[kind].append((len(peaks) / hours, len(blocks) / nb, hours))
    out = {}
    for k, v in by_speed.items():
        if v:
            h = sum(x[2] for x in v)
            out[k] = dict(n=len(v), per_h=sum(x[0] * x[2] for x in v) / h,
                          per_h_range=(min(x[0] for x in v), max(x[0] for x in v)),
                          blocks=statistics.mean(x[1] for x in v))
    out["drift"] = drift_n
    return out


def summary(name, res, lg=None) -> str:
    lab = res["lab"]
    prec = lab["moment"] / max(1, lab["moment"] + lab["no"])
    s = (f"{name:<22} {res['peaks']:3d} pk {res['peaks'] / res['hours']:5.1f}/h  "
         f"rec {len(res['hit_ev'])}/{len(res['ev'])}  on M/m/N/- {lab['moment']}/{lab['maybe']}/{lab['no']}/{lab['none']}"
         f"  P {prec:.2f}  busy-nothing hit {res['busy_hit']}/{res['busy']}"
         f"  AUC {auc(res['pos'], res['neg']):.2f}")
    if lg:
        d = lg.pop("drift")
        s += f"  | logs drift {d[0]}/{d[1]} " + " ".join(f"{k}:{v['per_h']:.1f}/h({v['per_h_range'][0]:.0f}-{v['per_h_range'][1]:.0f}) "
                                    f"cov{v['blocks']:.0%}" for k, v in lg.items())
    return s


def selftest() -> int:
    streams, _ = data()
    det = user_detection()
    for over in (CUR, REL):
        d = det.model_copy(update=over)
        for s in streams:
            a, _ = _orig_run(PeakTracker, s["msgs"], d, s["name"])
            b, _ = _orig_run(TunedTracker, s["msgs"], d, s["name"])
            assert [(p.ts_start, p.ts_end, p.score) for p in a] == [(p.ts_start, p.ts_end, p.score) for p in b], s["name"]
    print("selftest ok: TunedTracker defaults reproduce PeakTracker")
    return 0


def loso(names: list[str], variants: dict) -> None:
    """For each held-out stream, pick the knob value on the other four that maximises
    event recall + precision subject to 8-15 peaks/h (closest to 12/h if none fits);
    then score the held-out stream with it. Sums the held-out results."""
    streams, _ = data()
    snames = [s["name"] for s in streams]
    for name in names:
        cls, over, knob, grid = variants[name]
        if knob is None:
            continue
        cache = {}
        for g in grid:
            d = det_with(**{**over, knob: g})
            cache[g] = {sn: score_streams(cls, d, {sn}) for sn in snames}
        tot = dict(peaks=0, hours=0.0, hit=0, ev=0, M=0, N=0, busy_hit=0, busy=0, picks=[])
        for held in snames:
            def train_score(g):
                rs = [cache[g][sn] for sn in snames if sn != held]
                pk_ = sum(r["peaks"] for r in rs)
                h = sum(r["hours"] for r in rs)
                hit = sum(len(r["hit_ev"]) for r in rs)
                ev = sum(len(r["ev"]) for r in rs)
                m = sum(r["lab"]["moment"] for r in rs)
                n = sum(r["lab"]["no"] for r in rs)
                rate = pk_ / h
                ok = 8 <= rate <= 15
                return (ok, hit / max(1, ev) + m / max(1, m + n) if ok else -abs(rate - 12))
            g = max(grid, key=train_score)
            r = cache[g][held]
            tot["picks"].append(g)
            tot["peaks"] += r["peaks"]
            tot["hours"] += r["hours"]
            tot["hit"] += len(r["hit_ev"])
            tot["ev"] += len(r["ev"])
            tot["M"] += r["lab"]["moment"]
            tot["N"] += r["lab"]["no"]
            tot["busy_hit"] += r["busy_hit"]
            tot["busy"] += r["busy"]
        print(f"LOSO {name:<18} {knob}={tot['picks']}  {tot['peaks'] / tot['hours']:5.1f}/h  "
              f"rec {tot['hit']}/{tot['ev']}  P {tot['M'] / max(1, tot['M'] + tot['N']):.2f} "
              f"({tot['M']}/{tot['M'] + tot['N']})  busy-nothing hit {tot['busy_hit']}/{tot['busy']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--loso", action="store_true")
    ap.add_argument("--sweep", action="store_true", help="every grid value of each variant")
    ap.add_argument("--nologs", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    variants = {**V(), **extra_variants()}
    names = a.names or list(variants)
    if a.loso:
        loso(names, variants)
        return 0
    for name in names:
        cls, over, knob, grid = variants[name]
        values = grid if (a.sweep and knob) else [None]
        for g in values:
            d = det_with(**({**over, knob: g} if g is not None else over))
            res = score_streams(cls, d)
            lg = None if a.nologs else score_logs(cls, d)
            label = name if g is None else f"{name} {knob}={g}"
            print(summary(label, res, lg), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
