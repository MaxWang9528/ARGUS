"""Run the app's chat detector (argus.detect.replay.replay) on recorded chat logs and
dump the peaks to JSON, so the speech evaluation (another venv) can compare.

  ~/.venvs/livewatcher/bin/python -I chat_peaks.py out.json chat1.jsonl [chat2.jsonl ...]

Uses the repo's config.toml detection settings when present (read only), else defaults.
RELAXED=1 applies the dataset's relaxed thresholds (never written to disk).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from argus.chat.chatlog import read_chat_log  # noqa: E402
from argus.core.config import ConfigStore  # noqa: E402
from argus.detect.replay import replay  # noqa: E402


def main() -> int:
    out, files = sys.argv[1], sys.argv[2:]
    cfg = REPO / "config.toml"
    store = ConfigStore(cfg if cfg.exists() else None)
    if os.environ.get("RELAXED"):
        # the dataset's "chat_peak_relaxed" settings, for slower chats (in memory only)
        c = store.get()
        det = c.detection.model_copy(update=dict(multiplier=2.5, z_threshold=3.0,
                                                 min_msgs_per_sec=0.3, min_unique_chatters=3,
                                                 cooldown_s=45, min_sustain_s=6))
        store = ConfigStore(config=c.model_copy(update={"detection": det, "streamers": []}))
    msgs = sorted((m for f in files for m in read_chat_log(f)), key=lambda m: m.ts)
    peaks, samples = replay(msgs, store)
    rows = [p.model_dump() for p in peaks]
    z = {name: [[round(r.ts, 2), round(r.z, 3), round(r.rate, 3)] for r in rs]
         for name, rs in samples.items()}
    Path(out).write_text(json.dumps({"peaks": rows, "z": z}))
    print(f"{len(msgs)} messages -> {len(rows)} chat peaks", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
