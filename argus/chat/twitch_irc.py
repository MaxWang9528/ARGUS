"""Anonymous Twitch chat over IRC-on-websocket.

One connection joins every enabled streamer's channel, publishes each PRIVMSG as a
``ChatMessage`` on ``Topic.CHAT`` and follows config changes (join new channels,
part removed ones). It reconnects with exponential backoff and rejoins. ``run()``
also runs the chat log recorder.

``parse_line`` and ``to_chat_message`` are pure functions.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..core.config import ConfigStore
from ..core.events import EventBus, Topic
from ..core.models import ChatMessage
from .chatlog import ChatLogRecorder

log = logging.getLogger("argus.chat")

TWITCH_IRC_URL = "wss://irc-ws.chat.twitch.tv:443"
JOINS_PER_WINDOW = 18      # Twitch allows about 20 JOINs per 10 s for anonymous users
JOIN_WINDOW_S = 10.0
IDLE_TIMEOUT_S = 360.0     # Twitch sends PING about every 5 minutes
STABLE_AFTER_S = 30.0      # a connection that lived this long resets the backoff
BACKOFF_MIN_S, BACKOFF_MAX_S = 1.0, 60.0


# ---- pure parsing -----------------------------------------------------------------
_TAG_ESCAPES = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}


def _unescape_tag(value: str) -> str:
    if "\\" not in value:
        return value
    out, i = [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\":
            if i + 1 < len(value):
                nxt = value[i + 1]
                out.append(_TAG_ESCAPES.get(nxt, nxt))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


@dataclass(frozen=True)
class IrcLine:
    command: str
    params: list[str] = field(default_factory=list)
    prefix: str = ""
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def nick(self) -> str:
        """The login part of ``nick!user@host``."""
        return self.prefix.split("!", 1)[0].split("@", 1)[0]


def parse_line(line: str) -> IrcLine | None:
    """Parse one IRC line (IRCv3 tags supported). None for blank input."""
    line = line.rstrip("\r\n")
    if not line.strip():
        return None
    tags: dict[str, str] = {}
    prefix = ""
    rest = line
    if rest.startswith("@"):
        raw_tags, _, rest = rest[1:].partition(" ")
        for item in raw_tags.split(";"):
            if not item:
                continue
            key, _, value = item.partition("=")
            tags[key] = _unescape_tag(value)
        rest = rest.lstrip(" ")
    if rest.startswith(":"):
        prefix, _, rest = rest[1:].partition(" ")
        rest = rest.lstrip(" ")
    trailing: str | None = None
    if " :" in rest:
        rest, _, trailing = rest.partition(" :")
    elif rest.startswith(":"):
        rest, trailing = "", rest[1:]
    parts = rest.split()
    if not parts:
        return None
    params = parts[1:]
    if trailing is not None:
        params.append(trailing)
    return IrcLine(command=parts[0].upper(), params=params, prefix=prefix, tags=tags)


def to_chat_message(line: IrcLine, ts: float) -> ChatMessage | None:
    """A ChatMessage for a PRIVMSG line, else None. ``ts`` is the receive time."""
    if line.command != "PRIVMSG" or len(line.params) < 2:
        return None
    channel = line.params[0].lstrip("#").lower()
    text = line.params[1]
    if text.startswith("\x01ACTION ") and text.endswith("\x01"):
        text = text[len("\x01ACTION "):-1]
    user = line.tags.get("display-name") or line.nick
    return ChatMessage(streamer=channel, ts=ts, user_id=line.tags.get("user-id", ""),
                       user=user, text=text)


def backoff_delay(attempt: int, rng: random.Random | None = None) -> float:
    """1 s, 2 s, 4 s ... up to 60 s, with jitter (between half and the full delay)."""
    rng = rng or random
    base = min(BACKOFF_MAX_S, BACKOFF_MIN_S * 2 ** min(attempt, 10))
    return max(BACKOFF_MIN_S, base * rng.uniform(0.5, 1.0))


class _Reconnect(Exception):
    """The server asked us to reconnect, or the connection went quiet."""


class _JoinLimiter:
    """Sliding-window limit on JOIN/PART commands."""

    def __init__(self, limit: int = JOINS_PER_WINDOW, window_s: float = JOIN_WINDOW_S,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        self.limit, self.window_s, self.clock, self.sleep = limit, window_s, clock, sleep
        self._sent: deque[float] = deque()

    async def wait(self) -> None:
        while True:
            now = self.clock()
            while self._sent and self._sent[0] <= now - self.window_s:
                self._sent.popleft()
            if len(self._sent) < self.limit:
                self._sent.append(now)
                return
            await self.sleep(self._sent[0] + self.window_s - now + 0.01)


def _default_connect(url: str) -> Any:
    from websockets.asyncio.client import connect

    # Twitch closes connections that ignore its own IRC PINGs; websocket pings
    # are an extra keepalive that also catches a dead TCP connection.
    return connect(url, open_timeout=15, ping_interval=30, ping_timeout=30, max_size=2 ** 22)


# ---- the component ----------------------------------------------------------------
class TwitchChatSource:
    """Reads chat for every enabled streamer. Implements ``ChatSource``.

    ``connect(url)`` must return an async context manager yielding an object with
    ``send(str)`` and ``recv() -> str`` (a websockets connection). Tests pass a fake.
    """

    def __init__(self, *, store: ConfigStore, bus: EventBus,
                 connect: Callable[[str], Any] = _default_connect, url: str = TWITCH_IRC_URL,
                 record: bool = True, limiter: _JoinLimiter | None = None,
                 sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        self.store, self.bus = store, bus
        self._connect, self.url, self.record = connect, url, record
        self._limiter = limiter or _JoinLimiter()
        self._sleep = sleep
        self._ws: Any = None
        self._generation = 0
        self._joined: set[str] = set()
        self._wake = asyncio.Event()
        self.connections = 0       # successful connections so far (for status / tests)
        self.messages = 0          # PRIVMSGs published

    @property
    def joined(self) -> set[str]:
        return set(self._joined)

    def _wanted(self) -> set[str]:
        return {s.name.lower() for s in self.store.get().enabled_streamers()}

    async def run(self) -> None:
        sub = self.bus.subscribe(Topic.CONFIG_CHANGED)
        try:
            async with asyncio.TaskGroup() as tg:
                if self.record:
                    tg.create_task(ChatLogRecorder(store=self.store, bus=self.bus).run())
                tg.create_task(self._watch_config(sub))
                tg.create_task(self._sync_channels())
                tg.create_task(self._connection_loop())
        finally:
            sub.close()

    async def _watch_config(self, sub: Any) -> None:
        async for _ in sub:
            self._wake.set()

    # ---- connection -----------------------------------------------------------------
    async def _connection_loop(self) -> None:
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except _Reconnect as exc:
                log.info("reconnecting to Twitch chat: %s", exc)
            except Exception as exc:  # noqa: BLE001 - network errors of every kind
                log.warning("Twitch chat connection failed: %s: %s", type(exc).__name__, exc)
            if time.monotonic() - started >= STABLE_AFTER_S:
                attempt = 0
            delay = backoff_delay(attempt)
            attempt += 1
            await self._sleep(delay)

    async def _session(self) -> None:
        nick = f"justinfan{random.randint(10000, 99999)}"
        async with self._connect(self.url) as ws:
            try:
                await ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
                await ws.send("PASS SCHMOOPIIE")
                await ws.send(f"NICK {nick}")
                while True:
                    try:
                        frame = await asyncio.wait_for(ws.recv(), IDLE_TIMEOUT_S)
                    except TimeoutError:
                        raise _Reconnect("no data from Twitch for a while") from None
                    if isinstance(frame, bytes):
                        frame = frame.decode("utf-8", "replace")
                    now = time.time()
                    for raw in frame.split("\r\n"):
                        line = parse_line(raw)
                        if line is not None:
                            await self._handle(ws, line, now)
            finally:
                if self._ws is ws:
                    self._ws = None
                    self._joined.clear()

    async def _handle(self, ws: Any, line: IrcLine, now: float) -> None:
        cmd = line.command
        if cmd == "PRIVMSG":
            msg = to_chat_message(line, now)
            if msg is not None:
                self.messages += 1
                self.bus.publish(Topic.CHAT, msg)
        elif cmd == "PING":
            await ws.send("PONG :" + (line.params[-1] if line.params else "tmi.twitch.tv"))
        elif cmd == "001":
            self.connections += 1
            self._generation += 1
            self._ws = ws
            self._joined.clear()
            log.info("connected to Twitch chat as %s", line.params[0] if line.params else "?")
            self._wake.set()
        elif cmd == "RECONNECT":
            raise _Reconnect("Twitch asked us to reconnect")
        elif cmd == "NOTICE":
            channel = line.params[0].lstrip("#") if line.params else ""
            log.warning("Twitch notice: %s", line.params[-1] if line.params else "",
                        extra={"streamer": channel if channel != "*" else ""})
        elif cmd == "PART":
            self._joined.discard(line.params[0].lstrip("#").lower() if line.params else "")

    # ---- join / part ----------------------------------------------------------------
    async def _sync_channels(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                await self._sync_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the connection loop reconnects
                log.debug("channel sync stopped: %s", exc)

    async def _sync_once(self) -> None:
        ws, gen = self._ws, self._generation
        if ws is None:
            return
        wanted = self._wanted()
        for name in sorted(self._joined - wanted):
            await self._limiter.wait()
            if self._generation != gen or self._ws is not ws:
                return
            await ws.send(f"PART #{name}")
            self._joined.discard(name)
            log.info("left #%s", name, extra={"streamer": name})
        for name in sorted(wanted - self._joined):
            if name not in self._wanted():
                continue  # removed while we waited
            await self._limiter.wait()
            if self._generation != gen or self._ws is not ws:
                return
            await ws.send(f"JOIN #{name}")
            self._joined.add(name)
            log.info("joined #%s", name, extra={"streamer": name})
        if self._wanted() != self._joined:
            self._wake.set()  # the config changed while we were joining
