"""Shared helpers for the chat-detector tuning experiments.

Paths come from the environment, so no data or names live in the repo:
  ARGUS_DATASET   labelled dataset dir (manifest.json, labels.jsonl, streams/<name>/chat.jsonl)
  ARGUS_CHATLOGS  dir of unlabelled logs (<streamer>/<day>.jsonl)
  ARGUS_CHATDAY   which day file to read from ARGUS_CHATLOGS (default: newest per streamer)
  ARGUS_CODE      import argus from this dir instead of the repo (to test a patched peak.py)
  ARGUS_STALL     "t0,t1" unix seconds of a known connection stall in the unlabelled logs
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
# ARGUS_CODE: import argus from another checkout (e.g. a patched copy) instead of this repo
sys.path.insert(0, os.environ.get("ARGUS_CODE") or str(REPO))

from argus.chat.chatlog import read_chat_log  # noqa: E402
from argus.core.config import ConfigStore, Detection  # noqa: E402
from argus.core.models import ChatMessage  # noqa: E402
from argus.detect.peak import PeakTracker, is_counted  # noqa: E402

CHAT_LAG_S = 4.0  # a peak's "centre" = ts_peak - this (as the dataset's chat_peak source)


def user_detection() -> Detection:
    """The repo's config.toml [detection] (read only), else defaults."""
    cfg = REPO / "config.toml"
    return ConfigStore(cfg if cfg.exists() else None).get().detection


def dataset_dir() -> Path:
    d = os.environ.get("ARGUS_DATASET")
    if not d:
        raise SystemExit("set ARGUS_DATASET")
    return Path(d)


def load_dataset() -> tuple[list[dict], list[dict]]:
    """Streams (name, msgs, t0, t1) and labels (with an ``event`` id merging
    moments of the same stream within 20 s)."""
    d = dataset_dir()
    man = json.loads((d / "manifest.json").read_text())
    streams = []
    for s in man["streams"]:
        msgs = sorted(read_chat_log(d / s["chat"]), key=lambda m: m.ts)
        streams.append(dict(name=s["name"], msgs=msgs, t0=s["wall_start"],
                            t1=s["wall_start"] + s["media_duration_s"]))
    labels = [json.loads(x) for x in (d / man.get("labels", "labels.jsonl")).read_text().splitlines()
              if x.strip()]
    ev = 0
    last: dict[str, tuple[float, int]] = {}
    for lab in sorted(labels, key=lambda r: (r["stream"], r["center_wall"])):
        if lab["label"] != "moment":
            continue
        t = lab["t_event_wall"] or lab["center_wall"]
        prev = last.get(lab["stream"])
        if prev and t - prev[0] < 20:
            lab["event"] = prev[1]
        else:
            ev += 1
            lab["event"] = ev
        last[lab["stream"]] = (t, lab["event"])
    return streams, labels


def load_chatlogs() -> list[dict]:
    d = os.environ.get("ARGUS_CHATLOGS")
    if not d:
        raise SystemExit("set ARGUS_CHATLOGS")
    day = os.environ.get("ARGUS_CHATDAY")
    stall = os.environ.get("ARGUS_STALL")
    s0, s1 = (float(x) for x in stall.split(",")) if stall else (math.inf, -math.inf)
    out = []
    for sd in sorted(Path(d).iterdir()):
        files = sorted(sd.glob(f"{day}.jsonl" if day else "*.jsonl"))
        if not files:
            continue
        msgs = sorted(read_chat_log(files[-1]), key=lambda m: m.ts)
        if len(msgs) < 50:
            continue
        # split around the stall: each part is replayed separately
        parts = [[m for m in msgs if m.ts < s0], [m for m in msgs if m.ts > s1]]
        for i, p in enumerate(parts):
            if len(p) >= 50:
                out.append(dict(name=f"{sd.name}#{i}", msgs=p, t0=p[0].ts, t1=p[-1].ts))
    return out


def run(cls: type[PeakTracker], msgs: list[ChatMessage], det: Detection, name: str = "s"):
    """Replay messages through one tracker; returns (peaks, samples)."""
    tr = cls(name)
    peaks, samples = [], []
    for m in msgs:
        s, p = tr.tick(m.ts, det)
        samples += s
        peaks += p
        tr.feed(m, det)
    if msgs:
        s, p = tr.tick(msgs[-1].ts + det.smooth_s + 2 * det.bin_s, det)
        samples += s
        peaks += p
    return peaks, samples


def counts_per_second(msgs: list[ChatMessage], det: Detection, t0: float, t1: float):
    """Per-second counted messages (each user capped at 2/s) and per-second user sets,
    as the detector bins them. Index i covers [floor(t0)+i, +1)."""
    base = math.floor(t0)
    n = math.ceil(t1) - base
    users: list[dict[str, int]] = [dict() for _ in range(n)]
    texts: list[list[tuple[str, str]]] = [[] for _ in range(n)]
    for m in msgs:
        i = math.floor(m.ts) - base
        if 0 <= i < n and is_counted(m, det):
            k = m.user_id or m.user.lower()
            users[i][k] = users[i].get(k, 0) + 1
            texts[i].append((k, m.text))
    counts = [sum(min(v, 2) for v in u.values()) for u in users]
    return base, counts, users, texts


def auc(pos: list[float], neg: list[float]) -> float:
    if not pos or not neg:
        return float("nan")
    s = 0.0
    for p in pos:
        for q in neg:
            s += 1.0 if p > q else 0.5 if p == q else 0.0
    return s / (len(pos) * len(neg))
