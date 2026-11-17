"""Fixtures for the upload tests. Nothing here touches TikTok or a real browser."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from argus.core.config import Config, ConfigStore, General, Streamer, TikTok
from argus.core.events import EventBus
from argus.core.models import Clip, ClipStatus

T0 = 1_800_000_000.0


class FakeClock:
    """Wall clock, monotonic clock and sleep in one. Sleeping moves time on."""

    def __init__(self, start: float = T0) -> None:
        self.t = start
        self.sleeps: list[float] = []
        self.on_sleep = None  # optional callback run after each sleep

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds
        if self.on_sleep:
            self.on_sleep()
        await asyncio.sleep(0)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path: Path, bus: EventBus) -> ConfigStore:
    """Like the shared fixture, but the browser profile is under tmp_path too, so no
    test can ever reach the user's real TikTok profile."""
    cfg = Config(general=General(data_dir=str(tmp_path / "data")),
                 tiktok=TikTok(browser_profile_dir=str(tmp_path / "profile"), dry_run=True,
                               max_uploads_per_day=6, min_minutes_between_uploads=45),
                 streamers=[Streamer(name="alpha")])
    return ConfigStore(config=cfg, bus=bus)


@pytest.fixture
def make_clip(tmp_path: Path):
    """Build a clip with a rendered file of a plausible size."""
    counter = iter(range(10_000))

    def _make(*, status: ClipStatus = ClipStatus.QUEUED, size: int = 50_000, created_at: float | None = None,
              caption: str = "alpha on Twitch #alpha #clips", write: bool = True) -> Clip:
        n = next(counter)
        out = tmp_path / "out" / f"c{n}_blur_fit.mp4"
        if write:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"\0" * size)
        return Clip(id=f"c{n}", streamer="alpha", window_start=T0, window_end=T0 + 30,
                    renders={"blur_fit": str(out)}, active_layout="blur_fit", status=status,
                    caption=caption, created_at=created_at if created_at is not None else T0 + n)

    return _make
