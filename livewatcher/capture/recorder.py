"""The recorder: one capture worker process per live, enabled streamer.

Pipeline (per streamer)
-----------------------
``python -m livewatcher.capture.recorder worker --channel <name> --out <dir> ...``

The worker uses streamlink's Twitch plugin (same HLS fetching, low-latency prefetch
and ad detection as the ``streamlink`` command) but, instead of joining everything
into one byte stream for ffmpeg, writes each Twitch HLS segment to its own file as it
downloads (``seg_000123.ts.part``, renamed when complete). For every segment it prints
one JSON line on stdout with Twitch's PROGRAM-DATE-TIME, the duration, the ad flag and
the local arrival time. Filtered ad segments are reported as ``skip`` lines. There is
no ffmpeg in the recording path at all; segments are Twitch's own MPEG-TS files.

Why not ``streamlink --stdout | ffmpeg -f hls``: ffmpeg would timestamp segments by
when bytes reached it, which is wrong for the burst at start-up and after every ad
break, and the ad holes would be invisible. Here each segment keeps Twitch's own date,
so time mapping and holes are exact (see ``buffer.py``). (Also: streamlink 8.6 has no
``--twitch-disable-ads`` any more; its Twitch plugin always drops ad segments, silently.)

Checked live (720p60, low latency, 3.5 min): Twitch's segments are 2.000 s MPEG-TS
(H.264 + AAC + ID3), each starting on a keyframe, PTS continuous across files; they
arrive 0.27 s after their PROGRAM-DATE-TIME end; the worker uses ~0.8 % of one core
and 79 MB RSS; ~0.43 MB/s on disk.

The parent (``Recorder``) reads those lines into a ``BufferIndex``, deletes segments
older than ``capture.buffer_minutes``, rewrites ``live.m3u8`` and triggers snapshots.
The worker exits when the stream ends or when its stdin closes (the app died).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.config import Config, ConfigStore
from ..core.events import EventBus, Topic
from ..core.interfaces import WindowUnavailable
from ..core.models import StreamStatus
from .buffer import BufferIndex, Gap, Segment
from .live_check import LiveChecker, LiveInfo
from .snapshot import Snapshotter

log = logging.getLogger("livewatcher.capture")

TICK_S = 1.0
IDLE_TIMEOUT_S = 60.0        # no worker event for this long: assume it hung, restart
STATUS_EVERY_S = 30.0        # heartbeat for Topic.STREAM_STATUS
STATUS_BUFFER_EVERY_S = 5.0  # buffer range updates are published at most this often
BACKOFF_S = (2.0, 5.0, 15.0, 30.0, 60.0)
STOP_GRACE_S = 3.0
PLAYLIST = "live.m3u8"

WorkerArgv = Callable[[str, Path, Config], list[str]]


def default_worker_argv(name: str, out_dir: Path, cfg: Config) -> list[str]:
    cap = cfg.capture
    argv = [sys.executable, "-u", "-m", "livewatcher.capture.recorder", "worker",
            "--channel", name, "--out", str(out_dir), "--quality", cap.quality]
    if cap.low_latency:
        argv.append("--low-latency")
    if not cap.filter_ads:
        argv.append("--keep-ads")
    return argv


def _capture_key(cfg: Config) -> tuple:
    c = cfg.capture
    return (c.quality, c.low_latency, c.filter_ads)


@dataclass
class _Stream:
    name: str
    dir: Path
    index: BufferIndex
    snap: Snapshotter
    live: bool = False
    title: str = ""
    state: str = "stopped"
    last_error: str = ""
    proc: asyncio.subprocess.Process | None = None
    tasks: list[asyncio.Task] = field(default_factory=list)
    key: tuple = ()
    started_at: float = 0.0
    last_event: float = 0.0
    got_segment: bool = False
    fails: int = 0
    next_start: float = 0.0
    stopping: bool = False
    in_ad: bool = False
    last_pub: float = 0.0
    last_pub_key: tuple = ()
    last_pub_range: tuple = ()


class Recorder:
    """Implements ``StreamRecorder``. See the module docstring."""

    def __init__(self, *, store: ConfigStore, bus: EventBus, live_checker: Any = None,
                 worker_argv: WorkerArgv | None = None) -> None:
        self.store, self.bus = store, bus
        self.live_checker = live_checker or LiveChecker(store)
        self.worker_argv = worker_argv or default_worker_argv
        self._streams: dict[str, _Stream] = {}
        self._running = False
        self._check_task: asyncio.Task | None = None
        self._next_check = 0.0

    # ---- paths -----------------------------------------------------------------
    def _buffer_root(self) -> Path:
        return self.store.get().data_dir / "buffer"

    def _stream(self, name: str) -> _Stream:
        name = name.lower()
        st = self._streams.get(name)
        if st is None:
            data = self.store.get().data_dir
            st = _Stream(name=name, dir=data / "buffer" / name, index=BufferIndex(streamer=name),
                         snap=Snapshotter(name, data / "snapshots" / f"{name}.jpg"))
            self._streams[name] = st
        return st

    # ---- StreamRecorder API ------------------------------------------------------
    def status(self, streamer: str) -> StreamStatus:
        name = streamer.lower()
        st = self._streams.get(name)
        if st is None:
            return StreamStatus(streamer=name)
        off = self.store.get().capture.sync_offset_s
        start, end = st.index.start, st.index.end
        return StreamStatus(
            streamer=name, live=st.live or st.state == "recording", title=st.title,
            recorder_state=st.state,  # type: ignore[arg-type]
            buffer_start=None if start is None else start - off,
            buffer_end=None if end is None else end - off,
            last_error=st.last_error)

    def window_ready(self, streamer: str, end_ts: float) -> bool:
        if not self._running:
            raise WindowUnavailable("the recorder is not running")
        name = streamer.lower()
        st = self._streams.get(name)
        cfg = self.store.get()
        s = cfg.streamer(name)
        if st is None or s is None or not s.enabled:
            raise WindowUnavailable(f"{name} is not being recorded")
        target = end_ts + cfg.capture.sync_offset_s
        idx = st.index
        if idx.start is not None and idx.start > target:
            raise WindowUnavailable("the buffer restarted after the window ended")
        if idx.covers_until(target):
            return True
        if st.proc is None and not st.live:
            raise WindowUnavailable(f"{name} went offline before the window was recorded")
        return False

    def snapshot_path(self, streamer: str) -> Path | None:
        p = self.store.get().data_dir / "snapshots" / f"{streamer.lower()}.jpg"
        return p if p.exists() else None

    def playlist_path(self, streamer: str) -> Path | None:
        st = self._streams.get(streamer.lower())
        if st is None or not st.index.segments:
            return None
        p = st.dir / PLAYLIST
        return p if p.exists() else None

    def buffer_index(self, streamer: str) -> BufferIndex | None:
        """For the cutter: the live index of a streamer's buffer."""
        st = self._streams.get(streamer.lower())
        return st.index if st is not None else None

    def buffer_dir(self, streamer: str) -> Path:
        st = self._streams.get(streamer.lower())
        return st.dir if st is not None else self._buffer_root() / streamer.lower()

    # ---- main loop -------------------------------------------------------------
    async def run(self) -> None:
        self._running = True
        self._wipe_all()
        sub = self.bus.subscribe(Topic.CONFIG_CHANGED)
        try:
            while True:
                changed = False
                while not sub.queue.empty():
                    sub.queue.get_nowait()
                    changed = True
                if changed:
                    self._next_check = 0.0
                await self._tick()
                try:
                    await asyncio.wait_for(sub.queue.get(), TICK_S)
                    self._next_check = 0.0
                except TimeoutError:
                    pass
        finally:
            self._running = False
            sub.close()
            if self._check_task is not None:
                self._check_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._check_task
            await asyncio.gather(*(self._stop(st, "recorder stopped") for st in self._streams.values()),
                                 return_exceptions=True)
            for st in self._streams.values():
                st.live = False
                self._publish(st, force=True)

    def _wipe_all(self) -> None:
        """Segments from an earlier run are useless (and unindexed): remove them."""
        root = self._buffer_root()
        if root.is_dir():
            for d in root.iterdir():
                if d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
                else:
                    d.unlink(missing_ok=True)

    async def _tick(self) -> None:
        cfg = self.store.get()
        now = time.time()
        enabled = {s.name.lower() for s in cfg.enabled_streamers()}

        if now >= self._next_check and (self._check_task is None or self._check_task.done()):
            self._next_check = now + max(5.0, float(cfg.twitch.live_check_interval_s))
            if enabled:
                self._check_task = asyncio.create_task(self._live_check(enabled))

        for name in enabled:
            st = self._stream(name)
            running = st.proc is not None
            if running and st.key and st.key != _capture_key(cfg):
                log.info("capture settings changed: restarting", extra={"streamer": name})
                await self._stop(st, "settings changed")
                st.next_start = 0.0
                running = False
            if running and not st.stopping and now - st.last_event > IDLE_TIMEOUT_S:
                log.warning("capture worker silent for %.0f s: restarting", now - st.last_event,
                            extra={"streamer": name})
                st.last_error = "capture worker stopped responding"
                await self._stop(st, "hung")
                self._failed(st)
                running = False
            if st.live and not running and now >= st.next_start:
                await self._start(st, cfg)
            self._publish(st)

        for name, st in self._streams.items():
            if name not in enabled:
                if st.proc is not None:
                    await self._stop(st, "disabled")
                st.live, st.title = False, ""
                if st.state != "stopped":
                    st.state = "stopped"
                self._publish(st)

    async def _live_check(self, names: Iterable[str]) -> None:
        try:
            results: dict[str, LiveInfo] = await self.live_checker.check(names)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("live check failed: %s", exc)
            return
        for name, info in results.items():
            st = self._stream(name)
            if info.error:
                log.debug("live check error: %s", info.error, extra={"streamer": name})
                continue  # keep the previous answer
            if info.live != st.live:
                log.info("%s is %s", name, "live" if info.live else "offline", extra={"streamer": name})
                if info.live:
                    st.fails, st.next_start = 0, 0.0
            st.live = info.live
            if info.title:
                st.title = info.title
            if not info.live and st.proc is None and st.state in ("error", "starting"):
                st.state = "stopped"
            self._publish(st)

    # ---- one pipeline ------------------------------------------------------------
    async def _start(self, st: _Stream, cfg: Config) -> None:
        shutil.rmtree(st.dir, ignore_errors=True)  # clean restart: no leftovers
        st.dir.mkdir(parents=True, exist_ok=True)
        st.index.reset()
        st.got_segment, st.in_ad, st.stopping = False, False, False
        st.key = _capture_key(cfg)
        argv = self.worker_argv(st.name, st.dir, cfg)
        env = dict(os.environ)
        pkg_root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = pkg_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        try:
            st.proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env)
        except OSError as exc:
            st.last_error = f"could not start the capture worker: {exc}"
            log.error(st.last_error, extra={"streamer": st.name})
            self._failed(st)
            return
        st.state, st.started_at, st.last_event = "starting", time.time(), time.time()
        log.info("recording started (pid %s)", st.proc.pid, extra={"streamer": st.name})
        st.tasks = [asyncio.create_task(self._read_events(st, st.proc)),
                    asyncio.create_task(self._read_stderr(st, st.proc))]
        self._publish(st, force=True)

    async def _stop(self, st: _Stream, why: str) -> None:
        proc = st.proc
        if proc is None:
            return
        st.stopping = True
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), STOP_GRACE_S)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        for t in st.tasks:
            if not t.done():
                t.cancel()
        for t in st.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        st.tasks = []
        await st.snap.stop()
        st.proc = None
        if st.state != "error":
            st.state = "stopped"
        log.info("recording stopped (%s)", why, extra={"streamer": st.name})

    def _failed(self, st: _Stream) -> None:
        delay = BACKOFF_S[min(st.fails, len(BACKOFF_S) - 1)]
        st.fails += 1
        st.next_start = time.time() + delay
        st.state = "error"
        self._next_check = min(self._next_check, time.time() + 1.0)

    async def _read_events(self, st: _Stream, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        end_reason = ""
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            st.last_event = time.time()
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("t") == "end":
                end_reason = str(ev.get("reason", ""))
            elif ev.get("t") == "error":
                st.last_error = str(ev.get("message", ""))
            self._on_event(st, ev)
        rc = await proc.wait()
        if st.stopping:
            return
        # The worker exited by itself.
        st.proc = None
        others = [t for t in st.tasks if t is not asyncio.current_task() and not t.done()]
        if others:
            await asyncio.wait(others, timeout=1.0)  # let the last stderr lines through
        for t in others:
            t.cancel()
        await st.snap.stop()
        if rc == 0 and end_reason in ("stream ended", "offline"):
            # Keep ``live`` as it is: the live check (run now) decides. Until then
            # window_ready() answers False rather than dropping waiting clips.
            log.info("capture ended (%s)", end_reason, extra={"streamer": st.name})
            st.state = "stopped"
            st.fails = 0
            st.next_start = time.time() + BACKOFF_S[0]
            self._next_check = 0.0
        else:
            if not st.last_error:
                st.last_error = f"capture worker exited with code {rc}"
            log.warning("capture worker died (%s); retrying", st.last_error, extra={"streamer": st.name})
            if st.got_segment:
                st.fails = 0
            self._failed(st)
        self._publish(st, force=True)

    async def _read_stderr(self, st: _Stream, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                return
            text = raw.decode(errors="replace").rstrip()
            if not text:
                continue
            level, _, msg = text.partition(" ")
            if level in ("ERROR", "CRITICAL"):
                st.last_error = msg
                log.warning("worker: %s", msg, extra={"streamer": st.name})
            elif level == "WARNING":
                log.warning("worker: %s", msg, extra={"streamer": st.name})
            elif level == "INFO":
                log.info("worker: %s", msg, extra={"streamer": st.name})
            else:
                log.debug("worker: %s", text, extra={"streamer": st.name})

    def _on_event(self, st: _Stream, ev: dict[str, Any]) -> None:
        kind = ev.get("t")
        if kind == "start":
            log.info("capturing %s (low latency %s)", ev.get("quality"), ev.get("low_latency"),
                     extra={"streamer": st.name})
            return
        if kind == "skip":
            if not st.in_ad and ev.get("reason") == "ad":
                log.info("ad break: ad segments are filtered out", extra={"streamer": st.name})
            st.in_ad = ev.get("reason") == "ad"
            st.index.apply(ev)
            return
        if kind == "meta":
            st.index.apply(ev)
            return
        if kind != "seg":
            return
        result = st.index.apply(ev)
        st.in_ad = False
        if isinstance(result, Gap):
            log.info("buffer gap of %.1f s (%s)", result.end - result.start, result.reason,
                     extra={"streamer": st.name})
        if not st.got_segment:
            st.got_segment = True
            st.last_error = ""
        st.state = "recording"
        cfg = self.store.get()
        removed = st.index.trim(cfg.capture.buffer_minutes * 60.0)
        self._delete(st, removed)
        self._write_playlist(st)
        if st.snap.due(cfg.capture.snapshot_interval_s):
            seg_file = st.dir / str(ev["file"])
            st.snap.start(seg_file, lambda ts, n=st.name: self.bus.publish(
                Topic.SNAPSHOT, {"streamer": n, "ts": ts}))
        self._publish(st)

    def _delete(self, st: _Stream, segs: list[Segment]) -> None:
        for seg in segs:
            (st.dir / seg.file).unlink(missing_ok=True)

    def _write_playlist(self, st: _Stream) -> None:
        tmp = st.dir / (PLAYLIST + ".tmp")
        try:
            tmp.write_text(st.index.playlist_text(), encoding="utf-8")
            tmp.replace(st.dir / PLAYLIST)
        except OSError as exc:
            log.warning("could not write the playlist: %s", exc, extra={"streamer": st.name})

    def _publish(self, st: _Stream, *, force: bool = False) -> None:
        status = self.status(st.name)
        key = (status.live, status.recorder_state, status.title, status.last_error)
        rng = (status.buffer_start, status.buffer_end)
        now = time.time()
        if (force or key != st.last_pub_key or now - st.last_pub >= STATUS_EVERY_S
                or (rng != st.last_pub_range and now - st.last_pub >= STATUS_BUFFER_EVERY_S)):
            st.last_pub, st.last_pub_key, st.last_pub_range = now, key, rng
            self.bus.publish(Topic.STREAM_STATUS, status)


# =====================================================================================
# The capture worker (runs in its own process)
# =====================================================================================

class _Emitter:
    def __init__(self, out=None) -> None:
        self.out = out or sys.stdout
        self.lock = threading.Lock()

    def __call__(self, ev: dict[str, Any]) -> None:
        line = json.dumps(ev, separators=(",", ":"))
        with self.lock:
            self.out.write(line + "\n")
            self.out.flush()


def store_segment(out_dir: Path, name: str, chunks: Iterable[bytes], meta: dict[str, Any],
                  emit: Callable[[dict[str, Any]], None]) -> bool:
    """Write one segment as ``name.part``, rename it, then announce it. On a failed or
    empty download, nothing is left behind and a ``skip`` event is sent instead."""
    part = out_dir / (name + ".part")
    size = 0
    try:
        with part.open("wb") as fh:
            for chunk in chunks:
                if chunk:
                    fh.write(chunk)
                    size += len(chunk)
    except Exception as exc:  # noqa: BLE001 - any download error leaves a hole
        part.unlink(missing_ok=True)
        emit({"t": "skip", **meta, "reason": f"download failed: {type(exc).__name__}"})
        return False
    if size == 0:
        part.unlink(missing_ok=True)
        emit({"t": "skip", **meta, "reason": "empty segment"})
        return False
    os.replace(part, out_dir / name)
    emit({"t": "seg", "file": name, "bytes": size, "recv_end": time.time(), **meta})
    return True


def _ts(date: Any) -> float | None:
    return date.timestamp() if date is not None else None


class _WorkerState:
    def __init__(self, out_dir: Path, keep_ads: bool, emit: Callable[[dict[str, Any]], None]) -> None:
        self.out_dir, self.keep_ads, self.emit = out_dir, keep_ads, emit
        self.lock = threading.Lock()
        self.counter = 0
        self.durations: dict[int, float] = {}                  # real durations, before ads are zeroed
        self.regular: dict[int, tuple[float | None, float]] = {}  # from non-prefetch entries
        self.emitted: dict[int, tuple[float | None, float]] = {}  # what we announced
        self.init_bytes: bytes = b""                           # fMP4 init section, if any

    def prune(self) -> None:
        for d in (self.durations, self.regular, self.emitted):
            if len(d) > 400:
                for k in sorted(d)[:200]:
                    del d[k]


def _reader_class(state: _WorkerState, twitch: Any):
    """Subclass the Twitch plugin's reader. ``twitch`` is the plugin module the session
    actually loaded (streamlink loads plugins under its own module objects, so the
    classes must come from there)."""
    TwitchHLSStreamReader = twitch.TwitchHLSStreamReader
    TwitchHLSStreamWorker = twitch.TwitchHLSStreamWorker
    TwitchHLSStreamWriter = twitch.TwitchHLSStreamWriter

    class Worker(TwitchHLSStreamWorker):
        def process_segments(self, playlist):
            updates = []
            with state.lock:
                for seg in playlist.segments:
                    state.durations.setdefault(seg.num, seg.duration)
                    if not seg.prefetch:
                        state.durations[seg.num] = seg.duration
                        real = (_ts(seg.date), seg.duration)
                        state.regular[seg.num] = real
                        sent = state.emitted.get(seg.num)
                        if sent is not None and _differs(sent, real):
                            state.emitted[seg.num] = real
                            updates.append((seg.num, real))
                state.prune()
            for num, (date, dur) in updates:
                state.emit({"t": "meta", "num": num, "date": date, "dur": dur})
            return super().process_segments(playlist)

    class Writer(TwitchHLSStreamWriter):
        def should_filter_segment(self, segment) -> bool:
            return bool(segment.ad) and not state.keep_ads

        def write(self, segment, result, *data):
            is_map = bool(data[0]) if data else False
            if is_map:
                state.init_bytes = result.content
                return
            with state.lock:
                date, dur = state.regular.get(
                    segment.num, (_ts(segment.date), state.durations.get(segment.num, segment.duration)))
            meta = {"num": segment.num, "date": date, "dur": dur, "ad": bool(segment.ad),
                    "prefetch": bool(getattr(segment, "prefetch", False))}
            if self.should_filter_segment(segment):
                with contextlib.suppress(Exception):
                    result.close()
                state.emit({"t": "skip", **meta, "reason": "ad"})
                return
            with state.lock:
                state.counter += 1
                ext = ".mp4" if state.init_bytes else ".ts"
                name = f"seg_{state.counter:06d}{ext}"

            def chunks():
                if state.init_bytes:
                    yield state.init_bytes
                yield from result.iter_content(65536)

            if store_segment(state.out_dir, name, chunks(), meta, state.emit):
                with state.lock:
                    state.emitted[segment.num] = (date, dur)
                    real = state.regular.get(segment.num)
                if real is not None and _differs((date, dur), real):
                    with state.lock:
                        state.emitted[segment.num] = real
                    state.emit({"t": "meta", "num": segment.num, "date": real[0], "dur": real[1]})

    class Reader(TwitchHLSStreamReader):
        __worker__ = Worker
        __writer__ = Writer

    return Reader


def _differs(a: tuple[float | None, float], b: tuple[float | None, float]) -> bool:
    if (a[0] is None) != (b[0] is None):
        return True
    if a[0] is not None and b[0] is not None and abs(a[0] - b[0]) > 0.001:
        return True
    return abs(a[1] - b[1]) > 0.001


def pick_quality(available: Iterable[str], wanted: str) -> str | None:
    names = list(available)
    for q in (x.strip() for x in wanted.split(",")):
        if q in names:
            return q
    return None


def _watch_stdin() -> None:
    """Exit when the parent goes away (our stdin is its pipe)."""
    with contextlib.suppress(Exception):
        while sys.stdin.buffer.read(1024):
            pass
    os._exit(0)


def worker_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="livewatcher.capture.recorder worker")
    ap.add_argument("--channel", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--quality", default="720p60,720p,best")
    ap.add_argument("--low-latency", action="store_true")
    ap.add_argument("--keep-ads", action="store_true")
    ap.add_argument("--no-stdin-watch", action="store_true", help="for manual runs")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
    emit = _Emitter()
    if not args.no_stdin_watch:
        threading.Thread(target=_watch_stdin, daemon=True).start()

    import streamlink
    from streamlink.exceptions import NoPluginError, PluginError
    from streamlink.options import Options

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    session = streamlink.Streamlink()
    session.set_option("http-timeout", 10)
    try:
        streams = session.streams(f"https://twitch.tv/{args.channel}",
                                  options=Options({"low-latency": args.low_latency}))
    except (NoPluginError, PluginError, OSError) as exc:
        emit({"t": "error", "message": f"could not get streams: {exc}"})
        return 2
    if not streams:
        emit({"t": "end", "reason": "offline"})
        return 0
    quality = pick_quality(streams, args.quality)
    if quality is None:
        emit({"t": "error", "message": f"none of {args.quality!r} in {sorted(streams)}"})
        return 2
    stream = streams[quality]
    twitch = sys.modules.get(type(stream).__module__)
    if type(stream).__name__ != "TwitchHLSStream" or twitch is None:
        emit({"t": "error", "message": f"unexpected stream type {type(stream).__name__}"})
        return 2

    state = _WorkerState(out_dir, args.keep_ads, emit)
    reader = _reader_class(state, twitch)(stream)
    emit({"t": "start", "quality": quality, "low_latency": bool(stream.low_latency),
          "pid": os.getpid(), "ts": time.time()})
    reader.open()
    try:
        while reader.writer.is_alive():
            reader.writer.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        reader.close()
    emit({"t": "end", "reason": "stream ended", "ts": time.time()})
    return 0


def _exit(code: int) -> None:
    """Leave without interpreter shutdown: the stdin-watch thread is blocked in a read,
    and finalising stdin under it aborts the process (SIGABRT) on Python 3.14."""
    with contextlib.suppress(Exception):
        sys.stdout.flush()
        sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        _exit(worker_main(sys.argv[2:]))
    print("usage: python -m livewatcher.capture.recorder worker --channel NAME --out DIR", file=sys.stderr)
    sys.exit(2)
