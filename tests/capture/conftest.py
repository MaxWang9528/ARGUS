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
    for seg in segs:
        strip_sdt(seg)
    return segs


def strip_sdt(path: Path) -> None:
    """Drop the SDT packets (PID 0x11) ffmpeg's mpegts muxer writes. Twitch segments
    have none, and the static ffmpeg 7.0.2 build on this machine segfaults (SIGSEGV,
    no stderr) in its mpegts demuxer while parsing an SDT, so this makes the test
    segments look like Twitch's and keeps that crash out of the tests."""
    data = path.read_bytes()
    keep = [data[i:i + 188] for i in range(0, len(data) - 187, 188)
            if ((data[i + 1] & 0x1F) << 8 | data[i + 2]) != 0x11]
    path.write_bytes(b"".join(keep))
