"""Stub ffmpeg/ffprobe executables, so the renderer's subprocess handling (limits,
errors, cancelling, temp files) is tested for real without ffmpeg installed."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from livewatcher.core.config import ConfigStore
from livewatcher.core.events import EventBus
from livewatcher.core.models import Clip
from livewatcher.render.renderer import FfmpegRenderer

STUB_FFMPEG = textwrap.dedent('''
    import json, os, sys, time
    cfg = json.loads(open(os.environ["LW_STUB_CFG"]).read())
    args = sys.argv[1:]
    dest = args[-1]
    log = cfg["log"]
    graph = args[args.index("-filter_complex") + 1] if "-filter_complex" in args else ""
    with open(log, "a") as fh:
        fh.write(json.dumps({"event": "start", "t": time.time(), "pid": os.getpid(), "args": args}) + "\\n")
    mode = cfg.get("mode", "ok")
    if mode == "fontfail" and "drawtext" in graph:
        mode = "fail_font"
    if mode == "fail":
        open(dest, "wb").write(b"partial")
        for i in range(40):
            print(f"noise line {i}", file=sys.stderr)
        print("[in#0] Error opening input: boom", file=sys.stderr)
        sys.exit(1)
    if mode == "fail_font":
        print("[Parsed_drawtext_3] Cannot find a valid font for the family Sans", file=sys.stderr)
        print("Error initializing filter 'drawtext'", file=sys.stderr)
        sys.exit(1)
    if mode == "hang":
        open(dest, "wb").write(b"partial")
        open(cfg["pidfile"], "w").write(str(os.getpid()))
        time.sleep(60)
    if mode == "empty":
        sys.exit(0)
    time.sleep(cfg.get("sleep", 0.0))
    open(dest, "wb").write(b"rendered " + os.path.basename(dest).encode())
    with open(log, "a") as fh:
        fh.write(json.dumps({"event": "end", "t": time.time(), "pid": os.getpid()}) + "\\n")
''')

STUB_FFPROBE = textwrap.dedent('''
    import json, os, sys
    cfg = json.loads(open(os.environ["LW_STUB_CFG"]).read())
    if cfg.get("probe_fail"):
        print("stub.mp4: Invalid data found when processing input", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(cfg["probe"]))
''')

DEFAULT_PROBE = {
    "streams": [
        {"codec_type": "video", "width": 1280, "height": 720, "r_frame_rate": "30/1",
         "avg_frame_rate": "30/1"},
        {"codec_type": "audio"},
    ],
    "format": {"duration": "12.000000"},
}


class Stub:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.cfg_path = root / "stub.json"
        self.log = root / "stub.log"
        self.pidfile = root / "stub.pid"
        self.ffmpeg = root / "stub_ffmpeg.py"
        self.ffprobe = root / "stub_ffprobe.py"
        self.ffmpeg.write_text(STUB_FFMPEG)
        self.ffprobe.write_text(STUB_FFPROBE)
        self.set()

    def set(self, **cfg: object) -> None:
        full = {"log": str(self.log), "pidfile": str(self.pidfile), "probe": DEFAULT_PROBE, **cfg}
        self.cfg_path.write_text(json.dumps(full))

    def events(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def renderer(self, store: ConfigStore, bus: EventBus) -> FfmpegRenderer:
        return FfmpegRenderer(store=store, bus=bus, ffmpeg_cmd=[sys.executable, str(self.ffmpeg)],
                              ffprobe_cmd=[sys.executable, str(self.ffprobe)])


@pytest.fixture
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Stub:
    s = Stub(tmp_path)
    monkeypatch.setenv("LW_STUB_CFG", str(s.cfg_path))
    return s


@pytest.fixture
def raw_clip(store: ConfigStore) -> Clip:
    """A clip whose raw file exists (contents don't matter to the stubs)."""
    raw = store.get().data_dir / "clips" / "raw" / "abc123.mp4"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"raw")
    return Clip(id="abc123", streamer="alpha", window_start=0, window_end=12, raw_path=str(raw))
