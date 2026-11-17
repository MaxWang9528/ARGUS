"""Evaluate the video signal against the labelled test set, and against chat.

    python -I evaluate.py DATASET_DIR WORK_DIR [--app-python PY] [--config CFG]

The labels are a *sample* of 30 s windows (proposed by chat, loudness and at
random), each judged moment / maybe / no. So the main question is ranking: does
a window's video score separate moments from the rest? For every signal and
window we take the signal's max inside the window and turn it into a percentile
among all 30 s windows of that stream (so streams are comparable), then report
ROC AUC (moment vs no; maybe left out, and moment+maybe vs no). Chance is
measured by circularly shifting each stream's signal (200 shifts).

Top-N per hour picks are scored only where they land in labelled windows; most
land in unlabelled time, so those numbers are thin.

WORK_DIR caches features and chat replays; keep it outside the dataset.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import extract as X   # noqa: E402
import facefind as FF  # noqa: E402
import score as S     # noqa: E402
from labels import load_labels  # noqa: E402

WIN = 30
CHAT_LAG = (3, 10)     # chat about media time t arrives at t + 3..10 s
N_SHIFTS = 200

VARIANTS = {
    "motion": {"motion": 1},
    "face": {"face": 1},
    "flash": {"flash": 1},
    "cutrate": {"cutrate": 1},
    "combo": {"motion": 1, "face": 1, "flash": 0.5, "cutrate": 0.5},
    "kf_combo": {"motion": 1, "face": 1, "flash": 0.5},  # keyframes only, 0.5 fps
}


def video_features(stream_dir: Path, work: Path, duration: float, *, keyframes=False) -> dict:
    tag = f"{stream_dir.resolve().parent.name}_{stream_dir.name}"
    out = work / f"{tag}.{'kf' if keyframes else 'feat'}.npz"
    if out.exists():
        f = dict(np.load(out, allow_pickle=True))
    else:
        media = str(stream_dir / "media.mp4")
        probe = [f for _, f in zip(range(30), X.decode(media, 1 / 20, 854, 480))]
        region = FF.find_region(probe)  # facecam: Haar faces, one frame per 20 s
        fps = 0.5 if keyframes else 4.0
        rows = np.array(list(X.frame_features(
            X.decode(media, fps, 160, 90, keyframes=keyframes), region)), float).reshape(-1, 4)
        f = X.per_second(rows, fps)
        f["region"] = np.array(region if region else [np.nan] * 4)
        np.savez(out, **f)
    if abs(len(f["motion"]) - duration) > 4:
        raise SystemExit(f"{stream_dir}: decoded {len(f['motion'])} s, manifest says {duration:.0f} s")
    return f


def chat_replay(stream_dir: Path, work: Path, py: str, cfg: str) -> dict:
    out = work / f"{stream_dir.resolve().parent.name}_{stream_dir.name}.chat.json"
    if not out.exists():
        subprocess.run([py, "-I", str(HERE / "chat_peaks.py"), str(stream_dir / "chat.jsonl"),
                        str(out), "--config", cfg], check=True, stdout=subprocess.DEVNULL)
    return json.loads(out.read_text())


def chat_signal(cp: dict, wall_start: float, L: int) -> np.ndarray:
    """Chat z per media second, moved back by the chat lag (max over t+3..t+10)."""
    z = np.zeros(L + CHAT_LAG[1] + 1)
    for samples in cp["rate"].values():
        for ts, _, zz in samples:
            i = int(round(ts - wall_start))
            if 0 <= i < len(z):
                z[i] = max(z[i], zz)
    out = np.full(L, -np.inf)
    for d in range(CHAT_LAG[0], CHAT_LAG[1] + 1):
        out = np.maximum(out, z[d:d + L])
    return out


def window_max(sig: np.ndarray, w0: float, w1: float) -> float:
    a, b = max(0, int(w0)), min(len(sig), int(np.ceil(w1)) + 1)
    return float(np.max(sig[a:b])) if b > a else -np.inf


def all_window_max(sig: np.ndarray) -> np.ndarray:
    y = np.where(np.isfinite(sig), sig, -1e9)
    return np.array([y[i:i + WIN].max() for i in range(0, max(1, len(y) - WIN), 5)])


def pct(v: float, ref: np.ndarray) -> float:
    return float((ref < v).mean() + 0.5 * (ref == v).mean())


def auc(pos, neg) -> float:
    pos, neg = np.asarray(pos, float), np.asarray(neg, float)
    if not len(pos) or not len(neg):
        return float("nan")
    return float(((pos[:, None] > neg[None]).sum() + 0.5 * (pos[:, None] == neg[None]).sum())
                 / (len(pos) * len(neg)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("work")
    ap.add_argument("--app-python", default=os.path.expanduser("~/.venvs/livewatcher/bin/python"))
    ap.add_argument("--config", default=str(HERE.parents[2] / "config.toml"))
    a = ap.parse_args()
    ds, work = Path(a.dataset), Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    streams = load_labels(ds)
    rng = np.random.default_rng(0)

    rows = []           # one per labelled window: label, sources, chat_reacted, {signal: pct}
    shifted = {k: [] for k in ("combo", "face", "motion")}  # per shift, rows of pct
    picks = {}          # signal -> [moment, maybe, no, unlabelled]
    found = {}          # signal -> set of moment ids hit
    total_h = 0.0
    for name, info in sorted(streams.items()):
        sd, wins = info["dir"], info["windows"]
        f = video_features(sd, work, info["duration"])
        fk = video_features(sd, work, info["duration"], keyframes=True)
        z, zk = S.zscores(f), S.zscores(fk)
        L = len(f["motion"])
        total_h += L / 3600
        sig = {k: S.combined(zk if k.startswith("kf") else z, w)[:L] for k, w in VARIANTS.items()}
        cp = chat_replay(sd, work, a.app_python, a.config)
        sig["chat"] = chat_signal(cp, info["wall_start"], L)
        ref = {k: all_window_max(v) for k, v in sig.items()}
        reg = f["region"]
        print(f"{name}: {L / 60:.0f} min, {len(wins)} windows "
              f"({sum(w['label'] == 'moment' for w in wins)} moment), {len(cp['peaks'])} chat peaks, "
              f"facecam {'not found' if np.isnan(reg[0]) else np.round(reg, 2).tolist()}")
        for w in wins:
            p = {k: pct(window_max(v, w["w0"], w["w1"]), ref[k]) for k, v in sig.items()}
            p["fuse_mean"] = (p["chat"] + p["combo"]) / 2
            p["fuse_max"] = max(p["chat"], p["combo"])
            rows.append({**w, "stream": name, "p": p})
        for k in shifted:
            for j in range(N_SHIFTS):
                s2 = np.roll(sig[k], int(rng.integers(60, max(61, L - 60))))
                r2 = all_window_max(s2)
                shifted[k].append((j, [(w["label"], pct(window_max(s2, w["w0"], w["w1"]), r2))
                                       for w in wins]))
        # top-N/h picks (8 per hour) landing in labelled windows
        n = max(1, round(8 * L / 3600))
        chat_pts = [p["peak"] - 4 - info["wall_start"] for p in
                    sorted(cp["peaks"], key=lambda p: -p["rank"])][:n]
        pts = {k: [t for t, _ in S.pick(np.nan_to_num(sig[k], neginf=0), top=n)]
               for k in ("combo", "face", "motion")}
        pts["chat_peaks"] = chat_pts
        for k, ts in pts.items():
            c = picks.setdefault(k, [0, 0, 0, 0])
            fs = found.setdefault(k, set())
            for t in ts:
                inside = [w for w in wins if w["w0"] <= t <= w["w1"]]
                if not inside:
                    c[3] += 1
                    continue
                lab = max(inside, key=lambda w: ("no", "maybe", "moment").index(w["label"]))
                c[("moment", "maybe", "no").index(lab["label"])] += 1
                fs.update(w["id"] for w in inside if w["label"] == "moment")

    def split(key, maybe_pos):
        pos = [r["p"][key] for r in rows if r["label"] == "moment" or (maybe_pos and r["label"] == "maybe")]
        neg = [r["p"][key] for r in rows if r["label"] == "no"]
        return pos, neg

    nm = sum(r["label"] == "moment" for r in rows)
    nmb = sum(r["label"] == "maybe" for r in rows)
    nno = sum(r["label"] == "no" for r in rows)
    rnd = [r for r in rows if "random" in r["sources"]]
    print(f"\n{total_h * 60:.0f} min, {len(rows)} windows: {nm} moment, {nmb} maybe, {nno} no; "
          f"random windows: {sum(r['label'] == 'moment' for r in rnd)}/{len(rnd)} moment")
    print(f"{'signal':<11} AUC m-vs-no  AUC m+maybe-vs-no   mean pct(moment) mean pct(no)")
    for k in list(VARIANTS) + ["chat", "fuse_mean", "fuse_max"]:
        p1, n1 = split(k, False)
        p2, n2 = split(k, True)
        print(f"{k:<11} {auc(p1, n1):10.2f}  {auc(p2, n2):17.2f}   {np.mean(p1):14.2f} {np.mean(n1):12.2f}")
    for k, lst in shifted.items():
        by_j: dict = {}
        for j, items in lst:
            by_j.setdefault(j, []).extend(items)
        null = [auc([v for l, v in it if l != "no"], [v for l, v in it if l == "no"]) for it in by_j.values()]
        obs = auc(*split(k, True))
        print(f"chance AUC for {k} (m+maybe, circular shifts): mean {np.mean(null):.2f}, "
              f"95th pct {np.percentile(null, 95):.2f}; observed {obs:.2f}, p = {np.mean(np.array(null) >= obs):.3f}")

    # confound check: proposed windows (chat/loud) vs random ones, whatever the label
    def by_src(r):
        return "random-only" if r["sources"] == ["random"] else "proposed"
    print("\nby window origin (video combo / chat pct means; AUC m+maybe vs no within the group):")
    for g in ("proposed", "random-only"):
        sel = [r for r in rows if by_src(r) == g]
        for k in ("combo", "motion", "chat"):
            pos = [r["p"][k] for r in sel if r["label"] != "no"]
            neg = [r["p"][k] for r in sel if r["label"] == "no"]
            print(f"  {g:<12} n={len(sel):3d} ({len(pos)} pos) {k:<7} mean pct {np.mean([r['p'][k] for r in sel]):.2f}"
                  f"  AUC {auc(pos, neg):.2f}")
    for src in ("loud", "chat"):
        sel = [r for r in rows if any(s.startswith(src) for s in r["sources"])]
        pos = [r["p"]["combo"] for r in sel if r["label"] != "no"]
        neg = [r["p"]["combo"] for r in sel if r["label"] == "no"]
        print(f"  windows proposed by {src:<5} n={len(sel)}: combo AUC {auc(pos, neg):.2f}")
    # paired bootstrap: does fusion beat chat alone?
    for fk in ("fuse_max", "fuse_mean"):
        d = []
        for _ in range(2000):
            b = [rows[i] for i in rng.integers(0, len(rows), len(rows))]
            pos = [r for r in b if r["label"] != "no"]
            neg = [r for r in b if r["label"] == "no"]
            d.append(auc([r["p"][fk] for r in pos], [r["p"][fk] for r in neg])
                     - auc([r["p"]["chat"] for r in pos], [r["p"]["chat"] for r in neg]))
        print(f"{fk} - chat AUC (m+maybe), bootstrap: mean {np.nanmean(d):+.3f}, "
              f"95% CI [{np.nanpercentile(d, 2.5):+.3f}, {np.nanpercentile(d, 97.5):+.3f}]")

    print("\ntop-8/h picks: moment / maybe / no / unlabelled; moments hit")
    for k, c in picks.items():
        print(f"  {k:<11} {c[0]:3d} / {c[1]:3d} / {c[2]:3d} / {c[3]:3d}; {len(found[k])}/{nm}")

    hi = 0.8
    quiet = [r for r in rows if r["label"] in ("moment", "maybe") and r["chat_reacted"] in ("no", "weak")]
    print(f"\nmoment/maybe windows where chat reacted weakly or not: {len(quiet)}; "
          f"video combo pct >= {hi}: {sum(r['p']['combo'] >= hi for r in quiet)}, "
          f"chat pct >= {hi}: {sum(r['p']['chat'] >= hi for r in quiet)}")
    for r in quiet:
        print(f"    {r['stream']:<15} {r['id']} {r['label']:<6} video {r['p']['combo']:.2f} "
              f"face {r['p']['face']:.2f} chat {r['p']['chat']:.2f}")
    chatw = [r for r in rows if any(s.startswith("chat") for s in r["sources"])]
    for name, sel in (("kept", [r for r in chatw if r["p"]["combo"] >= 0.5]),
                      ("rejected", [r for r in chatw if r["p"]["combo"] < 0.5])):
        lab = [r["label"] for r in sel]
        print(f"chat-proposed windows video would {name} (combo pct {'>=' if name == 'kept' else '<'} 0.5): "
              f"{len(sel)}: {lab.count('moment')} moment, {lab.count('maybe')} maybe, {lab.count('no')} no")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
