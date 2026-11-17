"""TikTok accounts and the upload queue's state. See "Accounts" in docs/CONTRACTS.md.

    GET  /api/accounts                 every account, its limits and last known login
    POST /api/accounts/{name}/login    open a visible login window on its profile (202)
    POST /api/accounts/{name}/check    headless login check
    GET  /api/upload                   which account is next, and why the queue waits

Accounts are looked up by name in ``cfg.tiktok_accounts()``, never used as a path.
Nothing here returns cookie values or the cookies file. The login window runs as a
background task on the server's loop; one window per account at a time. Queue-only
state is read with ``getattr``, so the demo's fake queue works too.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request

from ..core.config import TikTokAccount
from ..core.models import ClipStatus
from ..upload import login
from ..upload.queue import account_slot, choose_account
from ..upload.tiktok_browser import classify_error

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger("argus.upload")

router = APIRouter()


class LoginWindows:
    """Login windows that are open and the last known login state of each account."""

    def __init__(self, lw: App, *, playwright_factory: Callable[[], Any] | None = None) -> None:
        self.lw = lw
        self.playwright_factory = playwright_factory  # None = the real Playwright
        self.checked: dict[str, tuple[bool, float]] = {}  # name -> (logged in, when)
        self.tasks: dict[str, asyncio.Task] = {}

    def is_open(self, name: str) -> bool:
        return name in self.tasks

    def logged_in(self, name: str) -> bool | None:
        """The newest of our checks and what the queue saw while uploading."""
        seen = [self.checked.get(name)]
        queue_seen = getattr(self.lw.upload_queue, "login_seen", None)
        if isinstance(queue_seen, dict):
            seen.append(queue_seen.get(name))
        known = [s for s in seen if s is not None]
        return max(known, key=lambda s: s[1])[0] if known else None

    def record(self, name: str, logged_in: bool) -> None:
        self.checked[name] = (logged_in, time.time())
        note = getattr(self.lw.upload_queue, "note_login", None)
        if note is not None:
            note(name, logged_in)  # a good login lifts that account's pause

    async def check(self, account: TikTokAccount) -> bool:
        state = await login.check_login(self.lw.store.get(), account,
                                        playwright_factory=self.playwright_factory)
        self.record(account.name, state)
        return state

    def open(self, account: TikTokAccount) -> None:
        """Start the window. The caller has checked it isn't open yet."""
        self.tasks[account.name] = asyncio.create_task(self._window(account))

    async def _window(self, account: TikTokAccount) -> None:
        name = account.name
        try:
            log.info("login window opened for TikTok account %s", name)
            await login.login_window(self.lw.store.get(), account,
                                     playwright_factory=self.playwright_factory,
                                     on_status=lambda state: self.checked.__setitem__(name, (state, time.time())))
            state = await self.check(account)
            log.info("login window for TikTok account %s closed: %s", name,
                     "logged in" if state else "not logged in")
        except asyncio.CancelledError:
            raise
        except login.ProfileBusy as exc:
            log.warning("login window for TikTok account %s: %s", name, exc)
        except Exception as exc:  # noqa: BLE001 - a background task: log, don't raise
            log.warning("login window for TikTok account %s failed: %s", name, classify_error(exc)[1])
        finally:
            self.tasks.pop(name, None)


# ---- helpers -------------------------------------------------------------------
def _lw(request: Request) -> App:
    return request.app.state.lw


def _windows(request: Request) -> LoginWindows:
    state = request.app.state
    if getattr(state, "account_logins", None) is None:
        state.account_logins = LoginWindows(state.lw)
    return state.account_logins


def _account(lw: App, name: str) -> TikTokAccount:
    account = next((a for a in lw.store.get().tiktok_accounts() if a.name == name), None)
    if account is None:
        raise HTTPException(404, f"no TikTok account called {name}")
    return account


# ---- routes --------------------------------------------------------------------
@router.get("/accounts")
async def list_accounts(request: Request) -> list[dict[str, Any]]:
    lw, windows = _lw(request), _windows(request)
    now = time.time()
    out = []
    for a in lw.store.get().tiktok_accounts():
        slot = account_slot(a, lw.db, now)
        out.append({
            "name": a.name,
            "enabled": a.enabled,
            "profile_dir": a.browser_profile_dir,
            "logged_in": windows.logged_in(a.name),
            "login_open": windows.is_open(a.name),
            "uploads_24h": slot.uploads_24h,
            "max_uploads_per_day": a.max_uploads_per_day,
            "min_minutes_between_uploads": a.min_minutes_between_uploads,
            "last_upload_ts": slot.last_upload_ts,
            "next_free_ts": slot.next_free_ts,
        })
    return out


@router.post("/accounts/{name}/login", status_code=202)
async def open_login(request: Request, name: str) -> dict[str, bool]:
    lw, windows = _lw(request), _windows(request)
    account = _account(lw, name)
    if windows.is_open(account.name):
        raise HTTPException(409, f"a login window for {account.name} is already open")
    if login.profile_busy(account):
        raise HTTPException(409, f"the browser profile of {account.name} is in use (an upload "
                                 "or a login check); try again in a moment")
    windows.open(account)
    return {"login_open": True}


@router.post("/accounts/{name}/check")
async def check_login(request: Request, name: str) -> dict[str, bool]:
    lw, windows = _lw(request), _windows(request)
    account = _account(lw, name)
    if windows.is_open(account.name):
        raise HTTPException(409, f"the login window for {account.name} is open; close it and "
                                 "the login is checked by itself")
    try:
        return {"logged_in": await windows.check(account)}
    except login.ProfileBusy as exc:
        raise HTTPException(409, f"{exc} (an upload is running); try again in a moment") from exc
    except Exception as exc:
        raise HTTPException(503, f"couldn't check the login: {classify_error(exc)[1]}") from exc


@router.get("/upload")
async def upload_state(request: Request) -> dict[str, Any]:
    lw = _lw(request)
    cfg = lw.store.get()
    queue = lw.upload_queue
    next_account = getattr(queue, "next_account", None)
    if next_account is not None:
        account, why = next_account()
    else:  # the demo's fake queue has no limits or pauses
        account, why = choose_account(cfg, lw.db, time.time(), dry_run=cfg.tiktok.dry_run)
    queued = len(lw.db.list_clips(status=ClipStatus.QUEUED, limit=100_000))
    return {
        "cycle": cfg.tiktok.cycle_accounts,
        "next_account": account.name if account else None,
        "queued": queued,
        "waiting_reason": why if queued and account is None else None,
        "paused_reason": getattr(queue, "paused_reason", None),
    }
