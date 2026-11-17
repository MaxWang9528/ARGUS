"""Cut a chat-time window out of the rolling buffer into ``clips/raw/<clip_id>.mp4``.

1. Apply ``capture.sync_offset_s`` (positive moves the clip later in the buffer).
2. Ask the buffer index for the segments that cover the window. That raises
   WindowUnavailable if the window starts before the buffer, is not recorded yet, or
   overlaps a gap (an ad break, a failed download).
3. Hard-link those segments into a scratch folder, so trimming the buffer can't delete
   them mid-cut.
4. One ffmpeg run: join the segments (they are consecutive MPEG-TS pieces of one
   stream, so byte concatenation is valid), seek after decoding for an exact start,
   and re-encode. Twitch puts a keyframe only every 2 s, so a stream copy could miss
   the start by up to 2 s; re-encoding trims to the frame.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import shutil
from pathlib import Path

from ..core.config import ConfigStore
from ..core.events import EventBus
from ..core.interfaces import WindowUnavailable
from .buffer import BufferIndex, Segment

log = logging.getLogger("argus.capture")

CUT_TIMEOUT_S = 300.0


def cut_cmd(ffmpeg: str, inputs: list[str], offset_s: float, duration_s: float, out: str) -> list[str]:
    """ffmpeg arguments for one cut. ``inputs`` are file names relative to the cwd."""
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", "concat:" + "|".join(inputs),
        "-ss", f"{offset_s:.3f}", "-t", f"{duration_s:.3f}",
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-af", "aresample=async=1", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", "-f", "mp4", out,
    ]


def ffmpeg_failure(what: str, rc: int | None, stderr: bytes, argv: list[str]) -> str:
    """An error message that always says why: stderr, else the exit code (a negative
    code is a signal, e.g. -11 for a crash) and the command line."""
    text = stderr.decode(errors="replace").strip()[-500:]
    how = f"exit code {rc}" + (f" (killed by signal {-rc})" if rc is not None and rc < 0 else "")
    if text:
        return f"ffmpeg {what} failed ({how}): {text}"
    return f"ffmpeg {what} failed ({how}, no stderr): {shlex.join(argv)}"


def _link_or_copy(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


class Cutter:
    """Implements ``ClipCutter``."""

    def __init__(self, *, store: ConfigStore, bus: EventBus, recorder: object | None = None) -> None:
        self.store, self.bus, self.recorder = store, bus, recorder

    def _index(self, streamer: str) -> tuple[BufferIndex, Path]:
        get = getattr(self.recorder, "buffer_index", None)
        index = get(streamer) if get is not None else None
        if index is None:
            raise WindowUnavailable(f"no buffer for {streamer}")
        get_dir = getattr(self.recorder, "buffer_dir", None)
        folder = get_dir(streamer) if get_dir is not None else (
            self.store.get().data_dir / "buffer" / streamer)
        return index, Path(folder)

    def plan(self, streamer: str, start_ts: float, end_ts: float) -> tuple[list[Segment], float, float]:
        """(segments, offset into the first one, duration) for a chat-time window."""
        off = self.store.get().capture.sync_offset_s
        index, _ = self._index(streamer.lower())
        segs, first_off = index.select(start_ts + off, end_ts + off)
        return segs, first_off, end_ts - start_ts

    async def cut(self, streamer: str, start_ts: float, end_ts: float, clip_id: str) -> Path:
        streamer = streamer.lower()
        cfg = self.store.get()
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError("ffmpeg is not installed")
        segs, first_off, duration = self.plan(streamer, start_ts, end_ts)
        _, folder = self._index(streamer)

        work = cfg.data_dir / "clips" / "tmp" / clip_id
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        out = cfg.data_dir / "clips" / "raw" / f"{clip_id}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        part = out.with_name(out.stem + ".part.mp4")
        try:
            names = []
            for i, seg in enumerate(segs):
                src = folder / seg.file
                if not src.exists():
                    raise WindowUnavailable(f"segment {seg.file} is gone from the buffer")
                name = f"{i:04d}{src.suffix}"
                _link_or_copy(src, work / name)
                names.append(name)
            argv = cut_cmd(ffmpeg, names, first_off, duration, str(part))
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(work), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            try:
                _, err = await asyncio.wait_for(proc.communicate(), CUT_TIMEOUT_S)
            except (TimeoutError, asyncio.CancelledError):
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                raise
            if proc.returncode != 0 or not part.exists():
                raise RuntimeError(ffmpeg_failure("cut", proc.returncode, err, argv))
            part.replace(out)
        finally:
            part.unlink(missing_ok=True)
            shutil.rmtree(work, ignore_errors=True)
            with contextlib.suppress(OSError):
                work.parent.rmdir()
        log.info("cut %s: %.1f s from %d segments", clip_id, duration, len(segs),
                 extra={"streamer": streamer})
        return out
