"""Peak shapes: for every peak a setting fires (dataset + unlabelled logs), how did
chat get there?

  python shapes.py <variant> [out_dir]     (variants from evaluate.py)

Per peak:
  pre/base  median rate over [trigger-45, trigger-15] s / the baseline the detector used.
            Near 1: chat jumped from its normal level. >> 1: chat had already climbed
            (a drift the baseline didn't follow).
  climb     seconds from the last bin at or below base + 25% of (peak - base) to the peak
  fall      rate 20 s after the peak's end, relative to base (1 = back to normal)
Writes peak_shapes_<variant>.png with the 40 peaks with the slowest climbs, and
prints the chat text of a few of them when PRINT_TEXT=1.
"""

from __future__ import annotations

import bisect
import os
import statistics
import sys
from pathlib import Path

import evaluate as ev
from common import load_chatlogs, run


def shape(p, samples):
    ts = [s.ts for s in samples]
    i_trig = bisect.bisect_left(ts, p.ts_start)
    i_pk = bisect.bisect_left(ts, p.ts_peak)
    trig = None
    # trigger = first sample after ts_start whose rate crossed the threshold
    for j in range(i_trig, min(len(samples), i_pk + 1)):
        if samples[j].rate >= samples[j].threshold:
            trig = j
            break
    trig = trig if trig is not None else i_pk
    base = samples[trig].baseline
    pre = [s.rate for s in samples[max(0, trig - 45):max(0, trig - 15)]]
    pre_m = statistics.median(pre) if pre else 0.0
    peak_rate = samples[i_pk].rate if i_pk < len(samples) else p.score * base
    lvl = base + 0.25 * (peak_rate - base)
    j = i_pk
    while j > max(0, i_pk - 300) and samples[j].rate > lvl:
        j -= 1
    i_end = bisect.bisect_left(ts, p.ts_end)
    after = samples[min(len(samples) - 1, i_end + 20)].rate
    return dict(pre_base=pre_m / max(base, 0.25), climb=i_pk - j, fall=after / max(base, 0.25),
                base=base, peak=peak_rate, trig=trig, i_pk=i_pk)


def main() -> int:
    name = sys.argv[1]
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    cls, over, _, _ = {**ev.V(), **ev.extra_variants()}[name]
    det = ev.det_with(**over)
    rows = []
    for s in load_chatlogs():
        peaks, samples = run(cls, s["msgs"], det, s["name"])
        mps = len(s["msgs"]) / max(1, s["t1"] - s["t0"])
        for p in peaks:
            f = shape(p, samples)
            f.update(stream=s["name"], p=p, samples=samples, msgs=s["msgs"], mps=mps)
            rows.append(f)
    print(f"{name}: {len(rows)} peaks on the logs")
    for k in ("pre_base", "climb", "fall"):
        v = sorted(r[k] for r in rows)
        q = lambda x: v[min(len(v) - 1, int(x * len(v)))]  # noqa: E731
        print(f"  {k:<9} p10 {q(.1):5.2f}  p50 {q(.5):5.2f}  p90 {q(.9):5.2f}")
    drift = [r for r in rows if r["pre_base"] >= 2 or r["climb"] >= 20]
    print(f"  drift-like (pre/base >= 2 or climb >= 20 s): {len(drift)} of {len(rows)}")
    if out:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        pick = sorted(rows, key=lambda r: -r["climb"])[:40]
        fig, axes = plt.subplots(8, 5, figsize=(16, 16))
        for ax, r in zip(axes.flat, pick):
            sm = r["samples"]
            a, b = max(0, r["i_pk"] - 180), min(len(sm), r["i_pk"] + 60)
            x = [k - r["i_pk"] for k in range(a, b)]
            ax.plot(x, [sm[k].rate for k in range(a, b)], lw=1)
            ax.plot(x, [sm[k].baseline for k in range(a, b)], lw=1, color="0.5", ls="--")
            ax.plot(x, [sm[k].threshold for k in range(a, b)], lw=0.7, color="C3", ls=":")
            ax.axvspan(r["p"].ts_start - r["p"].ts_peak, r["p"].ts_end - r["p"].ts_peak, color="C1", alpha=0.2)
            ax.set_title(f"climb {r['climb']}s pre/base {r['pre_base']:.1f} {r['mps']:.1f}/s", fontsize=7)
            ax.tick_params(labelsize=6)
        fig.tight_layout()
        fig.savefig(out / f"peak_shapes_{name}.png", dpi=70)
    if os.environ.get("PRINT_TEXT"):
        for r in sorted(rows, key=lambda r: -r["climb"])[:int(os.environ["PRINT_TEXT"])]:
            p = r["p"]
            txt = [m.text for m in r["msgs"] if p.ts_start - 5 <= m.ts <= p.ts_end][:25]
            print(f"\n--- climb {r['climb']} pre/base {r['pre_base']:.1f} {r['mps']:.1f} msg/s")
            print(" | ".join(t[:40] for t in txt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
