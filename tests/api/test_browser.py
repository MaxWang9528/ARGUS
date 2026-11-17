"""Browser smoke test: the real page against a running demo App (all fakes).

Skips cleanly when Playwright or Chromium can't start here. Set
``LW_SCREENSHOTS=<folder>`` to save a screenshot of each view.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from argus.app import App
from argus.cli import DEMO_STREAMERS
from argus.core.config import Config, ConfigStore, Detection, General, Streamer
from argus.core.events import Topic
from argus.core.fakes import FakeRecorder
from argus.core.models import Clip, PeakEvent

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


def _watch_errors(page) -> list[str]:
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
    return errors


class AccountsApi:
    """A stand-in for ``/api/accounts`` and ``/api/upload`` (another branch builds them),
    in the CONTRACTS shapes. The account list comes from the server's real config, so
    accounts added through the real ``PATCH /api/config`` show up."""

    def __init__(self, server: DemoServer) -> None:
        self.server = server
        self.state: dict[str, dict] = {}  # name -> {"logged_in", "login_open"}
        self.logins: list[str] = []
        self.upload = {"cycle": False, "next_account": "default", "queued": 0,
                       "waiting_reason": None, "paused_reason": None}

    def install(self, page) -> None:
        page.route(re.compile(r"/api/accounts$"), self._list)
        page.route(re.compile(r"/api/accounts/[^/]+/login$"), self._login)
        page.route(re.compile(r"/api/accounts/[^/]+/check$"), self._check)
        page.route(re.compile(r"/api/upload$"), self._upload)

    def rows(self) -> list[dict]:
        now = time.time()
        out = []
        for a in self.server.app.store.get().tiktok_accounts():
            st = self.state.get(a.name, {})
            out.append({
                "name": a.name, "enabled": a.enabled, "profile_dir": a.browser_profile_dir,
                "logged_in": st.get("logged_in"), "login_open": st.get("login_open", False),
                "uploads_24h": 2, "max_uploads_per_day": a.max_uploads_per_day,
                "min_minutes_between_uploads": a.min_minutes_between_uploads,
                "last_upload_ts": now - 600, "next_free_ts": now + 35 * 60})
        return out

    def _list(self, route) -> None:
        route.fulfill(json=self.rows())

    def _login(self, route) -> None:
        name = route.request.url.rsplit("/", 2)[-2]
        self.logins.append(name)
        self.state.setdefault(name, {})["login_open"] = True
        route.fulfill(status=202, json={"login_open": True})

    def _check(self, route) -> None:
        name = route.request.url.rsplit("/", 2)[-2]
        route.fulfill(json={"logged_in": bool(self.state.get(name, {}).get("logged_in"))})

    def _upload(self, route) -> None:
        self.upload["cycle"] = self.server.app.store.get().tiktok.cycle_accounts
        route.fulfill(json=self.upload)


def _by_viewers(names: list[str]) -> list[str]:
    return sorted(names, key=FakeRecorder._fake_viewers, reverse=True)


def _wait_for(check, timeout: float = 10) -> None:
    """Polls a server-side condition (the page can't wait on those)."""
    deadline = time.time() + timeout
    while not check():
        if time.time() > deadline:
            raise AssertionError("timed out waiting for the server")
        time.sleep(0.05)


def test_dashboard_smoke(server: DemoServer, browser) -> None:
    shots = os.environ.get("LW_SCREENSHOTS")
    page = browser.new_page(viewport={"width": 1366, "height": 860})
    errors = _watch_errors(page)
    AccountsApi(server).install(page)

    page.goto(server.url + "/#streams")
    cards = page.locator("[data-testid=stream-card]")
    cards.first.wait_for()
    assert cards.count() == 3
    for name in DEMO_STREAMERS:
        assert page.locator(f"[data-testid=stream-card][data-name={name}]").count() == 1
    # Sorted by viewers, highest first, with the counts shown.
    order = _by_viewers(DEMO_STREAMERS)
    page.wait_for_function(
        "want => [...document.querySelectorAll('[data-testid=stream-card]')].map(c => c.dataset.name).join() === want",
        arg=",".join(order))
    top = FakeRecorder._fake_viewers(order[0])
    cards.first.locator("[data-testid=viewers]", has_text=f"{top / 1000:.1f}K viewers").wait_for()
    # The Twitch category sits next to the title.
    cards.first.locator("[data-testid=category]", has_text="Grand Theft Auto VI").wait_for(timeout=10_000)
    # The fake recorder reports live and writes snapshots; the cards show them.
    page.locator("[data-testid=stream-card] img").first.wait_for(timeout=10_000)
    page.locator("[data-testid=stream-card] .uplot").first.wait_for(timeout=10_000)
    if shots:
        page.wait_for_timeout(3000)  # a few rate samples on the charts
        page.screenshot(path=f"{shots}/streams.png")

    # Arrow keys open the next streamer large, in the cards' order.
    page.keyboard.press("ArrowRight")
    page.locator(f"[data-testid=focus][data-name={order[0]}]").wait_for()
    page.keyboard.press("ArrowRight")
    page.locator(f"[data-testid=focus][data-name={order[1]}]").wait_for()
    page.keyboard.press("Escape")

    # Clip now -> the clip turns up in To review (auto-upload is off by default).
    first = page.locator(f"[data-testid=stream-card][data-name={DEMO_STREAMERS[0]}]")
    first.locator("[data-testid=clip-now]").click()
    page.locator("[data-testid=nav-clips]").click()
    item = page.locator("[data-testid=clip-item][data-status=pending_review]").first
    item.wait_for(timeout=20_000)
    assert item.locator("[data-testid=approve]").is_enabled()
    assert item.locator("[data-testid=reject]").is_enabled()
    item.locator("[data-testid=clip-score]", has_text="manual").wait_for()  # Clip now makes manual clips

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
    # The button disables as soon as the save starts, so wait for the config itself.
    for _ in range(50):
        saved = server.app.store.get().streamer(DEMO_STREAMERS[0])
        if saved.layout == "crop":
            break
        page.wait_for_timeout(100)
    assert saved.layout == "crop" and saved.crop_x == 0.2
    if shots:
        page.screenshot(path=f"{shots}/debug.png", full_page=True)

    # The auto-upload switch saves to the config and survives a reload.
    page.locator("[data-testid=nav-settings]").click()
    switch = page.locator("[data-testid=auto-upload]")
    assert not switch.is_checked()
    page.locator("header .header-switch", has_text="Auto-upload").click()
    page.wait_for_function("() => document.querySelector('[data-testid=auto-upload-big]').checked")
    assert server.app.store.get().general.auto_upload is True
    # Auto-clip switch: off saves to the config, on again restores it
    clip_switch = page.locator("[data-testid=auto-clip]")
    assert clip_switch.is_checked()
    page.locator("header .header-switch", has_text="Auto-clip").click()
    for _ in range(50):
        if server.app.store.get().general.auto_clip is False:
            break
        page.wait_for_timeout(100)
    assert server.app.store.get().general.auto_clip is False
    page.locator("header .header-switch", has_text="Auto-clip").click()
    for _ in range(50):
        if server.app.store.get().general.auto_clip is True:
            break
        page.wait_for_timeout(100)
    assert server.app.store.get().general.auto_clip is True
    if shots:
        page.wait_for_timeout(400)  # let the switch finish moving
        page.screenshot(path=f"{shots}/settings.png")
    page.reload()
    page.wait_for_function("() => document.querySelector('[data-testid=auto-upload]').checked", timeout=10_000)

    # Max streamers recorded saves to the config.
    box = page.locator("[data-testid=max-streamers]")
    with page.expect_response(lambda r: r.request.method == "PATCH" and r.url.endswith("/api/config")) as resp:
        box.fill("12")
        box.press("Enter")
    assert resp.value.ok and resp.value.request.post_data_json == {"general": {"max_streamers": 12}}
    assert server.app.store.get().general.max_streamers == 12
    # And the queue age, with the upload limits.
    page.locator("[data-testid=max-queue-age]").fill("3")
    with page.expect_response(lambda r: r.request.method == "PATCH" and r.url.endswith("/api/config")) as resp:
        page.locator(".limits button[type=submit]").click()
    assert resp.value.ok and server.app.store.get().tiktok.max_queue_age_h == 3

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


def _shoot(page, shots: str | None, name: str, full_page: bool = False) -> None:
    """Light, dark and 390 px narrow screenshots of what's on screen now."""
    if not shots:
        return
    size = page.viewport_size
    page.emulate_media(color_scheme="light")
    page.evaluate("() => document.fonts.ready")
    # No toasts, focus rings or scrolled sticky header in the pictures.
    page.evaluate("() => { Alpine.$data(document.body).toasts = []; document.activeElement?.blur(); scrollTo(0, 0); }")
    page.locator(".toast").first.wait_for(state="detached")
    page.screenshot(path=f"{shots}/{name}.png", full_page=full_page)
    page.emulate_media(color_scheme="dark")
    page.screenshot(path=f"{shots}/{name}-dark.png", full_page=full_page)
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(path=f"{shots}/{name}-narrow.png", full_page=True)
    page.set_viewport_size(size)
    page.emulate_media(color_scheme="light")


def _fake_streamers(n_live: int, n_offline: int) -> list[dict]:
    """/api/state entries for streamers the demo doesn't have: live but over max_streamers,
    and offline. No snapshot and no playlist, so the page asks for nothing but their rates."""
    games = ["Just Chatting", "Grand Theft Auto VI", "Counter-Strike 2", "League of Legends", "Fortnite"]
    out = []
    for i in range(n_live):
        out.append({"name": f"live_extra_{i:02d}", "enabled": True, "has_playlist": False, "has_snapshot": False,
                    "status": {"streamer": f"live_extra_{i:02d}", "live": True, "watched": False,
                               "title": f"Stream number {i}", "category": games[i % len(games)],
                               "viewers": None if i == n_live - 1 else 900 + i * 731,
                               "recorder_state": "stopped", "buffer_start": None, "buffer_end": None, "last_error": ""}})
    for i in range(n_offline):
        out.append({"name": f"off_{i:02d}", "enabled": i % 7 != 3, "has_playlist": False, "has_snapshot": False,
                    "status": {"streamer": f"off_{i:02d}", "live": False, "watched": True, "title": "",
                               "category": games[i % len(games)] if i % 2 else "", "viewers": None,
                               "recorder_state": "stopped", "buffer_start": None, "buffer_end": None, "last_error": ""}})
    return out


def test_streams_grouped_by_recording(server: DemoServer, browser) -> None:
    """About 60 streamers: recorded cards by viewers, the rest in two collapsed lists."""
    shots = os.environ.get("LW_SCREENSHOTS")
    server.app.store.update({"general": {"max_streamers": 3}})
    extra = _fake_streamers(20, 37)
    names = {e["name"] for e in extra}

    def state(route) -> None:
        body = route.fetch().json()
        body["streamers"] += extra
        route.fulfill(json=body)

    def rates(route) -> None:
        if route.request.url.split("/api/streamers/")[1].split("/")[0] in names:
            route.fulfill(json=[])
        else:
            route.continue_()

    page = browser.new_page(viewport={"width": 1366, "height": 900})
    errors = _watch_errors(page)
    AccountsApi(server).install(page)
    page.route(re.compile(r"/api/state$"), state)
    page.route(re.compile(r"/api/streamers/[^/]+/rates$"), rates)
    page.goto(server.url + "/#streams")

    order = _by_viewers(DEMO_STREAMERS)
    page.wait_for_function(
        "want => [...document.querySelectorAll('[data-testid=stream-card]')].map(c => c.dataset.name).join() === want",
        arg=",".join(order))
    unwatched = page.locator("[data-testid=group-unwatched]")
    offline = page.locator("[data-testid=group-offline]")
    unwatched.locator("summary", has_text="Live, not recorded (20)").wait_for()
    offline.locator("summary", has_text="Offline (37)").wait_for()
    # Collapsed: no rows, no charts or video for them.
    assert page.locator("[data-testid=unwatched-row]").count() == 0
    assert page.locator("[data-testid=offline-row]").count() == 0
    assert page.locator(".chart").count() == 3

    unwatched.locator("summary").click()
    rows = page.locator("[data-testid=unwatched-row]")
    rows.first.wait_for()
    assert rows.count() == 20
    # Highest viewers first; the one with an unknown count last.
    got = rows.evaluate_all("rs => rs.map(r => r.dataset.name)")
    assert got[0] == "live_extra_18" and got[-1] == "live_extra_19"
    assert rows.first.locator("[data-testid=viewers]").inner_text() == f"{(900 + 18 * 731) / 1000:.1f}K viewers"
    assert rows.first.locator(".category").inner_text() == "League of Legends"
    assert page.locator("[data-testid=unwatched-row] video, [data-testid=unwatched-row] .chart").count() == 0
    offline.locator("summary").click()
    page.locator("[data-testid=offline-row]").nth(36).wait_for()
    # Turned-off streamers come after the watched ones.
    last = page.locator("[data-testid=offline-row]").last
    assert "turned off" in last.inner_text()

    # Arrow keys walk the recorded cards only, and wrap.
    for name in [*order, order[0]]:
        page.keyboard.press("ArrowRight")
        page.locator(f"[data-testid=focus][data-name={name}]").wait_for()
    page.keyboard.press("ArrowLeft")
    page.locator(f"[data-testid=focus][data-name={order[-1]}]").wait_for()
    page.keyboard.press("Escape")
    page.locator("[data-testid=focus]").wait_for(state="detached")

    # A stream_status event over the WebSocket moves a streamer out of the cards when it
    # stops being recorded (the fake recorder republishes every couple of seconds).
    real_status = server.app.recorder.status
    server.app.recorder.status = lambda name: real_status(name).model_copy(
        update={"watched": name.lower() != order[-1]})
    page.locator(f"[data-testid=stream-card][data-name={order[-1]}]").wait_for(state="detached", timeout=15_000)
    unwatched.locator("summary", has_text="Live, not recorded (21)").wait_for()
    page.locator(f"[data-testid=unwatched-row][data-name={order[-1]}]").click()
    page.locator(f"[data-testid=focus][data-name={order[-1]}] [data-testid=clip-now]").wait_for()
    assert page.locator("[data-testid=focus] [data-testid=clip-now]").is_disabled()
    page.keyboard.press("Escape")
    server.app.recorder.status = real_status
    page.locator(f"[data-testid=stream-card][data-name={order[-1]}]").wait_for(timeout=15_000)
    page.locator("[data-testid=stream-card]").first.locator(".uplot").wait_for(timeout=10_000)
    if shots:
        page.locator("[data-testid=stream-card] img").first.wait_for(timeout=10_000)
        _shoot(page, shots, "streams", full_page=True)
    assert errors == []


def test_accounts_page(server: DemoServer, browser) -> None:
    shots = os.environ.get("LW_SCREENSHOTS")
    page = browser.new_page(viewport={"width": 1366, "height": 900})
    errors = _watch_errors(page)
    mock = AccountsApi(server)
    mock.upload.update(queued=4, waiting_reason="every account is at its daily limit")
    mock.install(page)
    page.goto(server.url + "/#accounts")

    # No accounts in the config: the implicit default one, with a note.
    default = page.locator("[data-testid=account][data-name=default]")
    default.wait_for()
    page.locator("[data-testid=implicit-note]", has_text="Adding an account replaces it").wait_for()
    default.locator("[data-testid=login-state]", has_text="Not checked").wait_for()
    assert default.locator("[data-testid=uploads-24h]").inner_text().split() == ["2", "of", "6"]
    assert default.locator("[data-testid=next-slot]").inner_text().startswith("Next slot in 3")
    assert default.locator("[data-testid=delete]").count() == 0
    page.locator("[data-testid=upload-reason]", has_text="Waiting: every account is at its daily limit").wait_for()
    page.locator(".toolbar .sub", has_text="4 clips queued, next goes to default").wait_for()

    # A bad name is refused before anything is sent.
    page.locator("[data-testid=acct-name]").fill("has space")
    page.locator("[data-testid=acct-add]").click()
    page.locator(".add-account .bad-text", has_text="no spaces").wait_for()

    # Add an account: the whole list goes in one config patch.
    page.locator("[data-testid=acct-name]").fill("clips_main")
    page.locator("[data-testid=acct-new-max]").fill("4")
    page.locator("[data-testid=acct-new-gap]").fill("30")
    with page.expect_response(lambda r: r.request.method == "PATCH" and r.url.endswith("/api/config")) as resp:
        page.locator("[data-testid=acct-add]").click()
    assert resp.value.ok
    assert resp.value.request.post_data_json == {"tiktok": {"accounts": [
        {"name": "clips_main", "enabled": True, "max_uploads_per_day": 4, "min_minutes_between_uploads": 30}]}}
    row = page.locator("[data-testid=account][data-name=clips_main]")
    row.locator("[data-testid=login-state]", has_text="Not checked").wait_for()
    default.wait_for(state="detached")
    assert page.locator("[data-testid=implicit-note]").count() == 0

    # A second one goes after it.
    page.locator("[data-testid=acct-name]").fill("clips.alt")
    with page.expect_response(lambda r: r.request.method == "PATCH" and r.url.endswith("/api/config")) as resp:
        page.locator("[data-testid=acct-add]").click()
    assert [a["name"] for a in resp.value.request.post_data_json["tiktok"]["accounts"]] == ["clips_main", "clips.alt"]
    page.locator("[data-testid=account][data-name='clips.alt']").wait_for()

    # Cycle.
    cycle = page.locator("[data-testid=cycle]")
    assert not cycle.is_checked()
    with page.expect_response(lambda r: r.request.method == "PATCH" and r.url.endswith("/api/config")) as resp:
        page.locator(".cycle-switch").click()
    assert resp.value.request.post_data_json == {"tiktok": {"cycle_accounts": True}}
    assert server.app.store.get().tiktok.cycle_accounts is True
    assert cycle.is_checked()

    # Log in: the button opens the window, the row says so, and polling picks up the result.
    with page.expect_response(lambda r: r.url.endswith("/api/accounts/clips_main/login")):
        row.locator("[data-testid=login]").click()
    assert mock.logins == ["clips_main"]
    row.locator("[data-testid=login-state]", has_text="Browser window open, log in there").wait_for()
    assert row.locator("[data-testid=login]").is_disabled()
    if shots:
        _shoot(page, shots, "accounts", full_page=True)
    mock.state["clips_main"] = {"logged_in": True, "login_open": False}  # the user logged in and closed it
    row.locator("[data-testid=login-state]", has_text="✓ Logged in").wait_for(timeout=10_000)
    with page.expect_response(lambda r: r.url.endswith("/api/accounts/clips_main/check")):
        row.locator("[data-testid=check]").click()

    # Limits and the enabled switch edit that account's entry only.
    row.locator("[data-testid=acct-max]").fill("2")
    with page.expect_response(lambda r: r.request.method == "PATCH" and r.url.endswith("/api/config")) as resp:
        row.locator("[data-testid=acct-save]").click()
    accts = resp.value.request.post_data_json["tiktok"]["accounts"]
    assert accts[0]["name"] == "clips_main" and accts[0]["max_uploads_per_day"] == 2
    assert accts[1]["name"] == "clips.alt" and accts[1]["max_uploads_per_day"] == 6
    alt = page.locator("[data-testid=account][data-name='clips.alt']")
    with page.expect_response(lambda r: r.request.method == "PATCH" and r.url.endswith("/api/config")):
        alt.locator(".account-head .switch").click()
    assert [a.enabled for a in server.app.store.get().tiktok.accounts] == [True, False]

    # Remove asks first, and says the profile folder stays.
    alt.locator("[data-testid=delete]").click()
    alt.locator(".confirm-box", has_text="profile folder stays on disk").wait_for()
    with page.expect_response(lambda r: r.request.method == "PATCH" and r.url.endswith("/api/config")):
        alt.locator("[data-testid=confirm-delete]").click()
    alt.wait_for(state="detached")
    assert [a.name for a in server.app.store.get().tiktok.accounts] == ["clips_main"]
    assert errors == []


def test_accounts_api_missing(server: DemoServer, browser) -> None:
    """A argus without the accounts API: a quiet note, and the page still works."""
    page = browser.new_page(viewport={"width": 1366, "height": 900})
    errors = _watch_errors(page)
    calls: list[str] = []

    def missing(route) -> None:
        calls.append(route.request.url)
        route.fulfill(status=404, json={"detail": "Not Found"})

    page.route(re.compile(r"/api/(accounts|upload)(/.*)?$"), missing)
    page.goto(server.url + "/#accounts")
    page.locator("[data-testid=accounts-missing]", has_text="Accounts API not available").wait_for()
    page.locator("[data-testid=account][data-name=default]").wait_for()
    assert page.locator("[data-testid=login-state]:visible").count() == 0
    # It doesn't keep asking.
    n = len(calls)
    page.locator("[data-testid=nav-clips]").click()
    page.locator("[data-testid=tab-queued]").click()
    page.locator("[data-testid=queue-note]").wait_for()
    page.locator("[data-testid=nav-accounts]").click()
    page.locator("[data-testid=accounts-missing]").wait_for()
    assert len(calls) == n
    # The only errors are the browser's own 404 lines for those requests.
    assert [e for e in errors if "404" not in e] == []


def test_clip_scores_and_queue_order(server: DemoServer, browser) -> None:
    shots = os.environ.get("LW_SCREENSHOTS")
    now = time.time()
    manual = PeakEvent(streamer="demo_beta", ts_start=now - 300, ts_peak=now - 300, ts_end=now - 300, source="manual")

    def clip(streamer: str, status: str, score: float, age: float, **kw) -> dict:
        return Clip(streamer=streamer, status=status, score=score, created_at=now - age,
                    window_start=now - age - 30, window_end=now - age, caption=f"{streamer} on Twitch", **kw
                    ).model_dump(mode="json")

    fakes = [
        clip("demo_alpha", "queued", 3.1, 900),
        clip("demo_gamma", "queued", 8.24, 120),
        clip("demo_alpha", "queued", 3.1, 1500),  # same score, older: before the other 3.1
        clip("demo_beta", "queued", 0.0, 60, peak_id=manual.id),
        clip("demo_gamma", "uploaded", 5.0, 4000, account="clips_main"),
        clip("demo_alpha", "dry_run_ok", 4.2, 5000, account="clips.alt"),
    ]

    def clips(route) -> None:
        route.fulfill(json=route.fetch().json() + fakes)

    def peaks(route) -> None:
        route.fulfill(json=[manual.model_dump(mode="json"), *route.fetch().json()])

    page = browser.new_page(viewport={"width": 1366, "height": 900})
    errors = _watch_errors(page)
    mock = AccountsApi(server)
    mock.upload.update(queued=4, next_account="clips_main",
                       waiting_reason="clips_main: next slot in 23 min (45 min between uploads)")
    mock.install(page)
    page.route(re.compile(r"/api/clips\?"), clips)
    page.route(re.compile(r"/api/peaks\?"), peaks)
    page.goto(server.url + "/#clips")
    page.locator("[data-testid=tab-queued]").click()

    items = page.locator("[data-testid=clip-item]")
    items.nth(3).wait_for()
    want = [fakes[3]["id"], fakes[1]["id"], fakes[2]["id"], fakes[0]["id"]]
    assert items.evaluate_all("cs => cs.map(c => c.dataset.id)") == want
    scores = [items.nth(i).locator("[data-testid=clip-score]").inner_text() for i in range(4)]
    assert scores == ["manual", "8.2× chat", "3.1× chat", "3.1× chat"]
    page.locator("[data-testid=waiting-reason]", has_text="clips_main: next slot in 23 min").wait_for()
    note = page.locator("[data-testid=queue-note]")
    assert "highest chat-to-normal ratio" in note.inner_text()
    assert "4 clips queued, next goes to clips_main" in note.inner_text()
    if shots:
        _shoot(page, shots, "clips-queued")

    page.locator("[data-testid=tab-uploaded]").click()
    up = page.locator(f"[data-testid=clip-item][data-id='{fakes[4]['id']}']")
    up.locator("[data-testid=clip-account]", has_text="Posted to clips_main").wait_for()
    dry = page.locator(f"[data-testid=clip-item][data-id='{fakes[5]['id']}']")
    assert dry.locator("[data-testid=clip-account]").inner_text() == "Dry run on clips.alt"
    assert up.locator("[data-testid=clip-score]").inner_text() == "5.0× chat"

    # A clip event over the WebSocket carries the score and account too.
    updated = {**fakes[1], "status": "uploaded", "account": "clips_main"}
    loop = server._loop
    assert loop is not None
    loop.call_soon_threadsafe(server.app.bus.publish, Topic.CLIP, Clip.model_validate(updated))
    page.locator(f"[data-testid=clip-item][data-id='{fakes[1]['id']}'] [data-testid=clip-account]",
                 has_text="Posted to clips_main").wait_for(timeout=10_000)
    if shots:
        _shoot(page, shots, "clips-uploaded")
    assert errors == []
