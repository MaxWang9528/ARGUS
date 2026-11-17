"""A small client for the official Twitch Helix API (no website endpoints, no scraping).

- App access token from the client-credentials flow (``POST id.twitch.tv/oauth2/token``),
  cached until a minute before it expires and fetched again once on a 401.
- ``GET /helix/streams?type=live&first=100&game_id=..&language=..`` pages with ``after``;
  Twitch returns streams by viewer count, highest first.
- ``GET /helix/games?name=..`` turns exact category names into ids (cached).
- A 429 raises ``RateLimited`` with how long to wait (from ``Ratelimit-Reset``).

The client secret is only ever sent in the token request body. It is never logged, put
in an exception message or returned.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from typing import Any

import httpx

log = logging.getLogger("argus.discover")

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
STREAMS_URL = "https://api.twitch.tv/helix/streams"
GAMES_URL = "https://api.twitch.tv/helix/games"
PAGE = 100       # Helix's largest page
MAX_PAGES = 10   # at most 1000 streams (10 points) per poll
SETUP_HINT = ("Stream discovery needs Twitch app keys: create an app at https://dev.twitch.tv/console, "
              "then set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET (or twitch.client_id / "
              "twitch.client_secret in config.toml).")


class HelixError(Exception):
    """A Helix request failed. The message never contains credentials."""


class MissingCredentials(HelixError):
    def __init__(self) -> None:
        super().__init__(SETUP_HINT)


class RateLimited(HelixError):
    def __init__(self, wait_s: float) -> None:
        super().__init__(f"Twitch rate limit hit; waiting {wait_s:.0f} s")
        self.wait_s = wait_s


def reset_wait(headers: httpx.Headers, now: float, default: float = 60.0) -> float:
    """Seconds until ``Ratelimit-Reset`` (an epoch time), within 1-300."""
    try:
        wait = float(headers.get("ratelimit-reset", "")) - now
    except ValueError:
        wait = default
    return min(300.0, max(1.0, wait))


class HelixClient:
    def __init__(self, client_id: str, client_secret: str, *, http: httpx.AsyncClient | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        if not client_id or not client_secret:
            raise MissingCredentials()
        self.client_id = client_id
        self._secret = client_secret
        self._http = http
        self._own_http = http is None
        self._clock = clock
        self._token = ""
        self._token_expires = 0.0
        self._games: dict[str, tuple[str, str]] = {}  # lower name -> (id, name)
        self.token_requests = 0  # for tests and the status line
        self.requests = 0

    def __repr__(self) -> str:  # never show the secret
        return f"HelixClient(client_id={self.client_id!r})"

    def same_keys(self, client_id: str, client_secret: str) -> bool:
        return (client_id, client_secret) == (self.client_id, self._secret)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=10)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and self._own_http:
            await self._http.aclose()
        self._http = None

    # ---- auth --------------------------------------------------------------------
    async def token(self, *, force: bool = False) -> str:
        if not force and self._token and self._clock() < self._token_expires - 60:
            return self._token
        self.token_requests += 1
        r = await self._client().post(TOKEN_URL, data={
            "client_id": self.client_id, "client_secret": self._secret,
            "grant_type": "client_credentials"})
        if r.status_code == 429:
            raise RateLimited(reset_wait(r.headers, self._clock()))
        if r.status_code in (400, 401, 403):
            raise HelixError(f"Twitch refused the app keys (HTTP {r.status_code}): {self._message(r)}. "
                             "Check the client id and secret at https://dev.twitch.tv/console.")
        if r.status_code >= 400:
            raise HelixError(f"token request failed: HTTP {r.status_code}")
        try:
            body = r.json()
            self._token = str(body["access_token"])
            expires = float(body.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError) as exc:
            raise HelixError("token request answered without a token") from exc
        self._token_expires = self._clock() + expires
        return self._token

    async def get(self, url: str, params: list[tuple[str, str]]) -> dict[str, Any]:
        """GET a Helix endpoint: refreshes the token once on 401, raises RateLimited on 429."""
        for attempt in (0, 1):
            token = await self.token(force=attempt == 1)
            self.requests += 1
            r = await self._client().get(url, params=params, headers={
                "Client-Id": self.client_id, "Authorization": f"Bearer {token}"})
            if r.status_code == 401 and attempt == 0:
                self._token = ""
                continue  # expired or revoked: one new token
            if r.status_code == 429:
                raise RateLimited(reset_wait(r.headers, self._clock()))
            if r.status_code >= 400:
                raise HelixError(f"{url.rsplit('/', 1)[-1]}: HTTP {r.status_code}: {self._message(r)}")
            try:
                body = r.json()
            except ValueError as exc:
                raise HelixError("Helix answered with something that isn't JSON") from exc
            if not isinstance(body, dict):
                raise HelixError("Helix answered with an unexpected shape")
            return body
        raise HelixError("Twitch keeps refusing the app access token (HTTP 401)")  # pragma: no cover

    def _message(self, r: httpx.Response) -> str:
        """Twitch's error message, with the secret blanked should it ever be echoed."""
        try:
            body = r.json()
            text = str(body.get("message") or body.get("error") or "")[:200]
        except (ValueError, AttributeError):
            text = ""
        return text.replace(self._secret, "***")

    # ---- endpoints ---------------------------------------------------------------
    async def game_ids(self, names: Iterable[str]) -> dict[str, tuple[str, str]]:
        """{lower name: (id, Twitch's name)} for the names Twitch knows (exact names)."""
        wanted = list(dict.fromkeys(n.strip() for n in names if n.strip()))
        missing = [n for n in wanted if n.lower() not in self._games]
        for i in range(0, len(missing), PAGE):
            chunk = missing[i:i + PAGE]
            body = await self.get(GAMES_URL, [("name", n) for n in chunk])
            for g in body.get("data") or []:
                gid, gname = str(g.get("id") or ""), str(g.get("name") or "")
                if gid and gname:
                    self._games[gname.lower()] = (gid, gname)
            for n in chunk:
                if n.lower() not in self._games:
                    log.warning("discovery: Twitch has no category called %r (use the exact name)", n)
                    self._games[n.lower()] = ("", n)  # don't ask again
        return {n.lower(): self._games[n.lower()] for n in wanted if self._games[n.lower()][0]}

    async def top_streams(self, *, game_ids: Iterable[str] = (), languages: Iterable[str] = (),
                          limit: int = 100, min_viewers: int = 0,
                          max_pages: int = MAX_PAGES) -> list[dict[str, Any]]:
        """Live streams, most viewers first: at least ``limit`` (when that many exist),
        stopping early once counts fall below ``min_viewers``."""
        base = [("type", "live"), ("first", str(PAGE))]
        base += [("game_id", g) for g in game_ids][:100]
        base += [("language", lang) for lang in languages if lang][:100]
        out: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(max(1, max_pages)):
            params = base + ([("after", cursor)] if cursor else [])
            body = await self.get(STREAMS_URL, params)
            data = [d for d in body.get("data") or [] if isinstance(d, dict)]
            out.extend(data)
            cursor = str((body.get("pagination") or {}).get("cursor") or "")
            low = min((_int(d.get("viewer_count")) for d in data), default=0)
            if not cursor or not data or len(out) >= limit or low < min_viewers:
                break
        return out


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0
