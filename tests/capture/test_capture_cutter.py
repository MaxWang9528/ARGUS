from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from livewatcher.capture.buffer import BufferIndex
from livewatcher.capture.cutter import Cutter, cut_cmd
from livewatcher.capture.recorder import Recorder
from livewatcher.capture.snapshot import Snapshotter, grab_frame
from livewatcher.core.config import ConfigStore
from livewatcher.core.events import EventBus
from livewatcher.core.interfaces import ClipCutter, WindowUnavailable

T0 = 1_800_000_000.0  # same as conftest.py: Twitch date of the first fake segment

LAG = 0.3  # each fake segment "arrives" this long after it ends -> clock_offset 0.3
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


class FakeRecorder:
    def __init__(self, index: BufferIndex, folder: Path) -> None:
        self.index, self.folder = index, folder

    def buffer_index(self, streamer: str) -> BufferIndex | None:
        return self.index if streamer == "alpha" else None

    def buffer_dir(self, streamer: str) -> Path:
        return self.folder


def seg_event(i: int, file: str, dur: float = 2.0) -> dict:
    date = T0 + i * dur
    return {"t": "seg", "num": 500 + i, "file": file, "date": date, "dur": dur,
            "recv_end": date + dur + LAG, "bytes": 1}


def chat(media_s: float) -> float:
    """Chat time of a moment ``media_s`` seconds into the fake buffer."""
    return T0 + LAG + media_s


def synthetic(n: int = 6, *, skip: tuple[int, ...] = ()) -> BufferIndex:
    idx = BufferIndex(streamer="alpha")
    for i in range(n):
        if i in skip:
            idx.apply({"t": "skip", "num": 500 + i, "date": T0 + 2 * i, "dur": 2.0, "reason": "ad"})
        else:
            idx.apply(seg_event(i, f"seg_{i:06d}.ts"))
    return idx


# ---- planning (no ffmpeg) -----------------------------------------------------------

def test_is_a_clip_cutter(store: ConfigStore, bus: EventBus) -> None:
    assert isinstance(Cutter(store=store, bus=bus, recorder=None), ClipCutter)


def test_plan_applies_sync_offset(store: ConfigStore, bus: EventBus, tmp_path: Path) -> None:
    cutter = Cutter(store=store, bus=bus, recorder=FakeRecorder(synthetic(), tmp_path))
    segs, off, dur = cutter.plan("alpha", chat(3.0), chat(8.0))
    assert [s.num for s in segs] == [501, 502, 503] and off == pytest.approx(1.0) and dur == 5.0
    store.update({"capture": {"sync_offset_s": 2.0}})          # + moves the clip later
    segs, off, dur = cutter.plan("alpha", chat(3.0), chat(8.0))
    assert [s.num for s in segs] == [502, 503, 504] and off == pytest.approx(1.0) and dur == 5.0
    store.update({"capture": {"sync_offset_s": -2.5}})
    segs, off, _ = cutter.plan("alpha", chat(3.0), chat(8.0))
    assert segs[0].num == 500 and off == pytest.approx(0.5)


def test_plan_rejects_unusable_windows(store: ConfigStore, bus: EventBus, tmp_path: Path) -> None:
    cutter = Cutter(store=store, bus=bus, recorder=FakeRecorder(synthetic(8, skip=(3, 4)), tmp_path))
    with pytest.raises(WindowUnavailable, match="before the buffer"):
        cutter.plan("alpha", chat(-2.0), chat(4.0))
    with pytest.raises(WindowUnavailable, match="gap"):
        cutter.plan("alpha", chat(4.0), chat(11.0))
    with pytest.raises(WindowUnavailable, match="after the buffer"):
        cutter.plan("alpha", chat(12.0), chat(18.0))
    with pytest.raises(WindowUnavailable, match="no buffer"):
        cutter.plan("beta", chat(0.0), chat(2.0))
    cutter.plan("alpha", chat(0.5), chat(5.5))                 # before the gap: fine
    cutter.plan("alpha", chat(10.5), chat(15.5))               # after it: fine


async def test_cut_with_missing_segment_file(store: ConfigStore, bus: EventBus, tmp_path: Path,
                                             monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/true")
    cutter = Cutter(store=store, bus=bus, recorder=FakeRecorder(synthetic(), tmp_path))
    with pytest.raises(WindowUnavailable, match="gone"):
        await cutter.cut("alpha", chat(1.0), chat(3.0), "c1")
    assert not (store.get().data_dir / "clips" / "tmp" / "c1").exists()


def test_cut_cmd_maps_one_video_and_audio() -> None:
    argv = cut_cmd("ffmpeg", ["0000.ts", "0001.ts"], 1.25, 5.0, "out.mp4")
    assert argv[argv.index("-i") + 1] == "concat:0000.ts|0001.ts"
    assert argv[argv.index("-ss") + 1] == "1.250" and argv[argv.index("-t") + 1] == "5.000"
    assert argv.index("-ss") > argv.index("-i")                  # decode, then seek: exact start
    assert "0:v:0" in argv and "0:a:0?" in argv and argv[-1] == "out.mp4"


def test_window_ready_against_a_fed_index(store: ConfigStore, bus: EventBus) -> None:
    """window_ready follows closed segments; the fake buffer ends at media 12 s."""
    rec = Recorder(store=store, bus=bus)
    rec._running = True
    st = rec._stream("alpha")
    st.live = True
    for i in range(5):
        st.index.apply(seg_event(i, f"seg_{i:06d}.ts"))      # closed up to media 10 s
    assert rec.window_ready("alpha", chat(9.9))
    assert not rec.window_ready("alpha", chat(11.0))          # segment 5 not closed yet
    st.index.apply(seg_event(5, "seg_000005.ts"))
    assert rec.window_ready("alpha", chat(11.0))
    store.update({"capture": {"sync_offset_s": 1.5}})
    assert not rec.window_ready("alpha", chat(11.0))
    st.live = False                                           # offline, worker gone
    with pytest.raises(WindowUnavailable, match="offline"):
        rec.window_ready("alpha", chat(11.0))


def test_snapshotter_throttles() -> None:
    snap = Snapshotter("alpha", Path("/nonexistent/alpha.jpg"))
    assert snap.due(2.0)
    snap.last_ts = 1e18
    assert not snap.due(2.0)


# ---- real cuts (ffmpeg) ---------------------------------------------------------------

def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json",
                          str(path)], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def gray_frame(path: Path, at: float) -> bytes:
    """One 64x36 grayscale frame at ``at`` seconds."""
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{at:.3f}", "-i", str(path), "-frames:v", "1",
         "-vf", "scale=64:36", "-pix_fmt", "gray", "-f", "rawvideo", "-"],
        check=True, capture_output=True).stdout


def diff(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / len(a)


@pytest.fixture
def real_buffer(sample_segments: list[Path], tmp_path: Path) -> tuple[BufferIndex, Path]:
    folder = tmp_path / "buffer" / "alpha"
    folder.mkdir(parents=True)
    idx = BufferIndex(streamer="alpha")
    for i, src in enumerate(sample_segments):
        shutil.copyfile(src, folder / src.name)
        idx.apply(seg_event(i, src.name))
    return idx, folder


@needs_ffmpeg
@pytest.mark.parametrize("sync", [0.0, 1.0])
async def test_real_cut_length_and_start(store: ConfigStore, bus: EventBus, sample_video: Path,
                                         real_buffer, sync: float) -> None:
    store.update({"capture": {"sync_offset_s": sync}})
    idx, folder = real_buffer
    cutter = Cutter(store=store, bus=bus, recorder=FakeRecorder(idx, folder))
    out = await cutter.cut("alpha", chat(3.0), chat(8.0), f"clip{int(sync)}")
    assert out == store.get().data_dir / "clips" / "raw" / f"clip{int(sync)}.mp4"
    info = probe(out)
    kinds = {s["codec_type"]: s for s in info["streams"]}
    assert set(kinds) == {"video", "audio"}
    assert float(info["format"]["duration"]) == pytest.approx(5.0, abs=0.5)
    assert float(kinds["audio"]["duration"]) == pytest.approx(float(kinds["video"]["duration"]), abs=0.15)
    first = gray_frame(out, 0.0)
    want = 3.0 + sync
    assert diff(first, gray_frame(sample_video, want)) < diff(first, gray_frame(sample_video, want - 0.5))
    assert diff(first, gray_frame(sample_video, want)) < diff(first, gray_frame(sample_video, want + 0.5))
    assert not (store.get().data_dir / "clips" / "tmp").exists()


@needs_ffmpeg
async def test_real_cut_over_gap_raises(store: ConfigStore, bus: EventBus, sample_segments: list[Path],
                                        tmp_path: Path) -> None:
    idx = BufferIndex(streamer="alpha")
    for i, src in enumerate(sample_segments):
        if i == 2:
            idx.apply({"t": "skip", "num": 500 + i, "date": T0 + 2 * i, "dur": 2.0, "reason": "ad"})
        else:
            idx.apply(seg_event(i, src.name))
    cutter = Cutter(store=store, bus=bus, recorder=FakeRecorder(idx, sample_segments[0].parent))
    with pytest.raises(WindowUnavailable, match="gap"):
        await cutter.cut("alpha", chat(3.0), chat(7.0), "gap")
    assert not (store.get().data_dir / "clips" / "raw" / "gap.mp4").exists()


@needs_ffmpeg
async def test_grab_frame_writes_a_jpeg(sample_segments: list[Path], tmp_path: Path) -> None:
    dest = tmp_path / "snapshots" / "alpha.jpg"
    assert await grab_frame(sample_segments[-1], dest)
    assert dest.read_bytes()[:2] == b"\xff\xd8"
    assert not list(dest.parent.glob("*.tmp.jpg"))
    assert not await grab_frame(tmp_path / "missing.ts", dest)
    assert dest.read_bytes()[:2] == b"\xff\xd8"                 # old frame kept on failure


def test_ffmpeg_failure_message_without_stderr() -> None:
    from livewatcher.capture.cutter import ffmpeg_failure

    msg = ffmpeg_failure("cut", -11, b"", ["ffmpeg", "-i", "concat:a.ts|b.ts", "out.mp4"])
    assert "exit code -11" in msg and "signal 11" in msg and "'concat:a.ts|b.ts'" in msg
    assert ffmpeg_failure("cut", 1, b"boom\n", ["ffmpeg"]) == "ffmpeg cut failed (exit code 1): boom"
