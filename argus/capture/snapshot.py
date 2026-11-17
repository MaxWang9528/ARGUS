"""Snapshots: the first keyframe of the newest closed segment, as a JPEG.

The recorder has no ffmpeg in its data path (segments are Twitch's own files, written
as they arrive), so there is no running decoder to add a second output to. Instead,
each time a new segment closes and ``capture.snapshot_interval_s`` has passed, one
short ffmpeg run decodes only that segment's first keyframe (``-skip_frame nokey``,
one frame). Twitch segments are 2 s long and start on a keyframe, so this gives a
fresh frame every 2 s at most.

The JPEG is written to a temp file and renamed over ``snapshots/<streamer>.jpg``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from pathlib import Path

log = logging.getLogger("argus.capture")

SNAPSHOT_TIMEOUT_S = 10.0


def ffmpeg_bin() -> str | None:
    return shutil.which("ffmpeg")


def snapshot_cmd(ffmpeg: str, segment: Path, out: Path) -> list[str]:
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-skip_frame", "nokey", "-i", str(segment),
            "-map", "0:v:0", "-frames:v", "1", "-q:v", "4", "-f", "image2", "-update", "1",
            "-c:v", "mjpeg", str(out)]


async def grab_frame(segment: Path, dest: Path, *, ffmpeg: str | None = None) -> bool:
    """Write the first keyframe of ``segment`` to ``dest`` atomically. False on failure."""
    ffmpeg = ffmpeg or ffmpeg_bin()
    if ffmpeg is None:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.stem + ".tmp.jpg")
    proc = await asyncio.create_subprocess_exec(
        *snapshot_cmd(ffmpeg, segment, tmp), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), SNAPSHOT_TIMEOUT_S)
    except (TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        tmp.unlink(missing_ok=True)
        raise
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        log.debug("snapshot failed for %s (exit code %s): %s", segment.name, proc.returncode,
                  err.decode(errors="replace").strip())
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(dest)
    return True


class Snapshotter:
    """Throttles snapshots for one streamer. Call ``maybe(segment_path)`` whenever a
    segment closes; at most one ffmpeg runs at a time per streamer."""

    def __init__(self, streamer: str, dest: Path) -> None:
        self.streamer = streamer
        self.dest = dest
        self.last_ts = 0.0
        self._task: asyncio.Task[bool] | None = None
        self._warned = False

    def due(self, interval_s: float) -> bool:
        busy = self._task is not None and not self._task.done()
        return not busy and time.time() - self.last_ts >= max(0.0, interval_s - 0.25)

    def start(self, segment: Path, on_done) -> bool:
        """Start a grab in the background. ``on_done(ts)`` runs after a success."""
        if ffmpeg_bin() is None:
            if not self._warned:
                log.warning("ffmpeg is not installed: no snapshots", extra={"streamer": self.streamer})
                self._warned = True
            return False
        self.last_ts = time.time()

        async def job() -> bool:
            ok = await grab_frame(segment, self.dest)
            if ok:
                on_done(time.time())
            return ok

        self._task = asyncio.create_task(job())
        return True

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None
