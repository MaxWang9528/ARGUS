"""FfmpegRenderer: raw 16:9 clip -> 1080x1920 H.264/AAC MP4 in one layout.

``app.py`` builds it as ``FfmpegRenderer(store=..., bus=...)``. Nothing here needs
ffmpeg at construction time; a missing binary only fails the render itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from livewatcher.core.config import ConfigStore, LayoutConfig
from livewatcher.core.events import EventBus
from livewatcher.core.models import Clip

from .ffmpeg_graph import build_filtergraph, parse_resolution
from .layouts import draw_preview, find_font

log = logging.getLogger("livewatcher.render")

VIDEO_ARGS = ("-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p")
AUDIO_ARGS = ("-c:a", "aac", "-b:a", "128k", "-ac", "2")
MAX_FPS = 60.0
TAIL_LINES = 20
PROBE_TIMEOUT_S = 30.0


class RenderError(RuntimeError):
    """ffmpeg (or ffprobe) failed. ``output`` holds the last lines it printed."""

    def __init__(self, message: str, output: str = "") -> None:
        self.output = output
        super().__init__(f"{message}\n{output}" if output else message)


@dataclass(frozen=True)
class SourceInfo:
    width: int
    height: int
    fps: float | None
    has_audio: bool
    duration: float | None


def _rate(text: str | None) -> float | None:
    try:
        num, _, den = (text or "").partition("/")
        val = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return None
    return val if val > 0 else None


def parse_probe(data: dict) -> SourceInfo:
    """SourceInfo from ``ffprobe -of json`` output. Raises RenderError."""
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video" and s.get("width")), None)
    if video is None:
        raise RenderError("the raw clip has no video stream")
    fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
    try:
        duration = float((data.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    return SourceInfo(int(video["width"]), int(video["height"]), fps,
                      any(s.get("codec_type") == "audio" for s in streams), duration)


class _Limiter:
    """At most ``limit()`` holders at once. The limit is read every time a waiter
    checks, so changing it in the config applies to the next render to start."""

    def __init__(self, limit) -> None:
        self._limit = limit
        self._active = 0
        self._cond: asyncio.Condition | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def active(self) -> int:
        return self._active

    def _condition(self) -> asyncio.Condition:
        loop = asyncio.get_running_loop()
        if self._cond is None or self._loop is not loop:
            self._cond, self._loop, self._active = asyncio.Condition(), loop, 0
        return self._cond

    def _max(self) -> int:
        try:
            return max(1, int(self._limit()))
        except (TypeError, ValueError):
            return 1

    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        cond = self._condition()
        async with cond:
            while self._active >= self._max():
                # Wake up now and then too, in case the limit was raised.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(cond.wait(), timeout=0.25)
            self._active += 1
        try:
            yield
        finally:
            async with cond:
                self._active -= 1
                cond.notify_all()


class FfmpegRenderer:
    """Implements ``core.interfaces.Renderer``.

    ``ffmpeg_cmd`` / ``ffprobe_cmd`` are the commands to run (a list, so tests can
    point them at a stub script). ``threads`` caps x264's threads (default: ffmpeg's).
    ``title_fallback``: if the title banner makes ffmpeg fail (no drawtext filter, bad
    font), render again without it instead of failing. Tests turn it off."""

    def __init__(self, *, store: ConfigStore, bus: EventBus,
                 ffmpeg_cmd: Sequence[str] = ("ffmpeg",), ffprobe_cmd: Sequence[str] = ("ffprobe",),
                 threads: int | None = None, title_fallback: bool = True) -> None:
        self.store, self.bus = store, bus
        self.ffmpeg_cmd = list(ffmpeg_cmd)
        self.ffprobe_cmd = list(ffprobe_cmd)
        self.threads = threads
        self.title_fallback = title_fallback
        self._limiter = _Limiter(lambda: self.store.get().general.max_concurrent_renders)

    @property
    def active_renders(self) -> int:
        return self._limiter.active

    # ---- Renderer interface ----------------------------------------------------
    async def render(self, clip: Clip, layout: LayoutConfig) -> Path:
        if not clip.raw_path:
            raise RenderError(f"clip {clip.id} has no raw file")
        raw = Path(clip.raw_path)
        if not raw.is_file():
            raise RenderError(f"raw clip not found: {raw}")
        async with self._limiter.slot():
            cfg = self.store.get()  # read now, not when the render was queued
            out_dir = cfg.data_dir / "clips" / "out"
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"{clip.id}_{layout.layout}.mp4"
            try:
                size = parse_resolution(cfg.render.resolution)
            except ValueError as exc:
                raise RenderError(str(exc)) from None
            title = None
            font = None
            if cfg.render.burn_title:
                s = cfg.streamer(clip.streamer)
                title = s.name if s else clip.streamer
                font = find_font()
                if font is None:
                    log.warning("no font found for the title banner, rendering without it",
                                extra={"streamer": clip.streamer})
                    title = None
            loop = asyncio.get_running_loop()
            started = loop.time()
            info = await self.probe(raw)
            try:
                await self._encode(raw, out, layout, size, info, title, font)
            except RenderError as exc:
                title_broke = any(k in exc.output.lower() for k in ("drawtext", "font"))
                if title is None or not self.title_fallback or not title_broke:
                    raise
                log.warning("title banner failed (%s), rendering without it",
                            exc.output.strip().splitlines()[-1] if exc.output.strip() else exc,
                            extra={"streamer": clip.streamer})
                await self._encode(raw, out, layout, size, info, None, None)
            log.info("rendered %s (%s) in %.1f s", clip.id, layout.layout, loop.time() - started,
                     extra={"streamer": clip.streamer})
            return out

    async def preview(self, snapshot_path: Path, layout: LayoutConfig) -> bytes:
        size = parse_resolution(self.store.get().render.resolution)
        return await asyncio.to_thread(draw_preview, Path(snapshot_path), layout, size)

    # ---- pieces ----------------------------------------------------------------
    async def probe(self, path: Path) -> SourceInfo:
        cmd = [*self.ffprobe_cmd, "-v", "error",
               "-show_entries", "stream=codec_type,width,height,avg_frame_rate,r_frame_rate:format=duration",
               "-of", "json", str(path)]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError:
            raise RenderError(f"ffprobe not found ({cmd[0]}); install ffmpeg") from None
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), PROBE_TIMEOUT_S)
        except BaseException:
            await _kill(proc)
            raise
        if proc.returncode != 0:
            raise RenderError(f"ffprobe failed on {path.name} (exit {proc.returncode})",
                              _tail(stderr.decode("utf-8", "replace")))
        try:
            return parse_probe(json.loads(stdout or b"{}"))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise RenderError(f"could not read ffprobe output: {exc}") from None

    def ffmpeg_args(self, raw: Path, dest: Path, graph: str, info: SourceInfo) -> list[str]:
        args = [*self.ffmpeg_cmd, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "warning", "-y",
                "-i", str(raw)]
        if info.has_audio:
            audio_map = "0:a:0"
        else:  # TikTok wants an audio track: add silence of the same length
            if info.duration:
                args += ["-t", f"{info.duration:.3f}"]
            args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
            audio_map = "1:a:0"
        args += ["-filter_complex", graph, "-map", "[v]", "-map", audio_map, *VIDEO_ARGS]
        if info.fps and info.fps > MAX_FPS + 0.01:
            args += ["-r", f"{MAX_FPS:g}"]
        args += [*AUDIO_ARGS, "-movflags", "+faststart"]
        if not info.has_audio and not info.duration:
            args += ["-shortest"]
        if self.threads:
            args += ["-threads", str(self.threads)]
        args += ["-f", "mp4", str(dest)]
        return args

    async def _encode(self, raw: Path, out: Path, layout: LayoutConfig, size: tuple[int, int],
                      info: SourceInfo, title: str | None, font: str | None) -> None:
        graph = build_filtergraph(layout, size, title, src_size=(info.width, info.height), font_file=font)
        tmp = out.with_name(f".{out.stem}.{uuid.uuid4().hex[:8]}.part.mp4")
        try:
            await self._run_ffmpeg(self.ffmpeg_args(raw, tmp, graph, info))
            if not tmp.is_file() or tmp.stat().st_size == 0:
                raise RenderError("ffmpeg exited without writing the output file")
            os.replace(tmp, out)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    async def _run_ffmpeg(self, args: list[str]) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError:
            raise RenderError(f"ffmpeg not found ({args[0]}); install ffmpeg") from None
        buf = bytearray()
        try:
            assert proc.stderr is not None
            while chunk := await proc.stderr.read(8192):
                buf += chunk
                if len(buf) > 64_000:
                    del buf[:-32_000]
            rc = await proc.wait()
        except BaseException:  # cancelled (or anything else): don't leave ffmpeg running
            await _kill(proc)
            raise
        if rc != 0:
            raise RenderError(f"ffmpeg failed (exit {rc})", _tail(buf.decode("utf-8", "replace")))


async def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    # Reap it even if we are being cancelled again meanwhile.
    with contextlib.suppress(Exception):
        await asyncio.shield(proc.wait())


def _tail(text: str, n: int = TAIL_LINES) -> str:
    lines = [ln.rstrip() for ln in text.replace("\r", "\n").splitlines() if ln.strip()]
    return "\n".join(lines[-n:])
