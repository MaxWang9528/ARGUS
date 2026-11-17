"""Filtergraph builders for the three vertical layouts. Pure functions, no I/O.

Every size and offset is computed here in Python, as an even integer, from the
probed source size. The graphs contain no ``iw``/``ih`` expressions, so there is
nothing for ffmpeg to evaluate except drawtext's ``x=(w-text_w)/2``. The same
geometry functions feed the dashboard preview (``layouts.py``), so the preview
shows exactly what the render does.

The graph reads ``[0:v]`` and ends in ``[v]`` (1080x1920 by default, yuv420p,
square pixels). Audio is mapped by the renderer, not here.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from livewatcher.core.config import LayoutConfig, Region

OUT_LABEL = "[v]"

# Background blur for blur_fit: the frame is shrunk by BG_SHRINK, blurred with
# gblur at BG_SIGMA (in shrunk pixels) and scaled back up. Cheap and very soft.
BG_SHRINK = 4
BG_SIGMA = 20

FIT_ZOOM_RANGE = (0.25, 4.0)


# ---- small helpers ---------------------------------------------------------------
def _clamp(v: float, lo: float, hi: float) -> float:
    if math.isnan(v):
        return lo
    return max(lo, min(hi, v))


def _even(x: float) -> int:
    """Nearest even integer, at least 2."""
    return max(2, round(x / 2.0) * 2)


def _even_floor(x: float) -> int:
    """Largest even integer <= x (and >= 0)."""
    return max(0, math.floor(x + 1e-9) // 2 * 2)


def _even_ceil(x: float) -> int:
    return max(2, math.ceil(x / 2.0 - 1e-9) * 2)


@dataclass(frozen=True)
class Rect:
    """Integer pixel rectangle."""

    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class Cover:
    """Scale a w x h picture up/down to (scale_w, scale_h) so it covers the target,
    then crop ``crop`` out of the scaled picture. ``crop.w/h`` is the target size."""

    scale_w: int
    scale_h: int
    crop: Rect


def cover(src_w: int, src_h: int, tw: int, th: int) -> Cover:
    """Scale-to-cover then centre-crop to exactly tw x th."""
    s = max(tw / src_w, th / src_h)
    sw = max(tw, _even_ceil(src_w * s))
    sh = max(th, _even_ceil(src_h * s))
    return Cover(sw, sh, Rect(_even_floor((sw - tw) / 2), _even_floor((sh - th) / 2), tw, th))


def parse_resolution(text: str) -> tuple[int, int]:
    """``"1080x1920"`` -> (1080, 1920). Rounded to even numbers. Raises ValueError."""
    m = re.fullmatch(r"\s*(\d+)\s*[xX*:]\s*(\d+)\s*", text or "")
    if not m:
        raise ValueError(f"bad render.resolution {text!r}, expected e.g. 1080x1920")
    w, h = int(m.group(1)), int(m.group(2))
    if not (16 <= w <= 8192 and 16 <= h <= 8192):
        raise ValueError(f"render.resolution {text!r} is out of range")
    return _even(w), _even(h)


def _check_sizes(src: tuple[int, int], out: tuple[int, int]) -> tuple[int, int, int, int]:
    sw, sh = int(src[0]), int(src[1])
    ow, oh = int(out[0]), int(out[1])
    if sw < 2 or sh < 2:
        raise ValueError(f"bad source size {src}")
    if ow < 2 or oh < 2 or ow % 2 or oh % 2:
        raise ValueError(f"output size must be even, got {out}")
    return sw, sh, ow, oh


# ---- geometry --------------------------------------------------------------------
@dataclass(frozen=True)
class BlurFitGeometry:
    out_w: int
    out_h: int
    bg: Cover            # source -> small background (out / BG_SHRINK)
    fg_w: int            # source scaled to this size (out_w * fit_zoom wide)
    fg_h: int
    fg_crop: Rect        # the part of the scaled source that is kept
    fg_x: int            # where it goes on the output
    fg_y: int
    # what part of the *source* stays visible, in source pixels (floats)
    visible: tuple[float, float, float, float]


def blur_fit_geometry(layout: LayoutConfig, src: tuple[int, int], out: tuple[int, int]) -> BlurFitGeometry:
    sw, sh, ow, oh = _check_sizes(src, out)
    zoom = _clamp(layout.fit_zoom, *FIT_ZOOM_RANGE)
    bg = cover(sw, sh, _even(ow / BG_SHRINK), _even(oh / BG_SHRINK))
    fw = _even(ow * zoom)
    fh = _even(sh * fw / sw)
    cw, ch = min(fw, ow), min(fh, oh)
    crop = Rect(_even_floor((fw - cw) / 2), _even_floor((fh - ch) / 2), cw, ch)
    fx, fy = _even_floor((ow - cw) / 2), _even_floor((oh - ch) / 2)
    kx, ky = sw / fw, sh / fh
    visible = (crop.x * kx, crop.y * ky, cw * kx, ch * ky)
    return BlurFitGeometry(ow, oh, bg, fw, fh, crop, fx, fy, visible)


@dataclass(frozen=True)
class CropGeometry:
    out_w: int
    out_h: int
    slice: Rect          # in source pixels


def crop_geometry(layout: LayoutConfig, src: tuple[int, int], out: tuple[int, int]) -> CropGeometry:
    """A slice with the output's aspect ratio at full source height (or full width
    when the source is narrower than the output), placed by crop_x."""
    sw, sh, ow, oh = _check_sizes(src, out)
    aspect = ow / oh
    if sw / sh >= aspect:
        cw, ch = min(_even(sh * aspect), _even_floor(sw)), _even_floor(sh)
    else:
        cw, ch = _even_floor(sw), min(_even(sw / aspect), _even_floor(sh))
    cx = _clamp(layout.crop_x, 0.0, 1.0)
    x = min(_even_floor((sw - cw) * cx), _even_floor(sw - cw))
    y = _even_floor((sh - ch) / 2)
    return CropGeometry(ow, oh, Rect(x, y, cw, ch))


def region_rect(region: Region, src: tuple[int, int]) -> Rect:
    """A fractional Region as an even pixel rectangle inside the source frame."""
    sw, sh = int(src[0]), int(src[1])
    x = _clamp(region.x, 0.0, 0.98)
    y = _clamp(region.y, 0.0, 0.98)
    w = _clamp(region.w, 0.02, 1.0 - x)
    h = _clamp(region.h, 0.02, 1.0 - y)
    px, py = _even_floor(x * sw), _even_floor(y * sh)
    pw = max(2, min(_even_floor(w * sw), _even_floor(sw - px)))
    ph = max(2, min(_even_floor(h * sh), _even_floor(sh - py)))
    return Rect(px, py, pw, ph)


@dataclass(frozen=True)
class StackPart:
    region: Rect         # in source pixels
    fit: Cover           # region -> out_w x height


@dataclass(frozen=True)
class StackedGeometry:
    out_w: int
    out_h: int
    top: StackPart
    bottom: StackPart

    @property
    def top_h(self) -> int:
        return self.top.fit.crop.h


def stacked_geometry(layout: LayoutConfig, src: tuple[int, int], out: tuple[int, int]) -> StackedGeometry:
    sw, sh, ow, oh = _check_sizes(src, out)
    top_h = int(_clamp(_even(oh * _clamp(layout.stack_split, 0.0, 1.0)), 2, oh - 2))
    bottom_h = oh - top_h
    parts = []
    for region, th in ((layout.stack_top, top_h), (layout.stack_bottom, bottom_h)):
        r = region_rect(region, (sw, sh))
        parts.append(StackPart(r, cover(r.w, r.h, ow, th)))
    return StackedGeometry(ow, oh, parts[0], parts[1])


# ---- drawtext escaping -----------------------------------------------------------
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def escape_option_value(text: str) -> str:
    """Escape for one filter option value (parsed by av_get_token with ':')."""
    return re.sub(r"([\\':])", r"\\\1", text)


def escape_graph(text: str) -> str:
    """Escape for the filtergraph description (parsed by av_get_token with '[],;')."""
    return re.sub(r"([\\'\[\],;])", r"\\\1", text)


def drawtext_arg(text: str) -> str:
    """A literal string as a drawtext option value inside -filter_complex.

    drawtext runs with ``expansion=none``, so ``%`` and ``{}`` are not special and
    only the two parser levels need escaping."""
    return escape_graph(escape_option_value(text))


def clean_title(text: str | None) -> str:
    return _CONTROL.sub(" ", text or "").strip()[:80]


@dataclass(frozen=True)
class Title:
    text: str
    font_file: str
    y: int
    font_size: int


def _drawtext(t: Title) -> str:
    border = max(4, round(t.font_size * 0.35))
    return ("drawtext="
            f"fontfile={drawtext_arg(t.font_file)}:text={drawtext_arg(t.text)}:expansion=none:"
            f"fontsize={t.font_size}:fontcolor=white:borderw=2:bordercolor=black@0.8:"
            f"box=1:boxcolor=black@0.55:boxborderw={border}:"
            f"x=(w-text_w)/2:y={t.y}")


def _title_size(out_h: int) -> int:
    return max(12, round(out_h * 0.034))


def _finish(title: Title | None) -> str:
    """The tail every graph ends with."""
    head = _drawtext(title) + "," if title else ""
    return f"{head}setsar=1,format=yuv420p{OUT_LABEL}"


def _make_title(title: str | None, font_file: str | None, out_h: int, y_for_size) -> Title | None:
    text = clean_title(title)
    if not text or not font_file:
        return None
    size = _title_size(out_h)
    return Title(text, font_file, max(0, int(y_for_size(size))), size)


# ---- graph builders --------------------------------------------------------------
def blur_fit(layout: LayoutConfig, out: tuple[int, int], title: str | None = None, *,
             src_size: tuple[int, int], font_file: str | None = None) -> str:
    """Full frame (scaled to out_w * fit_zoom, sides trimmed) over a blurred copy."""
    g = blur_fit_geometry(layout, src_size, out)
    bg = g.bg
    # Title sits in the middle of the blurred band above the picture.
    t = _make_title(title, font_file, g.out_h,
                    lambda size: max(round(g.out_h * 0.05), g.fg_y // 2 - size // 2))
    return ";".join([
        "[0:v]split=2[bg][fg]",
        (f"[bg]scale={bg.scale_w}:{bg.scale_h},crop={bg.crop.w}:{bg.crop.h}:{bg.crop.x}:{bg.crop.y},"
         f"gblur=sigma={BG_SIGMA},scale={g.out_w}:{g.out_h}[bgb]"),
        f"[fg]scale={g.fg_w}:{g.fg_h},crop={g.fg_crop.w}:{g.fg_crop.h}:{g.fg_crop.x}:{g.fg_crop.y}[fgs]",
        f"[bgb][fgs]overlay={g.fg_x}:{g.fg_y},{_finish(t)}",
    ])


def crop(layout: LayoutConfig, out: tuple[int, int], title: str | None = None, *,
         src_size: tuple[int, int], font_file: str | None = None) -> str:
    """A 9:16 slice of the frame, placed by crop_x, scaled to fill the output."""
    g = crop_geometry(layout, src_size, out)
    r = g.slice
    t = _make_title(title, font_file, g.out_h, lambda size: round(g.out_h * 0.12))
    return f"[0:v]crop={r.w}:{r.h}:{r.x}:{r.y},scale={g.out_w}:{g.out_h},{_finish(t)}"


def stacked(layout: LayoutConfig, out: tuple[int, int], title: str | None = None, *,
            src_size: tuple[int, int], font_file: str | None = None) -> str:
    """stack_top above stack_bottom, each scaled to cover its share and centre-cropped."""
    g = stacked_geometry(layout, src_size, out)
    chains = ["[0:v]split=2[top][bot]"]
    for name, part in (("top", g.top), ("bot", g.bottom)):
        r, f = part.region, part.fit
        chains.append(f"[{name}]crop={r.w}:{r.h}:{r.x}:{r.y},scale={f.scale_w}:{f.scale_h},"
                      f"crop={f.crop.w}:{f.crop.h}:{f.crop.x}:{f.crop.y}[{name}s]")
    # Title straddles the seam between the two parts.
    t = _make_title(title, font_file, g.out_h, lambda size: g.top_h - size // 2)
    chains.append(f"[tops][bots]vstack=inputs=2,{_finish(t)}")
    return ";".join(chains)


BUILDERS = {"blur_fit": blur_fit, "crop": crop, "stacked": stacked}


def build_filtergraph(layout: LayoutConfig, out: tuple[int, int], title: str | None = None, *,
                      src_size: tuple[int, int], font_file: str | None = None) -> str:
    """The ``-filter_complex`` string for ``layout.layout``.

    ``title`` is burned in only when ``font_file`` is given too (the renderer looks
    one up and leaves the title out if there is none)."""
    try:
        builder = BUILDERS[layout.layout]
    except KeyError:
        raise ValueError(f"unknown layout {layout.layout!r}") from None
    return builder(layout, out, title, src_size=src_size, font_file=font_file)


__all__ = [
    "BUILDERS", "OUT_LABEL", "BlurFitGeometry", "Cover", "CropGeometry", "Rect", "StackedGeometry",
    "blur_fit", "blur_fit_geometry", "build_filtergraph", "clean_title", "cover", "crop",
    "crop_geometry", "drawtext_arg", "escape_graph", "escape_option_value", "parse_resolution",
    "region_rect", "stacked", "stacked_geometry",
]
