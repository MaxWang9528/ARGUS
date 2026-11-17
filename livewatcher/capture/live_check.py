"""Who is live right now.

Three methods, tried in this order:
- **Helix** (``twitch.client_id`` and ``client_secret`` set): one ``GET /helix/streams``
  for up to 100 channels with an app access token. Gives title, category and viewers.
- **GQL** (no keys): one ``POST https://gql.twitch.tv/gql`` for up to 100 channels with
  Twitch's public web client id (the one streamlink uses), asking
  ``users(logins:) { login stream { type viewersCount title game { name } } }``. No
  credentials. A ``null`` stream means offline; an unknown login comes back as ``null``.
  Measured for 62 channels (10 live): one request, ~0.1 s warm (up to ~0.9 s for a
  cold first connection), ~0.9 KB up and ~3.6 KB down. Its answers matched the
  streamlink check exactly (live and category) on all 62.
- **streamlink** (fallback): ask the Twitch plugin for streams. No streams means
  offline. 0.1 s for an offline channel, 0.5-1.5 s for a live one. Blocking, so it runs
  in a thread, a few channels at a time. No viewer count.

If one method fails, the next one is used for that round.
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
GQL_URL = "https://gql.twitch.tv/gql"
GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # Twitch's public web client id (streamlink's too)
GQL_QUERY = ("query LivewatcherStreams($logins: [String!]) { users(logins: $logins) "
             "{ login stream { type viewersCount title game { name } } } }")
BATCH = 100  # logins per Helix or GQL request


@dataclass
class LiveInfo:
    live: bool
    title: str = ""
    category: str = ""       # Twitch category (game name)
    viewers: int | None = None  # None when unknown (always for streamlink)
    method: str = ""          # "helix", "gql" or "streamlink"
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
    title = category = ""
    if streams:
        try:
            title = plugin.get_title() or ""
            category = plugin.get_category() or ""
        except Exception as exc:  # noqa: BLE001 - metadata is optional
            log.debug("no stream metadata for %s: %s", name, exc)
    return LiveInfo(live=bool(streams), title=title, category=category, method="streamlink",
                    checked_at=time.time())


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
                log.warning("Helix live check failed, using GQL: %s", exc)
        try:
            return await self._check_gql(names)
        except (httpx.HTTPError, KeyError, ValueError, TypeError, AttributeError) as exc:
            log.warning("GQL live check failed, using streamlink: %s", exc)
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
        for i in range(0, len(names), BATCH):
            chunk = names[i:i + BATCH]
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
                    out[login] = LiveInfo(live=True, title=item.get("title", ""),
                                          category=item.get("game_name", ""),
                                          viewers=_int_or_none(item.get("viewer_count")),
                                          method="helix", checked_at=time.time())
        return out

    # ---- GQL (no keys) -------------------------------------------------------
    async def _check_gql(self, names: list[str]) -> dict[str, LiveInfo]:
        out = {n: LiveInfo(live=False, method="gql", checked_at=time.time()) for n in names}
        for i in range(0, len(names), BATCH):
            chunk = names[i:i + BATCH]
            r = await self._client().post(GQL_URL, headers={"Client-ID": GQL_CLIENT_ID}, json={
                "operationName": "LivewatcherStreams", "query": GQL_QUERY,
                "variables": {"logins": chunk}})
            r.raise_for_status()
            body = r.json()
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, dict) or not isinstance(data.get("users"), list):
                errors = body.get("errors") if isinstance(body, dict) else body
                raise ValueError(f"GQL answered without data: {str(errors)[:200]}")  # noqa: TRY004
            now = time.time()
            for user in data["users"]:
                if not user:
                    continue  # unknown login: offline
                login = str(user.get("login", "")).lower()
                stream = user.get("stream")
                if login not in out or not stream or stream.get("type", "live") != "live":
                    continue
                game = stream.get("game") or {}
                out[login] = LiveInfo(live=True, title=stream.get("title") or "",
                                      category=game.get("name") or "",
                                      viewers=_int_or_none(stream.get("viewersCount")),
                                      method="gql", checked_at=now)
        return out


def _int_or_none(v: Any) -> int | None:
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None
