"""Step 1: what do labelled windows look like in the chat-rate curve?

  python -I characterise.py [out_dir]     (needs numpy; matplotlib for plots)

Per window ([centre-15, centre+20], chat lags) it measures, around the window's
highest 5 s rate:
  peak      highest 5 s rate (msgs/s)
  b180      rolling 180 s median rate before the peak (what the app's baseline sees)
  pre       median rate over [peak-60, peak-20] s (the "recent level")
  ratio180  peak / b180;  ratio_pre  peak / pre;  lag  pre / b180
  jump10    peak - min rate over the 10 s before it, divided by sqrt(pre) style spread
  climb     seconds the rate took to go from pre + 20% of the rise to the peak
  fwhm      seconds the rate stayed above half way between pre and peak
  decay     rate 15 s after the peak relative to the rise (0 = back to pre, 1 = plateau)
  new_share share of window messages from chatters not seen in the prior 10 min
  rep_share share of window messages whose normalised text is repeated by 3+ users
Prints a table per label and the AUC of each feature, moment vs no.
"""

from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

import numpy as np

from common import auc, counts_per_second, load_dataset, user_detection

SMOOTH = 5


def norm(text: str) -> str:
    t = re.sub(r"\s+", " ", text.lower()).strip()
    t = re.sub(r"(.)\1{2,}", r"\1\1", t)
    words = t.split(" ")
    # "LUL LUL LUL" -> "lul"
    if len(set(words)) == 1:
        t = words[0]
    return t


def features(stream: dict, det):
    base, counts, users, texts = counts_per_second(stream["msgs"], det, stream["t0"] - 300,
                                                   stream["t1"] + 60)
    c = np.array(counts, float)
    rate = np.convolve(c, np.ones(SMOOTH) / SMOOTH)[: len(c)]
    rate10 = np.convolve(c, np.ones(10) / 10)[: len(c)]
    b180 = np.array([np.median(rate[max(0, i - 180):i]) if i > 10 else 0 for i in range(len(c))])
    seen_at: dict[str, int] = {}
    first_seen = []  # per second: users first seen in 10 min
    for i, u in enumerate(users):
        fs = 0
        for k, n in u.items():
            if k not in seen_at or i - seen_at[k] > 600:
                fs += n
            seen_at[k] = i
        first_seen.append(fs)
    fsa = np.array(first_seen, float)

    def at(t: float) -> int:
        return int(np.floor(t)) - base

    def win(c0: float) -> dict:
        a, b = at(c0 - 15), at(c0 + 20)
        ip = a + int(np.argmax(rate[a:b]))
        peak = rate[ip]
        pre = float(np.median(rate[ip - 60: ip - 20]))
        pre_mean = float(rate[ip - 60: ip - 15].mean())
        pre_p75 = float(np.percentile(rate[ip - 60: ip - 15], 75))
        ip10 = a + int(np.argmax(rate10[a:b]))
        pk10 = rate10[ip10]
        pm10 = float(rate10[ip10 - 60: ip10 - 15].mean())
        lo = float(rate[ip - 10: ip + 1].min())
        rise = peak - pre
        thr20 = pre + 0.2 * rise
        j = ip
        while j > ip - 60 and rate[j] > thr20:
            j -= 1
        half = pre + 0.5 * rise
        k0 = ip
        while k0 > ip - 60 and rate[k0] > half:
            k0 -= 1
        k1 = ip
        while k1 < ip + 60 and rate[k1] > half:
            k1 += 1
        after = rate[min(ip + 15, len(rate) - 1)]
        msgs = [t for i in range(a, b) for t in texts[i]]
        n = max(1, len(msgs))
        by_text = collections.defaultdict(set)
        for k, t in msgs:
            by_text[norm(t)].add(k)
        rep = sum(1 for k, t in msgs if len(by_text[norm(t)]) >= 3) / n
        return dict(peak=peak, b180=b180[ip], pre=pre, ratio180=peak / max(b180[ip], 0.25),
                    ratio_pre=peak / max(pre, 0.25), lag=pre / max(b180[ip], 0.25),
                    jump10=(peak - lo) / np.sqrt(max(pre, 0.25)), climb=ip - j, fwhm=k1 - k0,
                    decay=(after - pre) / max(rise, 0.1), new_share=fsa[a:b].sum() / n,
                    rep_share=rep,
                    zp_pre=(peak - pre_mean) / np.sqrt(max(pre_mean, 0.2) / SMOOTH),
                    zp_p75=(peak - pre_p75) / np.sqrt(max(pre_p75, 0.2) / SMOOTH),
                    zp_b180=(peak - b180[ip]) / np.sqrt(max(b180[ip], 0.2) / SMOOTH),
                    zp10_pre=(pk10 - pm10) / np.sqrt(max(pm10, 0.2) / 10),
                    excess10=(pk10 - pm10) * 10, msgs=len(msgs), t_peak=ip + base)

    return win, rate, b180, base


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    det = user_detection()
    streams, labels = load_dataset()
    rows = []
    curves = {}
    for s in streams:
        win, rate, b180, base = features(s, det)
        curves[s["name"]] = (rate, b180, base)
        for lab in labels:
            if lab["stream"] == s["name"]:
                f = win(lab["center_wall"])
                f.update(label=lab["label"], busy_nothing=lab["label"] == "no" and lab["chat_reacted"] == "yes",
                         stream=s["name"], id=lab["id"], reason=lab["reason"][:60],
                         full=lab["pool"] == "full_K8")
                rows.append(f)
    keys = ["peak", "b180", "pre", "ratio180", "ratio_pre", "lag", "jump10", "climb", "fwhm",
            "decay", "new_share", "rep_share", "zp_pre", "zp_p75", "zp_b180", "zp10_pre", "excess10"]
    for group, sel in [("moment", lambda r: r["label"] == "moment"),
                       ("maybe", lambda r: r["label"] == "maybe"),
                       ("no + chat reacted", lambda r: r["busy_nothing"]),
                       ("no, chat quiet/weak", lambda r: r["label"] == "no" and not r["busy_nothing"])]:
        g = [r for r in rows if sel(r)]
        print(f"\n{group} (n={len(g)}) median [p25, p75]")
        for k in keys:
            v = np.array([r[k] for r in g], float)
            print(f"  {k:<10} {np.median(v):7.2f}  [{np.percentile(v, 25):6.2f}, {np.percentile(v, 75):6.2f}]")
    print("\nAUC moment vs no (all no) | moment vs busy-nothing")
    pos = [r for r in rows if r["label"] == "moment"]
    neg = [r for r in rows if r["label"] == "no"]
    busy = [r for r in rows if r["busy_nothing"]]
    for k in keys:
        print(f"  {k:<10} {auc([r[k] for r in pos], [r[k] for r in neg]):.2f}"
              f"  {auc([r[k] for r in pos], [r[k] for r in busy]):.2f}")
    if out:
        out.mkdir(parents=True, exist_ok=True)
        (out / "window_features.json").write_text(json.dumps(rows, default=float, indent=0))
        try:
            plot(out, rows, curves)
        except ImportError:
            pass
    return 0


def plot(out: Path, rows: list[dict], curves: dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    pick = [r for r in rows if r["label"] == "moment" or r["busy_nothing"]]
    n = len(pick)
    cols = 5
    fig, axes = plt.subplots((n + cols - 1) // cols, cols, figsize=(cols * 3.2, 2.2 * ((n + cols - 1) // cols)))
    for ax, r in zip(axes.flat, pick):
        rate, b180, base = curves[r["stream"]]
        i = int(r["t_peak"]) - base
        x = np.arange(-120, 60)
        ax.plot(x, rate[i - 120:i + 60], lw=1, color="C0" if r["label"] == "moment" else "C3")
        ax.plot(x, b180[i - 120:i + 60], lw=1, color="0.5", ls="--")
        ax.axvspan(-60, -20, color="0.9")
        ax.set_title(f"{r['label']} {r['id']}", fontsize=7)
        ax.tick_params(labelsize=6)
    for ax in list(axes.flat)[n:]:
        ax.axis("off")
    fig.tight_layout()
    fig.savefig(out / "window_curves.png", dpi=80)


if __name__ == "__main__":
    sys.exit(main())
