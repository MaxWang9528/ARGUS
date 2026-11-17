"""Adapter for the stream-signal test set (manifest.json + labels*.jsonl)."""

from __future__ import annotations

import json
from pathlib import Path


def load_labels(ds: Path) -> dict:
    """{name: {"dir", "wall_start", "duration", "windows": [dict]}}; window times in media s."""
    man = json.loads((ds / "manifest.json").read_text())
    rows = [json.loads(line) for line in (ds / man["labels"]).read_text().splitlines() if line.strip()]
    out = {}
    for s in man["streams"]:
        d = ds / Path(s["media"]).parent
        if not (d / "media.mp4").exists():
            continue
        wins = []
        for r in rows:
            if r["stream"] != s["name"]:
                continue
            wins.append({"id": r["id"], "label": r["label"], "sources": r["sources"],
                         "w0": r["window_media"][0], "w1": r["window_media"][1],
                         "t_event": r.get("t_event_media"), "chat_reacted": r.get("chat_reacted")})
        out[s["name"]] = {"dir": d, "wall_start": s["wall_start"],
                          "duration": s["media_duration_s"], "windows": wins}
    return out
