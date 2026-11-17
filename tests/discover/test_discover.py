"""Stream discovery, against a fake Helix (httpx.MockTransport). Never the network."""

from __future__ import annotations

import argparse
import json
import logging
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from argus.api.server import create_app
from argus.app import App
from argus.capture.live_check import LiveChecker
from argus.core.config import Config, ConfigStore, General, Streamer
from argus.core.events import EventBus, Topic
from argus.discover import cli as discover_cli
from argus.discover.helix import SETUP_HINT, HelixClient, HelixError, RateLimited
from argus.discover.ranking import hold, rank_streams, watch_order
from argus.discover.service import StreamDiscovery

CID = "test-client-id"
SECRET = "s3cret-discovery-value"
CAT_A, CAT_B = ("Some Category", "111"), ("Another Category", "222")


def stream(login: str, viewers: int, *, game: tuple[str, str] = CAT_A, lang: str = "en",
           kind: str = "live") -> dict[str, Any]:
    return {"user_login": login, "user_name": login.title(), "viewer_count": viewers, "game_name": game[0],
            "game_id": game[1], "language": lang, "type": kind, "title": f"{login} stream"}


class FakeHelix:
    """Pages of ``page`` streams, most viewers first, filtered like Helix."""

    def __init__(self, streams: list[dict[str, Any]], *, page: int = 2) -> None:
        self.streams = streams
        self.page = page
        self.tokens = 0
        self.valid: set[str] = set()
        self.requests: list[httpx.Request] = []
        self.fail_next: list[httpx.Response] = []  # returned before the normal answer
        self.expire_tokens = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.startswith("https://id.twitch.tv/oauth2/token"):
            body = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            if body.get("client_secret") != SECRET or body.get("client_id") != CID:
                return httpx.Response(403, json={"status": 403, "message": "invalid client secret"})
            self.tokens += 1
            tok = f"tok{self.tokens}"
            self.valid = {tok}
            return httpx.Response(200, json={"access_token": tok, "expires_in": 3600, "token_type": "bearer"})
        if self.fail_next:
            return self.fail_next.pop(0)
        assert request.headers["Client-Id"] == CID
        auth = request.headers.get("Authorization", "")
        if self.expire_tokens or auth.removeprefix("Bearer ") not in self.valid:
            self.expire_tokens = False
            return httpx.Response(401, json={"status": 401, "message": "Invalid OAuth token"})
        q = request.url.params
        if request.url.path == "/helix/games":
            games = {CAT_A[0]: CAT_A, CAT_B[0]: CAT_B}
            return httpx.Response(200, json={"data": [{"id": games[n][1], "name": n}
                                                      for n in q.get_list("name") if n in games]})
        assert request.url.path == "/helix/streams"
        assert q.get("type") == "live" and q.get("first") == "100"
        rows = sorted(self.streams, key=lambda s: -s["viewer_count"])
        if q.get_list("game_id"):
            rows = [r for r in rows if r["game_id"] in q.get_list("game_id")]
        if q.get_list("language"):
            rows = [r for r in rows if r["language"] in q.get_list("language")]
        start = int(q.get("after") or 0)
        chunk = rows[start:start + self.page]
        nxt = start + self.page
        return httpx.Response(200, json={"data": chunk, "pagination": {"cursor": str(nxt)} if nxt < len(rows) else {}})


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def _no_env_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TWITCH_CLIENT_ID", raising=False)
    monkeypatch.delenv("TWITCH_CLIENT_SECRET", raising=False)


def make_store(tmp_path, *, keys: bool = True, streamers=("alpha", "beta"), **discovery) -> ConfigStore:
    cfg = Config.model_validate({
        "general": {"data_dir": str(tmp_path / "data")},
        "twitch": {"client_id": CID, "client_secret": SECRET} if keys else {},
        "discovery": {"enabled": True, **discovery},
        "streamers": [{"name": n} for n in streamers],
    })
    return ConfigStore(config=cfg, bus=EventBus())


def make_discovery(store: ConfigStore, helix: FakeHelix, clock: Clock | None = None) -> StreamDiscovery:
    return StreamDiscovery(store=store, bus=store.bus, clock=clock or Clock(),
                           http=httpx.AsyncClient(transport=httpx.MockTransport(helix)))


def http_for(helix: FakeHelix) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(helix))


# ---- the pure part -----------------------------------------------------------------
def test_rank_filters_sorts_and_dedupes() -> None:
    items = [stream("a", 50), stream("b", 900), stream("c", 10), stream("d", 300, lang="de"),
             stream("e", 700, kind="rerun"), stream("B", 950), stream("f", 300, game=CAT_B),
             {"user_login": "", "viewer_count": 5}]
    ranked = rank_streams(items, top_n=10, min_viewers=20, exclude=["@A"], languages=["EN"])
    assert [s.login for s in ranked] == ["b", "f"]
    assert ranked[0].viewers == 950 and ranked[0].category == CAT_A[0]
    assert [s.login for s in rank_streams(items, top_n=1)] == ["b"]
    assert [s.login for s in rank_streams(items, top_n=5, category_ids=["222"])] == ["f"]


def test_hold_keeps_recent_and_drops_excluded() -> None:
    held = hold({}, ["a", "b"], 100.0, 600)
    held = hold(held, ["b", "c"], 400.0, 600)
    assert held == {"a": 100.0, "b": 400.0, "c": 400.0}
    assert watch_order(["c", "b"], held) == ["c", "b", "a"]
    assert hold(held, ["c"], 701.0, 600) == {"b": 400.0, "c": 701.0}
    assert hold(held, [], 401.0, 600, exclude=["b"]) == {"a": 100.0, "c": 400.0}


# ---- the Helix client --------------------------------------------------------------
async def test_token_is_cached_and_refreshed_on_401() -> None:
    helix = FakeHelix([stream("a", 5)])
    clock = Clock()
    client = HelixClient(CID, SECRET, http=http_for(helix), clock=clock)
    await client.top_streams()
    await client.top_streams()
    assert helix.tokens == 1
    helix.expire_tokens = True  # revoked: one 401, then a new token
    assert len(await client.top_streams()) == 1
    assert helix.tokens == 2
    clock.t += 3600  # expired by time: fetched again before asking
    await client.top_streams()
    assert helix.tokens == 3
    auth = helix.requests[-1].headers["Authorization"]
    assert auth == "Bearer tok3" and helix.requests[-1].headers["Client-Id"] == CID


async def test_pagination_follows_the_cursor_and_stops() -> None:
    helix = FakeHelix([stream(f"c{i}", 1000 - i * 10) for i in range(9)], page=2)
    client = HelixClient(CID, SECRET, http=http_for(helix))
    got = await client.top_streams(limit=5)
    assert [s["user_login"] for s in got] == ["c0", "c1", "c2", "c3", "c4", "c5"]  # 3 pages
    pages = [r for r in helix.requests if r.url.path == "/helix/streams"]
    assert [r.url.params.get("after") for r in pages] == [None, "2", "4"]
    helix.requests.clear()
    assert len(await client.top_streams(limit=100)) == 9  # ends when the cursor does
    helix.requests.clear()
    await client.top_streams(limit=100, min_viewers=975)  # c3 (970) is below: stop there
    assert len([r for r in helix.requests if r.url.path == "/helix/streams"]) == 2


async def test_category_names_become_ids_and_are_cached(caplog: pytest.LogCaptureFixture) -> None:
    helix = FakeHelix([stream("a", 50), stream("b", 40, game=CAT_B)])
    client = HelixClient(CID, SECRET, http=http_for(helix))
    with caplog.at_level(logging.WARNING, logger="argus.discover"):
        found = await client.game_ids(["Another Category", "No Such Category"])
    assert found == {"another category": ("222", "Another Category")}
    assert "No Such Category" in caplog.text
    n = len(helix.requests)
    assert await client.game_ids(["another category", "No Such Category"]) == found
    assert len(helix.requests) == n  # cached, unknown names too
    rows = await client.top_streams(game_ids=["222"])
    assert [r["user_login"] for r in rows] == ["b"]
    assert helix.requests[-1].url.params.get_list("game_id") == ["222"]


async def test_429_raises_with_the_reset_wait() -> None:
    clock = Clock()
    helix = FakeHelix([stream("a", 5)])
    helix.fail_next = [httpx.Response(429, headers={"Ratelimit-Reset": str(int(clock.t + 42))})]
    client = HelixClient(CID, SECRET, http=http_for(helix), clock=clock)
    with pytest.raises(RateLimited) as err:
        await client.top_streams()
    assert err.value.wait_s == pytest.approx(42)


async def test_bad_keys_give_a_clear_error_without_the_secret() -> None:
    client = HelixClient(CID, "wrong-" + SECRET, http=http_for(FakeHelix([])))
    with pytest.raises(HelixError) as err:
        await client.top_streams()
    assert "refused the app keys" in str(err.value) and SECRET not in str(err.value)
    assert SECRET not in repr(client)


# ---- the component -----------------------------------------------------------------
async def test_poll_publishes_top_n_and_keep_minutes_holds(tmp_path) -> None:
    store = make_store(tmp_path, top_n=2, keep_minutes=10, exclude=["banned"])
    events = store.bus.subscribe(Topic.STREAMERS_CHANGED)
    helix = FakeHelix([stream("banned", 9999), stream("x", 500), stream("y", 400), stream("z", 300)])
    clock = Clock()
    disc = make_discovery(store, helix, clock)
    assert await disc.poll_once() == 90
    cfg = store.get()
    assert cfg.discovered_names() == ["x", "y"]
    assert [s.name for s in cfg.enabled_streamers()] == ["alpha", "beta", "x", "y"]
    assert events.queue.get_nowait()[1] == {"discovered": ["x", "y"]}

    # y drops out of the top 2: kept for keep_minutes, then dropped
    helix.streams = [stream("x", 500), stream("z", 450), stream("y", 100)]
    clock.t += 120
    await disc.poll_once()
    assert store.get().discovered_names() == ["x", "z", "y"]
    state = disc.public_state()
    y = next(s for s in state["streams"] if s["name"] == "y")
    assert not y["in_top"] and y["held_until"] == pytest.approx(clock.t - 120 + 600)
    assert next(s for s in state["streams"] if s["name"] == "x")["rank"] == 1
    clock.t += 600
    await disc.poll_once()
    assert store.get().discovered_names() == ["x", "z"]

    # going offline is the same as leaving the top
    helix.streams = [stream("z", 450)]
    clock.t += 60
    await disc.poll_once()
    assert store.get().discovered_names() == ["z", "x"]
    assert disc.live_hint("x") is None  # not live in the last answer: checked normally
    hint = disc.live_hint("z")
    assert hint is not None and hint.live and hint.viewers == 450 and hint.category == CAT_A[0]
    clock.t += 1000
    assert disc.live_hint("z") is None  # too old


async def test_categories_and_languages_are_sent(tmp_path) -> None:
    store = make_store(tmp_path, categories=["Another Category"], languages=["en", "de"])
    helix = FakeHelix([stream("a", 50), stream("b", 40, game=CAT_B), stream("c", 30, game=CAT_B, lang="de")])
    disc = make_discovery(store, helix)
    await disc.poll_once()
    assert store.get().discovered_names() == ["b", "c"]
    last = helix.requests[-1].url.params
    assert last.get_list("language") == ["en", "de"] and last.get_list("game_id") == ["222"]


async def test_unknown_categories_keep_the_list_and_say_why(tmp_path) -> None:
    store = make_store(tmp_path, categories=["Nothing Like It"])
    disc = make_discovery(store, FakeHelix([stream("a", 50)]))
    await disc.poll_once()
    assert store.get().discovered_names() == []  # nothing to watch; [[streamers]] stay as they are
    assert "none of discovery.categories" in disc.public_state()["error"]


async def test_429_backs_off_and_keeps_the_list(tmp_path) -> None:
    store = make_store(tmp_path)
    clock = Clock()
    helix = FakeHelix([stream("x", 500)])
    disc = make_discovery(store, helix, clock)
    await disc.poll_once()
    helix.fail_next = [httpx.Response(429, headers={"Ratelimit-Reset": str(int(clock.t + 75))})]
    assert await disc.poll_once() == pytest.approx(75)
    assert store.get().discovered_names() == ["x"]
    assert "rate limit" in disc.public_state()["error"]


async def test_disabled_or_missing_keys_turn_it_off(tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    store = make_store(tmp_path, keys=False, mode="only")
    disc = make_discovery(store, FakeHelix([stream("x", 500)]))
    with caplog.at_level(logging.WARNING, logger="argus.discover"):
        await disc.poll_once()
    assert "dev.twitch.tv/console" in caplog.text
    cfg = store.get()
    assert cfg.discovered_names() == [] and [s.name for s in cfg.enabled_streamers()] == ["alpha", "beta"]
    state = disc.public_state()
    assert not state["has_credentials"] and not state["active"] and state["setup"] == SETUP_HINT

    store2 = make_store(tmp_path)
    disc2 = make_discovery(store2, FakeHelix([stream("x", 500)]))
    await disc2.poll_once()
    assert store2.get().discovered_names() == ["x"]
    store2.update({"discovery": {"enabled": False}})
    assert store2.get().discovered_names() == ["x"]  # kept across a config edit...
    await disc2.poll_once()
    assert store2.get().discovered_names() == [] and store2.get()._discovered is None  # ...until polled


async def test_env_keys_win(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path, keys=False)
    monkeypatch.setenv("TWITCH_CLIENT_ID", CID)
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", SECRET)
    disc = make_discovery(store, FakeHelix([stream("x", 500)]))
    await disc.poll_once()
    assert store.get().discovered_names() == ["x"]


# ---- how discovered channels join the rest -----------------------------------------
def test_merge_only_and_overrides(tmp_path) -> None:
    cfg = Config(general=General(data_dir=str(tmp_path)), streamers=[
        Streamer.model_validate({"name": "Alpha", "multiplier": 4.0, "layout": "crop"}),
        Streamer(name="beta"), Streamer(name="off", enabled=False)])
    store = ConfigStore(config=cfg)
    store.set_discovered(["alpha", "new1", "off"])
    c = store.get()
    assert [s.name for s in c.enabled_streamers()] == ["Alpha", "beta", "new1"]
    assert c.detection_for("alpha").multiplier == 4.0 and c.layout_for("alpha").layout == "crop"
    assert c.detection_for("new1") == c.detection and c.layout_for("new1").layout == c.render.default_layout
    assert c.streamer("NEW1") is not None and c.streamer("nobody") is None
    assert c.is_discovered("new1") and not c.is_discovered("beta")
    c = store.update({"discovery": {"mode": "only"}})
    assert [s.name for s in c.enabled_streamers()] == ["Alpha", "new1"]
    store.set_discovered(None)
    assert [s.name for s in store.get().enabled_streamers()] == ["Alpha", "beta"]  # "only" waits for discovery


def test_discovered_channels_are_never_saved(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[[streamers]]\nname = "alpha"\n', encoding="utf-8")
    store = ConfigStore(path)
    store.set_discovered(["new1"])
    store.update({"general": {"auto_upload": True}})
    assert "new1" not in path.read_text(encoding="utf-8")
    assert store.get().discovered_names() == ["new1"]


async def test_live_checker_uses_hints() -> None:
    from argus.capture.live_check import LiveInfo

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.extend(body["variables"]["logins"])
        return httpx.Response(200, json={"data": {"users": []}})

    store = ConfigStore(config=Config())
    checker = LiveChecker(store, http=http_for(handler))  # type: ignore[arg-type]
    checker.hints = lambda n: LiveInfo(live=True, viewers=7, category="Some Category") if n == "hinted" else None
    out = await checker.check(["Hinted", "other"])
    assert seen == ["other"]
    assert out["hinted"].viewers == 7 and not out["other"].live


def test_chat_wants_discovered_channels(tmp_path) -> None:
    from argus.chat.twitch_irc import TwitchChatSource

    store = make_store(tmp_path)
    chat = TwitchChatSource(store=store, bus=store.bus, record=False)
    store.set_discovered(["new1"])
    assert chat._wanted() == {"alpha", "beta", "new1"}


# ---- dashboard and CLI -------------------------------------------------------------
def test_api_shows_discovered_channels_and_never_the_secret(tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    store = make_store(tmp_path, top_n=1)
    lw = App(store, demo=True, serve=False)
    lw.discovery = make_discovery(store, FakeHelix([stream("new1", 321, game=CAT_B), stream("x", 5)]))
    try:
        with TestClient(create_app(lw)) as client, caplog.at_level(logging.DEBUG):
            client.portal.call(lw.discovery.poll_once)
            state = client.get("/api/state").json()
            rows = {s["name"]: s for s in state["streamers"]}
            assert rows["new1"]["discovered"] and not rows["new1"]["configured"] and rows["new1"]["enabled"]
            assert not rows["alpha"]["discovered"] and rows["alpha"]["configured"]
            d = state["discovery"]
            assert d["active"] and d["streams"] == [{"name": "new1", "rank": 1, "in_top": True, "viewers": 321,
                                                     "category": "Another Category", "held_until": None}]
            assert client.get("/api/streamers/new1/rates").status_code == 200
            texts = [json.dumps(state), client.get("/api/config").text,
                     client.patch("/api/config", json={"discovery": {"top_n": 3}}).text,
                     client.get("/api/logs").text]
            for text in texts:
                assert SECRET not in text
        assert SECRET not in caplog.text
        assert all(SECRET not in str(r.args) for r in caplog.records)
    finally:
        lw.db.close()


def test_api_shows_the_setup_line_without_keys(tmp_path) -> None:
    store = make_store(tmp_path, keys=False)
    lw = App(store, demo=True, serve=False)
    try:
        with TestClient(create_app(lw)) as client:
            client.portal.call(lw.discovery.poll_once)
            d = client.get("/api/state").json()["discovery"]
            assert not d["has_credentials"] and "dev.twitch.tv/console" in d["setup"] and not d["streams"]
    finally:
        lw.db.close()


async def test_cli_prints_the_top_streams(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    store = make_store(tmp_path)
    helix = FakeHelix([stream("a", 1500), stream("b", 40, game=CAT_B), stream("c", 30, game=CAT_B)])
    args = argparse.Namespace(store=store, category=["Another Category"], top=1)
    assert await discover_cli.main(args, http=http_for(helix)) == 0
    out = capsys.readouterr().out
    assert "b" in out and "40 viewers" in out and " a " not in out and SECRET not in out


async def test_cli_without_keys_prints_setup(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    args = argparse.Namespace(store=make_store(tmp_path, keys=False), category=None, top=None)
    assert await discover_cli.main(args) == 2
    assert "dev.twitch.tv/console" in capsys.readouterr().err
