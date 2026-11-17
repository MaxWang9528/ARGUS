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
                             "category_captions": [{"category": "Minecraft", "caption": "{streamer} MC"}]}})
    app = App(store, demo=True, serve=False)
    monkeypatch.setattr(app.clips, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(app.recorder, "status", _Status("World of Warcraft"))
    assert app.clips.on_peak(_peak()).caption == "alpha World of Warcraft"
    monkeypatch.setattr(app.recorder, "status", _Status("minecraft"))
    assert app.clips.on_peak(_peak()).caption == "alpha MC"
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
    cfg = Config.model_validate({"general": {"clip_categories": ["Minecraft"]}})
    assert cfg.general.priority_categories == ["Minecraft"]


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


def test_clip_windows(store: ConfigStore, monkeypatch) -> None:
    from argus.core.models import PeakEvent
    app = App(store, demo=True, serve=False)
    monkeypatch.setattr(app.clips, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(app.recorder, "status", _Status("anything"))
    det = store.get().detection_for("alpha")
    # Clip now: manual_preroll_s (20) before the press, manual_postroll_s (11) after
    press = _peak("manual")
    manual = app.clips.on_peak(press)
    assert manual.window_start == press.ts_start - det.manual_preroll_s == press.ts_start - 20
    assert manual.window_end == press.ts_end + det.manual_postroll_s == press.ts_end + 11
    # auto, short peak: preroll before the rise, postroll after the end
    t = time.time()
    short = PeakEvent(streamer="alpha", ts_start=t, ts_peak=t + 5, ts_end=t + 9)
    c = app.clips.on_peak(short)
    assert (c.window_start, c.window_end) == (t - det.preroll_s, t + 9 + det.postroll_s)
    # auto, chat stays loud long after the busiest second: cut max_after_peak_s after it
    long = PeakEvent(streamer="alpha", ts_start=t, ts_peak=t + 5, ts_end=t + 40)
    c = app.clips.on_peak(long)
    assert c.window_end == t + 5 + det.max_after_peak_s
    assert c.window_end - c.window_start <= det.max_clip_s


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


def test_purge_deletes_files_and_rows_but_keeps_in_progress(store: ConfigStore) -> None:
    app = App(store, demo=True, serve=False)
    done = _clip_with_files(app, ClipStatus.UPLOADED, age_days=0)
    rejected = _clip_with_files(app, ClipStatus.REJECTED, age_days=0)
    queued = _clip_with_files(app, ClipStatus.QUEUED, age_days=0)
    review = _clip_with_files(app, ClipStatus.PENDING_REVIEW, age_days=0)

    got = app.purge_clips(["rejected", "uploaded", "queued"])
    assert got == {"clips": 2, "files": 4, "kept_in_progress": 1}
    for c in (done, rejected):
        assert app.db.get_clip(c.id) is None and not Path(c.raw_path).exists()
    for c in (queued, review):
        assert app.db.get_clip(c.id) is not None and Path(c.raw_path).exists()

    assert app.purge_clips()["clips"] == 1  # everything not in progress: the review clip
    assert app.db.get_clip(queued.id) is not None


def test_purge_by_age(store: ConfigStore) -> None:
    app = App(store, demo=True, serve=False)
    old = _clip_with_files(app, ClipStatus.UPLOADED, age_days=2)
    new = _clip_with_files(app, ClipStatus.UPLOADED, age_days=0)
    assert app.purge_clips(older_than_h=24)["clips"] == 1
    assert app.db.get_clip(old.id) is None and app.db.get_clip(new.id) is not None


def test_delete_one_clip(store: ConfigStore) -> None:
    import pytest

    from argus.app import ClipError
    app = App(store, demo=True, serve=False)
    clip = _clip_with_files(app, ClipStatus.FAILED, age_days=0)
    busy = _clip_with_files(app, ClipStatus.UPLOADING, age_days=0)
    assert app.delete_clip(clip.id) == 2
    assert app.db.get_clip(clip.id) is None
    with pytest.raises(ClipError):
        app.delete_clip(busy.id)


async def test_half_made_clips_are_recovered_at_start(store: ConfigStore) -> None:
    app = App(store, demo=True, serve=False)
    cutting = _clip_with_files(app, ClipStatus.CUTTING, age_days=0)
    waiting = Clip(streamer="alpha", window_start=0, window_end=1, status=ClipStatus.WAITING)
    app.db.save_clip(waiting)
    rendering = _clip_with_files(app, ClipStatus.RENDERING, age_days=0)
    done = _clip_with_files(app, ClipStatus.PENDING_REVIEW, age_days=0)
    app.clips.recover()
    for c in (cutting, waiting):
        got = app.db.get_clip(c.id)
        assert got.status == ClipStatus.DROPPED and "interrupted" in got.error
    for _ in range(100):  # the fake renderer takes a moment
        if app.db.get_clip(rendering.id).status == ClipStatus.PENDING_REVIEW:
            break
        await asyncio.sleep(0.05)
    assert app.db.get_clip(rendering.id).status == ClipStatus.PENDING_REVIEW
    assert app.db.get_clip(done.id).status == ClipStatus.PENDING_REVIEW
