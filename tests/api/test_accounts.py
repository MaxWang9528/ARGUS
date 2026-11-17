"""The accounts API: /api/accounts, login window, login check and /api/upload.

No real browser is ever started: the real Playwright launcher is replaced by one
that fails, and the tests that need a browser hand in a fake one.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from livewatcher.api.accounts import LoginWindows
from livewatcher.app import App
from livewatcher.core.config import Config, ConfigStore, General, Streamer, TikTok
from livewatcher.core.events import EventBus
from livewatcher.core.fakes import FakeUploader
from livewatcher.core.models import Clip, ClipStatus
from livewatcher.upload import login, tiktok_browser
from livewatcher.upload.queue import ClipUploadQueue

COOKIE_SECRET = "cookie-value-never-shown"


@pytest.fixture
def store(tmp_path: Path, bus: EventBus) -> ConfigStore:
    """Every profile under tmp_path, so nothing can reach the real TikTok profile."""
    cookies = tmp_path / "cookies.json"
    cookies.write_text(json.dumps([{"name": "sessionid", "value": COOKIE_SECRET, "domain": ".tiktok.com"}]))
    cfg = Config(
        general=General(data_dir=str(tmp_path / "data")),
        tiktok=TikTok(browser_profile_dir=str(tmp_path / "legacy-profile"), accounts=[
            {"name": "main", "browser_profile_dir": str(tmp_path / "p-main"),
             "max_uploads_per_day": 2, "min_minutes_between_uploads": 30},
            {"name": "second", "cookies_file": str(cookies)},
            {"name": "off", "enabled": False, "browser_profile_dir": str(tmp_path / "p-off")},
        ]),
        streamers=[Streamer(name="alpha")])
    return ConfigStore(config=cfg, bus=bus)


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch):
    def refuse():
        raise AssertionError("a test tried to start the real browser")
    monkeypatch.setattr(login, "_default_playwright", refuse)
    monkeypatch.setattr(tiktok_browser, "_default_playwright", refuse)


class FakeCtx:
    def __init__(self, browser, headless):
        self.browser, self.headless = browser, headless
        self.pages = [FakePg()]
        self.handlers = []
        self.closed = False

    def on(self, event, cb):
        if event == "close":
            self.handlers.append(cb)

    async def cookies(self, url):
        return [{"name": "sessionid", "value": "x"}] if self.browser.logged_in else []

    async def add_cookies(self, cookies):
        pass

    async def close(self):
        self.closed = True

    def user_closes(self):
        for cb in self.handlers:
            cb(self)


class FakePg:
    url = "about:blank"

    def on(self, event, cb):
        pass

    async def goto(self, url, **kw):
        self.url = url


class FakeBrowser:
    """Stands in for ``async_playwright()``; records every launch."""

    def __init__(self, logged_in=False, launch_error=None):
        self.logged_in, self.launch_error = logged_in, launch_error
        self.launches: list[dict] = []
        self.contexts: list[FakeCtx] = []
        self.chromium = self

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def launch_persistent_context(self, profile, **kw):
        self.launches.append({"profile": profile, **kw})
        if self.launch_error:
            raise self.launch_error
        ctx = FakeCtx(self, kw["headless"])
        self.contexts.append(ctx)
        return ctx


@pytest.fixture
def browser(client: TestClient, lw: App) -> FakeBrowser:
    fake = FakeBrowser()
    client.app.state.account_logins = LoginWindows(lw, playwright_factory=fake)
    return fake


def wait_until(cond, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def by_name(client) -> dict[str, dict]:
    return {a["name"]: a for a in client.get("/api/accounts").json()}


# ---- GET /api/accounts ---------------------------------------------------------------
def test_list_accounts_shape(client: TestClient, lw: App, tmp_path: Path):
    now = time.time()
    lw.db.add_upload("c1", "ok", ts=now - 3600, account="main")
    lw.db.add_upload("c2", "ok", ts=now - 600, account="main")
    lw.db.add_upload("c3", "dry_run", ts=now - 60, account="main")
    res = client.get("/api/accounts")
    assert res.status_code == 200
    rows = res.json()
    assert [a["name"] for a in rows] == ["main", "second", "off"]
    main = rows[0]
    assert set(main) == {"name", "enabled", "profile_dir", "logged_in", "login_open", "uploads_24h",
                         "max_uploads_per_day", "min_minutes_between_uploads", "last_upload_ts",
                         "next_free_ts"}
    assert main["enabled"] is True and main["profile_dir"] == str(tmp_path / "p-main")
    assert main["logged_in"] is None and main["login_open"] is False
    assert main["uploads_24h"] == 2 and main["max_uploads_per_day"] == 2
    assert main["min_minutes_between_uploads"] == 30
    assert main["last_upload_ts"] == pytest.approx(now - 600)
    assert main["next_free_ts"] == pytest.approx(now - 3600 + 24 * 3600)
    second = rows[1]
    assert second["profile_dir"] == str(lw.store.get().data_dir / "accounts" / "second" / "browser_profile")
    assert second["uploads_24h"] == 0 and second["last_upload_ts"] is None and second["next_free_ts"] is None
    assert rows[2]["enabled"] is False
    assert COOKIE_SECRET not in res.text and "cookies" not in res.text


def test_implicit_default_account(client: TestClient, lw: App, tmp_path: Path):
    lw.store.update({"tiktok": {"accounts": []}})
    lw.db.add_upload("old", "ok", "", time.time() - 60)  # an old-style row
    (acc,) = client.get("/api/accounts").json()
    assert acc["name"] == "default" and acc["profile_dir"] == str(tmp_path / "legacy-profile")
    assert acc["uploads_24h"] == 1


# ---- POST /api/accounts/{name}/check -------------------------------------------------
def test_check_login(client: TestClient, browser: FakeBrowser, tmp_path: Path):
    res = client.post("/api/accounts/main/check")
    assert res.status_code == 200 and res.json() == {"logged_in": False}
    assert browser.launches[-1]["headless"] is True
    assert browser.launches[-1]["profile"] == str(tmp_path / "p-main")
    assert by_name(client)["main"]["logged_in"] is False
    browser.logged_in = True
    assert client.post("/api/accounts/main/check").json() == {"logged_in": True}
    assert by_name(client)["main"]["logged_in"] is True
    assert by_name(client)["second"]["logged_in"] is None
    # a disabled account can be checked too
    assert client.post("/api/accounts/off/check").status_code == 200


def test_check_unknown_account_is_404(client: TestClient, browser: FakeBrowser):
    assert client.post("/api/accounts/nope/check").status_code == 404
    assert client.post("/api/accounts/..%2Fp-main/check").status_code in (404, 405)  # never a path
    assert browser.launches == []


def test_check_when_the_browser_cannot_start(client: TestClient, browser: FakeBrowser):
    browser.launch_error = RuntimeError("Executable doesn't exist at /x")
    res = client.post("/api/accounts/second/check")
    assert res.status_code == 503 and "isn't installed" in res.json()["detail"]
    assert COOKIE_SECRET not in res.text


def test_check_while_the_profile_is_in_use_is_409(client: TestClient, lw: App, browser: FakeBrowser):
    main = lw.store.get().tiktok_accounts()[0]
    with login.claim_profile(main):
        assert client.post("/api/accounts/main/check").status_code == 409
        assert client.post("/api/accounts/main/login").status_code == 409
    assert browser.launches == []


def test_a_good_check_lifts_the_accounts_pause(client: TestClient, lw: App, browser: FakeBrowser):
    q = ClipUploadQueue(store=lw.store, bus=lw.bus, db=lw.db,
                        uploader=FakeUploader(store=lw.store, bus=lw.bus, delay_s=0))
    lw.upload_queue = q
    q.pause_account("main", "logged out")
    browser.logged_in = True
    client.post("/api/accounts/main/check")
    assert q.account_paused == {}


# ---- POST /api/accounts/{name}/login -------------------------------------------------
def test_login_window_lifecycle(client: TestClient, browser: FakeBrowser, tmp_path: Path):
    res = client.post("/api/accounts/main/login")
    assert res.status_code == 202 and res.json() == {"login_open": True}
    assert by_name(client)["main"]["login_open"] is True
    assert client.post("/api/accounts/main/login").status_code == 409  # one window per account
    assert client.post("/api/accounts/main/check").status_code == 409
    wait_until(lambda: browser.contexts)
    window = browser.contexts[0]
    assert window.headless is False
    assert browser.launches[0]["profile"] == str(tmp_path / "p-main")
    wait_until(lambda: window.pages[0].url == login.LOGIN_URL)
    # another account can have its own window at the same time
    assert client.post("/api/accounts/second/login").status_code == 202

    browser.logged_in = True  # the user logs in, then closes the window
    client.portal.call(window.user_closes)
    wait_until(lambda: not by_name(client)["main"]["login_open"])
    assert window.closed
    assert browser.launches[-1]["headless"] is True  # checked again after the close
    assert by_name(client)["main"]["logged_in"] is True
    assert not login.profile_busy(client.app.state.lw.store.get().tiktok_accounts()[0])

    second = next(c for c in browser.contexts if c.headless is False and c is not window)
    client.portal.call(second.user_closes)
    wait_until(lambda: not by_name(client)["second"]["login_open"])


def test_login_unknown_account_is_404(client: TestClient, browser: FakeBrowser):
    assert client.post("/api/accounts/nope/login").status_code == 404
    assert browser.launches == []


def test_login_window_that_fails_to_open_clears_login_open(client: TestClient, browser: FakeBrowser):
    browser.launch_error = RuntimeError("Host system is missing dependencies")
    assert client.post("/api/accounts/main/login").status_code == 202
    wait_until(lambda: not by_name(client)["main"]["login_open"])
    assert client.post("/api/accounts/main/login").status_code == 202  # can try again


# ---- GET /api/upload -----------------------------------------------------------------
def queued_clip(lw: App, tmp_path: Path) -> Clip:
    out = tmp_path / "out.mp4"
    out.write_bytes(b"\0" * 50_000)
    clip = Clip(streamer="alpha", window_start=0, window_end=30, renders={"blur_fit": str(out)},
                status=ClipStatus.QUEUED)
    lw.db.save_clip(clip)
    return clip


def test_upload_state_with_the_demo_queue(client: TestClient, lw: App):
    res = client.get("/api/upload")
    assert res.status_code == 200
    assert res.json() == {"cycle": False, "next_account": "main", "queued": 0,
                          "waiting_reason": None, "paused_reason": None}
    lw.store.update({"tiktok": {"cycle_accounts": True}})
    assert client.get("/api/upload").json()["cycle"] is True


def test_upload_state_with_the_real_queue(client: TestClient, lw: App, tmp_path: Path):
    q = ClipUploadQueue(store=lw.store, bus=lw.bus, db=lw.db,
                        uploader=FakeUploader(store=lw.store, bus=lw.bus, delay_s=0))
    lw.upload_queue = q
    lw.store.update({"tiktok": {"dry_run": False, "cycle_accounts": True}})
    now = time.time()
    for ts in (now - 7200, now - 60):
        lw.db.add_upload("x", "ok", ts=ts, account="main")  # main is full
    queued_clip(lw, tmp_path)
    body = client.get("/api/upload").json()
    assert body["next_account"] == "second" and body["queued"] == 1 and body["waiting_reason"] is None

    q.pause_account("second", "logged out")
    body = client.get("/api/upload").json()
    assert body["next_account"] is None
    assert "account main: daily limit" in body["waiting_reason"]
    assert "account second: paused (logged out)" in body["waiting_reason"]
    assert body["paused_reason"] == "account second paused: logged out"

    q.pause("TikTok changed its page")
    assert client.get("/api/upload").json()["paused_reason"] == "TikTok changed its page"
