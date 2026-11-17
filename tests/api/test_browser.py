"""Browser smoke test: the real page against a running demo App (all fakes).

Skips cleanly when Playwright or Chromium can't start here. Set
``LW_SCREENSHOTS=<folder>`` to save a screenshot of each view.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
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
        if self._loop is not None and self._task is not None:
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
    page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

    page.goto(server.url + "/#streams")
    cards = page.locator("[data-testid=stream-card]")
    cards.first.wait_for()
    assert cards.count() == 3
    for name in DEMO_STREAMERS:
        assert page.locator(f"[data-testid=stream-card][data-name={name}]").count() == 1
    # The fake recorder reports live and writes snapshots; the cards show them.
    page.locator("[data-testid=stream-card] img").first.wait_for(timeout=10_000)
    page.locator("[data-testid=stream-card] .uplot").first.wait_for(timeout=10_000)
    if shots:
        page.wait_for_timeout(3000)  # a few rate samples on the charts
        page.screenshot(path=f"{shots}/streams.png")

    # Clip now -> the clip turns up in To review (auto-upload is off by default).
    first = page.locator(f"[data-testid=stream-card][data-name={DEMO_STREAMERS[0]}]")
    first.locator("[data-testid=clip-now]").click()
    page.locator("[data-testid=nav-clips]").click()
    page.locator("[data-testid=clip-item][data-status=pending_review]").first.wait_for(timeout=20_000)
    if shots:
        page.screenshot(path=f"{shots}/clips.png")

    page.locator("[data-testid=nav-debug]").click()
    page.locator("[data-testid=peak-log] tbody tr").first.wait_for(timeout=5_000)
    page.locator(".layout-preview img").wait_for(timeout=10_000)
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

    assert errors == []
