"""Shared fixtures. FROZEN after Phase 0 (add your own in tests/<area>/conftest.py)."""

from __future__ import annotations

import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from argus.core.config import Config, ConfigStore, General, Streamer
from argus.core.db import Database
from argus.core.events import EventBus

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def store(tmp_path: Path, bus: EventBus) -> ConfigStore:
    """An in-memory config with two streamers and data_dir under tmp_path."""
    cfg = Config(general=General(data_dir=str(tmp_path / "data")),
                 streamers=[Streamer(name="alpha"), Streamer(name="beta")])
    return ConfigStore(config=cfg, bus=bus)


@pytest.fixture
def db() -> Database:
    return Database(":memory:")


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 12 s 1280x720 30 fps test video with a tone and a keyframe every 2 s
    (like Twitch). The frame shows a running timestamp. Skips without ffmpeg."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    out = tmp_path_factory.mktemp("media") / "sample.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=12",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
         "-c:v", "libx264", "-preset", "ultrafast", "-g", "60", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(out)],
        check=True)
    return out


def make_chat_log(path: Path, *, streamer: str = "alpha", duration_s: int = 600, base_rate: float = 4.0,
                  spike_at_s: int = 400, spike_len_s: int = 10, spike_rate: float = 30.0,
                  t0: float = 1_800_000_000.0, seed: int = 7) -> Path:
    """Write a JSONL chat log (one ChatMessage per line) with one planted spike."""
    rng = random.Random(seed)
    with path.open("w", encoding="utf-8") as fh:
        for sec in range(duration_s):
            in_spike = spike_at_s <= sec < spike_at_s + spike_len_s
            rate = spike_rate if in_spike else base_rate
            n = max(0, round(rng.gauss(rate, rate ** 0.5)))
            for ts in sorted(t0 + sec + rng.random() for _ in range(n)):
                uid = str(rng.randrange(300))
                fh.write(json.dumps({"streamer": streamer, "ts": ts, "user_id": uid,
                                     "user": f"user{uid}", "text": rng.choice(["LUL", "POG", "gg", "W"])}) + "\n")
    return path


@pytest.fixture
def sample_chat_log(tmp_path: Path) -> Path:
    """10 minutes of chat at ~4 msg/s with one 10 s spike to ~30 msg/s at 400 s.
    The first message is at ts 1_800_000_000, so the spike starts at +400 s."""
    return make_chat_log(tmp_path / "sample_chat.jsonl")
