"""Evaluate the speech signal on the labelled dataset, next to the chat detector.

  python -I evaluate.py <dataset_dir> <chat.json> <transcript_dir> [--json out.json]

* ``<dataset_dir>``: manifest.json + labels jsonl. Each label is a 30 s window on the
  chat clock (``window_wall``), judged ``moment`` / ``maybe`` / ``no``, with the
  ``sources`` that proposed it (chat detectors, loudness, random).
* ``<chat.json>``: from ``chat_peaks.py`` (chat peaks + per-bin z of the chat rate).
* ``<transcript_dir>``: ``<streamer>.<model>.json`` from ``buffer_stream.py``.

Weights are the frozen defaults in ``speech.ScoreCfg`` (set before seeing labels).

Reports
1. Window ranking (AUC): can each signal tell judged moments from judged non-moments?
   speech = max score in the window; chat = max chat-rate z in the window shifted
   +CHAT_LAG_S (chat reacts after the event); fusion = mean of the two ranks.
2. Top-N per hour: detections; a detection inside a judged window counts as good
   (moment, or moment+maybe) or bad (no); unjudged detections are left out, so
   precision is over judged ones and coverage is reported. Recall = moments with a
   detection in their window.
3. Speech catches chat missed; chat-proposed windows speech would reject.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import speech  # noqa: E402

CHAT_LAG_S = 10       # chat-rate window extends this far past the label window
CONFIRM = 2.0         # speech score needed to confirm a chat peak (frozen)
CHAT_SOURCES = {"chat_peak", "chat_peak_relaxed", "chat_rate"}


def auc(pos: list[float], neg: list[float]) -> float:
    if not pos or not neg:
        return float("nan")
    w = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return w / (len(pos) * len(neg))


def load(dataset: str) -> tuple[dict, list[dict]]:
    man = json.load(open(os.path.join(dataset, "manifest.json")))
    labels = [json.loads(x) for x in open(os.path.join(dataset, man["labels"])) if x.strip()]
    return man, labels


class Track:
    """Per-second speech score of one stream on the chat clock."""

    def __init__(self, tr: dict, cfg: speech.ScoreCfg, part: str = "all") -> None:
        words = tr["words"]
        loud = {int(k): v for k, v in tr.get("loud", {}).items()}
        self.t0 = int(min([w["t"] for w in words] + list(loud)))
        n = int(max([w["t"] for w in words] + list(loud))) + 1 - self.t0
        self.words = [dict(w, t=w["t"] - self.t0) for w in words]
        la = [loud.get(self.t0 + i, float("nan")) for i in range(n)]
        r = speech.score(self.words, la, n, cfg)
        if part == "all":
            self.s = r["score"]
        elif part == "lexical":
            self.s = cfg.w_excl * (r["excl"] + 0.5 * r["rep"]) + cfg.w_swear * r["swear"] + cfg.w_laugh * r["laugh"]
        elif part == "wps":
            self.s = np.maximum(r["z_wps"], 0)
        elif part == "loud":
            self.s = np.maximum(r["z_loud"], 0)
        elif part == "text":   # everything that needs the transcript (no loudness)
            self.s = r["score"] - cfg.w_loud * np.maximum(r["z_loud"], 0)
        self.n = n

    def max_in(self, a: float, b: float) -> float:
        i, j = int(a) - self.t0, int(b) - self.t0 + 1
        seg = self.s[max(0, i):max(0, min(self.n, j))]
        return float(seg.max()) if len(seg) else 0.0


def chat_max(z: list, a: float, b: float) -> float:
    v = [r[1] for r in z if a <= r[0] <= b]
    return max(v) if v else 0.0


def inside(t: float, l: dict, pad: float = 0.0) -> bool:
    return l["window_wall"][0] - pad <= t <= l["window_wall"][1] + pad


def topn(dets: list[float], labels: list[dict], pos: set[str]) -> tuple[int, int, set[str]]:
    """(good, judged, moment ids caught) for detection times."""
    good = judged = 0
    caught: set[str] = set()
    for t in dets:
        ls = [l for l in labels if inside(t, l)]
        if not ls:
            continue
        judged += 1
        good += any(l["label"] in pos for l in ls)
        caught.update(l["id"] for l in ls if l["label"] == "moment")
    return good, judged, caught


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset"); ap.add_argument("chat"); ap.add_argument("trdir")
    ap.add_argument("--model", default="base")
    ap.add_argument("--per-hour", nargs="+", type=float, default=[8, 16, 30])
    ap.add_argument("--json", default=None)
    ap.add_argument("--v1", action="store_true", help="the first, pre-label scoring (wps vs all seconds)")
    a = ap.parse_args()
    man, labels = load(a.dataset)
    chat = json.load(open(a.chat))
    cfg = speech.ScoreCfg(wps_active=not a.v1)
    rng = random.Random(0)
    out: dict = {}

    streams = [s["name"] for s in man["streams"]]
    labels = [l for l in labels if l["stream"] in streams]
    tracks = {}
    for name in streams:
        cands = [os.path.join(a.trdir, f"{name}.{a.model}.json"),
                 os.path.join(a.trdir, f"{name}.offline.json")]
        p = next((c for c in cands if os.path.exists(c)), None)
        if p is None:
            print(f"no transcript for {name}, skipped"); continue
        tracks[name] = json.load(open(p))
    streams = [n for n in streams if n in tracks]
    labels = [l for l in labels if l["stream"] in tracks]
    hours = sum(s["media_duration_s"] for s in man["streams"] if s["name"] in tracks) / 3600
    print(f"{len(streams)} streams, {hours * 60:.0f} min, {len(labels)} judged windows: "
          f"{sum(l['label'] == 'moment' for l in labels)} moment, "
          f"{sum(l['label'] == 'maybe' for l in labels)} maybe, {sum(l['label'] == 'no' for l in labels)} no")

    # ---- 1. window ranking
    print("\n1. AUC, judged windows (moment vs no | moment+maybe vs no)")
    feats: dict[str, dict[str, float]] = {}
    for part in ("all", "text", "lexical", "wps", "loud"):
        for l in labels:
            tr = Track(tracks[l["stream"]], cfg, part) if (l["stream"], part) not in feats else None
            if tr is not None:
                feats[(l["stream"], part)] = tr  # cache per stream
        for l in labels:
            tr = feats[(l["stream"], part)]
            a_, b_ = l["window_wall"]
            feats.setdefault(l["id"], {})["speech_" + part] = tr.max_in(a_, b_)
    for l in labels:
        a_, b_ = l["window_wall"]
        feats[l["id"]]["chat"] = chat_max(chat["z"].get(l["stream"], []), a_, b_ + CHAT_LAG_S)
    ids = [l["id"] for l in labels]

    def ranks(key: str) -> dict[str, float]:
        v = sorted(ids, key=lambda i: feats[i][key])
        return {i: r / max(len(v) - 1, 1) for r, i in enumerate(v)}

    rc, rs = ranks("chat"), ranks("speech_all")
    for i in ids:
        feats[i]["fusion"] = (rc[i] + rs[i]) / 2
        feats[i]["fusion_max"] = max(rc[i], rs[i])
    keys = ["chat", "speech_all", "speech_text", "speech_lexical", "speech_wps", "speech_loud",
            "fusion", "fusion_max"]
    out["auc"] = {}
    for k in keys:
        mo = [feats[l["id"]][k] for l in labels if l["label"] == "moment"]
        mb = [feats[l["id"]][k] for l in labels if l["label"] == "maybe"]
        no = [feats[l["id"]][k] for l in labels if l["label"] == "no"]
        out["auc"][k] = (auc(mo, no), auc(mo + mb, no))
        print(f"  {k:15s} {out['auc'][k][0]:.2f} | {out['auc'][k][1]:.2f}")
    # only the windows the chat detectors did NOT propose (random / loud / incidental)
    nonchat = [l for l in labels if not set(l["sources"]) & CHAT_SOURCES]
    mo = [l for l in nonchat if l["label"] in ("moment", "maybe")]
    no = [l for l in nonchat if l["label"] == "no"]
    print(f"  windows not proposed by chat ({len(mo)} moment/maybe vs {len(no)} no): "
          f"chat {auc([feats[l['id']]['chat'] for l in mo], [feats[l['id']]['chat'] for l in no]):.2f}, "
          f"speech {auc([feats[l['id']]['speech_all'] for l in mo], [feats[l['id']]['speech_all'] for l in no]):.2f}")

    # ---- 2. top-N per hour, over each stream's labelled span only
    print("\n2. top-N per hour: precision over judged detections (moment | moment+maybe), "
          "recall of moments, judged/detections")
    out["topn"] = {}
    n_mom = sum(l["label"] == "moment" for l in labels)
    span = {s_["name"]: (s_["wall_start"] + 16, s_["wall_start"] + s_["media_duration_s"] - 16)
            for s_ in man["streams"]}
    for ph in a.per_hour:
        res: dict[str, list] = {}
        sp_c, ch_c = set(), set()
        for name in streams:
            T = feats[(name, "all")]
            L = [l for l in labels if l["stream"] == name]
            w0, w1 = span[name]
            i0, i1 = max(0, int(w0) - T.t0), min(T.n, int(w1) - T.t0)
            sec = np.arange(i0, i1) + T.t0
            ssp = T.s[i0:i1]
            # chat signal at t: max chat-rate z over [t, t + CHAT_LAG_S] (chat lags the event)
            zz = chat["z"].get(name, [])
            zt = np.array([r[0] for r in zz]); zv = np.array([r[1] for r in zz])
            sch = np.zeros(len(sec))
            for j, t in enumerate(sec):
                m = (zt >= t) & (zt <= t + CHAT_LAG_S)
                sch[j] = zv[m].max() if m.any() else 0.0
            def nrm(x):
                q = np.percentile(x, 95)
                return np.clip(x, 0, None) / (q if q > 0 else 1)
            fus = nrm(ssp) + nrm(sch)
            k = max(1, round(ph * len(sec) / 3600))
            pick = lambda x, kk=k: [int(sec[i]) for i, _ in speech.candidates(x, kk, min_gap_s=30)]
            sp, ch, fu = pick(ssp), pick(sch), pick(fus)
            # app detector peaks (relaxed thresholds), by rank, placed 4 s before ts_peak
            peaks = sorted((p for p in chat["peaks"] if p["streamer"] == name
                            and w0 <= p["ts_peak"] <= w1 + 10), key=lambda p: -p["rank"])
            det = [p["ts_peak"] - 4 for p in peaks[:k]]
            cf = [p["ts_peak"] - 4 for p in peaks
                  if T.max_in(p["ts_peak"] - 20, p["ts_peak"] + 5) >= CONFIRM][:k]
            un: list[float] = []
            for pair in zip(det + [None] * k, sp + [None] * k):
                for t in pair:
                    if t is not None and len(un) < k and all(abs(t - u) > 30 for u in un):
                        un.append(t)
            rnd = [w0 + rng.random() * (w1 - w0) for _ in range(k * 50)]
            for key, dets in (("random", rnd), ("chat_detector", det), ("chat_rate_z", ch),
                              ("speech", sp), ("fusion_sum", fu), ("det|speech", un),
                              ("det&speech", cf)):
                g1, j, c = topn(dets, L, {"moment"})
                g2, _, _ = topn(dets, L, {"moment", "maybe"})
                div = 50 if key == "random" else 1
                o = res.setdefault(key, [0, 0, 0, 0, set()])
                o[0] += g1 / div; o[1] += g2 / div; o[2] += j / div; o[3] += len(dets) / div
                o[4] |= c if key != "random" else set()
            sp_c |= topn(sp, L, {"moment"})[2]
            ch_c |= topn(ch, L, {"moment"})[2]
        print(f"  {ph:g}/hour")
        out["topn"][ph] = {}
        for key, (g1, g2, j, d, c) in res.items():
            rec = len(c) / max(n_mom, 1) if key != "random" else float("nan")
            print(f"    {key:13s} P {g1 / max(j, 1e-9):.2f} | {g2 / max(j, 1e-9):.2f}  "
                  f"R {rec:.2f} ({len(c)}/{n_mom})  judged {j:.1f}/{d:.0f}")
            out["topn"][ph][key] = [g1, g2, j, d, len(c)]
        extra = sorted(sp_c - ch_c)
        print(f"    moments speech's top-N caught that chat-rate's top-N missed: {len(extra)} {extra}")
        out["topn"][ph]["speech_only"] = extra

    # ---- 3. chat-proposed windows: confirm / reject by speech
    cw = [l for l in labels if set(l["sources"]) & CHAT_SOURCES]
    rej = [l for l in cw if feats[l["id"]]["speech_all"] < CONFIRM]
    kep = [l for l in cw if feats[l["id"]]["speech_all"] >= CONFIRM]

    def cnt(ls: list[dict]) -> str:
        return "/".join(str(sum(l["label"] == v for l in ls)) for v in ("moment", "maybe", "no"))
    print(f"\n3. chat-proposed windows (moment/maybe/no): speech >= {CONFIRM} keeps {cnt(kep)}, "
          f"rejects {cnt(rej)}")
    quiet = [l for l in labels if l["label"] == "moment" and l.get("chat_reacted") in ("no", "weak")]
    print(f"   moments chat barely reacted to: {len(quiet)}; speech score >= {CONFIRM} on "
          f"{sum(feats[l['id']]['speech_all'] >= CONFIRM for l in quiet)}")
    out["confirm"] = {"kept": cnt(kep), "rejected": cnt(rej), "rejected_ids": [l["id"] for l in rej],
                      "quiet_moments": [(l["id"], feats[l["id"]]["speech_all"]) for l in quiet]}
    out["feats"] = {i: feats[i] for i in ids}
    if a.json:
        json.dump(out, open(a.json, "w"), default=lambda o: sorted(o) if isinstance(o, set) else float(o), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
