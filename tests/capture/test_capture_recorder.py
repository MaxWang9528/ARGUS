from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path

import pytest

from livewatcher.capture import recorder as rec_mod
from livewatcher.capture.live_check import LiveInfo
from livewatcher.capture.recorder import (
    Candidate,
    Recorder,
    _Emitter,
    pick_quality,
    pick_watched,
    store_segment,
)
from livewatcher.core.config import ConfigStore
from livewatcher.core.events import EventBus, Topic
from livewatcher.core.interfaces import StreamRecorder, WindowUnavailable

FAKE_WORKER = str(Path(__file__).with_name("fake_worker.py"))


class FakeLive:
    def __init__(self, live: set[str], viewers: dict[str, int | None] | None = None,
                 categories: dict[str, str] | None = None):
        self.live = set(live)
        self.viewers = dict(viewers or {})
        self.categories = dict(categories or {})
        self.calls = 0

    async def check(self, names):
        self.calls += 1
        return {n: LiveInfo(live=n in self.live, title=f"{n} stream", method="fake",
                            viewers=self.viewers.get(n), category=self.categories.get(n, ""))
                for n in names}


def argv_for(*extra: str, seg_dur: float = 0.1):
    def make(name, out, cfg):
        return [sys.executable, FAKE_WORKER, "--out", str(out), "--seg-dur", str(seg_dur), *extra]
    return make


async def until(pred, timeout: float = 10.0, step: float = 0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(step)
    raise AssertionError("condition not met in time")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie still answers; check its state
    with contextlib.suppress(OSError):
        if Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z":
            return False
    return True


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(rec_mod, "TICK_S", 0.05)
    monkeypatch.setattr(rec_mod, "BACKOFF_S", (0.2, 0.4))
    monkeypatch.setattr(rec_mod, "STATUS_BUFFER_EVERY_S", 0.0)


@contextlib.asynccontextmanager
async def running(rec: Recorder):
    task = asyncio.create_task(rec.run())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_is_a_stream_recorder(store: ConfigStore, bus: EventBus) -> None:
    assert isinstance(Recorder(store=store, bus=bus), StreamRecorder)


async def test_records_rotates_and_cleans_up(store: ConfigStore, bus: EventBus) -> None:
    store.update({"capture": {"buffer_minutes": 0.01}})        # keep ~0.6 s
    data = store.get().data_dir
    (data / "buffer" / "alpha").mkdir(parents=True)
    (data / "buffer" / "alpha" / "seg_old.ts").write_bytes(b"old")
    (data / "buffer" / "gone").mkdir(parents=True)
    live = FakeLive({"alpha"})
    rec = Recorder(store=store, bus=bus, live_checker=live, worker_argv=argv_for())
    sub = bus.subscribe(Topic.STREAM_STATUS)
    async with running(rec):
        await until(lambda: rec.status("alpha").recorder_state == "recording")
        assert not (data / "buffer" / "gone").exists()
        assert not (data / "buffer" / "alpha" / "seg_old.ts").exists()
        st = rec.status("alpha")
        assert st.live and st.title == "alpha stream"
        pid = rec._streams["alpha"].proc.pid
        await until(lambda: len(rec._streams["alpha"].index.segments) >= 6
                    and rec._streams["alpha"].index.segments[0].idx >= 5)
        files = sorted(p.name for p in (data / "buffer" / "alpha").glob("seg_*.ts"))
        assert 5 <= len(files) <= 10
        assert files[0] != "seg_000001.ts"                    # old ones deleted
        st = rec.status("alpha")
        assert 0.5 <= st.buffer_end - st.buffer_start <= 1.0
        assert abs(st.buffer_end - time.time()) < 1.0
        pl = rec.playlist_path("alpha")
        assert pl is not None and pl.parent == data / "buffer" / "alpha"
        listed = [ln for ln in pl.read_text().splitlines() if ln.endswith(".ts")]
        assert all((pl.parent / name).exists() for name in listed)
        assert rec.status("beta").recorder_state == "stopped" and not rec.status("beta").live
        assert rec.playlist_path("beta") is None

        # window_ready: false until a closed segment reaches the end, then true
        end = time.time() + 0.5
        assert rec.window_ready("alpha", end) is False
        await until(lambda: rec.window_ready("alpha", end), timeout=3)
        with pytest.raises(WindowUnavailable):
            rec.window_ready("beta", end)
        with pytest.raises(WindowUnavailable):
            rec.window_ready("zeta", end)
    # cancelled: the child is gone and nothing is ready any more
    assert not alive(pid)
    assert rec.status("alpha").recorder_state == "stopped"
    with pytest.raises(WindowUnavailable):
        rec.window_ready("alpha", time.time())
    seen = []
    while not sub.queue.empty():
        seen.append(sub.queue.get_nowait()[1])
    assert any(s.streamer == "alpha" and s.recorder_state == "recording" for s in seen)
    assert any(s.streamer == "alpha" and s.recorder_state == "starting" for s in seen)


async def test_sync_offset_shifts_window_ready(store: ConfigStore, bus: EventBus) -> None:
    rec = Recorder(store=store, bus=bus, live_checker=FakeLive({"alpha"}), worker_argv=argv_for())
    async with running(rec):
        await until(lambda: rec.status("alpha").recorder_state == "recording")
        end = rec._streams["alpha"].index.end
        assert rec.window_ready("alpha", end - 0.01)
        store.update({"capture": {"sync_offset_s": 5.0}})       # clip 5 s later -> not yet
        assert not rec.window_ready("alpha", end - 0.01)
        st = rec.status("alpha")                                  # chat-time range moves back
        assert st.buffer_end == pytest.approx(rec._streams["alpha"].index.end - 5.0)


async def test_crashed_worker_is_restarted_with_a_clean_buffer(store: ConfigStore, bus: EventBus) -> None:
    rec = Recorder(store=store, bus=bus, live_checker=FakeLive({"alpha"}),
                   worker_argv=argv_for("--crash-after", "3"))
    pids: set[int] = set()
    async with running(rec):
        for _ in range(3):
            await until(lambda: rec._streams.get("alpha") is not None
                        and rec._streams["alpha"].proc is not None
                        and rec._streams["alpha"].proc.pid not in pids)
            pids.add(rec._streams["alpha"].proc.pid)
        await until(lambda: rec.status("alpha").recorder_state == "error")
        assert "crashed" in rec.status("alpha").last_error or "code 1" in rec.status("alpha").last_error
        await until(lambda: rec.status("alpha").recorder_state == "recording")
        # after a restart only the new run's segments are on disk
        await asyncio.sleep(0.05)
        files = {p.name for p in rec.buffer_dir("alpha").glob("seg_*")}
        listed = {s.file for s in rec._streams["alpha"].index.segments}
        assert files <= listed | {f.name for f in rec.buffer_dir("alpha").glob("*.part")}
    assert len(pids) >= 3
    assert not any(alive(p) for p in pids)


async def test_stream_end_then_offline(store: ConfigStore, bus: EventBus) -> None:
    live = FakeLive({"alpha"})
    rec = Recorder(store=store, bus=bus, live_checker=live, worker_argv=argv_for("--count", "5"))
    async with running(rec):
        await until(lambda: rec.status("alpha").recorder_state == "recording")
        last_end = rec._streams["alpha"].index.end
        live.live.clear()
        await until(lambda: rec._streams["alpha"].proc is None and not rec._streams["alpha"].live)
        assert rec.status("alpha").recorder_state == "stopped"
        # what was recorded can still be cut; anything later never will be
        assert rec.window_ready("alpha", last_end - 0.05)
        with pytest.raises(WindowUnavailable, match="offline"):
            rec.window_ready("alpha", last_end + 5)


async def test_disabling_a_streamer_stops_it(store: ConfigStore, bus: EventBus) -> None:
    rec = Recorder(store=store, bus=bus, live_checker=FakeLive({"alpha", "beta"}), worker_argv=argv_for())
    async with running(rec):
        await until(lambda: rec.status("beta").recorder_state == "recording")
        pid = rec._streams["beta"].proc.pid
        store.update({"streamers": [{"name": "alpha"}, {"name": "beta", "enabled": False}]})
        await until(lambda: rec._streams["beta"].proc is None)
        assert rec.status("beta").recorder_state == "stopped"
        assert rec.status("alpha").recorder_state == "recording"
        await until(lambda: not alive(pid), timeout=3)


async def test_capture_setting_change_restarts(store: ConfigStore, bus: EventBus) -> None:
    rec = Recorder(store=store, bus=bus, live_checker=FakeLive({"alpha"}), worker_argv=argv_for())
    async with running(rec):
        await until(lambda: rec.status("alpha").recorder_state == "recording")
        pid = rec._streams["alpha"].proc.pid
        store.update({"capture": {"quality": "480p"}})
        await until(lambda: rec._streams["alpha"].proc is not None and rec._streams["alpha"].proc.pid != pid)


async def test_silent_worker_is_killed(store: ConfigStore, bus: EventBus, monkeypatch) -> None:
    monkeypatch.setattr(rec_mod, "IDLE_TIMEOUT_S", 0.5)
    rec = Recorder(store=store, bus=bus, live_checker=FakeLive({"alpha"}), worker_argv=argv_for("--silent"))
    async with running(rec):
        await until(lambda: rec._streams.get("alpha") is not None and rec._streams["alpha"].proc is not None)
        pid = rec._streams["alpha"].proc.pid
        await until(lambda: rec.status("alpha").last_error == "capture worker stopped responding")
        await until(lambda: not alive(pid), timeout=3)


async def test_ad_gap_reaches_the_index(store: ConfigStore, bus: EventBus) -> None:
    rec = Recorder(store=store, bus=bus, live_checker=FakeLive({"alpha"}), worker_argv=argv_for("--gap-at", "3"))
    async with running(rec):
        await until(lambda: len(rec._streams.get("alpha").index.segments) >= 4
                    if rec._streams.get("alpha") else False)
        idx = rec._streams["alpha"].index
        assert len(idx.gaps) == 1 and idx.gaps[0].reason == "ad"
        g0, g1, _ = idx.gaps_chat()[0]
        with pytest.raises(WindowUnavailable, match="gap"):
            idx.select(g0 - 0.05, g1 + 0.05)
        assert "#EXT-X-DISCONTINUITY" in rec.playlist_path("alpha").read_text()


# ---- max_streamers ------------------------------------------------------------------

def C(name: str, viewers: int | None, category: str = "") -> Candidate:
    return Candidate(name, category, viewers)


def test_pick_ranks_by_viewers_none_last() -> None:
    cands = [C("a", 100), C("b", None), C("c", 300), C("d", 200)]
    assert pick_watched(cands, [], 3, []) == ["c", "d", "a"]
    assert pick_watched(cands, [], 10, []) == ["c", "d", "a", "b"]
    assert pick_watched([C("x", None), C("y", None)], [], 1, []) == ["x"]  # stable by name


def test_pick_category_first() -> None:
    cands = [C("big", 90_000, "Just Chatting"), C("gta", 50, "grand theft auto vi"),
             C("gta2", None, "Grand Theft Auto VI"), C("mid", 5_000, "DayZ")]
    got = pick_watched(cands, [], 3, ["Grand Theft Auto VI"])
    assert got == ["gta", "gta2", "big"]
    # category beats hysteresis: a watched non-GTA streamer loses to any GTA one
    assert pick_watched(cands, ["big", "mid"], 2, ["Grand Theft Auto VI"]) == ["gta", "gta2"]
    # no categories configured: viewers only
    assert pick_watched(cands, [], 2, []) == ["big", "mid"]


def test_pick_hysteresis() -> None:
    # watched "a" (1000) keeps its slot against +19 %, loses at exactly +20 %
    assert pick_watched([C("a", 1000), C("b", 1190)], ["a"], 1, []) == ["a"]
    assert pick_watched([C("a", 1000), C("b", 1200)], ["a"], 1, []) == ["b"]
    # no flip-flop: once "b" holds the slot, "a" needs +20 % on it
    assert pick_watched([C("a", 1000), C("b", 1200)], ["b"], 1, []) == ["b"]
    # without a holder, plain ranking
    assert pick_watched([C("a", 1000), C("b", 1190)], [], 1, []) == ["b"]
    # a known count beats a watched None; two None never swap
    assert pick_watched([C("a", None), C("b", 1)], ["a"], 1, []) == ["b"]
    assert pick_watched([C("b", None), C("a", None)], ["b"], 1, []) == ["b"]
    # several slots: only the weakest holder is at risk
    cands = [C("a", 1000), C("b", 500), C("c", 590), C("d", 610)]
    assert pick_watched(cands, ["a", "b"], 2, []) == ["a", "d"]
    assert pick_watched(cands[:3], ["a", "b"], 2, []) == ["a", "b"]


def test_pick_max_zero_keeps_everyone() -> None:
    cands = [C("a", 1), C("b", None), C("c", 3)]
    assert sorted(pick_watched(cands, [], 0, ["x"])) == ["a", "b", "c"]


async def test_max_streamers_records_only_the_top(store: ConfigStore, bus: EventBus) -> None:
    store.update({"general": {"max_streamers": 1},
                  "streamers": [{"name": "alpha"}, {"name": "beta"}]})
    live = FakeLive({"alpha", "beta"}, viewers={"alpha": 1000, "beta": 500})
    rec = Recorder(store=store, bus=bus, live_checker=live, worker_argv=argv_for())
    sub = bus.subscribe(Topic.STREAM_STATUS)
    async with running(rec):
        await until(lambda: rec.status("alpha").recorder_state == "recording")
        await asyncio.sleep(0.3)
        b = rec.status("beta")
        assert b.live and not b.watched and b.viewers == 500 and b.recorder_state == "stopped"
        assert rec._streams["beta"].proc is None and not rec.buffer_dir("beta").exists()
        assert rec.status("alpha").watched and rec.status("alpha").viewers == 1000
        with pytest.raises(WindowUnavailable, match="max_streamers"):
            rec.window_ready("beta", time.time())
        pid = rec._streams["alpha"].proc.pid

        # +19 %: alpha keeps its slot
        live.viewers["beta"] = 1190
        calls = live.calls
        rec.request_live_check()
        await until(lambda: live.calls > calls and rec.status("beta").viewers == 1190)
        await asyncio.sleep(0.2)
        assert rec._streams["alpha"].proc is not None and rec._streams["alpha"].proc.pid == pid
        assert not rec.status("beta").watched

        # +20 %: beta takes over, alpha's worker stops and its buffer goes
        live.viewers["beta"] = 1200
        rec.request_live_check()
        await until(lambda: rec.status("beta").recorder_state == "recording")
        a = rec.status("alpha")
        assert a.live and not a.watched and a.recorder_state == "stopped"
        assert rec._streams["alpha"].proc is None
        assert not rec.buffer_dir("alpha").exists() and rec.playlist_path("alpha") is None
        await until(lambda: not alive(pid), timeout=3)

        # a GTA stream outranks everyone regardless of viewers
        store.update({"general": {"clip_categories": ["Grand Theft Auto VI"]}})
        live.categories["alpha"] = "Grand Theft Auto VI"
        rec.request_live_check()
        await until(lambda: rec.status("alpha").recorder_state == "recording")
        assert not rec.status("beta").watched and rec._streams["beta"].proc is None

        # offline: viewers unknown, and watched again (nothing to be outside of)
        live.live.discard("beta")
        rec.request_live_check()
        await until(lambda: not rec.status("beta").live)
        assert rec.status("beta").viewers is None and rec.status("beta").watched
    seen = []
    while not sub.queue.empty():
        seen.append(sub.queue.get_nowait()[1])
    assert any(s.streamer == "beta" and s.viewers == 1190 for s in seen)
    assert any(s.streamer == "alpha" and not s.watched for s in seen)


async def test_max_streamers_zero_records_everyone(store: ConfigStore, bus: EventBus) -> None:
    store.update({"general": {"max_streamers": 0},
                  "streamers": [{"name": "alpha"}, {"name": "beta"}, {"name": "gamma"}]})
    live = FakeLive({"alpha", "beta", "gamma"}, viewers={"alpha": 1, "beta": None, "gamma": 3})
    rec = Recorder(store=store, bus=bus, live_checker=live, worker_argv=argv_for())
    async with running(rec):
        await until(lambda: all(rec.status(n).recorder_state == "recording" for n in ("alpha", "beta", "gamma")))
        assert all(rec.status(n).watched for n in ("alpha", "beta", "gamma"))
        # lowering the limit takes effect at the next check (config changes trigger one)
        store.update({"general": {"max_streamers": 2}})
        await until(lambda: rec._streams["beta"].proc is None and not rec.status("beta").watched)
        assert rec.status("alpha").recorder_state == "recording"
        assert rec.status("gamma").recorder_state == "recording"


async def test_not_running_raises(store: ConfigStore, bus: EventBus) -> None:
    rec = Recorder(store=store, bus=bus, live_checker=FakeLive(set()))
    with pytest.raises(WindowUnavailable):
        rec.window_ready("alpha", time.time())
    assert rec.status("alpha").recorder_state == "stopped"
    assert rec.snapshot_path("alpha") is None and rec.playlist_path("alpha") is None


# ---- worker pieces ----------------------------------------------------------------

def test_store_segment(tmp_path: Path) -> None:
    events: list[dict] = []
    meta = {"num": 5, "date": 1.0, "dur": 2.0, "ad": False, "prefetch": True}
    assert store_segment(tmp_path, "seg_000001.ts", [b"ab", b"", b"cd"], meta, events.append)
    assert (tmp_path / "seg_000001.ts").read_bytes() == b"abcd"
    assert events[-1]["t"] == "seg" and events[-1]["bytes"] == 4 and events[-1]["num"] == 5

    def broken():
        yield b"xx"
        raise ConnectionError("reset")

    assert not store_segment(tmp_path, "seg_000002.ts", broken(), meta, events.append)
    assert events[-1]["t"] == "skip" and "ConnectionError" in events[-1]["reason"]
    assert not store_segment(tmp_path, "seg_000003.ts", [], meta, events.append)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["seg_000001.ts"]


def test_emitter_writes_json_lines() -> None:
    buf = io.StringIO()
    e = _Emitter(buf)
    e({"t": "seg", "n": 1})
    e({"t": "end"})
    assert [json.loads(x)["t"] for x in buf.getvalue().splitlines()] == ["seg", "end"]


def test_pick_quality() -> None:
    avail = ["audio_only", "160p30", "480p30", "720p60", "1080p60", "best", "worst"]
    assert pick_quality(avail, "720p60,720p,best") == "720p60"
    assert pick_quality(["720p", "best"], "720p60,720p,best") == "720p"
    assert pick_quality(["1080p60", "best"], "720p60, 720p ,best") == "best"
    assert pick_quality(["480p"], "720p60") is None


def test_default_worker_argv(store: ConfigStore) -> None:
    from livewatcher.capture.recorder import default_worker_argv

    cfg = store.get()
    argv = default_worker_argv("alpha", Path("/x"), cfg)
    assert argv[:5] == [sys.executable, "-u", "-m", "livewatcher.capture.recorder", "worker"]
    assert "--low-latency" in argv and "--keep-ads" not in argv
    store.update({"capture": {"low_latency": False, "filter_ads": False}})
    argv = default_worker_argv("alpha", Path("/x"), store.get())
    assert "--low-latency" not in argv and "--keep-ads" in argv
