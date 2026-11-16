"""Filtergraph strings: snapshots, geometry properties, and a static check.

ffmpeg may not be installed, so besides the snapshots every graph is parsed here
with a re-implementation of ffmpeg's own tokenizer (``av_get_token``) and run
through a frame-size simulator: each crop has to fit the frame it gets, every
label is produced and used once, and the result has to be exactly 1080x1920.
"""

from __future__ import annotations

import itertools
import re

import pytest

from livewatcher.core.config import Config, LayoutConfig, Region
from livewatcher.render.ffmpeg_graph import (
    blur_fit_geometry,
    build_filtergraph,
    crop_geometry,
    drawtext_arg,
    parse_resolution,
    region_rect,
    stacked_geometry,
)

OUT = (1080, 1920)
STACKED = Config().layout_for("x", "stacked")  # the Streamer defaults: facecam + gameplay
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

SNAPSHOTS = {
    "blur_fit_default": (LayoutConfig(), (1280, 720),
        ("[0:v]split=2[bg][fg];[bg]scale=854:480,crop=270:480:292:0,gblur=sigma=20,scale=1080:1920[bgb];"
        "[fg]scale=1080:608,crop=1080:608:0:0[fgs];[bgb][fgs]overlay=0:656,setsar=1,format=yuv420p[v]")),
    "blur_fit_zoom13": (LayoutConfig(fit_zoom=1.3), (1280, 720),
        ("[0:v]split=2[bg][fg];[bg]scale=854:480,crop=270:480:292:0,gblur=sigma=20,scale=1080:1920[bgb];"
        "[fg]scale=1404:790,crop=1080:790:162:0[fgs];[bgb][fgs]overlay=0:564,setsar=1,format=yuv420p[v]")),
    "blur_fit_4x3": (LayoutConfig(), (1440, 1080),
        ("[0:v]split=2[bg][fg];[bg]scale=640:480,crop=270:480:184:0,gblur=sigma=20,scale=1080:1920[bgb];"
        "[fg]scale=1080:810,crop=1080:810:0:0[fgs];[bgb][fgs]overlay=0:554,setsar=1,format=yuv420p[v]")),
    "blur_fit_4x3_zoom13": (LayoutConfig(fit_zoom=1.3), (640, 480),
        ("[0:v]split=2[bg][fg];[bg]scale=640:480,crop=270:480:184:0,gblur=sigma=20,scale=1080:1920[bgb];"
        "[fg]scale=1404:1052,crop=1080:1052:162:0[fgs];[bgb][fgs]overlay=0:434,setsar=1,format=yuv420p[v]")),
    "crop_centre": (LayoutConfig(layout="crop"), (1280, 720),
        ("[0:v]crop=404:720:438:0,scale=1080:1920,setsar=1,format=yuv420p[v]")),
    "crop_left": (LayoutConfig(layout="crop", crop_x=0), (1280, 720),
        ("[0:v]crop=404:720:0:0,scale=1080:1920,setsar=1,format=yuv420p[v]")),
    "crop_right": (LayoutConfig(layout="crop", crop_x=1), (1280, 720),
        ("[0:v]crop=404:720:876:0,scale=1080:1920,setsar=1,format=yuv420p[v]")),
    "crop_right_1080p": (LayoutConfig(layout="crop", crop_x=1), (1920, 1080),
        ("[0:v]crop=608:1080:1312:0,scale=1080:1920,setsar=1,format=yuv420p[v]")),
    "crop_4x3": (LayoutConfig(layout="crop", crop_x=0.25), (640, 480),
        ("[0:v]crop=270:480:92:0,scale=1080:1920,setsar=1,format=yuv420p[v]")),
    "stacked_default": (STACKED, (1280, 720),
        ("[0:v]split=2[top][bot];[top]crop=320:288:0:432,scale=1080:972,crop=1080:768:0:102[tops];"
        "[bot]crop=896:720:192:0,scale=1434:1152,crop=1080:1152:176:0[bots];"
        "[tops][bots]vstack=inputs=2,setsar=1,format=yuv420p[v]")),
    "stacked_4x3": (STACKED, (640, 480),
        ("[0:v]split=2[top][bot];[top]crop=160:192:0:288,scale=1080:1296,crop=1080:768:0:264[tops];"
        "[bot]crop=448:480:96:0,scale=1080:1158,crop=1080:1152:0:2[bots];"
        "[tops][bots]vstack=inputs=2,setsar=1,format=yuv420p[v]")),
    "stacked_split_half": (STACKED.model_copy(update={"stack_split": 0.5}), (1920, 1080),
        ("[0:v]split=2[top][bot];[top]crop=480:432:0:648,scale=1080:972,crop=1080:960:0:6[tops];"
        "[bot]crop=1344:1080:288:0,scale=1196:960,crop=1080:960:58:0[bots];"
        "[tops][bots]vstack=inputs=2,setsar=1,format=yuv420p[v]")),
}


@pytest.mark.parametrize("name", SNAPSHOTS)
def test_snapshot(name: str) -> None:
    layout, src, expected = SNAPSHOTS[name]
    assert build_filtergraph(layout, OUT, src_size=src) == expected


def test_snapshot_with_title() -> None:
    g = build_filtergraph(LayoutConfig(layout="crop"), OUT, "Alpha", src_size=(1280, 720), font_file=FONT)
    assert g == (
        "[0:v]crop=404:720:438:0,scale=1080:1920,"
        f"drawtext=fontfile={FONT}:text=Alpha:expansion=none:fontsize=65:fontcolor=white:"
        "borderw=2:bordercolor=black@0.8:box=1:boxcolor=black@0.55:boxborderw=23:"
        "x=(w-text_w)/2:y=230,setsar=1,format=yuv420p[v]")


def test_title_needs_font() -> None:
    plain = build_filtergraph(LayoutConfig(), OUT, src_size=(1280, 720))
    assert build_filtergraph(LayoutConfig(), OUT, "Alpha", src_size=(1280, 720), font_file=None) == plain
    assert build_filtergraph(LayoutConfig(), OUT, "  \n ", src_size=(1280, 720), font_file=FONT) == plain


def test_fit_zoom_one_loses_nothing() -> None:
    g = blur_fit_geometry(LayoutConfig(fit_zoom=1.0), (1280, 720), OUT)
    assert g.visible == pytest.approx((0, 0, 1280, 720), abs=1)
    g = blur_fit_geometry(LayoutConfig(fit_zoom=1.3), (1280, 720), OUT)
    assert g.visible[2] == pytest.approx(1280 / 1.3, abs=2)


def test_unknown_layout() -> None:
    with pytest.raises(ValueError):
        build_filtergraph(LayoutConfig.model_construct(layout="nope"), OUT, src_size=(1280, 720))


def test_parse_resolution() -> None:
    assert parse_resolution("1080x1920") == (1080, 1920)
    assert parse_resolution(" 720 x 1280 ") == (720, 1280)
    w, h = parse_resolution("1081x1919")
    assert w % 2 == 0 and h % 2 == 0 and abs(w - 1081) == 1 and abs(h - 1919) == 1
    for bad in ("", "1080", "axb", "0x0", "99999x2"):
        with pytest.raises(ValueError):
            parse_resolution(bad)


# ---- a small model of ffmpeg's filtergraph parser -------------------------------------
def av_get_token(s: str, i: int, term: str) -> tuple[str, int]:
    """Port of libavutil/avstring.c av_get_token: returns (token, new index)."""
    ws = " \n\t\r"
    while i < len(s) and s[i] in ws:
        i += 1
    out: list[str] = []
    end = 0
    while i < len(s) and s[i] not in term:
        c = s[i]
        i += 1
        if c == "\\" and i < len(s):
            out.append(s[i])
            i += 1
            end = len(out)
        elif c == "'":
            while i < len(s) and s[i] != "'":
                out.append(s[i])
                i += 1
            if i < len(s):
                i += 1
                end = len(out)
        else:
            out.append(c)
    while len(out) > end and out[-1] in ws:
        out.pop()
    return "".join(out), i


def parse_options(args: str) -> tuple[list[str], dict[str, str]]:
    """Positional values and key=value pairs, as libavfilter's process_options sees them."""
    pos: list[str] = []
    kv: dict[str, str] = {}
    i = 0
    while i < len(args):
        m = re.match(r"[A-Za-z0-9_\-/.]+=", args[i:])
        if m:
            key = m.group(0)[:-1]
            val, i = av_get_token(args, i + len(m.group(0)), ":")
            assert key not in kv, f"duplicate option {key}"
            kv[key] = val
        else:
            assert not kv, f"positional value after named options in {args!r}"
            val, i = av_get_token(args, i, ":")
            pos.append(val)
        if i < len(args):
            assert args[i] == ":"
            i += 1
    return pos, kv


def parse_graph(g: str) -> list[list[dict]]:
    """Chains of filters: {"name", "args", "ins", "outs"}."""
    chains: list[list[dict]] = [[]]
    i = 0
    while i < len(g):
        ins: list[str] = []
        while i < len(g) and g[i] == "[":
            j = g.index("]", i)
            ins.append(g[i + 1:j])
            i = j + 1
        name, i = av_get_token(g, i, "=,;[")
        args = ""
        if i < len(g) and g[i] == "=":
            args, i = av_get_token(g, i + 1, "[],;")
        outs: list[str] = []
        while i < len(g) and g[i] == "[":
            j = g.index("]", i)
            outs.append(g[i + 1:j])
            i = j + 1
        chains[-1].append({"name": name, "args": args, "ins": ins, "outs": outs})
        if i < len(g):
            assert g[i] in ",;", f"unexpected {g[i]!r} at {i} in {g!r}"
            if g[i] == ";":
                chains.append([])
            i += 1
    return chains


ALLOWED = {  # filter -> (max positional, allowed keys)
    "split": (1, set()),
    "scale": (2, set()),
    "crop": (4, set()),
    "gblur": (0, {"sigma"}),
    "overlay": (2, set()),
    "vstack": (0, {"inputs"}),
    "setsar": (1, set()),
    "format": (1, set()),
    "drawtext": (0, {"fontfile", "text", "expansion", "fontsize", "fontcolor", "borderw", "bordercolor",
                     "box", "boxcolor", "boxborderw", "x", "y"}),
}


def simulate(g: str, src: tuple[int, int]) -> tuple[tuple[int, int], dict[str, dict[str, str]]]:
    """Run the graph on frame sizes. Returns the [v] size and the drawtext options."""
    labels: dict[str, tuple[int, int]] = {"0:v": src}
    used: set[str] = set()
    drawtext: dict[str, dict[str, str]] = {}
    for chain in parse_graph(g):
        cur: list[tuple[int, int]] = []
        for n, f in enumerate(chain):
            assert f["name"] in ALLOWED, f"unexpected filter {f['name']}"
            assert n == 0 or not f["ins"], "input label in the middle of a chain"
            assert n == len(chain) - 1 or not f["outs"], "output label in the middle of a chain"
            if f["ins"]:
                for lab in f["ins"]:
                    assert lab in labels, f"label [{lab}] used before it is made"
                    assert lab not in used, f"label [{lab}] used twice"
                    used.add(lab)
                cur = [labels[lab] for lab in f["ins"]]
            pos, kv = parse_options(f["args"])
            max_pos, keys = ALLOWED[f["name"]]
            assert len(pos) <= max_pos and set(kv) <= keys, f"bad options for {f['name']}: {f['args']!r}"
            ints = [int(p) for p in pos] if f["name"] in ("scale", "crop", "overlay") else []
            assert all(v >= 0 and v % 2 == 0 for v in ints), f"odd or negative size in {f}"
            name = f["name"]
            if name == "split":
                assert len(cur) == 1
                cur = cur * int(pos[0])
            elif name == "scale":
                assert len(cur) == 1 and all(v > 0 for v in ints)
                cur = [(ints[0], ints[1])]
            elif name == "crop":
                (w, h), (cw, ch, cx, cy) = cur[0], ints
                assert 0 < cw and 0 < ch and cx + cw <= w and cy + ch <= h, f"crop {ints} outside {w}x{h}"
                cur = [(cw, ch)]
            elif name == "overlay":
                (mw, mh), (ow, oh) = cur
                assert ints[0] + ow <= mw and ints[1] + oh <= mh, "overlay sticks out"
                cur = [(mw, mh)]
            elif name == "vstack":
                assert len(cur) == int(kv["inputs"]) and len({w for w, _ in cur}) == 1
                cur = [(cur[0][0], sum(h for _, h in cur))]
            elif name == "drawtext":
                assert kv["expansion"] == "none"
                drawtext.update({"opts": kv})
            assert len(cur) >= 1
        outs = chain[-1]["outs"]
        assert len(outs) == len(cur), f"{len(cur)} streams but labels {outs}"
        for lab, size in zip(outs, cur, strict=True):
            assert lab not in labels, f"label [{lab}] made twice"
            labels[lab] = size
    unused = set(labels) - used - {"0:v", "v"}
    assert not unused, f"labels never used: {unused}"
    assert "v" in labels and "v" not in used
    return labels["v"], drawtext


SOURCES = [(1280, 720), (1920, 1080), (640, 480), (1440, 1080), (853, 480), (1281, 721), (720, 1280),
           (608, 1080), (2560, 1080), (320, 180)]
LAYOUT_VARIANTS = [
    LayoutConfig(),
    LayoutConfig(fit_zoom=1.3),
    LayoutConfig(fit_zoom=2.5),
    LayoutConfig(fit_zoom=0.0),
    LayoutConfig(fit_zoom=99),
    LayoutConfig(layout="crop", crop_x=0),
    LayoutConfig(layout="crop", crop_x=0.5),
    LayoutConfig(layout="crop", crop_x=1),
    LayoutConfig(layout="crop", crop_x=-3),
    LayoutConfig(layout="crop", crop_x=7),
    STACKED,
    STACKED.model_copy(update={"stack_split": 0.0}),
    STACKED.model_copy(update={"stack_split": 1.0}),
    LayoutConfig(layout="stacked", stack_top=Region(x=0.9, y=0.9, w=0.5, h=0.5),
                 stack_bottom=Region(x=-1, y=2, w=0, h=0)),
    LayoutConfig(layout="stacked", stack_top=Region(x=0.123, y=0.456, w=0.333, h=0.111), stack_split=0.37),
]


@pytest.mark.parametrize(("layout", "src"), list(itertools.product(LAYOUT_VARIANTS, SOURCES)))
@pytest.mark.parametrize("out", [OUT, (720, 1280)])
def test_graph_simulates_to_output_size(layout: LayoutConfig, src: tuple[int, int],
                                        out: tuple[int, int]) -> None:
    g = build_filtergraph(layout, out, "alpha", src_size=src, font_file=FONT)
    size, _ = simulate(g, src)
    assert size == out
    # The only expression left for ffmpeg to evaluate is drawtext's x.
    no_title = build_filtergraph(layout, out, src_size=src)
    assert not re.search(r"\b(iw|ih|ow|oh)\b|[()]", no_title)


TRICKY = ["it's: a [test], ok; 100% \\o/", "'", "\\", ":::", "a\nb\tc", "%{localtime}", "ünïcødé 🎮",
          "  padded  "]


@pytest.mark.parametrize("title", TRICKY)
@pytest.mark.parametrize("layout", ["blur_fit", "crop", "stacked"])
def test_title_escaping_round_trips(title: str, layout: str) -> None:
    font = "C:\\Fonts\\o'k [x], y;z.ttf"
    lc = STACKED if layout == "stacked" else LayoutConfig(layout=layout)
    g = build_filtergraph(lc, OUT, title, src_size=(1280, 720), font_file=font)
    size, dt = simulate(g, (1280, 720))
    assert size == OUT
    opts = dt["opts"]
    expected = re.sub(r"[\x00-\x1f]+", " ", title).strip()
    assert opts["text"] == expected
    assert opts["fontfile"] == font


def test_drawtext_arg_matches_ffmpeg_docs_example() -> None:
    # From ffmpeg-filters "Notes on filtergraph escaping".
    text = "this is a 'string': may contain one, or more, special characters"
    assert drawtext_arg(text) == (
        r"this is a \\\'string\\\'\\: may contain one\, or more\, special characters")


@pytest.mark.parametrize("src", SOURCES)
@pytest.mark.parametrize("crop_x", [0.0, 0.3, 0.5, 1.0])
def test_crop_geometry(src: tuple[int, int], crop_x: float) -> None:
    r = crop_geometry(LayoutConfig(layout="crop", crop_x=crop_x), src, OUT).slice
    assert r.x >= 0 and r.y >= 0 and r.x + r.w <= src[0] and r.y + r.h <= src[1]
    assert abs(r.w / r.h - 9 / 16) < 0.02
    if src[0] / src[1] > 9 / 16:  # wide source: full height
        assert r.h >= src[1] - 1
    if crop_x == 0:
        assert r.x == 0
    if crop_x == 1:
        assert src[0] - (r.x + r.w) <= 2


@pytest.mark.parametrize("src", SOURCES)
def test_stacked_geometry(src: tuple[int, int]) -> None:
    for split in (0.0, 0.25, 0.4, 0.5, 0.99):
        g = stacked_geometry(STACKED.model_copy(update={"stack_split": split}), src, OUT)
        assert g.top.fit.crop.h + g.bottom.fit.crop.h == OUT[1]
        assert g.top.fit.crop.w == g.bottom.fit.crop.w == OUT[0]
        for part in (g.top, g.bottom):
            r = part.region
            assert r.x + r.w <= src[0] and r.y + r.h <= src[1] and r.w > 0 and r.h > 0
            # cover: scaled picture at least as big as the target on both axes
            assert part.fit.scale_w >= part.fit.crop.w and part.fit.scale_h >= part.fit.crop.h


def test_region_rect_clamps() -> None:
    assert region_rect(Region(x=0, y=0, w=1, h=1), (1280, 720)) == region_rect(
        Region(x=-1, y=-1, w=5, h=5), (1280, 720))
    r = region_rect(Region(x=1.0, y=1.0, w=1.0, h=1.0), (1280, 720))
    assert r.x + r.w <= 1280 and r.y + r.h <= 720 and r.w >= 2 and r.h >= 2


@pytest.mark.parametrize("bad", [
    "[0:v]crop=404:720:900:0,scale=1080:1920[v]",                 # crop past the right edge
    "[0:v]crop=404:720:438:0,scale=1080:1921[v]",                 # odd size
    "[0:v]split=2[a][b];[a]scale=1080:1920[v]",                   # [b] never used
    "[0:v]scale=1080:1920[x];[x]scale=1080:1920[x]",              # label made twice, no [v]
    "[0:v]crop=404:720:438:0,scale=1080:1920,drawtext=text=a:b[v]",  # unescaped ':' in text
    "[0:v]crop=404:720:438:0,scale=1080:1920,nosuch=1[v]",
])
def test_simulator_rejects_broken_graphs(bad: str) -> None:
    with pytest.raises((AssertionError, KeyError, ValueError)):
        simulate(bad, (1280, 720))
