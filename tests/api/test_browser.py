"""Browser smoke test: the real page against a running demo App (all fakes).

Skips cleanly when Playwright or Chromium can't start here. Set
``LW_SCREENSHOTS=<folder>`` to save a screenshot of each view.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from livewatcher.app import App
from livewatcher.cli import DEMO_STREAMERS
from livewatcher.core.config import Config, ConfigStore, Detection, General, Streamer

sync_api = pytest.importorskip("playwright.sync_api")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DemoServer:
    """``App(demo=True).run()`` on its own event loop in a thread, on a spare port."""

    def __init__(self, data_dir: Path) -> None:
        self.port = _free_port()
        cfg = Config(
            general=General(data_dir=str(data_dir), dashboard_port=self.port),
            detection=Detection(preroll_s=3, postroll_s=1),
            streamers=[Streamer(name=n) for n in DEMO_STREAMERS])
        self.app = App(ConfigStore(config=cfg), demo=True)
        self.url = f"http://127.0.0.1:{self.port}"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._thread = threading.Thread(target=self._main, daemon=True)

    async def _run(self) -> None:
        self._loop, self._task = asyncio.get_running_loop(), asyncio.current_task()
        await self.app.run()

    def _main(self) -> None:
        # asyncio.run cancels and awaits whatever uvicorn left behind before closing the loop.
        with contextlib.suppress(asyncio.CancelledError):
            asyncio.run(self._run())

    def start(self) -> None:
        self._thread.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.url + "/api/state", timeout=1) as r:
                    if r.status == 200:
                        return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("the demo server didn't start")

    def stop(self) -> None:
        if self._thread.is_alive() and self._loop is not None and self._task is not None:
            self._loop.call_soon_threadsafe(self._task.cancel)
        self._thread.join(timeout=10)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[DemoServer]:
    srv = DemoServer(tmp_path / "data")
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture
def browser() -> Iterator[object]:
    with sync_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - missing browser or system libraries
            pytest.skip(f"Chromium can't launch here: {str(exc).strip().splitlines()[0]}")
        try:
            yield b
        finally:
            b.close()


def test_dashboard_smoke(server: DemoServer, browser) -> None:
    shots = os.environ.get("LW_SCREENSHOTS")
    page = browser.new_page(viewport={"width": 1366, "height": 860})
    errors: list[str] = []
    # Alpine reports template errors with console.warn, so those count too.
    page.on("console", lambda m: errors.append(f"console: {m.text}")
            if m.type == "error" or (m.type == "warning" and "Alpine" in m.text) else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    # The console line doesn't name the resource; these do.
    # ERR_ABORTED is the page itself dropping a request (a <video> whose src changed).
    page.on("requestfailed", lambda r: errors.append(f"requestfailed: {r.url} {r.failure}")
            if "ERR_ABORTED" not in (r.failure or "") else None)
    page.on("response", lambda r: errors.append(f"http {r.status}: {r.url}") if r.status >= 500 else None)

    page.goto(server.url + "/#streams")
    cards = page.locator("[data-testid=stream-card]")
    cards.first.wait_for()
    assert cards.count() == 3
    for name in DEMO_STREAMERS:
        assert page.locator(f"[data-testid=stream-card][data-name={name}]").count() == 1
    # The Twitch category sits next to the title.
    cards.first.locator("[data-testid=category]", has_text="Grand Theft Auto VI").wait_for(timeout=10_000)
    # The fake recorder reports live and writes snapshots; the cards show them.
    page.locator("[data-testid=stream-card] img").first.wait_for(timeout=10_000)
    page.locator("[data-testid=stream-card] .uplot").first.wait_for(timeout=10_000)
    if shots:
        page.wait_for_timeout(3000)  # a few rate samples on the charts
        page.screenshot(path=f"{shots}/streams.png")

    # Arrow keys open the next streamer large.
    page.keyboard.press("ArrowRight")
    page.locator(f"[data-testid=focus][data-name={DEMO_STREAMERS[0]}]").wait_for()
    page.keyboard.press("ArrowRight")
    page.locator(f"[data-testid=focus][data-name={DEMO_STREAMERS[1]}]").wait_for()
    page.keyboard.press("Escape")

    # Clip now -> the clip turns up in To review (auto-upload is off by default).
    first = page.locator(f"[data-testid=stream-card][data-name={DEMO_STREAMERS[0]}]")
    first.locator("[data-testid=clip-now]").click()
    page.locator("[data-testid=nav-clips]").click()
    item = page.locator("[data-testid=clip-item][data-status=pending_review]").first
    item.wait_for(timeout=20_000)
    assert item.locator("[data-testid=approve]").is_enabled()
    assert item.locator("[data-testid=reject]").is_enabled()

    # The player is big and 9:16.
    box = item.locator("[data-testid=clip-player]").bounding_box()
    assert box is not None and box["height"] >= 0.6 * 860
    assert abs(box["width"] / box["height"] - 9 / 16) < 0.02
    chart = item.locator("[data-testid=clip-chart]")
    if shutil.which("ffmpeg"):
        _check_replay(page, item, chart)
    else:  # the demo writes placeholder files: no preview, so no chart either
        item.locator(".replay-chart .chart-empty", has_text="No chat data for this clip").wait_for(timeout=10_000)
    if shots:
        _clip_screenshots(page, server, shots)
    _check_no_chat_data(page, server, shots)

    page.locator("[data-testid=nav-debug]").click()
    page.locator("[data-testid=peak-log] tbody tr").first.wait_for(timeout=5_000)
    assert "Grand Theft Auto VI" in page.locator("[data-testid=health-category]").first.inner_text()
    page.locator(".layout-preview img").wait_for(timeout=10_000)
    # Layout tool: adjust the crop, see the preview follow, save it to the config.
    page.locator(".layout-tool .seg button", has_text="Crop").click()
    page.locator("[data-testid=crop-x]").fill("0.2")
    page.wait_for_function("() => document.querySelector('.layout-preview img').src.includes('crop_x=0.2')")
    save = page.locator("[data-testid=save-layout]")
    assert save.is_enabled()
    save.click()
    page.wait_for_function("() => document.querySelector('[data-testid=save-layout]').disabled")
    saved = server.app.store.get().streamer(DEMO_STREAMERS[0])
    assert saved.layout == "crop" and saved.crop_x == 0.2
    if shots:
        page.screenshot(path=f"{shots}/debug.png", full_page=True)

    # The auto-upload switch saves to the config and survives a reload.
    page.locator("[data-testid=nav-settings]").click()
    switch = page.locator("[data-testid=auto-upload]")
    assert not switch.is_checked()
    page.locator("header .header-switch").click()
    page.wait_for_function("() => document.querySelector('[data-testid=auto-upload-big]').checked")
    assert server.app.store.get().general.auto_upload is True
    if shots:
        page.wait_for_timeout(400)  # let the switch finish moving
        page.screenshot(path=f"{shots}/settings.png")
    page.reload()
    page.wait_for_function("() => document.querySelector('[data-testid=auto-upload]').checked", timeout=10_000)

    # Dark mode renders too.
    if shots:
        page.emulate_media(color_scheme="dark")
        page.locator("[data-testid=nav-streams]").click()
        page.wait_for_timeout(500)
        page.screenshot(path=f"{shots}/streams-dark.png")
        page.set_viewport_size({"width": 390, "height": 800})
        page.wait_for_timeout(300)
        page.screenshot(path=f"{shots}/streams-narrow.png")

    # Losing the server shows the banner (the page keeps retrying by itself).
    # Everything up to here must be clean. After the stop, responses cut off mid-send
    # (length mismatches, 500s from the cancelled app) and failed reconnects are expected.
    assert errors == []
    page.set_viewport_size({"width": 1366, "height": 860})
    server.stop()
    page.locator(".banner", has_text="Lost the connection").wait_for(timeout=10_000)
    assert [e for e in errors if "pageerror" in e or "Alpine" in e] == []


def _check_replay(page, item, chart) -> None:
    """The chart replays with the video: the playhead follows playback and seeking."""
    chart.locator(".uplot").wait_for(timeout=10_000)
    video = item.locator("video")
    video.evaluate("v => v.readyState >= 1 || new Promise(r => v.addEventListener('loadedmetadata', r, { once: true }))")
    el = chart.element_handle()
    x0 = float(chart.get_attribute("data-playhead-x"))
    video.evaluate("v => { v.muted = true; return v.play(); }")
    # requestAnimationFrame moves the drawn playhead while the video plays.
    page.wait_for_function("el => parseFloat(el.dataset.playhead) > 0.8", arg=el, timeout=10_000)
    x1 = float(chart.get_attribute("data-playhead-x"))
    assert x1 > x0
    page.wait_for_function(f"el => parseFloat(el.dataset.playheadX) > {x1 + 2}", arg=el, timeout=10_000)
    # Paused, it stays put.
    video.evaluate("v => v.pause()")
    page.wait_for_timeout(100)
    held = chart.get_attribute("data-playhead-x")
    page.wait_for_timeout(400)
    assert chart.get_attribute("data-playhead-x") == held
    # Clicking the chart seeks the video, and the playhead lands where the click was.
    w = chart.bounding_box()["width"]
    target = round(w * 0.45)
    chart.locator(".u-over").click(position={"x": target - _over_left(chart), "y": 40})
    page.wait_for_function(f"el => Math.abs(parseFloat(el.dataset.playheadX) - {target}) < 3", arg=el, timeout=5_000)
    t = video.evaluate("v => v.currentTime")
    assert abs(t - float(chart.get_attribute("data-playhead"))) < 0.05 and t > 0
    # Dragging seeks as it goes.
    ob, cb = chart.locator(".u-over").bounding_box(), chart.bounding_box()
    y = ob["y"] + ob["height"] / 2
    page.mouse.move(ob["x"] + ob["width"] * 0.4, y)
    page.mouse.down()
    page.mouse.move(ob["x"] + ob["width"] * 0.6, y, steps=6)
    page.mouse.up()
    end = ob["x"] + ob["width"] * 0.6 - cb["x"]
    page.wait_for_function(f"el => Math.abs(parseFloat(el.dataset.playheadX) - {end}) < 3", arg=el, timeout=5_000)
    assert video.evaluate("v => v.currentTime") > t
    # Scrubbing the paused video moves the playhead too.
    video.evaluate("v => { v.currentTime = 0.5; }")
    page.wait_for_function("el => Math.abs(parseFloat(el.dataset.playhead) - 0.5) < 0.02", arg=el, timeout=5_000)


def _over_left(chart) -> float:
    return chart.locator(".u-over").evaluate("o => o.offsetLeft")


def _clip_screenshots(page, server: DemoServer, shots: str) -> None:
    """Swap the demo's 16:9 test clip for a real 1080x1920 one, then shoot the Clips tab."""
    if not shutil.which("ffmpeg"):
        page.screenshot(path=f"{shots}/clips.png")
        return
    with urllib.request.urlopen(server.url + "/api/clips") as r:
        clip = next(c for c in json.load(r) if c["status"] == "pending_review")
    dur = max(clip["window_end"] - clip["window_start"], 1)
    out = Path(clip["renders"][clip["active_layout"]])
    tmp = out.with_suffix(".tall.mp4")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"testsrc=size=1080x1920:rate=30:duration={dur:.2f}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(tmp)], check=True)
    tmp.replace(out)
    page.reload()
    item = page.locator(f"[data-testid=clip-item][data-id='{clip['id']}']")
    chart = item.locator("[data-testid=clip-chart] .uplot")
    chart.wait_for(timeout=10_000)
    video = item.locator("video")
    video.evaluate("v => v.readyState >= 1 || new Promise(r => v.addEventListener('loadedmetadata', r, { once: true }))")
    video.evaluate("v => new Promise(r => { v.addEventListener('seeked', r, { once: true }); v.currentTime = v.duration * 0.55; })")
    page.wait_for_timeout(300)
    page.screenshot(path=f"{shots}/clips.png")
    page.emulate_media(color_scheme="dark")
    page.wait_for_timeout(400)
    page.screenshot(path=f"{shots}/clips-dark.png")
    page.set_viewport_size({"width": 390, "height": 800})
    page.wait_for_timeout(400)
    page.screenshot(path=f"{shots}/clips-narrow.png", full_page=True)
    page.set_viewport_size({"width": 820, "height": 1000})
    page.wait_for_timeout(400)
    page.screenshot(path=f"{shots}/clips-mid.png")
    page.set_viewport_size({"width": 1366, "height": 860})
    page.emulate_media(color_scheme="light")


def _check_no_chat_data(page, server: DemoServer, shots: str | None) -> None:
    """No rates, or no playable file: the chart says so quietly."""
    with urllib.request.urlopen(server.url + "/api/clips") as r:
        clip = next(c for c in json.load(r) if c["status"] == "pending_review")
    item = page.locator(f"[data-testid=clip-item][data-id='{clip['id']}']")
    empty = item.locator(".replay-chart .chart-empty", has_text="No chat data for this clip")
    page.route("**/api/clips/*/rates", lambda route: route.fulfill(json=[]))
    page.reload()
    empty.wait_for(timeout=10_000)
    assert item.locator("video").count() == 1  # the video still plays, there's just no chart
    page.unroute("**/api/clips/*/rates")
    # A render that isn't video: "No preview", and no chart either.
    # Served through a route, not by overwriting the file: the browser may replay the
    # earlier, cached response for the same URL after a reload.
    page.route("**/api/clips/*/video*", lambda route: route.fulfill(
        status=200, content_type="video/mp4", body=b"not a video"))
    page.reload()
    item.locator(".no-preview").wait_for(timeout=10_000)
    empty.wait_for(timeout=10_000)
    if shots:
        page.screenshot(path=f"{shots}/clips-no-data.png")
