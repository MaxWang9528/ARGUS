"""Phase 2: retention cleanup and clean shutdown of the whole app."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from argus.app import App
from argus.core.config import ConfigStore
from argus.core.models import Clip, ClipStatus

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


def test_detection_rejects_non_positive_bins(store: ConfigStore) -> None:
    import pytest
    from pydantic import ValidationError
    for field in ("bin_s", "smooth_s"):
        with pytest.raises(ValidationError):
            store.update({"detection": {field: 0}})


async def test_clip_events_are_snapshots(store: ConfigStore) -> None:
    from argus.core.events import Topic
    app = App(store, demo=True, serve=False)
    with app.bus.subscribe(Topic.CLIP) as sub:
        clip = Clip(streamer="alpha", window_start=0, window_end=1)
        app.clips._save(clip)
        clip.status = ClipStatus.CUTTING
        app.clips._save(clip)
        first = (await sub.queue.get())[1]
        second = (await sub.queue.get())[1]
    assert (first.status, second.status) == (ClipStatus.WAITING, ClipStatus.CUTTING)


class _Status:
    def __init__(self, category: str) -> None:
        from argus.core.models import StreamStatus
        self.st = StreamStatus(streamer="alpha", live=True, category=category)

    def __call__(self, streamer: str):
        return self.st


def _peak(source: str = "auto"):
    from argus.core.models import PeakEvent
    now = time.time()
    return PeakEvent(streamer="alpha", ts_start=now, ts_peak=now, ts_end=now, source=source)


def test_any_category_is_clipped_and_sets_the_caption(store: ConfigStore, monkeypatch) -> None:
    store.update({"tiktok": {"caption_template": "{streamer} {category}",
                             "category_captions": [{"category": "Grand Theft Auto VI", "caption": "{streamer} GTA6"}]}})
    app = App(store, demo=True, serve=False)
    monkeypatch.setattr(app.clips, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(app.recorder, "status", _Status("World of Warcraft"))
    assert app.clips.on_peak(_peak()).caption == "alpha World of Warcraft"
    monkeypatch.setattr(app.recorder, "status", _Status("grand theft auto vi"))
    assert app.clips.on_peak(_peak()).caption == "alpha GTA6"
    monkeypatch.setattr(app.recorder, "status", _Status(""))
    assert app.clips.on_peak(_peak()).caption == "alpha"


def test_auto_clip_off_skips_auto_peaks_only(store: ConfigStore, monkeypatch) -> None:
    store.update({"general": {"auto_clip": False}})
    app = App(store, demo=True, serve=False)
    monkeypatch.setattr(app.clips, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(app.recorder, "status", _Status("Just Chatting"))
    assert app.clips.on_peak(_peak()) is None
    assert app.clips.on_peak(_peak("manual")) is not None  # clip now always works


def test_old_clip_categories_key_still_loads(tmp_path) -> None:
    from argus.core.config import Config
    cfg = Config.model_validate({"general": {"clip_categories": ["Grand Theft Auto VI"]}})
    assert cfg.general.priority_categories == ["Grand Theft Auto VI"]


def test_clip_rates_saved_and_read_back(store: ConfigStore) -> None:
    from argus.core.models import RateSample
    app = App(store, demo=True, serve=False)
    t0 = 1_800_000_000.0
    from collections import deque
    app._rates["alpha"] = deque(RateSample(streamer="alpha", ts=t0 + i, rate=i, baseline=1, threshold=3, z=0)
                                for i in range(100))
    clip = Clip(streamer="alpha", window_start=t0 + 40, window_end=t0 + 60)
    app.save_clip_rates(clip)
    app._rates.clear()  # history gone; the saved file still answers
    got = app.clip_rates(clip)
    assert [r.ts - t0 for r in got] == [float(i) for i in range(35, 66)]
    raw = store.get().data_dir / "clips" / "raw" / f"{clip.id}.mp4"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"x")
    clip.raw_path, clip.status, clip.created_at = str(raw), ClipStatus.UPLOADED, time.time() - 30 * DAY
    app.db.save_clip(clip)
    app.cleanup()
    assert not app._clip_rates_path(clip.id).exists()


def test_clip_now_window_is_shifted_later(store: ConfigStore, monkeypatch) -> None:
    app = App(store, demo=True, serve=False)
    monkeypatch.setattr(app.clips, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(app.recorder, "status", _Status("anything"))
    det = store.get().detection_for("alpha")  # preroll 25, postroll 6
    press = _peak("manual")
    manual = app.clips.on_peak(press)
    assert manual.window_start == press.ts_start - det.preroll_s + 5  # 20 s before the press
    assert manual.window_end == press.ts_end + det.postroll_s + 5     # 11 s after it
    peak = _peak("auto")
    auto = app.clips.on_peak(peak)
    assert auto.window_start == peak.ts_start - det.preroll_s         # automatic clips unchanged
    assert auto.window_end == peak.ts_end + det.postroll_s


def test_offline_peak_asks_for_a_live_check(store: ConfigStore, monkeypatch) -> None:
    from argus.core.models import StreamStatus
    app = App(store, demo=True, serve=False)
    asked = []
    monkeypatch.setattr(app.recorder, "status", lambda s: StreamStatus(streamer=s, live=False))
    app.recorder.request_live_check = lambda: asked.append(True)
    assert app.clips.on_peak(_peak()) is None
    assert asked == [True]


def test_expired_clip_with_render_can_be_queued_again(store: ConfigStore) -> None:
    app = App(store, demo=True, serve=False)
    clip = _clip_with_files(app, ClipStatus.DROPPED, age_days=0)
    clip.error = "expired: no upload slot within 6 h"
    app.db.save_clip(clip)
    assert app.clips.approve(clip.id).status == ClipStatus.QUEUED
