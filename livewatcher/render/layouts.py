"""Layout previews (Pillow) and font lookup.

``draw_preview`` outlines what each layout keeps of a snapshot, using the very same
geometry functions as the filtergraphs in ``ffmpeg_graph.py``:
- ``crop``: the 9:16 slice;
- ``stacked``: the top and bottom regions;
- ``blur_fit``: the trim lines where ``fit_zoom`` cuts the sides off.
"""

from __future__ import annotations

import functools
import io
import logging
import os
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from livewatcher.core.config import LayoutConfig

from .ffmpeg_graph import blur_fit_geometry, crop_geometry, stacked_geometry

log = logging.getLogger("livewatcher.render")

DEFAULT_OUT = (1080, 1920)

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation-sans/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)


@functools.cache
def find_font() -> str | None:
    """A bold sans font file for the title banner, or None if none can be found.

    ``LIVEWATCHER_FONT`` overrides the search."""
    env = os.environ.get("LIVEWATCHER_FONT")
    if env and Path(env).is_file():
        return env
    for cand in FONT_CANDIDATES:
        if Path(cand).is_file():
            return cand
    if shutil.which("fc-match"):
        try:
            res = subprocess.run(["fc-match", "-f", "%{file}", "sans:bold"], capture_output=True,
                                 text=True, timeout=5, check=False)
            path = res.stdout.strip()
            if path and Path(path).is_file() and path.lower().endswith((".ttf", ".otf", ".ttc")):
                return path
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = find_font()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


Box = tuple[float, float, float, float]  # x0, y0, x1, y1 in image pixels

YELLOW = (255, 214, 10)
CYAN = (0, 220, 255)
GREEN = (60, 230, 120)
RED = (255, 70, 70)


def preview_regions(layout: LayoutConfig, size: tuple[int, int],
                    out: tuple[int, int] = DEFAULT_OUT) -> list[tuple[str, Box, tuple[int, int, int]]]:
    """(label, box, colour) for each region the layout keeps, in image pixels."""
    w, h = size
    if layout.layout == "crop":
        r = crop_geometry(layout, size, out).slice
        return [(f"9:16 crop (crop_x {layout.crop_x:.2f})", (r.x, r.y, r.x + r.w, r.y + r.h), YELLOW)]
    if layout.layout == "stacked":
        g = stacked_geometry(layout, size, out)
        share = round(100 * g.top_h / g.out_h)
        out_list = []
        for label, part, colour in ((f"TOP ({share}%)", g.top, YELLOW),
                                    (f"BOTTOM ({100 - share}%)", g.bottom, CYAN)):
            r = part.region
            out_list.append((label, (r.x, r.y, r.x + r.w, r.y + r.h), colour))
        return out_list
    g = blur_fit_geometry(layout, size, out)
    x, y, vw, vh = g.visible
    trimmed = vw < w - 1 or vh < h - 1
    label = (f"fit_zoom {layout.fit_zoom:.2f}: sides trimmed" if trimmed
             else f"fit_zoom {layout.fit_zoom:.2f}: full frame")
    return [(label, (x, y, x + vw, y + vh), RED if trimmed else GREEN)]


def draw_preview(snapshot: Path | str | bytes | Image.Image, layout: LayoutConfig,
                 out: tuple[int, int] = DEFAULT_OUT, quality: int = 85) -> bytes:
    """JPEG bytes of the snapshot with the layout's regions outlined and labelled.
    Everything the render drops is dimmed."""
    if isinstance(snapshot, Image.Image):
        img = snapshot.convert("RGB")
    else:
        src = io.BytesIO(snapshot) if isinstance(snapshot, bytes) else snapshot
        with Image.open(src) as im:
            img = im.convert("RGB")
    w, h = img.size
    regions = preview_regions(layout, (w, h), out)

    # Dim everything outside the kept regions: the mask mixes in black where it is
    # non-zero (about 47% outside, 0 inside).
    mask = Image.new("L", (w, h), 120)
    mdraw = ImageDraw.Draw(mask)
    for _, box, _ in regions:
        mdraw.rectangle(_px(box), fill=0)
    img = Image.composite(Image.new("RGB", (w, h), (0, 0, 0)), img, mask)
    draw = ImageDraw.Draw(img)

    line = max(2, round(min(w, h) / 160))
    font = _font(max(10, round(h / 24)))
    for label, box, colour in regions:
        x0, y0, x1, y1 = _px(box)
        draw.rectangle((x0, y0, x1, y1), outline=colour, width=line)
        if layout.layout == "blur_fit":
            # The trim lines, full height, so they read even when the box is the frame.
            for xx in (x0, x1):
                draw.line((xx, 0, xx, h), fill=colour, width=line)
        _label(draw, (x0 + line + 2, y0 + line + 2), label, colour, font, (w, h))

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _px(box: Box) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = box
    return round(x0), round(y0), max(round(x0), round(x1) - 1), max(round(y0), round(y1) - 1)


def _label(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, colour: tuple[int, int, int],
           font: ImageFont.FreeTypeFont | ImageFont.ImageFont, size: tuple[int, int]) -> None:
    x, y = xy
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    tw, th = r - l, b - t
    pad = max(2, th // 4)
    x = max(0, min(x, size[0] - tw - 2 * pad))
    y = max(0, min(y, size[1] - th - 2 * pad))
    draw.rectangle((x, y, x + tw + 2 * pad, y + th + 2 * pad), fill=(0, 0, 0))
    draw.text((x + pad - l, y + pad - t), text, fill=colour, font=font)
