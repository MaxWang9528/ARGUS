"""The interfaces each branch implements. FROZEN after Phase 0.

Every implementation is constructed with keyword arguments ``store`` (ConfigStore),
``bus`` (EventBus) and, where it needs one, ``db`` (Database). ``app.py`` wires them.
Long-running ``run()`` coroutines must exit cleanly when cancelled.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .config import LayoutConfig
from .models import Clip, StreamStatus, UploadResult


class WindowUnavailable(Exception):
    """The asked-for window can't be cut: it overlaps an ad gap, is older than the
    buffer, or the recorder stopped before it was complete."""


@runtime_checkable
class ChatSource(Protocol):
    """Branch A. Reads chat for every enabled streamer and publishes Topic.CHAT."""

    async def run(self) -> None: ...


@runtime_checkable
class PeakDetector(Protocol):
    """Branch A. Subscribes to Topic.CHAT. Publishes Topic.RATE every bin and
    Topic.PEAK when a peak ends. Reads thresholds from the store on every bin."""

    async def run(self) -> None: ...


@runtime_checkable
class StreamRecorder(Protocol):
    """Branch B. Checks who is live, records each live streamer into a rolling
    buffer, writes snapshots, and publishes Topic.STREAM_STATUS and Topic.SNAPSHOT."""

    async def run(self) -> None: ...

    def status(self, streamer: str) -> StreamStatus: ...

    def window_ready(self, streamer: str, end_ts: float) -> bool:
        """True when everything up to chat-time ``end_ts`` is in closed segments.
        Raises WindowUnavailable if it never will be (offline, recorder stopped)."""
        ...

    def snapshot_path(self, streamer: str) -> Path | None:
        """Latest JPEG frame, or None if there isn't one yet."""
        ...

    def playlist_path(self, streamer: str) -> Path | None:
        """HLS playlist (.m3u8) of the live buffer, with segments beside it, or None."""
        ...


@runtime_checkable
class ClipCutter(Protocol):
    """Branch B."""

    async def cut(self, streamer: str, start_ts: float, end_ts: float, clip_id: str) -> Path:
        """Cut chat-time window [start_ts, end_ts] into ``clips/raw/<clip_id>.mp4``.
        Applies capture.sync_offset_s. Raises WindowUnavailable when it can't."""
        ...


@runtime_checkable
class Renderer(Protocol):
    """Branch C."""

    async def render(self, clip: Clip, layout: LayoutConfig) -> Path:
        """Render clip.raw_path to ``clips/out/<clip.id>_<layout.layout>.mp4`` (1080x1920).
        Limits itself to general.max_concurrent_renders at a time."""
        ...

    async def preview(self, snapshot_path: Path, layout: LayoutConfig) -> bytes:
        """JPEG bytes: the snapshot with the layout's regions drawn on top."""
        ...


@runtime_checkable
class Uploader(Protocol):
    """Branch D. Uploads one rendered clip. Must not raise for expected failures:
    return an UploadResult instead."""

    async def upload(self, clip: Clip, caption: str, dry_run: bool) -> UploadResult: ...


@runtime_checkable
class UploadQueue(Protocol):
    """Branch D. Watches the database for clips with status ``queued`` and uploads
    them one at a time through an Uploader, enforcing the daily and spacing limits.
    Updates clip status in the database and publishes Topic.CLIP."""

    async def run(self) -> None: ...
