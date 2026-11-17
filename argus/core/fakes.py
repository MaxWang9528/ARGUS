"""Stand-ins for every interface, so the app and each branch run without Twitch,
ffmpeg or TikTok. FROZEN after Phase 0. Used by ``argus demo`` and by tests.
"""

from __future__ import annotations

import asyncio
import io
import random
import shutil
import statistics
import time
from collections import deque
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .config import ConfigStore, LayoutConfig
from .db import Database
from .events import EventBus, Topic
from .interfaces import WindowUnavailable
from .models import ChatMessage, Clip, ClipStatus, PeakEvent, RateSample, StreamStatus, UploadResult


class FakeChatSource:
    """Steady made-up chat with a burst every ``spike_every_s`` seconds."""

    def __init__(self, *, store: ConfigStore, bus: EventBus, base_rate: float = 3.0,
                 spike_every_s: float = 45.0, spike_len_s: float = 8.0, spike_factor: float = 8.0):
        self.store, self.bus = store, bus
        self.base_rate, self.spike_every_s = base_rate, spike_every_s
        self.spike_len_s, self.spike_factor = spike_len_s, spike_factor

    async def run(self) -> None:
        start = time.time()
        rng = random.Random(1)
        while True:
            await asyncio.sleep(0.1)
            now = time.time()
            for i, s in enumerate(self.store.get().enabled_streamers()):
                phase = (now - start + i * 13) % self.spike_every_s
                in_spike = phase > self.spike_every_s - self.spike_len_s
                rate = self.base_rate * (self.spike_factor if in_spike else 1.0)
                for _ in range(_poisson(rng, rate * 0.1)):
                    uid = str(rng.randrange(400))
                    self.bus.publish(Topic.CHAT, ChatMessage(
                        streamer=s.name.lower(), ts=now, user_id=uid, user=f"user{uid}",
                        text=rng.choice(["LUL", "POG", "no way", "KEKW", "clip it", "W"])))


def _poisson(rng: random.Random, lam: float) -> int:
    n, p, limit = 0, 1.0, pow(2.718281828, -lam)
    while True:
        p *= rng.random()
        if p <= limit:
            return n
        n += 1


class FakePeakDetector:
    """A deliberately small detector: 1 s bins, median baseline, fixed 3x rule."""

    def __init__(self, *, store: ConfigStore, bus: EventBus, warmup_s: int = 15):
        self.store, self.bus, self.warmup_s = store, bus, warmup_s

    async def run(self) -> None:
        counts: dict[str, int] = {}
        users: dict[str, set[str]] = {}
        history: dict[str, deque[float]] = {}
        active: dict[str, dict] = {}
        with self.bus.subscribe(Topic.CHAT) as sub:
            next_bin = time.time() + 1
            while True:
                timeout = max(0.0, next_bin - time.time())
                try:
                    _, msg = await asyncio.wait_for(sub.get(), timeout)
                    counts[msg.streamer] = counts.get(msg.streamer, 0) + 1
                    users.setdefault(msg.streamer, set()).add(msg.user_id)
                    continue
                except TimeoutError:
                    pass
                now, next_bin = next_bin, next_bin + 1
                for s in self.store.get().enabled_streamers():
                    name = s.name.lower()
                    det = self.store.get().detection_for(name)
                    rate = float(counts.pop(name, 0))
                    uniq = len(users.pop(name, set()))
                    hist = history.setdefault(name, deque(maxlen=120))
                    base = statistics.median(hist) if hist else rate
                    threshold = max(base * det.multiplier, det.min_msgs_per_sec)
                    self.bus.publish(Topic.RATE, RateSample(
                        streamer=name, ts=now, rate=rate, baseline=base, threshold=threshold,
                        z=(rate - base) / max(1.0, base ** 0.5), unique_chatters=uniq))
                    cur = active.get(name)
                    if cur is None and len(hist) >= self.warmup_s and rate > threshold:
                        active[name] = {"start": now, "peak": now, "max": rate, "base": base}
                    elif cur is not None:
                        if rate > cur["max"]:
                            cur["max"], cur["peak"] = rate, now
                        if rate < cur["base"] * det.end_ratio or now - cur["start"] > det.max_clip_s:
                            del active[name]
                            self.bus.publish(Topic.PEAK, PeakEvent(
                                streamer=name, ts_start=cur["start"], ts_peak=cur["peak"], ts_end=now,
                                score=cur["max"] / max(cur["base"], 0.1)))
                    if name not in active:
                        hist.append(rate)


class FakeRecorder:
    """Pretends every enabled streamer is live. Snapshots are generated pictures."""

    def __init__(self, *, store: ConfigStore, bus: EventBus):
        self.store, self.bus = store, bus
        self._started = time.time()
        self._running = False

    def _snap_dir(self) -> Path:
        d = self.store.get().data_dir / "snapshots"
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def run(self) -> None:
        self._running = True
        self._started = time.time()
        try:
            while True:
                cfg = self.store.get()
                for i, s in enumerate(cfg.enabled_streamers()):
                    name = s.name.lower()
                    img = Image.new("RGB", (640, 360), ((40 + i * 70) % 255, 60, (150 + i * 40) % 255))
                    draw = ImageDraw.Draw(img)
                    draw.rectangle([0, 216, 160, 360], fill=(20, 20, 20))  # pretend facecam
                    draw.text((20, 20), f"{name}  (fake stream)", fill="white")
                    draw.text((20, 50), time.strftime("%H:%M:%S"), fill="white")
                    tmp = self._snap_dir() / f"{name}.tmp.jpg"
                    img.save(tmp, "JPEG")
                    tmp.replace(self._snap_dir() / f"{name}.jpg")
                    self.bus.publish(Topic.SNAPSHOT, {"streamer": name, "ts": time.time()})
                    self.bus.publish(Topic.STREAM_STATUS, self.status(name))
                await asyncio.sleep(cfg.capture.snapshot_interval_s)
        finally:
            self._running = False

    def status(self, streamer: str) -> StreamStatus:
        now = time.time()
        buf = self.store.get().capture.buffer_minutes * 60
        return StreamStatus(
            streamer=streamer.lower(), live=self._running, title="Fake stream for the demo", category="Just Chatting",
            viewers=self._fake_viewers(streamer),
            recorder_state="recording" if self._running else "stopped",
            buffer_start=max(self._started, now - buf), buffer_end=now)

    @staticmethod
    def _fake_viewers(streamer: str) -> int:
        return 1000 + (sum(map(ord, streamer.lower())) * 7919) % 50000

    def window_ready(self, streamer: str, end_ts: float) -> bool:
        if not self._running:
            raise WindowUnavailable("recorder is not running")
        return time.time() >= end_ts + 0.5

    def snapshot_path(self, streamer: str) -> Path | None:
        p = self._snap_dir() / f"{streamer.lower()}.jpg"
        return p if p.exists() else None

    def playlist_path(self, streamer: str) -> Path | None:
        return None


async def _ffmpeg(*args: str) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    return await proc.wait() == 0


class FakeCutter:
    """Writes a test-pattern video of the right length (a placeholder file if
    ffmpeg is missing)."""

    def __init__(self, *, store: ConfigStore, bus: EventBus, recorder: object | None = None):
        self.store, self.bus = store, bus

    async def cut(self, streamer: str, start_ts: float, end_ts: float, clip_id: str) -> Path:
        out = self.store.get().data_dir / "clips" / "raw" / f"{clip_id}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        dur = f"{min(max(end_ts - start_ts, 1.0), 8.0):.2f}"
        ok = await _ffmpeg(
            "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=30:duration={dur}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(out))
        if not ok:
            out.write_bytes(b"fake clip (ffmpeg not installed)")
        return out


class FakeRenderer:
    """Copies the raw clip. ``preview`` draws the layout's regions for real."""

    def __init__(self, *, store: ConfigStore, bus: EventBus):
        self.store, self.bus = store, bus

    async def render(self, clip: Clip, layout: LayoutConfig) -> Path:
        out = self.store.get().data_dir / "clips" / "out" / f"{clip.id}_{layout.layout}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.sleep(0.3)
        shutil.copyfile(clip.raw_path, out)
        return out

    async def preview(self, snapshot_path: Path, layout: LayoutConfig) -> bytes:
        img = Image.open(snapshot_path).convert("RGB")
        w, h = img.size
        draw = ImageDraw.Draw(img)
        if layout.layout == "stacked":
            for region, colour in ((layout.stack_top, "yellow"), (layout.stack_bottom, "cyan")):
                draw.rectangle([region.x * w, region.y * h, (region.x + region.w) * w - 1,
                                (region.y + region.h) * h - 1], outline=colour, width=3)
        elif layout.layout == "crop":
            cw = h * 9 / 16
            x0 = (w - cw) * layout.crop_x
            draw.rectangle([x0, 0, x0 + cw - 1, h - 1], outline="yellow", width=3)
        buf = io.BytesIO()
        img.save(buf, "JPEG")
        return buf.getvalue()


class FakeUploader:
    def __init__(self, *, store: ConfigStore, bus: EventBus, delay_s: float = 1.0,
                 results: list[UploadResult] | None = None):
        self.store, self.bus, self.delay_s = store, bus, delay_s
        self.results = list(results or [])  # scripted results for tests, used in order
        self.calls: list[tuple[str, str, bool]] = []
        self.accounts: list[str] = []  # account name per call

    async def upload(self, clip: Clip, caption: str, dry_run: bool, account: Any = None) -> UploadResult:
        self.calls.append((clip.id, caption, dry_run))
        self.accounts.append(account.name if account is not None else "")
        await asyncio.sleep(self.delay_s)
        if self.results:
            return self.results.pop(0)
        return UploadResult(kind="dry_run" if dry_run else "ok")


class FakeUploadQueue:
    """Uploads queued clips one at a time. No rate limits."""

    def __init__(self, *, store: ConfigStore, bus: EventBus, db: Database, uploader: object):
        self.store, self.bus, self.db, self.uploader = store, bus, db, uploader

    async def run(self) -> None:
        while True:
            for clip in self.db.list_clips(status=ClipStatus.QUEUED, oldest_first=True, limit=1):
                clip.status = ClipStatus.UPLOADING
                self._save(clip)
                dry = self.store.get().tiktok.dry_run
                res = await self.uploader.upload(clip, clip.caption, dry)
                self.db.add_upload(clip.id, res.kind, res.error)
                clip.status = {"ok": ClipStatus.UPLOADED, "dry_run": ClipStatus.DRY_RUN_OK}.get(
                    res.kind, ClipStatus.FAILED)
                clip.error, clip.debug_screenshot = res.error, res.screenshot
                self._save(clip)
            await asyncio.sleep(1.0)

    def _save(self, clip: Clip) -> None:
        self.db.save_clip(clip)
        self.bus.publish(Topic.CLIP, clip)
