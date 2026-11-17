"""Run the app's chat detector over a dataset stream's chat and write its peaks
as JSON (chat clock, i.e. local epoch seconds). Needs the app's own env:

    ~/.venvs/livewatcher/bin/python chat_peaks.py CHAT.jsonl OUT.json [--config config.toml]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("chat")
    ap.add_argument("out")
    ap.add_argument("--config", default="config.example.toml")
    a = ap.parse_args()
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # the repo root
    from argus.chat.chatlog import read_chat_log
    from argus.core.config import ConfigStore
    from argus.detect.replay import replay

    msgs = sorted(read_chat_log(a.chat), key=lambda m: m.ts)
    peaks, samples = replay(msgs, ConfigStore(Path(a.config)))
    rate = {name: [[s.ts, s.rate, s.z] for s in v] for name, v in samples.items()} \
        if samples and hasattr(next(iter(samples.values()))[0], "z") else {}
    Path(a.out).write_text(json.dumps({
        "peaks": [{"start": p.ts_start, "peak": p.ts_peak, "end": p.ts_end, "score": p.score,
                   "rank": p.rank} for p in peaks],
        "rate": rate,
        "first_ts": msgs[0].ts if msgs else None, "last_ts": msgs[-1].ts if msgs else None,
    }))
    print(f"{a.chat}: {len(peaks)} chat peaks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
