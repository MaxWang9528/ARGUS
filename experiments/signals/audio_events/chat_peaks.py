"""Run the app's chat detector (argus.detect.replay.replay) on a chat log, dump peaks as JSON.

Needs argus, so run it with the app's environment, from the repo root:
    ~/.venvs/livewatcher/bin/python -I experiments/signals/audio_events/chat_peaks.py \
        CONFIG.toml CHAT.jsonl OUT.json [STREAMER] [--relaxed]

CONFIG.toml gives the detection settings (the local config.toml, so the comparison uses
the settings the app really runs with). ``--relaxed`` lowers the thresholds in memory
(never saved) the way the dataset's ``chat_peak_relaxed`` source does, for slow chats.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from argus.chat.chatlog import read_chat_log  # noqa: E402
from argus.core.config import Config, ConfigStore  # noqa: E402
from argus.detect.replay import replay  # noqa: E402


RELAXED = {"multiplier": 2.5, "z_threshold": 3.0, "min_msgs_per_sec": 0.3,
           "min_unique_chatters": 3, "cooldown_s": 45, "min_sustain_s": 6}


def main() -> None:
    relaxed = "--relaxed" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--relaxed"]
    cfg, chat, out = args[:3]
    only = args[3].lower() if len(args) > 3 else None
    raw = tomllib.loads(Path(cfg).read_text(encoding="utf-8"))
    if relaxed:
        raw.setdefault("detection", {}).update(RELAXED)
        for st in raw.get("streamers", []):
            for k in RELAXED:
                st.pop(k, None)
    store = ConfigStore(config=Config.model_validate(raw))  # in memory only
    msgs = sorted((m for m in read_chat_log(chat) if not only or m.streamer.lower() == only),
                  key=lambda m: m.ts)
    peaks, samples = replay(msgs, store, only)
    rows = [p.model_dump(include={"streamer", "ts_start", "ts_peak", "ts_end", "score", "rank",
                                  "reaction", "chatters"}) for p in peaks]
    rate = {k: [[s.ts, s.rate, s.z] for s in v] for k, v in samples.items()}
    Path(out).write_text(json.dumps({"peaks": rows, "n_messages": len(msgs),
                                     "t0": msgs[0].ts if msgs else None,
                                     "t1": msgs[-1].ts if msgs else None, "rate": rate}))
    print(f"{chat}: {len(msgs)} messages, {len(rows)} peaks")


if __name__ == "__main__":
    main()
