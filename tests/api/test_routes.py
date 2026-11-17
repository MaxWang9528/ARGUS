"""Every endpoint in the "Dashboard API" table, against App(store, demo=True, serve=False)."""

from __future__ import annotations

import os
import time
import tomllib
from pathlib import Path

import pytest
from api_helpers import SECRET, data_dir, make_live, saved_clip, wait_for, write_snapshot
from fastapi.testclient import TestClient

from argus.api.server import create_app
from argus.app import App
from argus.core.config import ConfigStore
from argus.core.events import Topic
from argus.core.models import ClipStatus, LogLine, RateSample


# ---- state and streamers --------------------------------------------------------
def test_state(client, lw):
    body = client.get("/api/state").json()
    assert body["auto_upload"] is False and body["dry_run"] is True
    assert body["components"] == lw.components
    # the demo runs on fakes, except the upload queue (pure logic, driving the fake uploader)
    assert {k for k, v in body["components"].items() if v == "real"} == {"upload_queue"}
    assert [s["name"] for s in body["streamers"]] == ["alpha", "beta"]
    alpha = body["streamers"][0]
    assert alpha["enabled"] is True and alpha["has_playlist"] is False and alpha["has_snapshot"] is False
    assert alpha["status"]["streamer"] == "alpha" and alpha["status"]["recorder_state"] == "stopped"
    write_snapshot(lw, "alpha")
    assert client.get("/api/state").json()["streamers"][0]["has_snapshot"] is True


def test_rates_come_from_the_rate_history(client, lw, collecting):
    assert client.get("/api/streamers/nobody/rates").status_code == 404
    assert client.get("/api/streamers/alpha/rates").json() == []
    now = time.time()
    for i in range(3):
        client.portal.call(lw.bus.publish, Topic.RATE, RateSample(
            streamer="alpha", ts=now + i, rate=2.0 + i, baseline=2.0, threshold=6.0, z=0.0))
    wait_for(lambda: len(client.get("/api/streamers/alpha/rates").json()) == 3)
    rates = client.get("/api/streamers/ALPHA/rates").json()
    assert [r["rate"] for r in rates] == [2.0, 3.0, 4.0] and rates[0]["threshold"] == 6.0


def test_snapshot(client, lw):
    assert client.get("/api/streamers/alpha/snapshot.jpg").status_code == 404
    assert client.get("/api/streamers/nobody/snapshot.jpg").status_code == 404
    write_snapshot(lw, "alpha")
    r = client.get("/api/streamers/alpha/snapshot.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "no-store" and r.content[:2] == b"\xff\xd8"


@pytest.fixture
def playlist(lw, monkeypatch, tmp_path) -> Path:
    folder = data_dir(lw) / "buffer" / "alpha"
    folder.mkdir(parents=True)
    (folder / "live.m3u8").write_text("#EXTM3U\n#EXTINF:2.0,\nseg_00001.ts\n")
    (folder / "seg_00001.ts").write_bytes(b"\x47" * 188)
    secret = tmp_path / "secret.txt"
    secret.write_text("do not serve")
    (data_dir(lw) / "buffer" / "outside.txt").write_text("also not")
    os.symlink(secret, folder / "link.ts")
    monkeypatch.setattr(lw.recorder, "playlist_path",
                        lambda s: folder / "live.m3u8" if s == "alpha" else None)
    return folder


def test_live_playlist_and_segments(client, playlist):
    assert client.get("/api/streamers/beta/live/live.m3u8").status_code == 404  # no playlist
    assert client.get("/api/streamers/nobody/live/live.m3u8").status_code == 404
    assert client.get("/api/state").json()["streamers"][0]["has_playlist"] is True
    r = client.get("/api/streamers/alpha/live/live.m3u8")
    assert r.status_code == 200 and r.headers["content-type"] == "application/vnd.apple.mpegurl"
    assert r.headers["cache-control"] == "no-store" and r.text.startswith("#EXTM3U")
    r = client.get("/api/streamers/alpha/live/seg_00001.ts")
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp2t"
    assert client.get("/api/streamers/alpha/live/missing.ts").status_code == 404


@pytest.mark.parametrize("path", [
    "..%2Foutside.txt", "%2e%2e%2foutside.txt", "%2e%2e", "..", "..%2F..%2F..%2Fsecret.txt",
    "link.ts", "%2Fetc%2Fpasswd", "..%5Coutside.txt",
])
def test_live_rejects_paths_that_escape_the_folder(client, playlist, path):
    r = client.get(f"/api/streamers/alpha/live/{path}")
    assert r.status_code == 404, r.text
    assert b"not serve" not in r.content and b"also not" not in r.content


def test_layout_preview(client, lw, monkeypatch):
    assert client.get("/api/streamers/nobody/layout-preview.jpg").status_code == 404
    assert client.get("/api/streamers/alpha/layout-preview.jpg").status_code == 501  # no snapshot yet
    write_snapshot(lw, "alpha")
    seen = []
    real = lw.renderer.preview

    async def spy(snapshot, layout):
        seen.append(layout)
        return await real(snapshot, layout)

    monkeypatch.setattr(lw.renderer, "preview", spy)
    r = client.get("/api/streamers/alpha/layout-preview.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "no-store"
    assert seen[-1] == lw.store.get().layout_for("alpha")
    r = client.get("/api/streamers/alpha/layout-preview.jpg", params={
        "layout": "stacked", "stack_top": "0,0.5,0.3,0.5", "stack_bottom": "0.2,0,0.6,1", "stack_split": 0.35})
    assert r.status_code == 200
    got = seen[-1]
    assert got.layout == "stacked" and got.stack_split == 0.35
    assert (got.stack_top.y, got.stack_top.w) == (0.5, 0.3) and got.stack_bottom.x == 0.2
    client.get("/api/streamers/alpha/layout-preview.jpg", params={"layout": "crop", "crop_x": 0.1})
    assert seen[-1].layout == "crop" and seen[-1].crop_x == 0.1
    client.get("/api/streamers/alpha/layout-preview.jpg", params={"fit_zoom": 1.4})
    assert seen[-1].fit_zoom == 1.4
    for params in ({"layout": "sideways"}, {"stack_top": "1,2,3"}, {"crop_x": 2}, {"fit_zoom": "big"}):
        assert client.get("/api/streamers/alpha/layout-preview.jpg", params=params).status_code == 422

    async def unsupported(snapshot, layout):
        raise NotImplementedError

    monkeypatch.setattr(lw.renderer, "preview", unsupported)
    assert client.get("/api/streamers/alpha/layout-preview.jpg").status_code == 501


# ---- clip now, peaks, the clip pipeline ------------------------------------------
def test_clip_now_offline_unknown_and_live(client, lw, monkeypatch):
    assert client.post("/api/streamers/nobody/clip-now").status_code == 404
    r = client.post("/api/streamers/alpha/clip-now")  # the fake recorder isn't running: offline
    assert r.status_code == 409 and "not live" in r.json()["detail"]
    make_live(lw, monkeypatch)
    r = client.post("/api/streamers/ALPHA/clip-now")
    assert r.status_code == 200
    clip = r.json()
    assert clip["streamer"] == "alpha" and clip["status"] == "waiting"
    done = wait_for(lambda: (c := client.get(f"/api/clips/{clip['id']}").json())["status"]
                    in ("pending_review", "failed", "dropped") and c)
    assert done["status"] == "pending_review", done
    assert done["renders"]["blur_fit"] and done["raw_path"]
    peaks = client.get("/api/peaks", params={"streamer": "Alpha"}).json()
    # ClipService also records the peak of the refused (offline) attempt, so there are two.
    assert peaks[0]["source"] == "manual" and peaks[0]["id"] == clip["peak_id"]
    assert client.get("/api/peaks", params={"streamer": "beta"}).json() == []
    assert client.get("/api/peaks", params={"since": time.time() + 60}).json() == []
    assert len(client.get("/api/peaks").json()) == len(peaks)


def test_clip_list_filters(client, lw):
    a = saved_clip(lw, ClipStatus.PENDING_REVIEW)
    time.sleep(0.01)
    b = saved_clip(lw, ClipStatus.FAILED, streamer="beta", error="boom")
    time.sleep(0.01)
    c = saved_clip(lw, ClipStatus.UPLOADED)
    assert [x["id"] for x in client.get("/api/clips").json()] == [c.id, b.id, a.id]  # newest first
    assert [x["id"] for x in client.get("/api/clips?status=pending_review,failed").json()] == [b.id, a.id]
    assert [x["id"] for x in client.get("/api/clips?streamer=BETA").json()] == [b.id]
    assert len(client.get("/api/clips?limit=1").json()) == 1
    assert client.get("/api/clips?status=nonsense").status_code == 422
    assert client.get("/api/clips?limit=0").status_code == 422
    assert client.get(f"/api/clips/{a.id}").json()["caption"] == "hello"
    assert client.get("/api/clips/doesnotexist").status_code == 404


def test_clip_video_raw_and_range(client, lw):
    clip = saved_clip(lw, video=bytes(range(256)) * 16)
    r = client.get(f"/api/clips/{clip.id}/video")
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4" and len(r.content) == 4096
    assert r.headers["accept-ranges"] == "bytes"
    r = client.get(f"/api/clips/{clip.id}/video", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100 and r.headers["content-range"] == "bytes 0-99/4096"
    assert client.get(f"/api/clips/{clip.id}/video?layout=blur_fit").status_code == 200
    assert client.get(f"/api/clips/{clip.id}/video?layout=crop").status_code == 404  # not rendered
    assert client.get("/api/clips/nope/video").status_code == 404
    r = client.get(f"/api/clips/{clip.id}/raw")
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    assert client.get("/api/clips/nope/raw").status_code == 404
    waiting = saved_clip(lw, ClipStatus.WAITING)
    waiting.raw_path, waiting.renders = None, {}
    lw.db.save_clip(waiting)
    assert client.get(f"/api/clips/{waiting.id}/video").status_code == 404
    assert client.get(f"/api/clips/{waiting.id}/raw").status_code == 404


def test_clip_files_outside_their_folders_are_refused(client, lw, tmp_path):
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"private")
    clip = saved_clip(lw)
    sneaky = data_dir(lw) / "clips" / "out" / ".." / ".." / ".." / "outside.mp4"
    clip.renders = {"blur_fit": str(outside), "crop": str(sneaky), "stacked": "/etc/passwd"}
    clip.raw_path = str(data_dir(lw) / "clips" / "out" / f"{clip.id}_blur_fit.mp4")  # wrong folder
    clip.debug_screenshot = str(outside)
    lw.db.save_clip(clip)
    for url in (f"/api/clips/{clip.id}/video", f"/api/clips/{clip.id}/video?layout=crop",
                f"/api/clips/{clip.id}/video?layout=stacked", f"/api/clips/{clip.id}/raw",
                f"/api/clips/{clip.id}/screenshot"):
        r = client.get(url)
        assert r.status_code == 404, url
        assert b"private" not in r.content and b"root:" not in r.content


def test_screenshot(client, lw):
    clip = saved_clip(lw, ClipStatus.FAILED, error="captcha")
    assert client.get(f"/api/clips/{clip.id}/screenshot").status_code == 404  # none yet
    shot = data_dir(lw) / "upload_debug" / f"{clip.id}.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    clip.debug_screenshot = str(shot)
    lw.db.save_clip(clip)
    r = client.get(f"/api/clips/{clip.id}/screenshot")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert client.get("/api/clips/nope/screenshot").status_code == 404


def test_approve_and_reject(client, lw):
    assert client.post("/api/clips/nope/approve").status_code == 404
    assert client.post("/api/clips/nope/reject").status_code == 404
    clip = saved_clip(lw)
    sub = lw.bus.subscribe(Topic.CLIP)
    r = client.post(f"/api/clips/{clip.id}/approve")
    assert r.status_code == 200 and r.json()["status"] == "queued"
    assert sub.queue.get_nowait()[1].status is ClipStatus.QUEUED  # announced on the bus
    r = client.post(f"/api/clips/{clip.id}/approve")
    assert r.status_code == 409 and "queued" in r.json()["detail"]
    r = client.post(f"/api/clips/{clip.id}/reject")
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    uploaded = saved_clip(lw, ClipStatus.UPLOADED)
    assert client.post(f"/api/clips/{uploaded.id}/reject").status_code == 409
    no_render = saved_clip(lw, ClipStatus.FAILED)
    no_render.renders = {}
    lw.db.save_clip(no_render)
    assert client.post(f"/api/clips/{no_render.id}/approve").status_code == 409


def test_rerender(client, lw):
    assert client.post("/api/clips/nope/rerender", json={"layout": "crop"}).status_code == 404
    clip = saved_clip(lw)
    r = client.post(f"/api/clips/{clip.id}/rerender", json={"layout": "crop"})
    assert r.status_code == 200
    body = r.json()
    assert body["active_layout"] == "crop" and set(body["renders"]) == {"blur_fit", "crop"}
    assert body["status"] == "pending_review"
    assert client.get(f"/api/clips/{clip.id}/video?layout=crop").status_code == 200
    assert client.post(f"/api/clips/{clip.id}/rerender", json={"layout": "sideways"}).status_code == 409
    assert client.post(f"/api/clips/{clip.id}/rerender", json={}).status_code == 422
    queued = saved_clip(lw, ClipStatus.QUEUED)
    assert client.post(f"/api/clips/{queued.id}/rerender", json={"layout": "crop"}).status_code == 409


def test_caption(client, lw):
    clip = saved_clip(lw)
    r = client.patch(f"/api/clips/{clip.id}", json={"caption": "new caption #clips"})
    assert r.status_code == 200 and r.json()["caption"] == "new caption #clips"
    assert lw.db.get_clip(clip.id).caption == "new caption #clips"
    assert client.patch("/api/clips/nope", json={"caption": "x"}).status_code == 404
    assert client.patch(f"/api/clips/{clip.id}", json={}).status_code == 422
    assert client.patch(f"/api/clips/{clip.id}", json={"caption": "x" * 2201}).status_code == 422


# ---- config ------------------------------------------------------------------------
@pytest.fixture
def file_lw(tmp_path, bus):
    """An App whose config is a copy of config.example.toml on disk."""
    text = Path("config.example.toml").read_text(encoding="utf-8")
    text = text.replace('data_dir = "clips"', f'data_dir = "{tmp_path / "data"}"')
    # one streamer with an override, instead of the example's long list
    text = text[:text.index("\n[[streamers]]") + 1] + '[[streamers]]\nname = "somestreamer"\nmultiplier = 2.5\n'
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    app = App(ConfigStore(path, bus=bus), demo=True, serve=False)
    yield app, path
    app.db.close()


def test_config_round_trip_saves_the_file(file_lw):
    lw, path = file_lw
    with TestClient(create_app(lw)) as client:
        cfg = client.get("/api/config").json()
        assert cfg["streamers"][0]["name"] == "somestreamer" and cfg["streamers"][0]["multiplier"] == 2.5
        assert cfg["streamers"][0]["fit_zoom"] is None  # sent back as-is below
        r = client.patch("/api/config", json={"general": {"auto_upload": True},
                                              "detection": {"multiplier": 4.5}})
        assert r.status_code == 200
        assert r.json()["general"]["auto_upload"] is True and r.json()["detection"]["multiplier"] == 4.5
        assert client.get("/api/state").json()["auto_upload"] is True

        # The page edits a streamer by sending the whole list back, nulls included.
        streamers = client.get("/api/config").json()["streamers"]
        streamers[0]["multiplier"] = 2.0
        streamers[0]["cooldown_s"] = 30
        streamers[0]["crop_x"] = 0.25
        streamers.append({"name": "newcomer", "enabled": False, "layout": None})
        r = client.patch("/api/config", json={"streamers": streamers})
        assert r.status_code == 200, r.text

        saved = path.read_text(encoding="utf-8")
        assert "# also switchable from the dashboard" in saved
        data = tomllib.loads(saved)
        assert data["general"]["auto_upload"] is True and data["detection"]["multiplier"] == 4.5
        assert [s["name"] for s in data["streamers"]] == ["somestreamer", "newcomer"]
        assert data["streamers"][0]["cooldown_s"] == 30 and data["streamers"][0]["crop_x"] == 0.25
        reloaded = ConfigStore(path).get()
        assert reloaded.detection_for("somestreamer").multiplier == 2.0
        assert reloaded.detection_for("somestreamer").cooldown_s == 30
        assert reloaded.streamer("newcomer").enabled is False
        assert client.get("/api/config").json() == r.json()

        # Removing a key from the list removes the override.
        streamers = r.json()["streamers"]
        del streamers[0]["cooldown_s"]
        client.patch("/api/config", json={"streamers": streamers})
        assert ConfigStore(path).get().detection_for("somestreamer").cooldown_s == 45


@pytest.mark.parametrize("patch", [
    {"general": {"auto_upload": "maybe"}},
    {"nonsense": {"a": 1}},
    {"detection": {"no_such_setting": 1}},
    {"render": {"default_layout": "sideways"}},
    {"streamers": [{"name": "alpha", "multiplier": "lots"}]},
    {"streamers": [{"name": "alpha"}, {"name": "ALPHA"}]},
    {"streamers": [{"enabled": True}]},
])
def test_bad_config_patch_is_422_and_changes_nothing(client, lw, patch):
    before = client.get("/api/config").json()
    r = client.patch("/api/config", json=patch)
    assert r.status_code == 422, r.text
    assert client.get("/api/config").json() == before


def test_config_patch_must_be_an_object(client):
    assert client.patch("/api/config", json=[1, 2]).status_code == 422
    assert client.patch("/api/config", content=b"not json",
                        headers={"content-type": "application/json"}).status_code == 422


def test_client_secret_never_appears(file_lw):
    lw, path = file_lw
    with TestClient(create_app(lw)) as client, client.websocket_connect("/api/ws") as ws:
        r = client.patch("/api/config", json={"twitch": {"client_id": "abc", "client_secret": SECRET}})
        assert r.status_code == 200 and r.json()["twitch"]["client_secret"] == ""
        assert lw.store.get().twitch.client_secret == SECRET  # it was stored
        assert SECRET in path.read_text(encoding="utf-8")  # and saved
        event = ws.receive_json()
        assert event["topic"] == "config_changed" and SECRET not in str(event)
        assert event["data"]["patch"]["twitch"] == {"client_id": "abc", "client_secret": ""}

        # A blank secret in a patch (what the page sends back) keeps the stored one.
        cfg = client.get("/api/config").json()
        r = client.patch("/api/config", json={"twitch": cfg["twitch"]})
        assert r.status_code == 200 and lw.store.get().twitch.client_secret == SECRET
        assert client.patch("/api/config", json={"twitch": {"client_secret": ""}}).status_code == 200
        assert lw.store.get().twitch.client_secret == SECRET

        bodies = [client.get(u).text for u in ("/api/config", "/api/state", "/api/logs", "/api/clips")]
        bodies.append(client.patch("/api/config", json={"general": {"auto_upload": False}}).text)
        assert all(SECRET not in b for b in bodies)


# ---- logs ---------------------------------------------------------------------------
def test_logs(client, lw, collecting):
    assert client.get("/api/logs").json() == []
    for i in range(510):
        client.portal.call(lw.bus.publish, Topic.LOG, LogLine(level="info", source="test", message=f"line {i}"))
    logs = wait_for(lambda: (x := client.get("/api/logs").json()) and x[-1]["message"] == "line 509" and x)
    assert len(logs) == 500 and logs[0]["message"] == "line 10" and logs[0]["source"] == "test"


# ---- the page and the origin guard ----------------------------------------------------
def test_page_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "app.js" in r.text and r.headers["cache-control"] == "no-cache"
    assert client.get("/app.js").status_code == 200
    assert client.get("/vendor/alpine-3.14.9.min.js").status_code == 200
    assert client.get("/api/no-such-endpoint").status_code == 404


def test_cross_origin_writes_are_refused(client, lw):
    clip = saved_clip(lw)
    r = client.post(f"/api/clips/{clip.id}/reject", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and lw.db.get_clip(clip.id).status is ClipStatus.PENDING_REVIEW
    r = client.post(f"/api/clips/{clip.id}/reject", headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    assert client.get("/api/state", headers={"Origin": "https://evil.example"}).status_code == 200


def test_purge_and_delete_endpoints(client, lw):
    from argus.core.models import Clip, ClipStatus
    a = Clip(streamer="alpha", window_start=0, window_end=1, status=ClipStatus.REJECTED)
    b = Clip(streamer="alpha", window_start=0, window_end=1, status=ClipStatus.UPLOADING)
    c = Clip(streamer="alpha", window_start=0, window_end=1, status=ClipStatus.FAILED)
    for x in (a, b, c):
        lw.db.save_clip(x)
    assert client.delete(f"/api/clips/{b.id}").status_code == 409   # in progress
    assert client.delete(f"/api/clips/{c.id}").status_code == 200
    assert client.delete("/api/clips/nope").status_code == 404
    assert client.post("/api/clips/purge", json={"statuses": ["bogus"]}).status_code == 422
    r = client.post("/api/clips/purge", json={"statuses": ["rejected", "uploading"]})
    assert r.status_code == 200 and r.json()["clips"] == 1 and r.json()["kept_in_progress"] == 1
    assert lw.db.get_clip(a.id) is None and lw.db.get_clip(b.id) is not None
