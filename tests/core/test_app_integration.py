"""Phase 2: retention cleanup and clean shutdown of the whole app."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from livewatcher.app import App
from livewatcher.core.config import ConfigStore
from livewatcher.core.models import Clip, ClipStatus

DAY = 86400.0


def _clip_with_files(app: App, status: ClipStatus, age_days: float) -> Clip:
    data = app.store.get().data_dir
    clip = Clip(streamer="alpha", window_start=0, window_end=1, status=status,
                created_at=time.time() - age_days * DAY)
    raw = data / "clips" / "raw" / f"{clip.id}.mp4"
    out = data / "clips" / "out" / f"{clip.id}_blur_fit.mp4"
    for p in (raw, out):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    clip.raw_path, clip.renders = str(raw), {"blur_fit": str(out)}
    app.db.save_clip(clip)
    return clip


def test_cleanup_deletes_old_finished_clips_only(store: ConfigStore) -> None:
    app = App(store, demo=True, serve=False)
    old_done = _clip_with_files(app, ClipStatus.UPLOADED, age_days=8)
    old_busy = _clip_with_files(app, ClipStatus.QUEUED, age_days=8)
    fresh = _clip_with_files(app, ClipStatus.PENDING_REVIEW, age_days=1)

    assert app.cleanup() == 2

    gone = app.db.get_clip(old_done.id)
    assert gone is not None and gone.raw_path is None and gone.renders == {}
    assert not Path(old_done.raw_path).exists() and not Path(old_done.renders["blur_fit"]).exists()
    for kept in (old_busy, fresh):
        assert Path(kept.raw_path).exists()
        assert app.db.get_clip(kept.id).raw_path == kept.raw_path
    assert app.cleanup() == 0  # nothing left to do, nothing re-saved


def test_cleanup_is_off_at_zero_days(store: ConfigStore) -> None:
    store.update({"general": {"retention_days": 0}})
    app = App(store, demo=True, serve=False)
    clip = _clip_with_files(app, ClipStatus.UPLOADED, age_days=30)
    assert app.cleanup() == 0
    assert Path(clip.raw_path).exists()


def test_cleanup_never_leaves_data_dir(store: ConfigStore, tmp_path: Path) -> None:
    app = App(store, demo=True, serve=False)
    outside = tmp_path / "precious.mp4"
    outside.write_bytes(b"x")
    clip = Clip(streamer="alpha", window_start=0, window_end=1, status=ClipStatus.UPLOADED,
                created_at=time.time() - 30 * DAY, raw_path=str(outside))
    app.db.save_clip(clip)
    app.cleanup()
    assert outside.exists()


def test_cleanup_removes_stale_scratch(store: ConfigStore) -> None:
    app = App(store, demo=True, serve=False)
    tmp = store.get().data_dir / "clips" / "tmp"
    stale, live = tmp / "old", tmp / "new"
    for d in (stale, live):
        d.mkdir(parents=True)
    os.utime(stale, (time.time() - 2 * DAY,) * 2)
    app.cleanup()
    assert not stale.exists() and live.exists()


async def test_app_with_dashboard_stops_cleanly(store: ConfigStore, capsys) -> None:
    store.update({"general": {"dashboard_port": 0}})
    app = App(store, demo=True)
    task = asyncio.create_task(app.run())
    await asyncio.sleep(1.0)
    task.cancel()
    try:
        await asyncio.wait_for(task, 10)
    except asyncio.CancelledError:
        pass
    assert "Traceback" not in capsys.readouterr().err
