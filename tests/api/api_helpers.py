"""Helpers shared by the dashboard API tests."""

from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from PIL import Image

from argus.app import App
from argus.core.models import Clip, ClipStatus, StreamStatus

SECRET = "s3cret-value-never-shown"


def data_dir(lw: App) -> Path:
    return lw.store.get().data_dir


def jpeg_bytes(size: tuple[int, int] = (64, 36)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (30, 90, 160)).save(buf, "JPEG")
    return buf.getvalue()


def write_snapshot(lw: App, streamer: str) -> Path:
    path = data_dir(lw) / "snapshots" / f"{streamer}.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(jpeg_bytes())
    return path


def make_live(lw: App, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the fake recorder report every streamer live with a ready buffer."""
    def status(streamer: str) -> StreamStatus:
        now = time.time()
        return StreamStatus(streamer=streamer.lower(), live=True, recorder_state="recording",
                            buffer_start=now - 300, buffer_end=now)

    monkeypatch.setattr(lw.recorder, "status", status)
    monkeypatch.setattr(lw.recorder, "window_ready", lambda streamer, end_ts: time.time() >= end_ts)
    lw.store.update({"detection": {"preroll_s": 1, "postroll_s": 0.2}})


def saved_clip(lw: App, status: ClipStatus = ClipStatus.PENDING_REVIEW, *, streamer: str = "alpha",
               video: bytes = b"\x00" * 4096, **fields) -> Clip:
    """A clip with a raw file and a blur_fit render on disk, saved to the database."""
    clip = Clip(streamer=streamer, window_start=time.time() - 30, window_end=time.time() - 5,
                status=status, caption="hello", **fields)
    raw = data_dir(lw) / "clips" / "raw" / f"{clip.id}.mp4"
    out = data_dir(lw) / "clips" / "out" / f"{clip.id}_blur_fit.mp4"
    for path in (raw, out):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(video)
    clip.raw_path = str(raw)
    clip.renders = {"blur_fit": str(out)}
    lw.db.save_clip(clip)
    return clip


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise AssertionError("timed out waiting")
