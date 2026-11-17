from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

T0 = 1_800_000_000.0  # Twitch date of the first fake segment


@pytest.fixture(scope="session")
def sample_segments(sample_video: Path, tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    """The 12 s sample video split into six 2 s MPEG-TS segments, like Twitch's
    (keyframe at the start of each, timestamps continuous across files)."""
    d = tmp_path_factory.mktemp("segs")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(sample_video),
         "-c", "copy", "-f", "segment", "-segment_time", "2", "-segment_format", "mpegts",
         str(d / "seg_%06d.ts")],
        check=True)
    segs = sorted(d.glob("seg_*.ts"))
    assert len(segs) == 6, segs
    return segs
