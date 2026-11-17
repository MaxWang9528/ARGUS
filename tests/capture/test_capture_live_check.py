from __future__ import annotations

import json
from typing import ClassVar

import httpx
import pytest
from streamlink.exceptions import NoStreamsError, PluginError

from livewatcher.capture.live_check import LiveChecker, streamlink_check
from livewatcher.core.config import ConfigStore


class FakePlugin:
    """Stands in for streamlink's Twitch plugin."""

    channels: ClassVar[dict[str, object]] = {}

    def __init__(self, session, url):
        self.name = url.rstrip("/").rsplit("/", 1)[-1]

    def streams(self):
        what = self.channels.get(self.name, {})
        if isinstance(what, Exception):
            raise what
        return what

    def get_title(self):
        return f"title of {self.name}"


class FakeSession:
    def __init__(self):
        self.calls: list[str] = []

    def resolve_url(self, url):
        self.calls.append(url)
        return "twitch", FakePlugin, url


@pytest.fixture
def fake_channels():
    FakePlugin.channels = {
        "alpha": {"720p60": object(), "best": object()},
        "beta": {},
        "gamma": NoStreamsError(),
        "delta": PluginError("boom"),
    }
    yield FakePlugin.channels
    FakePlugin.channels = {}


def test_streamlink_check_states(fake_channels) -> None:
    s = FakeSession()
    a = streamlink_check(s, "alpha")
    assert a.live and a.title == "title of alpha" and a.method == "streamlink" and not a.error
    assert not streamlink_check(s, "beta").live
    g = streamlink_check(s, "gamma")
    assert not g.live and not g.error
    d = streamlink_check(s, "delta")
    assert not d.live and "boom" in d.error
    assert s.calls[0] == "https://twitch.tv/alpha"


def _down() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))


async def test_checker_falls_back_to_streamlink_when_gql_fails(store: ConfigStore, fake_channels) -> None:
    session = FakeSession()
    checker = LiveChecker(store, session_factory=lambda: session, http=_down())
    res = await checker.check(["Alpha", "beta"])
    assert set(res) == {"alpha", "beta"}
    assert res["alpha"].live and not res["beta"].live
    assert res["alpha"].method == "streamlink" and res["alpha"].viewers is None
    await checker.check(["alpha"])
    assert len(session.calls) == 3  # session reused
    await checker.aclose()


def _gql_transport(log: list[dict], users: dict[str, dict | None], *, body: object = None):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "gql.twitch.tv"
        assert request.headers["Client-ID"]
        payload = json.loads(request.content)
        log.append(payload)
        if body is not None:
            return httpx.Response(200, json=body)
        logins = payload["variables"]["logins"]
        # unknown logins come back as null; answer in a different order than asked
        out = [None if users.get(n) is None else {"login": n, **users[n]} for n in reversed(logins)]
        return httpx.Response(200, json={"data": {"users": out}})
    return httpx.MockTransport(handler)


async def test_gql_check_without_keys(store: ConfigStore) -> None:
    users = {
        "alpha": {"stream": {"type": "live", "viewersCount": 1234, "title": "hi", "game": {"name": "GTA VI"}}},
        "beta": {"stream": None},
        "gamma": {"stream": {"type": "rerun", "viewersCount": 5, "title": "", "game": None}},
        "delta": {"stream": {"type": "live", "viewersCount": None, "title": "", "game": None}},
    }
    sent: list[dict] = []
    checker = LiveChecker(store, http=httpx.AsyncClient(transport=_gql_transport(sent, users)),
                          session_factory=lambda: pytest.fail("streamlink must not be used"))
    names = ["Alpha", "beta", "gamma", "delta", "nobody"] + [f"x{i}" for i in range(150)]
    res = await checker.check(names)
    assert len(sent) == 2 and len(sent[0]["variables"]["logins"]) == 100  # batched
    a = res["alpha"]
    assert a.live and a.viewers == 1234 and a.title == "hi" and a.category == "GTA VI" and a.method == "gql"
    assert not res["beta"].live and res["beta"].viewers is None and not res["beta"].error
    assert not res["gamma"].live
    assert res["delta"].live and res["delta"].viewers is None and res["delta"].category == ""
    assert not res["nobody"].live and not res["nobody"].error
    await checker.aclose()


@pytest.mark.parametrize("body", [{"errors": [{"message": "integrity"}]}, {"data": None}, ["x"]])
async def test_gql_error_answer_falls_back(store: ConfigStore, fake_channels, body) -> None:
    session = FakeSession()
    checker = LiveChecker(store, http=httpx.AsyncClient(transport=_gql_transport([], {}, body=body)),
                          session_factory=lambda: session)
    res = await checker.check(["alpha"])
    assert res["alpha"].live and res["alpha"].method == "streamlink"
    await checker.aclose()


def _helix_transport(log: list[httpx.Request], *, fail_first_streams: int = 0, broken: bool = False):
    state = {"streams_calls": 0, "tokens": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        if request.url.host == "id.twitch.tv":
            state["tokens"] += 1
            return httpx.Response(200, json={"access_token": f"tok{state['tokens']}", "expires_in": 3600})
        if broken:
            return httpx.Response(500)
        state["streams_calls"] += 1
        if state["streams_calls"] <= fail_first_streams:
            return httpx.Response(401)
        assert request.headers["Client-Id"] == "cid"
        assert request.headers["Authorization"].startswith("Bearer tok")
        logins = request.url.params.get_list("user_login")
        data = ([{"user_login": "Alpha", "type": "live", "title": "hi", "game_name": "GTA VI",
                  "viewer_count": 4321}] if "alpha" in logins else [])
        return httpx.Response(200, json={"data": data})

    return httpx.MockTransport(handler), state


async def test_helix_check(store: ConfigStore) -> None:
    store.update({"twitch": {"client_id": "cid", "client_secret": "sec"}})
    reqs: list[httpx.Request] = []
    transport, state = _helix_transport(reqs)
    checker = LiveChecker(store, http=httpx.AsyncClient(transport=transport),
                          session_factory=lambda: pytest.fail("streamlink must not be used"))
    res = await checker.check(["alpha", "beta"])
    assert res["alpha"].live and res["alpha"].title == "hi" and res["alpha"].method == "helix"
    assert res["alpha"].viewers == 4321 and res["alpha"].category == "GTA VI"
    assert not res["beta"].live and res["beta"].viewers is None
    await checker.check(["alpha"])
    assert state["tokens"] == 1  # token cached
    await checker.aclose()


async def test_helix_refreshes_a_rejected_token(store: ConfigStore) -> None:
    store.update({"twitch": {"client_id": "cid", "client_secret": "sec"}})
    transport, state = _helix_transport([], fail_first_streams=1)
    checker = LiveChecker(store, http=httpx.AsyncClient(transport=transport))
    res = await checker.check(["alpha"])
    assert res["alpha"].live and state["tokens"] == 2
    await checker.aclose()


async def test_helix_failure_falls_back_to_streamlink(store: ConfigStore, fake_channels) -> None:
    store.update({"twitch": {"client_id": "cid", "client_secret": "sec"}})
    transport, _ = _helix_transport([], broken=True)
    session = FakeSession()
    checker = LiveChecker(store, http=httpx.AsyncClient(transport=transport), session_factory=lambda: session)
    res = await checker.check(["alpha", "beta"])
    assert res["alpha"].live and res["alpha"].method == "streamlink"
    assert session.calls
    await checker.aclose()
