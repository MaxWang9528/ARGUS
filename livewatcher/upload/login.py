"""`livewatcher login-tiktok`: log in to TikTok once in the saved browser profile.

Phase 0 file. Branch D reuses ``open_context`` so the uploader and the login use
the same profile, channel and cookies.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
from pathlib import Path
from typing import Any

from ..core.config import Config

UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload"
SESSION_COOKIE = "sessionid"


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


async def open_context(playwright: Any, cfg: Config, *, headless: bool | None = None) -> Any:
    """Open the persistent browser profile from the config. Caller closes it."""
    tk = cfg.tiktok
    profile = Path(os.path.expanduser(tk.browser_profile_dir))
    profile.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {
        "headless": tk.headless if headless is None else headless,
        "viewport": {"width": 1280, "height": 900},
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if tk.browser_channel and tk.browser_channel != "chromium":
        kwargs["channel"] = tk.browser_channel
    context = await playwright.chromium.launch_persistent_context(str(profile), **kwargs)
    if tk.cookies_file:
        await context.add_cookies(load_cookies(tk.cookies_file))
    return context


async def is_logged_in(context: Any) -> bool:
    cookies = await context.cookies("https://www.tiktok.com")
    return any(c["name"] == SESSION_COOKIE and c["value"] for c in cookies)


async def main(args: argparse.Namespace) -> int:
    from playwright.async_api import async_playwright

    cfg = args.store.get()
    async with async_playwright() as pw:
        context = await open_context(pw, cfg, headless=False)
        page = context.pages[0] if context.pages else await context.new_page()
        if await is_logged_in(context):
            print("Already logged in to TikTok. Nothing to do.")
            await context.close()
            return 0
        await page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded")
        print("A browser window is open. Log in to TikTok there.")
        print("This command finishes by itself once you're logged in (10 minute limit).")
        for _ in range(300):
            await page.wait_for_timeout(2000)
            if await is_logged_in(context):
                await page.wait_for_timeout(3000)  # let the profile settle
                print("Logged in. The session is saved in", cfg.tiktok.browser_profile_dir)
                await context.close()
                return 0
        print("Timed out waiting for the login.")
        await context.close()
        return 1
