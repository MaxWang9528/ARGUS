"""Layout previews drawn with Pillow."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from argus.core.config import Config, LayoutConfig, Region
from argus.render import layouts
from argus.render.ffmpeg_graph import crop_geometry
from argus.render.layouts import draw_preview, preview_regions
from argus.render.renderer import FfmpegRenderer

STACKED = Config().layout_for("alpha", "stacked")
VARIANTS = [LayoutConfig(), LayoutConfig(fit_zoom=1.3), LayoutConfig(layout="crop", crop_x=0),
            LayoutConfig(layout="crop", crop_x=1), STACKED,
            LayoutConfig(layout="stacked", stack_top=Region(x=0.8, y=0.8, w=0.9, h=0.9))]


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    img = Image.new("RGB", (1280, 720), (200, 200, 200))
    path = tmp_path / "snap.jpg"
    img.save(path, "JPEG")
    return path


def _jpeg(data: bytes) -> Image.Image:
    assert data[:3] == b"\xff\xd8\xff"
    Image.open(io.BytesIO(data)).verify()
    img = Image.open(io.BytesIO(data))
    assert img.format == "JPEG"
    return img.convert("RGB")


@pytest.mark.parametrize("layout", VARIANTS)
async def test_preview_is_jpeg(snapshot: Path, store, bus, layout: LayoutConfig) -> None:
    data = await FfmpegRenderer(store=store, bus=bus).preview(snapshot, layout)
    assert _jpeg(data).size == (1280, 720)


def test_crop_preview_dims_outside_the_slice(snapshot: Path) -> None:
    layout = LayoutConfig(layout="crop", crop_x=0)
    img = _jpeg(draw_preview(snapshot, layout))
    r = crop_geometry(layout, (1280, 720), (1080, 1920)).slice
    inside = img.getpixel((r.x + r.w // 2, 400))
    outside = img.getpixel((1100, 400))
    assert sum(inside) > 500 and sum(outside) < 400


def test_blur_fit_preview_marks_trim(snapshot: Path) -> None:
    [(label, box, _)] = preview_regions(LayoutConfig(fit_zoom=1.3), (1280, 720))
    assert "trimmed" in label
    assert box[0] == pytest.approx((1280 - 1280 / 1.3) / 2, abs=2)
    [(label, box, _)] = preview_regions(LayoutConfig(fit_zoom=1.0), (1280, 720))
    assert "full frame" in label and box == pytest.approx((0, 0, 1280, 720), abs=1)
    img = _jpeg(draw_preview(snapshot, LayoutConfig(fit_zoom=1.3)))
    assert sum(img.getpixel((20, 400))) < 400  # trimmed edge is dimmed


def test_stacked_preview_regions() -> None:
    regions = preview_regions(STACKED, (1280, 720))
    assert [r[0] for r in regions] == ["TOP (40%)", "BOTTOM (60%)"]
    assert regions[0][1] == (0, 432, 320, 720)


def test_preview_without_font(snapshot: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(layouts, "find_font", lambda: None)
    _jpeg(draw_preview(snapshot, STACKED))


def test_preview_accepts_rgba_png_and_bytes(tmp_path: Path) -> None:
    path = tmp_path / "snap.png"
    Image.new("RGBA", (640, 480), (10, 20, 30, 128)).save(path)
    assert _jpeg(draw_preview(path, LayoutConfig(layout="crop"))).size == (640, 480)
    assert _jpeg(draw_preview(path.read_bytes(), LayoutConfig())).size == (640, 480)


def test_find_font_here() -> None:
    font = layouts.find_font()
    assert font is None or Path(font).is_file()
