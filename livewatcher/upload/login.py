"""Logging in to TikTok: ``./lw login-tiktok [--account NAME]`` and the dashboard's
login window and login check.

The uploader, the CLI and the dashboard all open a profile through ``open_context``,
so they use the same profile, channel and cookies. Each TikTok account has its own
profile (``cfg.tiktok_accounts()``). Chromium can't open one profile twice, so every
user marks it busy with ``claim_profile`` while it is open.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import http.cookiejar
import json
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from ..core.config import Config, TikTokAccount

UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload"
SESSION_COOKIE = "sessionid"
LOGIN_URL = "https://www.tiktok.com/login"


def load_cookies(path: str) -> list[dict[str, Any]]:
    """Read cookies from a JSON export (list of cookie objects) or a Netscape
    cookies.txt file, in the shape Playwright's ``add_cookies`` wants."""
    p = Path(os.path.expanduser(path))
    text = p.read_text(encoding="utf-8")
    cookies: list[dict[str, Any]] = []
    if text.lstrip().startswith("["):
        for c in json.loads(text):
            cookies.append({
                "name": c["name"], "value": c["value"], "domain": c.get("domain", ".tiktok.com"),
                "path": c.get("path", "/"), "secure": bool(c.get("secure", True)),
                "httpOnly": bool(c.get("httpOnly", False))})
    else:
        jar = http.cookiejar.MozillaCookieJar(str(p))
        jar.load(ignore_discard=True, ignore_expires=True)
        for c in jar:
            cookies.append({"name": c.name, "value": c.value or "", "domain": c.domain,
                            "path": c.path, "secure": bool(c.secure), "httpOnly": False})
    return [c for c in cookies if "tiktok" in c["domain"]]


class ProfileBusy(Exception):
    """The browser profile is already open (an upload, a login window or a check)."""


_BUSY_PROFILES: set[str] = set()  # resolved profile dirs open right now (one event loop)


def _profile_key(profile_dir: str) -> str:
    return str(Path(os.path.expanduser(profile_dir)).resolve())


def profile_busy(account: TikTokAccount) -> bool:
    return _profile_key(account.browser_profile_dir) in _BUSY_PROFILES


@contextlib.contextmanager
def claim_profile(account: TikTokAccount) -> Iterator[None]:
    """Mark the account's profile as open for the duration. Raises ProfileBusy."""
    key = _profile_key(account.browser_profile_dir)
    if key in _BUSY_PROFILES:
        raise ProfileBusy(f"the browser profile of account {account.name} is already open")
    _BUSY_PROFILES.add(key)
    try:
        yield
    finally:
        _BUSY_PROFILES.discard(key)


def find_account(cfg: Config, name: str | None) -> TikTokAccount | None:
    """The named account (None = the first one), or None if there's no such account."""
    accounts = cfg.tiktok_accounts()
    if name is None:
        return accounts[0] if accounts else None
    return next((a for a in accounts if a.name == name), None)


def login_hint(account: TikTokAccount | None) -> str:
    """The command that logs this account in."""
    if account is None or account.name == "default":
        return "./lw login-tiktok"
    return f"./lw login-tiktok --account {account.name}"


def _display_env() -> dict[str, str] | None:
    """A headed window on WSL needs a display; WSLg serves :0."""
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return {**os.environ, "DISPLAY": ":0"}
    return None


async def open_context(playwright: Any, cfg: Config, account: TikTokAccount | None = None, *,
                       headless: bool | None = None) -> Any:
    """Open an account's persistent browser profile (None = the first account).
    The caller closes it, and should hold ``claim_profile`` while it is open."""
    tk = cfg.tiktok
    if account is None:
        account = cfg.tiktok_accounts()[0]
    profile = Path(os.path.expanduser(account.browser_profile_dir))
    profile.mkdir(parents=True, exist_ok=True)
    headless = tk.headless if headless is None else headless
    kwargs: dict[str, Any] = {
        "headless": headless,
        "viewport": {"width": 1280, "height": 900},
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if tk.browser_channel and tk.browser_channel != "chromium":
        kwargs["channel"] = tk.browser_channel
    if not headless and (env := _display_env()):
        kwargs["env"] = env
    context = await playwright.chromium.launch_persistent_context(str(profile), **kwargs)
    if account.cookies_file:
        try:
            await context.add_cookies(load_cookies(account.cookies_file))
        except BaseException:
            await context.close()
            raise
    return context


async def is_logged_in(context: Any) -> bool:
    cookies = await context.cookies("https://www.tiktok.com")
    return any(c["name"] == SESSION_COOKIE and c["value"] for c in cookies)


def _default_playwright() -> Any:
    from playwright.async_api import async_playwright

    return async_playwright()


async def check_login(cfg: Config, account: TikTokAccount, *,
                      playwright_factory: Callable[[], Any] | None = None) -> bool:
    """Open the profile headless and look for the session cookie. Raises ProfileBusy
    if the profile is open elsewhere."""
    with claim_profile(account):
        async with (playwright_factory or _default_playwright)() as pw:
            context = await open_context(pw, cfg, account, headless=True)
            try:
                return await is_logged_in(context)
            finally:
                await asyncio.shield(context.close())


async def login_window(cfg: Config, account: TikTokAccount, *,
                       playwright_factory: Callable[[], Any] | None = None,
                       on_status: Callable[[bool], None] | None = None,
                       timeout_s: float = 1800.0, poll_s: float = 2.0) -> None:
    """Open a visible window on the account's profile at TikTok's login page and wait
    until the user closes it (or ``timeout_s``). ``on_status`` gets the login state
    every poll. Raises ProfileBusy if the profile is open elsewhere."""
    with claim_profile(account):
        async with (playwright_factory or _default_playwright)() as pw:
            context = await open_context(pw, cfg, account, headless=False)
            closed = asyncio.Event()
            context.on("close", lambda *_: closed.set())
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                page.on("close", lambda *_: closed.set() if not context.pages else None)
                await page.goto(LOGIN_URL, wait_until="domcontentloaded")
                loop = asyncio.get_running_loop()
                deadline = loop.time() + timeout_s
                while not closed.is_set() and loop.time() < deadline:
                    try:
                        state = await is_logged_in(context)
                    except Exception:  # noqa: BLE001 - the window was closed meanwhile
                        break
                    if on_status:
                        on_status(state)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(closed.wait(), poll_s)
            finally:
                with contextlib.suppress(Exception):
                    await asyncio.shield(context.close())


async def main(args: argparse.Namespace) -> int:
    from playwright.async_api import async_playwright

    cfg = args.store.get()
    name = getattr(args, "account", None)
    account = find_account(cfg, name)
    if account is None:
        names = ", ".join(a.name for a in cfg.tiktok_accounts())
        print(f"No TikTok account called {name!r}. Accounts: {names}")
        return 2
    print(f"Account {account.name}: profile {account.browser_profile_dir}")
    try:
        with claim_profile(account):
            async with async_playwright() as pw:
                context = await open_context(pw, cfg, account, headless=False)
                page = context.pages[0] if context.pages else await context.new_page()
                if await is_logged_in(context):
                    print("Already logged in to TikTok. Nothing to do.")
                    await context.close()
                    return 0
                await page.goto(LOGIN_URL, wait_until="domcontentloaded")
                print("A browser window is open. Log in to TikTok there.")
                print("This command finishes by itself once you're logged in (10 minute limit).")
                for _ in range(300):
                    await page.wait_for_timeout(2000)
                    if await is_logged_in(context):
                        await page.wait_for_timeout(3000)  # let the profile settle
                        print("Logged in. The session is saved in", account.browser_profile_dir)
                        await context.close()
                        return 0
                print("Timed out waiting for the login.")
                await context.close()
                return 1
    except ProfileBusy as exc:
        print(exc)
        return 1
