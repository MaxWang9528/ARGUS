"""Twitch IRC line parsing and the connection logic (with a fake websocket)."""

from __future__ import annotations

import asyncio
import random

import pytest

from argus.chat.twitch_irc import (
    TwitchChatSource,
    _JoinLimiter,
    backoff_delay,
    parse_line,
    to_chat_message,
)
from argus.core.events import Topic

REAL_PRIVMSG = (
    "@badge-info=subscriber/14;badges=subscriber/12,premium/1;client-nonce=7c1b2e;color=#FF4500;"
    "display-name=Some_Viewer;emotes=25:0-4;first-msg=0;flags=;id=b34ccfc7-4977-403a-8a94-33c6bac34fb8;"
    "mod=0;returning-chatter=0;room-id=71092938;subscriber=1;tmi-sent-ts=1700000000123;turbo=0;"
    "user-id=123456789;user-type= :some_viewer!some_viewer@some_viewer.tmi.twitch.tv "
    "PRIVMSG #XQC :Kappa that was insane :D"
)


def test_parse_real_privmsg():
    line = parse_line(REAL_PRIVMSG + "\r\n")
    assert line is not None
    assert line.command == "PRIVMSG"
    assert line.params == ["#XQC", "Kappa that was insane :D"]
    assert line.nick == "some_viewer"
    assert line.tags["user-id"] == "123456789"
    assert line.tags["display-name"] == "Some_Viewer"
    assert line.tags["user-type"] == ""
    assert line.tags["flags"] == ""

    msg = to_chat_message(line, ts=1700000000.5)
    assert msg is not None
    assert msg.streamer == "xqc"
    assert msg.ts == 1700000000.5
    assert msg.user_id == "123456789"
    assert msg.user == "Some_Viewer"
    assert msg.text == "Kappa that was insane :D"


def test_parse_ping():
    line = parse_line("PING :tmi.twitch.tv")
    assert line is not None and line.command == "PING" and line.params == ["tmi.twitch.tv"]
    assert to_chat_message(line, 0) is None


def test_parse_tagged_non_privmsg_lines():
    line = parse_line("@emote-sets=0;user-type= :tmi.twitch.tv GLOBALUSERSTATE")
    assert line is not None and line.command == "GLOBALUSERSTATE" and line.params == []
    line = parse_line(":tmi.twitch.tv 001 justinfan12345 :Welcome, GLHF!")
    assert line is not None and line.command == "001" and line.params == ["justinfan12345", "Welcome, GLHF!"]
    line = parse_line("@msg-id=msg_channel_suspended :tmi.twitch.tv NOTICE #gone :This channel does not exist")
    assert line is not None and line.command == "NOTICE" and line.tags["msg-id"] == "msg_channel_suspended"
    line = parse_line(":justinfan1!justinfan1@justinfan1.tmi.twitch.tv JOIN #somechannel")
    assert line is not None and line.command == "JOIN" and line.params == ["#somechannel"]
    assert parse_line(":tmi.twitch.tv RECONNECT").command == "RECONNECT"
    assert parse_line("") is None and parse_line("   \r\n") is None


def test_tag_escapes_and_empty_display_name():
    line = parse_line(r"@display-name=;system-msg=hello\sworld\:\\done;user-id=42 :someone!someone@x "
                      "PRIVMSG #chan :\x01ACTION waves at chat\x01")
    assert line.tags["system-msg"] == "hello world;\\done"
    msg = to_chat_message(line, 1.0)
    assert msg.user == "someone"           # login when display-name is empty
    assert msg.text == "waves at chat"     # /me action unwrapped
    assert msg.user_id == "42"


def test_privmsg_without_tags():
    msg = to_chat_message(parse_line(":bob!bob@bob.tmi.twitch.tv PRIVMSG #chan :hi"), 2.0)
    assert msg.streamer == "chan" and msg.user == "bob" and msg.user_id == "" and msg.text == "hi"


def test_backoff_grows_to_a_minute_with_jitter():
    rng = random.Random(0)
    delays = [backoff_delay(a, rng) for a in range(12)]
    assert 1.0 <= delays[0] <= 1.0 + 1e-9
    assert all(1.0 <= d <= 60.0 for d in delays)
    assert delays[-1] >= 30.0
    assert len({round(backoff_delay(8, rng), 3) for _ in range(10)}) > 1  # jitter


async def test_join_limiter_spaces_out_joins():
    clock = [0.0]
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)
        clock[0] += s

    lim = _JoinLimiter(limit=20, window_s=10.0, clock=lambda: clock[0], sleep=fake_sleep)
    sent_at = []
    for _ in range(45):
        await lim.wait()
        sent_at.append(clock[0])
    assert sent_at[19] == 0.0 and sent_at[20] >= 10.0 and sent_at[40] >= 20.0
    # Never more than 20 in any 10 s window.
    assert all(sum(1 for t in sent_at if s <= t < s + 10) <= 20 for s in sent_at)


# ---- the connection with a fake websocket ----------------------------------------------
class FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.incoming: asyncio.Queue = asyncio.Queue()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def recv(self) -> str:
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    def push(self, *lines: str) -> None:
        self.incoming.put_nowait("\r\n".join(lines) + "\r\n")


async def until(cond, timeout: float = 2.0) -> None:
    async def wait():
        while not cond():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), timeout)


@pytest.fixture
def sockets():
    return [FakeWS(), FakeWS(), FakeWS()]


async def test_source_joins_answers_ping_follows_config_and_reconnects(store, bus, sockets):
    opened: list[FakeWS] = []

    def connect(url):
        ws = sockets[len(opened)]
        opened.append(ws)
        return ws

    async def no_sleep(_):
        await asyncio.sleep(0)

    src = TwitchChatSource(store=store, bus=bus, connect=connect, record=False,
                           limiter=_JoinLimiter(limit=100), sleep=no_sleep)
    chat = bus.subscribe(Topic.CHAT)
    task = asyncio.create_task(src.run())
    try:
        await until(lambda: len(opened) == 1 and len(sockets[0].sent) >= 3)
        ws = sockets[0]
        assert ws.sent[0] == "CAP REQ :twitch.tv/tags twitch.tv/commands"
        assert ws.sent[2].startswith("NICK justinfan") and len(ws.sent[2]) == len("NICK justinfan") + 5

        # Two lines in one frame: welcome and a ping.
        ws.push(":tmi.twitch.tv 001 justinfan12345 :Welcome, GLHF!", "PING :tmi.twitch.tv")
        await until(lambda: {"JOIN #alpha", "JOIN #beta"} <= set(ws.sent))
        assert "PONG :tmi.twitch.tv" in ws.sent

        ws.push("@user-id=7;display-name=Viewer :viewer!viewer@viewer.tmi.twitch.tv PRIVMSG #alpha :POG")
        _, msg = await asyncio.wait_for(chat.get(), 1.0)
        assert (msg.streamer, msg.user, msg.user_id, msg.text) == ("alpha", "Viewer", "7", "POG")

        # Config change: beta removed, gamma added.
        store.update({"streamers": [{"name": "alpha"}, {"name": "Gamma"}]})
        await until(lambda: "PART #beta" in ws.sent and "JOIN #gamma" in ws.sent)
        assert src.joined == {"alpha", "gamma"}

        # The connection drops: reconnect and rejoin from the current config.
        ws.incoming.put_nowait(ConnectionResetError("boom"))
        await until(lambda: len(opened) == 2 and len(sockets[1].sent) >= 3)
        ws2 = sockets[1]
        ws2.push(":tmi.twitch.tv 001 justinfan12345 :Welcome, GLHF!")
        await until(lambda: {"JOIN #alpha", "JOIN #gamma"} <= set(ws2.sent))
        assert "JOIN #beta" not in ws2.sent

        # Twitch asks us to reconnect.
        ws2.push(":tmi.twitch.tv RECONNECT")
        await until(lambda: len(opened) == 3)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert src.connections == 2
    assert len(bus._subs) == 1  # only the test's own subscription is left


async def test_source_starts_the_chat_log(store, bus):
    ws = FakeWS()
    src = TwitchChatSource(store=store, bus=bus, connect=lambda url: ws, limiter=_JoinLimiter(limit=100))
    task = asyncio.create_task(src.run())
    try:
        await until(lambda: len(ws.sent) >= 3)
        ws.push(":tmi.twitch.tv 001 justinfan1 :Welcome",
                "@user-id=1;display-name=A :a!a@a PRIVMSG #alpha :hello")
        logdir = store.get().data_dir / "chatlogs" / "alpha"
        await until(lambda: logdir.exists() and any(p.read_text() for p in logdir.glob("*.jsonl")))
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_arrival_clock_restores_send_times_after_a_stall():
    from argus.chat.twitch_irc import ArrivalClock, sent_ts
    clock = ArrivalClock()
    # Normal traffic: received 0.3 s after Twitch sent it, local clock 2 s ahead of Twitch.
    for i in range(20):
        sent = 1000.0 + i
        assert abs(clock.stamp(sent + 2.3, sent) - (sent + 2.3)) < 1e-9
    # A 30 s stall, then the backlog arrives at once: each keeps its own time.
    stamped = [clock.stamp(1050.0, 1020.0 + i) for i in range(28)]
    assert abs(stamped[0] - 1022.3) < 1e-9 and abs(stamped[-1] - 1049.3) < 1e-9
    # No send time: the receive time.
    assert clock.stamp(1060.0, None) == 1060.0
    assert sent_ts(parse_line(REAL_PRIVMSG)) == 1700000000.123


def test_arrival_clock_survives_a_stall_longer_than_its_window():
    from argus.chat.twitch_irc import ArrivalClock
    clock = ArrivalClock(window_s=60.0)
    for i in range(20):
        clock.stamp(1000.3 + i, 1000.0 + i)
    # 90 s of nothing, then the backlog arrives at once.
    stamped = [clock.stamp(1110.0, 1020.0 + i) for i in range(89)]
    assert abs(stamped[0] - 1020.3) < 1e-9 and abs(stamped[-1] - 1108.3) < 1e-9
