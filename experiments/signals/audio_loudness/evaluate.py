"""Evaluate the audio signal against the labelled dataset, next to chat, and fusions.

NOTE: the first half (``audio_series`` etc.) is shared; the dataset turned out to have
hand-labelled 30 s *windows* (moment / maybe / no) rather than clip spans, so the
scoring is window based - see ``evaluate_windows`` below.

    python -I evaluate.py <dataset_dir> <cache_dir> [--chat-json-dir DIR] [--out results.json]

Per stream it needs (see the dataset's README/manifest): the joined media (audio is
decoded from it with the same filter chain the live path uses), ``timeline.json``
(media time -> local wall clock, the clock chat and labels use), the labels and the
chat replay output of ``chat_replay.py`` (``<stream>.chat.json`` in --chat-json-dir).

Hit rule, the same for every detector: a candidate's moment time lies inside a label's
[start - TOL, end + TOL]. Chat candidates are moved back by the measured chat lag
first. Each label counts once (recall); each candidate is a hit or not (precision).
Budgets are N candidates per hour of stream, picked threshold-free by ranked
non-maximum suppression (at least MIN_SEP_S apart), plus each detector's own
thresholded events. Chance = uniformly random candidates with the same budget.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audio_signal as A  # noqa: E402

TOL = 5.0
MIN_SEP_S = 30.0
BUDGETS = (2, 4, 6, 10, 20)
GAP_MUTE_S = 5.0          # no audio candidates this soon after a buffer gap
FEATURES = ("score", "z_level", "z_rise", "z_flux", "z_voice")


# ------------------------------------------------------------------------- loading
def media_to_wall(tl: dict):
    segs = tl["segments"]
    ms = np.array([s["media_start"] for s in segs])
    ws = np.array([s["wall_start"] for s in segs])

    def f(t: float) -> float:
        i = max(0, int(np.searchsorted(ms, t, side="right")) - 1)
        return float(ws[i] + (t - ms[i]))
    return f


def audio_series(media: Path, tl: dict, cache: Path, p: A.AudioParams) -> list[dict]:
    """Per-second features + z rows on the wall clock (cached per stream)."""
    cache.mkdir(parents=True, exist_ok=True)
    cf = cache / f"{media.parent.name}.features.json"
    if cf.exists() and cf.stat().st_mtime > media.stat().st_mtime:
        rows = json.loads(cf.read_text())
    else:
        rows = A.features(A.decode(str(media)))
        cf.write_text(json.dumps(rows))
    to_wall = media_to_wall(tl)
    gap_media = [g["after_media_t"] for g in tl.get("gaps", [])]
    tr, out = A.AudioTracker(p), []
    gi = 0
    for r in rows:
        if gi < len(gap_media) and r["t"] > gap_media[gi]:
            tr = A.AudioTracker(p)        # a hole in the buffer: start the baseline again
            gi += 1                        # (the live path restarts ffmpeg there too)
        z, _ = tr.push(r)
        z = dict(z, **{k: r[k] for k in ("mom", "short", "voice", "flux")})
        z["media_t"], z["t"] = r["t"], to_wall(r["t"] - 0.5) + 0.5
        if any(0 <= r["t"] - g < GAP_MUTE_S for g in gap_media):
            for k in FEATURES:
                z[k] = 0.0
        out.append(z)
    return out


def audio_events(rows: list[dict], p: A.AudioParams) -> list[tuple[float, float]]:
    """The tracker's own thresholded peaks (re-run on the wall-clock rows)."""
    tr, ev = A.AudioTracker(p), []
    for r in rows:
        _, pk = tr.push(r)
        if pk:
            ev.append((r["t"] - (pk.t_end - pk.t_peak), pk.score))
    return ev


# ------------------------------------------------------------------------- metrics
def nms(ts: np.ndarray, v: np.ndarray, n: int, sep: float = MIN_SEP_S) -> list[tuple[float, float]]:
    order = np.argsort(-v)
    picked: list[tuple[float, float]] = []
    for i in order:
        if v[i] <= 0 or not np.isfinite(v[i]):
            break
        if all(abs(ts[i] - t) >= sep for t, _ in picked):
            picked.append((float(ts[i]), float(v[i])))
            if len(picked) >= n:
                break
    return picked


def match(cands: list[tuple[float, float]], labels: list[tuple[float, float]], tol: float = TOL):
    hit_c = [any(a - tol <= t <= b + tol for a, b in labels) for t, _ in cands]
    hit_l = [any(a - tol <= t <= b + tol for t, _ in cands) for a, b in labels]
    return hit_c, hit_l


def chance(span: tuple[float, float], n: int, labels, rng, reps: int = 300) -> tuple[float, float]:
    ps, rs = [], []
    for _ in range(reps):
        ts = np.sort(rng.uniform(span[0], span[1], n * 3))
        c = nms(ts, rng.random(len(ts)) + 0.1, n)
        hc, hl = match(c, labels)
        ps.append(np.mean(hc) if hc else 0)
        rs.append(np.mean(hl) if hl else 0)
    return float(np.mean(ps)), float(np.mean(rs))


def lag_xcorr(t_a: np.ndarray, a: np.ndarray, t_c: np.ndarray, c: np.ndarray, max_lag: int = 20):
    """Lag (s) at which chat z best follows the audio score (positive = chat later)."""
    grid = np.arange(max(t_a[0], t_c[0]), min(t_a[-1], t_c[-1]), 1.0)
    if len(grid) < 120:
        return None, None
    ai = np.interp(grid, t_a, a)
    ci = np.interp(grid, t_c, c)
    ai, ci = (ai - ai.mean()) / (ai.std() + 1e-9), (ci - ci.mean()) / (ci.std() + 1e-9)
    best = max(((float(np.mean(ai[:len(ai) - L] * ci[L:])), L) for L in range(0, max_lag + 1)))
    return best[1], best[0]


# ------------------------------------------------------------------------- window labels
CHAT_SHIFT = (4.0, 8.0)   # chat about a window [a, b] is looked for in [a + 4, b + 8]


def auc(pos: list[float], neg: list[float]) -> float | None:
    """P(score of a random positive > a random negative), ties count half."""
    if not pos or not neg:
        return None
    return float(np.mean([(p > n) + 0.5 * (p == n) for p in pos for n in neg]))


def wmax(t: np.ndarray, v: np.ndarray, a: float, b: float) -> float:
    i0, i1 = np.searchsorted(t, a), np.searchsorted(t, b, side="right")
    return float(v[i0:i1].max()) if i1 > i0 else 0.0


def load_window_streams(root: Path, chat_dir: Path, labels_file: str | None = None) -> list[dict]:
    man = json.loads((root / "manifest.json").read_text())
    lab = [json.loads(x) for x in (root / (labels_file or man["labels"])).read_text().splitlines() if x.strip()]
    out = []
    for s in man["streams"]:
        n = s["name"]
        d = root / "streams" / n
        if not (d / "media.mp4").exists():
            continue
        tl = json.loads((d / "timeline.json").read_text())
        cj = {k: json.loads((chat_dir / f"{n}{k}.chat.json").read_text())
              for k in ("", ".relaxed") if (chat_dir / f"{n}{k}.chat.json").exists()}
        out.append({"name": n, "media": d / "media.mp4", "tl": tl, "chat": cj,
                    "labels": [x for x in lab if x["stream"] == n],
                    "span": (tl["wall_start"], tl["wall_start"] + tl["segments_total_dur_s"])})
    return out


def evaluate_windows(streams: list[dict], cache: Path, p: A.AudioParams) -> dict:
    W: list[dict] = []          # one row per labelled window, all detector scores
    fired: dict = {}            # detector -> budget -> list of (window id, fired)
    for s in streams:
        rows = audio_series(s["media"], s["tl"], cache, p)
        t = np.array([r["t"] for r in rows])
        au = np.array([r["score"] for r in rows])
        span, hours = s["span"], (s["span"][1] - s["span"][0]) / 3600
        cs = np.array(s["chat"][""]["samples"])
        ct, cz = cs[:, 0], np.maximum(np.nan_to_num(cs[:, 3]), 0)
        # chat z on the event clock (moved back by the mid chat lag), audio grid
        lag = sum(CHAT_SHIFT) / 2
        cz_e = np.interp(t + lag, ct, cz, left=0, right=0)
        au_w = np.array([au[max(0, i - 4):i + 5].max() for i in range(len(au))])  # +-4 s slack
        series = {"audio": au, "chat": cz_e, "fuse_sum": cz_e / 2 + au_w,
                  "fuse_boost": cz_e * (1 + au_w / 4)}
        for k in ("z_level", "z_rise", "z_flux", "z_voice"):
            series["audio_" + k] = np.maximum([r[k] for r in rows], 0)
        cand: dict = {}
        for n_h in BUDGETS:
            n = max(1, round(n_h * hours))
            for k, v in series.items():
                cand.setdefault(k, {})[n_h] = [c[0] for c in nms(t, v, n)]
            h = max(1, n // 2)   # union: half the budget each
            ch = cand["chat"].get(n_h) and [c[0] for c in nms(t, cz_e, h)]
            ad = [c for c in cand["audio"][n_h] if all(abs(c - x) >= MIN_SEP_S for x in ch)]
            cand.setdefault("fuse_union", {})[n_h] = ch + ad[: n - len(ch)]
        ev = audio_events(rows, p)
        cand.setdefault("audio_events", {})["thr"] = [e[0] for e in ev]
        for key, name in (("", "chat_peaks"), (".relaxed", "chat_peaks_relaxed")):
            pk = [x["ts_peak"] - lag for x in s["chat"][key]["peaks"]]
            cand.setdefault(name, {})["thr"] = pk
            for G in (3.0, 5.5):
                cand.setdefault(f"{name}_gated_audio{G:g}", {})["thr"] = [
                    x for x in pk if wmax(t, au, x - 8, x + 4) >= G]
            cand.setdefault(f"{name}+audio_events", {})["thr"] = pk + [
                e[0] for e in ev if all(abs(e[0] - x) >= MIN_SEP_S for x in pk)]
        for lb in s["labels"]:
            a, b = lb["window_wall"]
            row = {"stream": s["name"], "id": lb["id"], "label": lb["label"], "sources": lb["sources"],
                   "chat_reacted": lb["chat_reacted"]}
            for k, v in series.items():
                row[k] = wmax(t, v, a, b)
            row["chat_raw"] = wmax(ct, cz, a + CHAT_SHIFT[0], b + CHAT_SHIFT[1])
            W.append(row)
            for k, d in cand.items():
                for nb, cs_ in d.items():
                    hit = any(a - TOL <= x <= b + TOL for x in cs_)
                    fired.setdefault(k, {}).setdefault(nb, []).append((lb["id"], hit))
        for k, d in cand.items():   # counts for the per-hour rate
            for nb, cs_ in d.items():
                fired.setdefault("_count", {}).setdefault((k, nb), 0)
                fired["_count"][(k, nb)] += len(cs_)
    hours = sum((s["span"][1] - s["span"][0]) / 3600 for s in streams)
    return {"windows": W, "fired": fired, "hours": hours}


def summarize(res: dict) -> dict:
    W, F = res["windows"], res["fired"]
    lab = {w["id"]: w for w in W}
    subsets = {
        "all": lambda w: w["sources"] != ["incidental"],
        "no loud source": lambda w: "loud" not in w["sources"] and w["sources"] != ["incidental"],
        "random only": lambda w: "random" in w["sources"],
    }
    out: dict = {"auc": {}, "fire": {}, "n": {}}
    keys = ["audio", "audio_z_level", "audio_z_rise", "audio_z_flux", "audio_z_voice", "chat", "chat_raw",
            "fuse_sum", "fuse_boost"]
    for sn, f in subsets.items():
        ws = [w for w in W if f(w)]
        out["n"][sn] = {l: sum(w["label"] == l for w in ws) for l in ("moment", "maybe", "no")}
        for k in keys:
            pos = [w[k] for w in ws if w["label"] == "moment"]
            pm = [w[k] for w in ws if w["label"] in ("moment", "maybe")]
            neg = [w[k] for w in ws if w["label"] == "no"]
            out["auc"].setdefault(sn, {})[k] = (auc(pos, neg), auc(pm, neg))
    for k, d in F.items():
        if k == "_count":
            continue
        for nb, lst in d.items():
            r = {}
            for l in ("moment", "maybe", "no"):
                xs = [h for i, h in lst if lab[i]["label"] == l and lab[i]["sources"] != ["incidental"]]
                r[l] = (sum(xs), len(xs))
            # chat-silent moments (chat_reacted no/weak) and chat-reacted "no" windows
            cm = [h for i, h in lst if lab[i]["label"] == "moment" and lab[i]["chat_reacted"] != "yes"]
            cn = [h for i, h in lst if lab[i]["label"] == "no" and lab[i]["chat_reacted"] == "yes"]
            r["moment_chat_quiet"] = (sum(cm), len(cm))
            r["no_chat_busy"] = (sum(cn), len(cn))
            r["per_hour"] = F["_count"][(k, nb)] / res["hours"]
            out["fire"].setdefault(k, {})[str(nb)] = r
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("cache")
    ap.add_argument("--chat-json-dir", required=True)
    ap.add_argument("--labels", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    streams = load_window_streams(Path(a.dataset), Path(a.chat_json_dir), a.labels)
    res = evaluate_windows(streams, Path(a.cache), A.AudioParams())
    sm = summarize(res)
    print(f"{res['hours']:.2f} h, windows {sm['n']}")
    print("AUC moment-vs-no / (moment+maybe)-vs-no, window max of each score")
    for sn, d in sm["auc"].items():
        print(f"  [{sn}]  " + "  ".join(f"{k} {v[0] if v[0] is None else round(v[0], 2)}/"
                                       f"{v[1] if v[1] is None else round(v[1], 2)}" for k, v in d.items()))
    print("fires inside window (+-5 s): moment / maybe / no / moment-chat-quiet / no-chat-busy, rate")
    for k, d in sm["fire"].items():
        for nb, r in d.items():
            f = lambda x: f"{x[0]}/{x[1]}"
            print(f"  {k:<34} {nb:>4}  M {f(r['moment']):>6}  ? {f(r['maybe']):>6}  N {f(r['no']):>6}"
                  f"  Mq {f(r['moment_chat_quiet']):>5}  Nb {f(r['no_chat_busy']):>5}  {r['per_hour']:5.1f}/h")
    if a.out:
        Path(a.out).write_text(json.dumps({"summary": sm, "windows": res["windows"]}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
