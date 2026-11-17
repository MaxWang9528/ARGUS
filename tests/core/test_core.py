from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path

from argus.app import App
from argus.core.config import ConfigStore
from argus.core.events import Topic
from argus.core.models import ClipStatus


def test_example_config_is_valid(tmp_path):
    text = Path("config.example.toml").read_text(encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    store = ConfigStore(path)
    cfg = store.get()
    names = [s.name for s in cfg.streamers]
    assert len(names) >= 50 and len(set(names)) == len(names)
    assert cfg.general.max_streamers == 12
    assert cfg.general.priority_categories == []
    assert cfg.general.auto_clip is True
    assert cfg.caption_for("x", "Minecraft") == "x 🔴 Minecraft on Twitch #x #twitch #clips"
    # per-streamer detection overrides
    path.write_text(_one_streamer(text), encoding="utf-8")
    store = ConfigStore(path)
    assert store.get().detection_for("somestreamer").multiplier == 2.5
    assert store.get().detection_for("other").multiplier == 3.0


def _one_streamer(example: str) -> str:
    """The example config with its streamer list cut down to one entry with an override."""
    head = example[:example.index("\n[[streamers]]") + 1]
    return head + '[[streamers]]\nname = "somestreamer"\nmultiplier = 2.5\n'


def test_update_keeps_comments_and_validates(tmp_path, bus):
    path = tmp_path / "config.toml"
    path.write_text(Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8")
    store = ConfigStore(path, bus=bus)
    sub = bus.subscribe(Topic.CONFIG_CHANGED)
    store.update({"general": {"auto_upload": True}, "detection": {"multiplier": 4.5}})
    saved = path.read_text(encoding="utf-8")
    assert "# also switchable from the dashboard" in saved
    data = tomllib.loads(saved)
    assert data["general"]["auto_upload"] is True and data["detection"]["multiplier"] == 4.5
    assert data["general"]["dashboard_port"] == 8787
    assert sub.queue.qsize() == 1
    assert ConfigStore(path).get().general.auto_upload is True


async def test_demo_pipeline_makes_a_clip(store):
    app = App(store, demo=True, serve=False)
    task = asyncio.create_task(app.run())
    try:
        await asyncio.sleep(0.5)
        store.update({"detection": {"preroll_s": 1, "postroll_s": 0.5,
                                     "manual_preroll_s": 1, "manual_postroll_s": 0.5}})
        clip = app.clips.clip_now("alpha")
        for _ in range(100):
            await asyncio.sleep(0.2)
            clip = app.db.get_clip(clip.id)
            if clip.status is ClipStatus.PENDING_REVIEW:
                break
        assert clip.status is ClipStatus.PENDING_REVIEW, (clip.status, clip.error)
        assert clip.out_path and clip.raw_path
        clip = await app.clips.rerender(clip.id, "crop")
        assert set(clip.renders) == {"blur_fit", "crop"} and clip.active_layout == "crop"
        app.clips.approve(clip.id)
        for _ in range(50):
            await asyncio.sleep(0.2)
            if app.db.get_clip(clip.id).status is ClipStatus.DRY_RUN_OK:
                break
        assert app.db.get_clip(clip.id).status is ClipStatus.DRY_RUN_OK
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
