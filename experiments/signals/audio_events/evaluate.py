"""Audio events vs chat vs fusion, against the dataset's hand-labelled 30 s windows.

    python -I evaluate.py WORK_DIR LABELS.jsonl

WORK_DIR comes from prepare.py. Labels are candidate windows (proposed by chat, loudness
and random sources, then judged "moment" / "maybe" / "no"), so they are not an exhaustive
list of moments. Two views:

1. Window ranking (uses every label, no thresholds): each window gets a score from each
   signal; ROC AUC of "moment" vs "no" ("maybe" left out). Signals are made comparable
   across streams by turning them into per-stream percentiles over all 30 s windows of
   that stream (5 s stride).
     audio   max excitement in [w0, w1]
     chat    max chat z (argus rate samples) in [w0, w1 + 5]  (chat lags)
     fused   mean of the two percentiles;  fused_max = max of the two
2. Top-N per hour: each method picks its N*hours best moments (30 s apart). A pick hits a
   label when it lies inside the label's window. Reported: recall of "moment" windows,
   label precision = (moments + 0.5 maybe) / picks that landed on any labelled window,
   and how many picks landed on no labelled window at all (unknown, not wrong).
   Methods: the two discrete detectors (chat peaks with the app config and the relaxed
   one, pick time ts_peak - 4 s as in the dataset; audio candidates at their peak), the
   continuous audio/chat/fused scores, and chat_relaxed peaks with an audio veto.
All parameters were fixed before this script was first run on labels.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import excitement as X  # noqa: E402
import scorer as S  # noqa: E402

WIN_S = 30
STRIDE_S = 5
CHAT_LAG_S = 5
NMS_S = 30.0
PER_HOUR = (6, 12, 24)
VETO_PCT = 0.5   # chat peaks whose surrounding audio is below the stream's median window are dropped


def auc(pos: list[float], neg: list[float]) -> float:
    if not pos or not neg:
        return float("nan")
    p, n = np.array(pos)[:, None], np.array(neg)[None, :]
    return float(((p > n) + 0.5 * (p == n)).mean())


def win_max(t: np.ndarray, x: np.ndarray, a: float, b: float) -> float:
    m = (t >= a) & (t <= b)
    return float(x[m].max()) if m.any() else 0.0


def nms(cands: list[tuple[float, float]], k: int) -> list[float]:
    out: list[float] = []
    for _, t in sorted(cands, reverse=True):
        if all(abs(t - u) >= NMS_S for u in out):
            out.append(t)
            if len(out) >= k:
                break
    return out


class Stream:
    def __init__(self, work: Path, name: str):
        d = np.load(work / f"{name}.audio.npz")
        self.t = d["t"] + 0.5                      # row k covers [k, k+1)
        g = S.group_matrix(d["scores"], d["keep"])
        self.e, self.z = X.excitement(g, d["rms"])
        # ablations (same pipeline, other weights): YAMNet events only, loudness only
        self.e_ev, _ = X.excitement(g, d["rms"], {**X.WEIGHTS, "loud": 0.0})
        self.e_loud, _ = X.excitement(g, d["rms"], {k: (1.0 if k == "loud" else 0.0) for k in X.WEIGHTS})
        self.cands = X.candidates(self.e, self.z)
        self.chat = json.loads((work / f"{name}.chat.json").read_text())
        self.chat_rel = json.loads((work / f"{name}.chat_relaxed.json").read_text())
        r = np.array(self.chat_rel["rate"].get(name, [[0, 0, 0]]))
        self.ct, self.cz = r[:, 0], r[:, 2]
        self.t0, self.t1 = float(self.t[0]), float(self.t[-1])
        self.hours = (self.t1 - self.t0) / 3600
        # reference distributions for percentiles: all 30 s windows of the stream
        starts = np.arange(self.t0 + X.WARMUP_S, self.t1 - WIN_S, STRIDE_S)
        self.ref_a = np.sort([self.a_score(s, s + WIN_S) for s in starts])
        self.ref_c = np.sort([self.c_score(s, s + WIN_S) for s in starts])
        self.grid = starts + WIN_S / 2

    def a_score(self, a: float, b: float) -> float:
        return win_max(self.t, self.e, a, b)

    def c_score(self, a: float, b: float) -> float:
        return win_max(self.ct, self.cz, a, b + CHAT_LAG_S)

    def pct(self, ref: np.ndarray, v: float) -> float:
        return float(np.searchsorted(ref, v, side="right") / max(len(ref), 1))

    def scores(self, a: float, b: float) -> dict[str, float]:
        pa, pc = self.pct(self.ref_a, self.a_score(a, b)), self.pct(self.ref_c, self.c_score(a, b))
        return {"audio": pa, "chat": pc, "fused": (pa + pc) / 2, "fused_max": max(pa, pc),
                "audio:events only": win_max(self.t, self.e_ev, a, b),
                "audio:loudness only": win_max(self.t, self.e_loud, a, b)}

    def methods(self) -> dict[str, list[tuple[float, float]]]:
        m: dict[str, list[tuple[float, float]]] = {}
        m["det:chat(app cfg)"] = [(p["rank"], p["ts_peak"] - 4) for p in self.chat["peaks"]]
        m["det:chat(relaxed)"] = [(p["rank"], p["ts_peak"] - 4) for p in self.chat_rel["peaks"]]
        m["det:audio"] = [(c.score, c.t_peak + float(self.t[0])) for c in self.cands]
        veto = []
        for p in self.chat_rel["peaks"]:
            c = p["ts_peak"] - 4
            if self.pct(self.ref_a, self.a_score(c - 15, c + 15)) >= VETO_PCT:
                veto.append((p["rank"], c))
        m["det:chat(relaxed)+audio veto"] = veto
        for key in ("audio", "chat", "fused", "fused_max"):
            m["cont:" + key] = [(self.scores(c - WIN_S / 2, c + WIN_S / 2)[key], float(c)) for c in self.grid]
        return m


def main() -> None:
    work, labels_path = Path(sys.argv[1]), Path(sys.argv[2])
    labels = [json.loads(l) for l in labels_path.read_text().splitlines() if l.strip()]
    names = sorted({l["stream"] for l in labels if (work / f"{l['stream']}.audio.npz").exists()})
    streams = {n: Stream(work, n) for n in names}
    hours = sum(s.hours for s in streams.values())
    lab = [l for l in labels if l["stream"] in streams and "incidental" not in l["sources"]]
    cnt = {v: sum(l["label"] == v for l in lab) for v in ("moment", "maybe", "no")}
    print(f"{len(streams)} streams, {hours * 60:.0f} min, labels {cnt} (incidental left out)")

    # 1. window ranking
    sc = {l["id"]: streams[l["stream"]].scores(*l["window_wall"]) for l in lab}
    print("\nwindow ROC AUC, moment vs no (0.5 = chance); columns: all labels | 'no' from random only |"
          " leaving out windows proposed by loudness alone")
    for key in ("audio", "audio:events only", "audio:loudness only", "chat", "fused", "fused_max"):
        pos = [sc[l["id"]][key] for l in lab if l["label"] == "moment"]
        neg = [sc[l["id"]][key] for l in lab if l["label"] == "no"]
        neg_r = [sc[l["id"]][key] for l in lab if l["label"] == "no" and "random" in l["sources"]]
        nl = [l for l in lab if l["sources"] != ["loud"]]
        pos_nl = [sc[l["id"]][key] for l in nl if l["label"] == "moment"]
        neg_nl = [sc[l["id"]][key] for l in nl if l["label"] == "no"]
        print(f"  {key:<20} {auc(pos, neg):.2f} | {auc(pos, neg_r):.2f} | {auc(pos_nl, neg_nl):.2f}"
              f"   (n={len(pos)}/{len(neg)})")

    # 2. top-N per hour
    print(f"\n{'N/h':>4} {'method':<30} {'picks':>5} {'on label':>8} {'label prec':>10} {'moment recall':>13}")
    allm = {n: s.methods() for n, s in streams.items()}
    rng = np.random.default_rng(0)
    for per in PER_HOUR:
        rows = {}
        for n, s in streams.items():
            L = [l for l in lab if l["stream"] == n]
            k = max(1, round(per * s.hours))
            picks = {m: nms(c, k) for m, c in allm[n].items()}
            picks["random"] = list(rng.uniform(s.t0 + X.WARMUP_S, s.t1, k))
            for m, ts in picks.items():
                r = rows.setdefault(m, [0, 0, 0.0, set()])
                r[0] += len(ts)
                for t in ts:
                    on = [l for l in L if l["window_wall"][0] <= t <= l["window_wall"][1]]
                    if on:
                        r[1] += 1
                        best = max(on, key=lambda l: {"moment": 2, "maybe": 1, "no": 0}[l["label"]])
                        r[2] += {"moment": 1.0, "maybe": 0.5, "no": 0.0}[best["label"]]
                        r[3].update(l["id"] for l in on if l["label"] == "moment")
        for m, (n_, on, good, found) in rows.items():
            print(f"{per:>4} {m:<30} {n_:>5} {on:>8} {good / max(on, 1):>10.2f} "
                  f"{len(found) / max(cnt['moment'], 1):>13.2f}")
        print()

    # 3. what audio adds / removes
    def audio_fires(l: dict) -> bool:
        s = streams[l["stream"]]
        return any(l["window_wall"][0] <= c.t_peak + float(s.t[0]) <= l["window_wall"][1] for c in s.cands)

    val = {"moment": 1.0, "maybe": 0.5, "no": 0.0}
    full = [l for l in lab if l.get("pool", "full_K8") == "full_K8"]
    print(f"per-source precision, pool full_K8 only ({len(full)} windows; maybe = 0.5):")
    for src in ("chat_peak", "chat_peak_relaxed", "chat_rate", "loud", "random"):
        L = [l for l in full if src in l["sources"]]
        if L:
            print(f"  {src:<18} n={len(L):>3}  precision {sum(val[l['label']] for l in L) / len(L):.2f}  "
                  f"moments {sum(l['label'] == 'moment' for l in L)}")
    for name, L in (("audio candidate inside", [l for l in full if audio_fires(l)]),
                    ("no audio candidate", [l for l in full if not audio_fires(l)]),
                    ("audio pct >= 0.8", [l for l in full if sc[l["id"]]["audio"] >= 0.8]),
                    ("audio pct < 0.5", [l for l in full if sc[l["id"]]["audio"] < 0.5])):
        if L:
            print(f"  {name:<22} n={len(L):>3}  precision {sum(val[l['label']] for l in L) / len(L):.2f}  "
                  f"moments {sum(l['label'] == 'moment' for l in L)}")
    print("\nmoments chat missed (chat_reacted no/weak), any pool; det:audio fires? + percentiles:")
    for l in lab:
        if l["label"] in ("moment", "maybe") and l["chat_reacted"] in ("no", "weak"):
            s = sc[l["id"]]
            print(f"  {l['label']:<6} chat={l['chat_reacted']:<4} {'+'.join(l['sources']):<28} "
                  f"audio_fires={'Y' if audio_fires(l) else 'n'} audio {s['audio']:.2f} chat {s['chat']:.2f}  "
                  f"{l['reason'][:55]}")
    print("relaxed chat peaks that landed on a label, with the audio percentile around them:")
    for n, s in streams.items():
        for p in s.chat_rel["peaks"]:
            c = p["ts_peak"] - 4
            on = [l for l in lab if l["stream"] == n and l["window_wall"][0] <= c <= l["window_wall"][1]]
            if on:
                pa = s.pct(s.ref_a, s.a_score(c - 15, c + 15))
                print(f"  {n:<15} {on[0]['label']:<7} audio {pa:.2f} {'VETO' if pa < VETO_PCT else 'keep'}  "
                      f"{on[0]['reason'][:60]}")
    print("\naudio group z (mean) in moment vs no windows:")
    for g in X.WEIGHTS:
        vals = {v: [] for v in ("moment", "no")}
        for l in lab:
            if l["label"] in vals:
                s = streams[l["stream"]]
                m = (s.t >= l["window_wall"][0]) & (s.t <= l["window_wall"][1])
                vals[l["label"]].append(float(s.z[g][m].max()) if m.any() else 0.0)
        print(f"  {g:<7} moment {np.mean(vals['moment']):.2f}  no {np.mean(vals['no']):.2f}  "
              f"AUC {auc(vals['moment'], vals['no']):.2f}")


if __name__ == "__main__":
    main()
