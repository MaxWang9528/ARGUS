"""Who is live right now.

Two methods:
- **Helix** (``twitch.client_id`` and ``client_secret`` set): one ``GET /helix/streams``
  for up to 100 channels with an app access token. Gives the title too.
- **streamlink** (no keys): ask the Twitch plugin for streams. No streams means
  offline. Measured on this machine: 0.1 s for an offline channel, 0.5-1.5 s for a
  live one. Blocking, so it runs in a thread, a few channels at a time.

If Helix fails, the streamlink method is used for that round.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from ..core.config import ConfigStore

log = logging.getLogger("livewatcher.capture")

HELIX_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX_STREAMS_URL = "https://api.twitch.tv/helix/streams"


@dataclass
class LiveInfo:
    live: bool
    title: str = ""
    method: str = ""          # "helix" or "streamlink"
    error: str = ""           # set when the check itself failed (live is then a guess)
    checked_at: float = 0.0


def streamlink_check(session: Any, name: str) -> LiveInfo:
    """Blocking. ``session`` is a ``streamlink.Streamlink``."""
    from streamlink.exceptions import NoPluginError, NoStreamsError, PluginError

    url = f"https://twitch.tv/{name}"
    try:
        _, plugin_cls, resolved = session.resolve_url(url)
        plugin = plugin_cls(session, resolved)
        streams = plugin.streams()
    except NoStreamsError:
        return LiveInfo(live=False, method="streamlink", checked_at=time.time())
    except (PluginError, NoPluginError, OSError) as exc:
        return LiveInfo(live=False, method="streamlink", error=f"{type(exc).__name__}: {exc}",
                        checked_at=time.time())
    title = ""
    if streams:
        try:
            title = plugin.get_title() or ""
        except Exception:  # noqa: BLE001 - metadata is optional
            title = ""
    return LiveInfo(live=bool(streams), title=title, method="streamlink", checked_at=time.time())


def _default_session() -> Any:
    import streamlink

    session = streamlink.Streamlink()
    session.set_option("http-timeout", 10)
    return session


class LiveChecker:
    """``await check(names)`` -> {name: LiveInfo}. Names are lower-cased."""

    def __init__(self, store: ConfigStore, *, session_factory: Callable[[], Any] | None = None,
                 http: httpx.AsyncClient | None = None, concurrency: int = 4) -> None:
        self.store = store
        self._session_factory = session_factory or _default_session
        self._session: Any = None
        self._http = http
        self._sem = asyncio.Semaphore(concurrency)
        self._token: str = ""
        self._token_expires = 0.0
        self._token_for: tuple[str, str] = ("", "")

    # ---- public --------------------------------------------------------------
    async def check(self, names: Iterable[str]) -> dict[str, LiveInfo]:
        names = sorted({n.lower() for n in names})
        if not names:
            return {}
        tw = self.store.get().twitch
        if tw.client_id and tw.client_secret:
            try:
                return await self._check_helix(names, tw.client_id, tw.client_secret)
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                log.warning("Helix live check failed, using streamlink: %s", exc)
        return await self._check_streamlink(names)

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ---- streamlink ----------------------------------------------------------
    async def _check_streamlink(self, names: list[str]) -> dict[str, LiveInfo]:
        if self._session is None:
            self._session = await asyncio.to_thread(self._session_factory)

        async def one(name: str) -> tuple[str, LiveInfo]:
            async with self._sem:
                return name, await asyncio.to_thread(streamlink_check, self._session, name)

        return dict(await asyncio.gather(*(one(n) for n in names)))

    # ---- Helix ---------------------------------------------------------------
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=10)
        return self._http

    async def _app_token(self, client_id: str, secret: str, *, force: bool = False) -> str:
        if (not force and self._token and self._token_for == (client_id, secret)
                and time.time() < self._token_expires - 60):
            return self._token
        r = await self._client().post(HELIX_TOKEN_URL, data={
            "client_id": client_id, "client_secret": secret, "grant_type": "client_credentials"})
        r.raise_for_status()
        body = r.json()
        self._token = body["access_token"]
        self._token_expires = time.time() + float(body.get("expires_in", 3600))
        self._token_for = (client_id, secret)
        return self._token

    async def _check_helix(self, names: list[str], client_id: str, secret: str) -> dict[str, LiveInfo]:
        out = {n: LiveInfo(live=False, method="helix", checked_at=time.time()) for n in names}
        for i in range(0, len(names), 100):
            chunk = names[i:i + 100]
            params = [("user_login", n) for n in chunk] + [("first", "100")]
            for attempt in (0, 1):
                token = await self._app_token(client_id, secret, force=attempt == 1)
                r = await self._client().get(HELIX_STREAMS_URL, params=params, headers={
                    "Client-Id": client_id, "Authorization": f"Bearer {token}"})
                if r.status_code == 401 and attempt == 0:
                    continue  # token expired or revoked: get a new one once
                r.raise_for_status()
                break
            for item in r.json().get("data", []):
                login = str(item.get("user_login", "")).lower()
                if login in out and item.get("type", "live") == "live":
                    out[login] = LiveInfo(live=True, title=item.get("title", ""), method="helix",
                                          checked_at=time.time())
        return out
