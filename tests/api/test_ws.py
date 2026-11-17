"""WS /api/ws relays bus events (never chat) and closes its subscription."""

from __future__ import annotations

import time

import pytest
from api_helpers import wait_for
from starlette.websockets import WebSocketDisconnect

from argus.core.events import Topic
from argus.core.models import ChatMessage, Clip, LogLine, PeakEvent, RateSample, StreamStatus


def test_events_reach_the_client(client, lw):
    before = len(lw.bus._subs)
    with client.websocket_connect("/api/ws") as ws:
        assert len(lw.bus._subs) == before + 1
        publish = lambda topic, payload: client.portal.call(lw.bus.publish, topic, payload)
        peak = PeakEvent(streamer="alpha", ts_start=1.0, ts_peak=2.0, ts_end=3.0, score=4.5)
        publish(Topic.CHAT, ChatMessage(streamer="alpha", ts=1.0, text="never relayed"))
        publish(Topic.PEAK, peak)
        msg = ws.receive_json()
        assert msg == {"topic": "peak", "data": peak.model_dump(mode="json")}

        now = time.time()
        payloads = [
            (Topic.CHAT, ChatMessage(streamer="alpha", ts=now)),
            (Topic.RATE, RateSample(streamer="alpha", ts=now, rate=1, baseline=1, threshold=3, z=0)),
            (Topic.CLIP, Clip(streamer="alpha", window_start=now - 10, window_end=now)),
            (Topic.STREAM_STATUS, StreamStatus(streamer="alpha", live=True)),
            (Topic.SNAPSHOT, {"streamer": "alpha", "ts": now}),
            (Topic.CONFIG_CHANGED, {"patch": {"general": {"auto_upload": True}}}),
            (Topic.LOG, LogLine(message="hello", streamer="alpha")),
        ]
        for topic, payload in payloads:
            publish(topic, payload)
        got = [ws.receive_json() for _ in range(len(payloads) - 1)]
        assert [m["topic"] for m in got] == ["rate", "clip", "stream_status", "snapshot", "config_changed", "log"]
        assert got[1]["data"]["status"] == "waiting" and got[3]["data"] == {"streamer": "alpha", "ts": now}
        assert got[4]["data"] == {"patch": {"general": {"auto_upload": True}}}
    # The server notices the close and drops its subscription.
    wait_for(lambda: len(lw.bus._subs) == before)


def test_clip_actions_are_pushed(client, lw):
    from api_helpers import saved_clip

    clip = saved_clip(lw)
    with client.websocket_connect("/api/ws") as ws:
        client.patch(f"/api/clips/{clip.id}", json={"caption": "live update"})
        msg = ws.receive_json()
        assert msg["topic"] == "clip" and msg["data"]["caption"] == "live update"


def test_cross_origin_socket_is_refused(client, lw):
    before = len(lw.bus._subs)
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(
            "/api/ws", headers={"Origin": "https://evil.example"}):
        pass
    assert len(lw.bus._subs) == before
