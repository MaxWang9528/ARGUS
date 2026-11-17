"""Accounts in the database, the uploader and the login helpers. Fake browsers only."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from test_tiktok_browser import FakeContext, FakePage, FakePlaywright

from livewatcher import cli
from livewatcher.core.db import Database
from livewatcher.upload import login
from livewatcher.upload.tiktok_browser import NOT_LOGGED_IN, TikTokUploader

# ---- database ------------------------------------------------------------------------
OLD_SCHEMA = """
CREATE TABLE uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX uploads_ts ON uploads(ts);
"""


def test_migrates_an_old_uploads_table(tmp_path: Path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO uploads(clip_id, ts, kind) VALUES('c1', 100, 'ok')")
    conn.execute("INSERT INTO uploads(clip_id, ts, kind) VALUES('c2', 200, 'dry_run')")
    conn.commit()
    conn.close()

    db = Database(path)
    assert db.count_uploads_since(0, account="default") == 1
    assert db.count_uploads_since(0, kind="dry_run", account="default") == 1
    assert db.last_upload_ts(account="default") == 100
    assert db.last_upload_account() == "default"
    db.add_upload("c3", "ok", ts=300, account="second")
    assert db.count_uploads_since(0) == 2  # all accounts
    assert db.count_uploads_since(0, account="second") == 1
    db.close()

    db = Database(path)  # opening again is a no-op migration
    assert db.last_upload_account() == "second"
    assert db.last_upload_account(["default"]) == "default"
    assert db.last_upload_account([]) is None
    assert db.upload_times_since(0, account="default") == [100]
    db.close()
    cols = [r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(uploads)")]
    assert cols.count("account") == 1


def test_new_database_has_the_account_column():
    db = Database(":memory:")
    db.add_upload("c", "ok", "", 5.0)  # old positional call: the default account
    assert db.count_uploads_since(0, account="default") == 1
    assert db.last_upload_account() == "default"


# ---- uploader ------------------------------------------------------------------------
def two_accounts(store, tmp_path, cookies: str = ""):
    store.update({"tiktok": {"accounts": [
        {"name": "main", "browser_profile_dir": str(tmp_path / "p-main")},
        {"name": "second", "browser_profile_dir": str(tmp_path / "p-second"), "cookies_file": cookies},
    ]}})
    return store.get().tiktok_accounts()


class CookieContext(FakeContext):
    def __init__(self, page, logged_in=True):
        super().__init__(page, logged_in)
        self.added = []

    async def add_cookies(self, cookies):
        self.added += cookies


@pytest.fixture
def fake_browser():
    return FakePlaywright(CookieContext(FakePage()))


def make_uploader(store, bus, browser, clock):
    return TikTokUploader(store=store, bus=bus, playwright_factory=lambda: browser,
                          sleep=clock.sleep, monotonic=clock, element_timeout_s=5,
                          processing_timeout_s=60, confirm_timeout_s=20, enabled_grace_s=3, poll_s=0.5)


async def test_upload_opens_the_given_accounts_profile_and_cookies(store, bus, clock, make_clip,
                                                                    tmp_path, fake_browser):
    cookies = tmp_path / "c.json"
    cookies.write_text(json.dumps([{"name": "sessionid", "value": "SECRET", "domain": ".tiktok.com"}]))
    _, second = two_accounts(store, tmp_path, str(cookies))
    up = make_uploader(store, bus, fake_browser, clock)
    res = await up.upload(make_clip(), "cap #a", dry_run=True, account=second)
    assert res.kind == "dry_run", res.error
    assert fake_browser.launch_kwargs["profile"] == str(tmp_path / "p-second")
    assert [c["name"] for c in fake_browser.context.added] == ["sessionid"]
    assert up.current_account is None
    assert not login.profile_busy(second)


async def test_upload_without_account_uses_the_first(store, bus, clock, make_clip, tmp_path, fake_browser):
    two_accounts(store, tmp_path)
    up = make_uploader(store, bus, fake_browser, clock)
    res = await up.upload(make_clip(), "cap", dry_run=True)
    assert res.kind == "dry_run", res.error
    assert fake_browser.launch_kwargs["profile"] == str(tmp_path / "p-main")
    assert fake_browser.context.added == []


async def test_logged_out_message_names_the_account(store, bus, clock, make_clip, tmp_path, fake_browser):
    _, second = two_accounts(store, tmp_path)
    fake_browser.context.logged_in = False
    up = make_uploader(store, bus, fake_browser, clock)
    res = await up.upload(make_clip(), "cap", dry_run=True, account=second)
    assert res.kind == "blocked"
    assert res.error.startswith(NOT_LOGGED_IN)
    assert "--account second" in res.error


async def test_busy_profile_is_transient_without_a_browser(store, bus, clock, make_clip, tmp_path):
    main, _ = two_accounts(store, tmp_path)

    def no_browser():
        raise AssertionError("the browser must not be opened")

    up = TikTokUploader(store=store, bus=bus, playwright_factory=no_browser)
    with login.claim_profile(main):
        res = await up.upload(make_clip(), "cap", dry_run=True, account=main)
    assert res.kind == "transient" and "already open" in res.error


# ---- login helpers -------------------------------------------------------------------
def test_claim_profile_is_exclusive(store, tmp_path):
    main, second = two_accounts(store, tmp_path)
    with login.claim_profile(main):
        assert login.profile_busy(main) and not login.profile_busy(second)
        with pytest.raises(login.ProfileBusy), login.claim_profile(main):
            pass
    assert not login.profile_busy(main)


def test_find_account_and_hint(store, tmp_path):
    assert login.find_account(store.get(), None).name == "default"
    assert login.login_hint(login.find_account(store.get(), None)) == "./lw login-tiktok"
    two_accounts(store, tmp_path)
    assert login.find_account(store.get(), None).name == "main"
    assert login.find_account(store.get(), "second").name == "second"
    assert login.find_account(store.get(), "nope") is None
    assert login.login_hint(login.find_account(store.get(), "second")) == "./lw login-tiktok --account second"


class WindowContext(FakeContext):
    """A headed context the "user" closes."""

    def __init__(self, page, logged_in=False):
        super().__init__(page, logged_in)
        self.handlers = {}

    def on(self, event, cb):
        self.handlers.setdefault(event, []).append(cb)

    def user_closes(self):
        for cb in self.handlers.get("close", []):
            cb(self)


class WindowPage(FakePage):
    def on(self, event, cb):
        pass


async def test_check_login_is_headless(store, tmp_path):
    main, _ = two_accounts(store, tmp_path)
    browser = FakePlaywright(FakeContext(FakePage(), logged_in=True))
    assert await login.check_login(store.get(), main, playwright_factory=lambda: browser) is True
    assert browser.launch_kwargs["headless"] is True
    assert browser.launch_kwargs["profile"] == str(tmp_path / "p-main")
    assert browser.context.closed and not login.profile_busy(main)


async def test_login_window_is_headed_and_waits_for_close(store, tmp_path):
    main, _ = two_accounts(store, tmp_path)
    ctx = WindowContext(WindowPage())
    browser = FakePlaywright(ctx)
    seen = []
    task = asyncio.create_task(login.login_window(store.get(), main, playwright_factory=lambda: browser,
                                                  on_status=seen.append, poll_s=0.01))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if seen:
            break
    assert browser.launch_kwargs["headless"] is False
    assert ctx.pages[0].url == login.LOGIN_URL
    assert login.profile_busy(main) and not task.done()
    ctx.logged_in = True
    ctx.user_closes()
    await asyncio.wait_for(task, 2)
    assert seen[0] is False
    assert ctx.closed and not login.profile_busy(main)


def test_cli_login_unknown_account_opens_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no config.toml here: the built-in defaults
    assert cli.main(["login-tiktok", "--account", "nope"]) == 2
    assert "No TikTok account called 'nope'" in capsys.readouterr().out
