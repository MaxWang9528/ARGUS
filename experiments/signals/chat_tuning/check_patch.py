"""Check that a patched argus/detect/peak.py (the recommended change in
peak_recommended.diff) gives the same peaks as the experiment's TunedTracker variant
it was written from, and print its numbers.

  ARGUS_CODE=<dir holding the patched argus package> python check_patch.py

The variant runs against the unpatched repo in a subprocess (it subclasses the
original PeakTracker), the patched PeakTracker in this process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

KNOBS = dict(pre_far=45, pre_near=15, poisson_k=1.0, jump_s=10, jump_z=4)
SETTINGS = dict(multiplier=2.5, z_threshold=4.0, min_msgs_per_sec=0.6, min_unique_chatters=3,
                cooldown_s=45, min_sustain_s=6)


def keys(cls) -> dict[str, list]:
    import evaluate as ev
    from common import load_chatlogs, run
    det = ev.det_with(**SETTINGS)
    streams, _ = ev.data()
    out = {}
    for s in streams + load_chatlogs():
        peaks, _ = run(cls, s["msgs"], det, s["name"])
        out[s["name"]] = sorted([round(p.ts_start, 3), round(p.ts_end, 3)] for p in peaks)
    return out


def main() -> int:
    if sys.argv[1:] == ["--ref"]:
        from tracker import variant
        print(json.dumps(keys(variant("ref", **KNOBS))))
        return 0
    if not os.environ.get("ARGUS_CODE"):
        raise SystemExit("set ARGUS_CODE to the patched copy")
    env = {k: v for k, v in os.environ.items() if k != "ARGUS_CODE"}
    ref = json.loads(subprocess.run([sys.executable, __file__, "--ref"], env=env, check=True,
                                    capture_output=True, text=True).stdout)
    import evaluate as ev
    from common import PeakTracker
    got = keys(PeakTracker)
    same = sum(len({tuple(x) for x in got[k]} & {tuple(x) for x in ref[k]}) for k in got)
    diff = sum(len({tuple(x) for x in got[k]} ^ {tuple(x) for x in ref[k]}) for k in got)
    print(f"patched PeakTracker vs TunedTracker variant: {same} peaks identical, {diff} differ")
    det = ev.det_with(**SETTINGS)
    res = ev.score_streams(_with_margins(PeakTracker), det)
    print(ev.summary("patched peak.py", res, ev.score_logs(PeakTracker, det)))
    return 0


def _with_margins(cls):
    """Margins are a TunedTracker thing; give the plain tracker z / z_threshold so
    evaluate's AUC column still has a value (not comparable with the table's)."""
    class M(cls):
        def _close(self, idx, bin_s, det):
            s, p = super()._close(idx, bin_s, det)
            self.margins = getattr(self, "margins", [])
            self.margins.append(s.z / det.z_threshold)
            return s, p
    return M


if __name__ == "__main__":
    sys.exit(main())
