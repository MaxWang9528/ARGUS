"""FfmpegRenderer against stub ffmpeg/ffprobe scripts (always run) and against the
real ffmpeg (``needs_ffmpeg``)."""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from livewatcher.core.config import Config, ConfigStore, General, LayoutConfig, Render, Streamer
from livewatcher.core.events import EventBus
from livewatcher.core.interfaces import Renderer
from livewatcher.core.models import Clip
from livewatcher.render import renderer as renderer_mod
from livewatcher.render.renderer import FfmpegRenderer, RenderError, SourceInfo, parse_probe
from tests.conftest import needs_ffmpeg

STACKED = Config().layout_for("alpha", "stacked")


def out_dir(store: ConfigStore) -> Path:
    return store.get().data_dir / "clips" / "out"


def test_implements_interface(store: ConfigStore, bus: EventBus) -> None:
    r = FfmpegRenderer(store=store, bus=bus)  # must not need ffmpeg to construct
    assert isinstance(r, Renderer)


async def test_render_writes_layout_file(stub, store, bus, raw_clip) -> None:
    r = stub.renderer(store, bus)
    other = out_dir(store) / "abc123_crop.mp4"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_bytes(b"the crop render")
    path = await r.render(raw_clip, LayoutConfig(layout="blur_fit"))
    assert path == out_dir(store) / "abc123_blur_fit.mp4"
    assert path.read_bytes().startswith(b"rendered .abc123_blur_fit.")  # written under a temp name
    assert other.read_bytes() == b"the crop render"
    assert sorted(p.name for p in out_dir(store).iterdir()) == ["abc123_blur_fit.mp4", "abc123_crop.mp4"]
    args = stub.events()[0]["args"]
    graph = args[args.index("-filter_complex") + 1]
    assert graph.startswith("[0:v]split=2") and "drawtext=" in graph and "text=alpha:" in graph
    assert args[-1].endswith(".part.mp4") and args[-3:-1] == ["-f", "mp4"]


async def test_title_uses_configured_name_and_can_be_off(stub, tmp_path, bus, raw_clip) -> None:
    cfg = Config(general=General(data_dir=str(tmp_path / "data")), streamers=[Streamer(name="AlphaTV")])
    store = ConfigStore(config=cfg, bus=bus)
    raw_clip.streamer = "alphatv"
    await stub.renderer(store, bus).render(raw_clip, LayoutConfig(layout="crop"))
    store.update({"render": {"burn_title": False}})
    await stub.renderer(store, bus).render(raw_clip, LayoutConfig(layout="crop"))
    graphs = [e["args"][e["args"].index("-filter_complex") + 1] for e in stub.events() if e["event"] == "start"]
    assert "text=AlphaTV:" in graphs[0]
    assert "drawtext" not in graphs[1]


async def test_missing_font_renders_without_title(stub, store, bus, raw_clip, monkeypatch) -> None:
    monkeypatch.setattr(renderer_mod, "find_font", lambda: None)
    await stub.renderer(store, bus).render(raw_clip, LayoutConfig())
    args = stub.events()[0]["args"]
    assert "drawtext" not in args[args.index("-filter_complex") + 1]


async def test_font_error_retries_without_title(stub, store, bus, raw_clip) -> None:
    stub.set(mode="fontfail")
    path = await stub.renderer(store, bus).render(raw_clip, LayoutConfig())
    assert path.is_file()
    starts = [e for e in stub.events() if e["event"] == "start"]
    assert len(starts) == 2
    assert "drawtext" not in starts[1]["args"][starts[1]["args"].index("-filter_complex") + 1]


def test_ffmpeg_args(store, bus) -> None:
    r = FfmpegRenderer(store=store, bus=bus)
    raw, dest = Path("/in/raw.mp4"), Path("/out/x.part.mp4")
    a = r.ffmpeg_args(raw, dest, "G", SourceInfo(1280, 720, 30.0, True, 12.0))
    s = " ".join(a)
    for frag in ("-i /in/raw.mp4", "-filter_complex G", "-map [v]", "-map 0:a:0", "-c:v libx264",
                 "-crf 20", "-pix_fmt yuv420p", "-c:a aac", "-b:a 128k", "-ac 2", "-movflags +faststart",
                 "-nostdin", "-y"):
        assert frag in s
    assert "-r" not in a and "anullsrc" not in s
    assert a[-1] == str(dest)
    # High frame rate is capped at 60; a silent source gets a silent stereo track.
    a = r.ffmpeg_args(raw, dest, "G", SourceInfo(1280, 720, 120.0, False, 7.5))
    s = " ".join(a)
    assert "-r 60" in s and "-t 7.500 -f lavfi -i anullsrc=channel_layout=stereo" in s
    assert "-map 1:a:0" in s and "-shortest" not in a
    a = r.ffmpeg_args(raw, dest, "G", SourceInfo(1280, 720, 59.94, False, None))
    assert "-r" not in a and "-shortest" in a


def test_parse_probe() -> None:
    info = parse_probe({"streams": [{"codec_type": "audio"},
                                    {"codec_type": "video", "width": 1920, "height": 1080,
                                     "avg_frame_rate": "0/0", "r_frame_rate": "60000/1001"}],
                        "format": {"duration": "N/A"}})
    assert (info.width, info.height, info.has_audio, info.duration) == (1920, 1080, True, None)
    assert info.fps == pytest.approx(59.94, abs=0.01)
    with pytest.raises(RenderError):
        parse_probe({"streams": [{"codec_type": "audio"}]})


async def run_three(stub, store, bus, raw_clip) -> list[tuple[float, float]]:
    r = stub.renderer(store, bus)
    stub.set(sleep=0.4)
    layouts = [LayoutConfig(layout=n) for n in ("blur_fit", "crop", "stacked")]
    paths = await asyncio.gather(*(r.render(raw_clip, lay) for lay in layouts))
    assert [p.name for p in paths] == ["abc123_blur_fit.mp4", "abc123_crop.mp4", "abc123_stacked.mp4"]
    assert r.active_renders == 0
    starts = {e["pid"]: e["t"] for e in stub.events() if e["event"] == "start"}
    ends = {e["pid"]: e["t"] for e in stub.events() if e["event"] == "end"}
    return sorted((starts[p], ends[p]) for p in starts)


def max_overlap(spans: list[tuple[float, float]]) -> int:
    return max(sum(1 for s, e in spans if s <= t < e) for t, _ in spans)


async def test_limit_one_runs_one_after_another(stub, store, bus, raw_clip) -> None:
    assert store.get().general.max_concurrent_renders == 1
    spans = await run_three(stub, store, bus, raw_clip)
    assert len(spans) == 3
    for (_, end), (start, _) in itertools.pairwise(spans):
        assert start >= end
    assert max_overlap(spans) == 1


async def test_limit_three_runs_together(stub, store, bus, raw_clip) -> None:
    store.update({"general": {"max_concurrent_renders": 3}})
    spans = await run_three(stub, store, bus, raw_clip)
    assert max_overlap(spans) == 3


async def test_limit_is_read_when_render_starts(stub, store, bus, raw_clip) -> None:
    r = stub.renderer(store, bus)
    stub.set(sleep=0.6)
    tasks = [asyncio.create_task(r.render(raw_clip, LayoutConfig(layout=n)))
             for n in ("blur_fit", "crop", "stacked")]
    await asyncio.sleep(0.2)
    assert r.active_renders == 1
    store.update({"general": {"max_concurrent_renders": 3}})  # the two waiting ones may start now
    await asyncio.gather(*tasks)
    starts = sorted(e["t"] for e in stub.events() if e["event"] == "start")
    ends = sorted(e["t"] for e in stub.events() if e["event"] == "end")
    assert starts[2] < ends[0]  # the third started before the first finished


async def test_failed_render_raises_with_ffmpeg_output(stub, store, bus, raw_clip) -> None:
    stub.set(mode="fail")
    final = out_dir(store) / "abc123_crop.mp4"
    with pytest.raises(RenderError) as err:
        await stub.renderer(store, bus).render(raw_clip, LayoutConfig(layout="crop"))
    msg = str(err.value)
    assert "exit 1" in msg and "Error opening input: boom" in msg
    assert "noise line 39" in msg and "noise line 5\n" not in msg  # only the last lines
    assert err.value.output.endswith("boom")
    assert not final.exists()
    assert list(out_dir(store).iterdir()) == []  # no partial file left


async def test_failed_render_keeps_previous_render(stub, store, bus, raw_clip) -> None:
    final = out_dir(store) / "abc123_crop.mp4"
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"old good render")
    stub.set(mode="fail")
    with pytest.raises(RenderError):
        await stub.renderer(store, bus).render(raw_clip, LayoutConfig(layout="crop"))
    assert final.read_bytes() == b"old good render"
    assert [p.name for p in out_dir(store).iterdir()] == ["abc123_crop.mp4"]


async def test_no_output_written_is_an_error(stub, store, bus, raw_clip) -> None:
    stub.set(mode="empty")
    with pytest.raises(RenderError, match="without writing"):
        await stub.renderer(store, bus).render(raw_clip, LayoutConfig())


async def test_probe_failure(stub, store, bus, raw_clip) -> None:
    stub.set(probe_fail=True)
    with pytest.raises(RenderError, match="Invalid data found"):
        await stub.renderer(store, bus).render(raw_clip, LayoutConfig())
    assert stub.events() == []


async def test_missing_binaries(store, bus, raw_clip) -> None:
    r = FfmpegRenderer(store=store, bus=bus, ffmpeg_cmd=["/nonexistent/ffmpeg"],
                       ffprobe_cmd=["/nonexistent/ffprobe"])
    with pytest.raises(RenderError, match="ffprobe not found"):
        await r.render(raw_clip, LayoutConfig())


async def test_missing_ffmpeg_after_probe(stub, store, bus, raw_clip) -> None:
    r = FfmpegRenderer(store=store, bus=bus, ffmpeg_cmd=["/nonexistent/ffmpeg"],
                       ffprobe_cmd=[sys.executable, str(stub.ffprobe)])
    with pytest.raises(RenderError, match="ffmpeg not found"):
        await r.render(raw_clip, LayoutConfig())
    assert r.active_renders == 0


async def test_missing_raw(store, bus) -> None:
    r = FfmpegRenderer(store=store, bus=bus)
    with pytest.raises(RenderError):
        await r.render(Clip(streamer="alpha", window_start=0, window_end=1), LayoutConfig())
    with pytest.raises(RenderError):
        await r.render(Clip(streamer="alpha", window_start=0, window_end=1, raw_path="/nope.mp4"),
                       LayoutConfig())


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_cancel_kills_ffmpeg(stub, store, bus, raw_clip) -> None:
    stub.set(mode="hang")
    r = stub.renderer(store, bus)
    task = asyncio.create_task(r.render(raw_clip, LayoutConfig()))
    deadline = time.monotonic() + 10
    while not stub.pidfile.exists() or not stub.pidfile.read_text():
        assert time.monotonic() < deadline, "stub never started"
        await asyncio.sleep(0.05)
    pid = int(stub.pidfile.read_text())
    assert _alive(pid)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not _alive(pid)
    assert list(out_dir(store).iterdir()) == []
    assert r.active_renders == 0


async def test_cancel_while_waiting_frees_nothing_it_did_not_take(stub, store, bus, raw_clip) -> None:
    r = stub.renderer(store, bus)
    stub.set(sleep=0.5)
    first = asyncio.create_task(r.render(raw_clip, LayoutConfig(layout="crop")))
    await asyncio.sleep(0.1)
    waiting = asyncio.create_task(r.render(raw_clip, LayoutConfig(layout="stacked")))
    await asyncio.sleep(0.1)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert r.active_renders == 1
    await first
    assert r.active_renders == 0
    assert len([e for e in stub.events() if e["event"] == "start"]) == 1


# ---- real ffmpeg ------------------------------------------------------------------------
def ffprobe_json(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def _real_clip(store: ConfigStore, src: Path, clip_id: str = "real01", streamer: str = "alpha") -> Clip:
    raw = store.get().data_dir / "clips" / "raw" / f"{clip_id}.mp4"
    raw.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, raw)
    return Clip(id=clip_id, streamer=streamer, window_start=0, window_end=12, raw_path=str(raw))


@needs_ffmpeg
@pytest.mark.parametrize("layout", [LayoutConfig(), LayoutConfig(fit_zoom=1.3),
                                    LayoutConfig(layout="crop", crop_x=0), STACKED])
async def test_real_render(sample_video: Path, store, bus, layout: LayoutConfig) -> None:
    clip = _real_clip(store, sample_video)
    r = FfmpegRenderer(store=store, bus=bus)
    t0 = time.monotonic()
    path = await r.render(clip, layout)
    elapsed = time.monotonic() - t0
    print(f"render {layout.layout} fit_zoom={layout.fit_zoom}: {elapsed:.1f} s for 12 s of video")
    assert path == out_dir(store) / f"real01_{layout.layout}.mp4"
    src, info = ffprobe_json(sample_video), ffprobe_json(path)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    assert (video["width"], video["height"]) == (1080, 1920)
    assert video["codec_name"] == "h264" and video["pix_fmt"] == "yuv420p"
    assert audio and audio[0]["codec_name"] == "aac" and audio[0]["channels"] == 2
    assert abs(float(info["format"]["duration"]) - float(src["format"]["duration"])) <= 0.2
    assert video["avg_frame_rate"] in ("30/1", "30000/1000")
    assert [p.name for p in out_dir(store).iterdir()] == [path.name]


@pytest.fixture
def odd_video(tmp_path: Path) -> Path:
    """4:3, 25 fps, no audio, 3 s."""
    src = tmp_path / "odd.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc2=size=640x480:rate=25:duration=3", "-c:v", "libx264",
                    "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(src)], check=True)
    return src


@needs_ffmpeg
async def test_real_render_odd_source_without_audio(odd_video: Path, store, bus) -> None:
    src = odd_video
    r = FfmpegRenderer(store=store, bus=bus)
    for layout in (LayoutConfig(layout="crop", crop_x=1), STACKED, LayoutConfig(fit_zoom=1.3)):
        path = await r.render(_real_clip(store, src, "odd01"), layout)
        info = ffprobe_json(path)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        assert (video["width"], video["height"]) == (1080, 1920)
        assert any(s["codec_type"] == "audio" for s in info["streams"])
        assert abs(float(info["format"]["duration"]) - 3.0) <= 0.2


@needs_ffmpeg
async def test_real_title_with_special_characters(sample_video: Path, tmp_path: Path, bus) -> None:
    name = "we:ird'name, [x]; 100%"
    cfg = Config(general=General(data_dir=str(tmp_path / "data")), render=Render(burn_title=True),
                 streamers=[Streamer(name=name)])
    store = ConfigStore(config=cfg, bus=bus)
    r = FfmpegRenderer(store=store, bus=bus)
    path = await r.render(_real_clip(store, sample_video, streamer=name.lower()), LayoutConfig(layout="crop"))
    assert path.is_file()


@needs_ffmpeg
async def test_real_failure_reports_ffmpeg_message(sample_video: Path, store, bus, monkeypatch) -> None:
    monkeypatch.setattr(renderer_mod, "build_filtergraph", lambda *a, **k: "[0:v]nosuchfilter[v]")
    with pytest.raises(RenderError) as err:
        await FfmpegRenderer(store=store, bus=bus).render(_real_clip(store, sample_video), LayoutConfig())
    assert "nosuchfilter" in str(err.value)
    assert list(out_dir(store).iterdir()) == []
