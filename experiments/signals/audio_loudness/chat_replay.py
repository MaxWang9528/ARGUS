"""Run the app's chat detector over a recorded chat log and dump peaks + rate samples
as JSON, so ``evaluate.py`` (numpy venv, no app deps) can compare against it.

Run with the app's environment, from anywhere:
    <app-venv>/bin/python -I chat_replay.py <repo> <chat.jsonl> <out.json> [--config config.toml]
"""

from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("chat")
    ap.add_argument("out")
    ap.add_argument("--config", default=None, help="config TOML (default: built-in defaults)")
    a = ap.parse_args()
    sys.path.insert(0, a.repo)
    from argus.chat.chatlog import read_chat_log
    from argus.core.config import ConfigStore
    from argus.detect.replay import replay

    msgs = sorted(read_chat_log(a.chat), key=lambda m: m.ts)
    peaks, samples = replay(msgs, ConfigStore(a.config))
    out = {"peaks": [p.model_dump(include={"ts_start", "ts_peak", "ts_end", "score", "rank", "chatters",
                                            "reaction"}) for p in peaks],
           "samples": [[s.ts, s.rate, s.baseline, s.z] for v in samples.values() for s in v],
           "n_messages": len(msgs)}
    with open(a.out, "w") as fh:
        json.dump(out, fh)
    print(f"{a.chat}: {len(msgs)} msgs, {len(peaks)} chat peaks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
