"""``StreamDiscovery``: every ``discovery.poll_s`` seconds, ask Helix for the top live
streams and hand the channel names to ``ConfigStore.set_discovered``.

From there they are ordinary streamers: ``cfg.enabled_streamers()`` includes them, so
the recorder live-checks and records them (within ``general.max_streamers``), chat joins
and parts them (``streamers_changed`` wakes it; joins are spaced by its own limiter), the
detector tracks them and the dashboard lists them. Nothing is written to config.toml.

The recorder's live checker uses ``live_hint`` so channels discovery just saw live
(with viewers and category) aren't checked a second time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from ..capture.live_check import LiveInfo
from ..core.config import ConfigStore
from ..core.events import EventBus, Topic
from .helix import SETUP_HINT, HelixClient, HelixError, RateLimited
from .ranking import DiscoveredStream, eligible_streams, hold, watch_order

log = logging.getLogger("argus.discover")

HINT_GRACE_S = 30.0  # a hint stays usable for poll_s + this


class StreamDiscovery:
    def __init__(self, *, store: ConfigStore, bus: EventBus, http: httpx.AsyncClient | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.store, self.bus = store, bus
        self._http = http
        self._clock = clock
        self._client: HelixClient | None = None
        self._held: dict[str, float] = {}             # channel -> last time in the top N
        self._top: list[str] = []
        self._seen: dict[str, DiscoveredStream] = {}  # every eligible live stream last poll
        self._fetched_at = 0.0
        self.last_poll = 0.0
        self.last_error = ""
        self.categories_found: list[str] = []
        self._warned_setup = False

    # ---- one round --------------------------------------------------------------
    def _off(self) -> None:
        self._held.clear()
        self._top, self._seen, self._fetched_at = [], {}, 0.0
        self.store.set_discovered(None)

    def _helix(self) -> HelixClient | None:
        cid, secret = self.store.get().twitch_credentials()
        if not (cid and secret):
            return None
        if self._client is None or not self._client.same_keys(cid, secret):
            self._client = HelixClient(cid, secret, http=self._http, clock=self._clock)
        return self._client

    async def poll_once(self) -> float:
        """Fetch, rank and publish once. Returns seconds until the next poll."""
        cfg = self.store.get()
        d = cfg.discovery
        if not d.enabled:
            self.last_error = ""
            self._off()
            return d.poll_s
        client = self._helix()
        if client is None:
            self.last_error = SETUP_HINT
            if not self._warned_setup:
                log.warning(SETUP_HINT)
                self._warned_setup = True
            self._off()
            return d.poll_s
        self._warned_setup = False
        exclude = list(d.exclude) + [s.name for s in cfg.streamers if not s.enabled]
        try:
            ids: list[str] = []
            if d.categories:
                found = await client.game_ids(d.categories)
                self.categories_found = [name for _, name in found.values()]
                ids = [gid for gid, _ in found.values()]
                if not ids:
                    raise HelixError("none of discovery.categories is a Twitch category "
                                     "(names must match Twitch exactly)")
            items = await client.top_streams(
                game_ids=ids, languages=d.languages, min_viewers=d.min_viewers,
                limit=d.top_n + len(exclude) + 20)
        except RateLimited as exc:
            self.last_error = str(exc)
            log.warning("discovery: %s", exc)
            return exc.wait_s
        except (HelixError, httpx.HTTPError) as exc:
            # Keep watching what we have: a Twitch hiccup shouldn't drop every channel.
            self.last_error = f"{type(exc).__name__}: {exc}" if isinstance(exc, httpx.HTTPError) else str(exc)
            log.warning("discovery failed: %s", self.last_error)
            return d.poll_s
        now = self._clock()
        live = eligible_streams(items, min_viewers=d.min_viewers, exclude=exclude,
                                languages=d.languages, category_ids=ids)
        top = [s.login for s in live[:d.top_n]]
        self._held = hold(self._held, top, now, d.keep_minutes * 60, exclude)
        self._top = top
        self._seen = {s.login: s for s in live}
        self._fetched_at = self.last_poll = now
        self.last_error = ""
        names = watch_order(top, self._held)
        before = set(cfg.discovered_names())
        if self.store.set_discovered(names):
            added, gone = set(names) - before, before - set(names)
            if added or gone:
                log.info("discovery: watching %d channels (%d new, %d dropped)",
                         len(names), len(added), len(gone))
        return d.poll_s

    # ---- component --------------------------------------------------------------
    async def run(self) -> None:
        sub = self.bus.subscribe(Topic.CONFIG_CHANGED)
        try:
            while True:
                try:
                    delay = await self.poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("discovery round failed")
                    delay = 60.0
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wait_for_change(sub), max(1.0, delay))
        finally:
            sub.close()
            if self._client is not None:
                await self._client.aclose()

    @staticmethod
    async def _wait_for_change(sub: Any) -> None:
        """Return early when discovery or the Twitch keys are edited."""
        while True:
            _, payload = await sub.get()
            patch = payload.get("patch") if isinstance(payload, dict) else None
            if isinstance(patch, dict) and ({"discovery", "twitch", "streamers"} & patch.keys()):
                return

    # ---- for the live checker and the dashboard ----------------------------------
    def live_hint(self, name: str) -> LiveInfo | None:
        """A fresh live answer from the last poll, or None (check it the usual way)."""
        s = self._seen.get(name.lower())
        if s is None or self._clock() - self._fetched_at > self.store.get().discovery.poll_s + HINT_GRACE_S:
            return None
        return LiveInfo(live=True, title=s.title, category=s.category, viewers=s.viewers,
                        method="helix", checked_at=self._fetched_at)

    def public_state(self) -> dict[str, Any]:
        """For ``GET /api/state``. Has no credentials in it."""
        cfg = self.store.get()
        d = cfg.discovery
        cid, secret = cfg.twitch_credentials()
        keep_s = d.keep_minutes * 60
        rank = {n: i + 1 for i, n in enumerate(self._top)}
        streams = []
        for name in cfg.discovered_names():
            s = self._seen.get(name)
            streams.append({
                "name": name, "rank": rank.get(name), "in_top": name in rank,
                "viewers": s.viewers if s else None, "category": s.category if s else "",
                "held_until": None if name in rank else self._held.get(name, 0.0) + keep_s,
            })
        return {
            "enabled": d.enabled,
            "active": d.enabled and bool(cid and secret) and self.last_poll > 0,
            "has_credentials": bool(cid and secret),
            "setup": "" if cid and secret else SETUP_HINT,
            "mode": d.mode,
            "error": self.last_error if self.last_error != SETUP_HINT else "",
            "last_poll": self.last_poll or None,
            "streams": streams,
        }
