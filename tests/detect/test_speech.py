"""Speech check: scoring, rank factor, gate, failures and manual clips (no real model)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from argus.app import App
from argus.core.config import ConfigStore
from argus.core.db import Database
from argus.core.fakes import FakeTranscriber
from argus.core.models import Clip, ClipStatus, PeakEvent, StreamStatus, Transcript, Word
from argus.detect import speech as sp
from argus.detect.speech import SpeechCheck, rank_factor, speech_score


def _words(text: str, t0: float = 2.0, dt: float = 0.4) -> list[Word]:
    return [Word(w=w, t=t0 + i * dt, e=t0 + i * dt + 0.3) for i, w in enumerate(text.split())]


# ---- scoring ---------------------------------------------------------------
def test_calm_talk_scores_zero() -> None:
    s = speech_score(_words("so then we went to the store and talked about the plan for tomorrow"), 30)
    assert s.score == 0 and s.hits == []


def test_exclamations_and_swears_score() -> None:
    one = speech_score(_words("okay oh my god that was close"), 30)
    assert 1.0 <= one.score <= 1.6 and one.hits == ["oh my god"]
    swear = speech_score(_words("well damn"), 30)
    assert 0.8 <= swear.score <= 1.1 and swear.hits == ["damn"]
    many = speech_score(_words("no way what the fuck let's go"), 30)
    assert many.score > one.score
    assert {"no way", "what the fuck", "let's go"} <= set(many.hits)


def test_score_counts_a_burst_not_the_whole_window() -> None:
    # the same two exclamations, together or 20 s apart: together scores higher
    together = speech_score(_words("no way oh my god"), 30)
    apart = speech_score(_words("no way", t0=1) + _words("oh my god", t0=21), 30)
    assert together.score > apart.score > 0


def test_score_accepts_dicts_and_empty() -> None:
    assert speech_score([]).score == 0
    assert speech_score([{"w": "wow", "t": 1.0}]).score > 0


def test_rank_factor_bounds() -> None:
    assert rank_factor(0.5, 0.25) == 1.0
    assert rank_factor(0.0, 0.25) < 1.0 < rank_factor(2.0, 0.25)
    assert rank_factor(100, 0.25) == rank_factor(3, 0.25)
    assert rank_factor(0, 0) == rank_factor(5, 0) == 1.0
    assert rank_factor(0, 10) == 0.5 and rank_factor(5, 10) == 2.0


# ---- SpeechCheck -----------------------------------------------------------
def _clip(tmp_path: Path, rank: float = 10.0) -> Clip:
    raw = tmp_path / "raw.mp4"
    raw.write_bytes(b"x")
    return Clip(streamer="alpha", window_start=0, window_end=30, raw_path=str(raw), rank=rank)


async def test_check_stores_transcript_and_scales_rank(store: ConfigStore, tmp_path: Path) -> None:
    check = SpeechCheck(store, transcriber=FakeTranscriber("no way oh my god"))
    clip = _clip(tmp_path)
    assert await check.check(clip) is None
    assert clip.transcript == "no way oh my god"
    assert clip.speech_score and clip.speech_score > 2
    assert clip.speech_hits == ["no way", "oh my god"]
    assert clip.rank == pytest.approx(10 * rank_factor(clip.speech_score, 0.25), rel=1e-3)


async def test_calm_clip_loses_rank_and_gate(store: ConfigStore, tmp_path: Path) -> None:
    check = SpeechCheck(store, transcriber=FakeTranscriber("and then I said we should go there"))
    clip = _clip(tmp_path)
    assert await check.check(clip) is None  # gate off by default
    assert clip.speech_score == 0 and clip.rank < 10
    store.update({"speech": {"gate_threshold": 0.5}})
    clip = _clip(tmp_path)
    reason = await check.check(clip)
    assert reason and "gate_threshold" in reason
    # an excited clip passes the same gate
    assert await SpeechCheck(store, transcriber=FakeTranscriber("oh my god")).check(_clip(tmp_path)) is None


class _Broken:
    async def transcribe(self, path: Path, start: float = 0.0, duration: float | None = None) -> Transcript:
        raise RuntimeError("model exploded")


async def test_failure_leaves_clip_alone(store: ConfigStore, tmp_path: Path, caplog) -> None:
    store.update({"speech": {"gate_threshold": 5}})
    clip = _clip(tmp_path)
    assert await SpeechCheck(store, transcriber=_Broken()).check(clip) is None
    assert clip.speech_score is None and clip.transcript == "" and clip.rank == 10
    assert "model exploded" in caplog.text


async def test_missing_package_turns_it_off(store: ConfigStore, tmp_path: Path, monkeypatch, caplog) -> None:
    monkeypatch.setattr(sp, "faster_whisper_available", lambda: False)
    check = SpeechCheck(store)
    clip = _clip(tmp_path)
    assert await check.check(clip) is None and await check.check(clip) is None
    assert clip.speech_score is None
    assert caplog.text.count("faster-whisper is not installed") == 1


async def test_disabled_does_nothing(store: ConfigStore, tmp_path: Path) -> None:
    store.update({"speech": {"enabled": False}})
    fake = FakeTranscriber()
    assert await SpeechCheck(store, transcriber=fake).check(_clip(tmp_path)) is None
    assert fake.calls == []


async def test_concurrency_is_limited(store: ConfigStore, tmp_path: Path) -> None:
    import asyncio

    state = {"now": 0, "most": 0}

    class Slow:
        async def transcribe(self, path, start=0.0, duration=None):
            state["now"] += 1
            state["most"] = max(state["most"], state["now"])
            await asyncio.sleep(0.02)
            state["now"] -= 1
            return Transcript(text="wow", words=[Word(w="wow", t=1, e=1.2)], duration=10)

    check = SpeechCheck(store, transcriber=Slow())
    await asyncio.gather(*(check.check(_clip(tmp_path)) for _ in range(5)))
    assert state["most"] == 1
    store.update({"speech": {"max_concurrent": 2}})
    await asyncio.gather(*(check.check(_clip(tmp_path)) for _ in range(5)))
    assert state["most"] == 2


def test_old_clip_rows_still_load() -> None:
    db = Database(":memory:")
    old = Clip(streamer="alpha", window_start=0, window_end=1)
    data = old.model_dump(mode="json")
    for k in ("transcript", "speech_score", "speech_hits"):
        data.pop(k)
    loaded = Clip.model_validate(data)
    assert loaded.transcript == "" and loaded.speech_score is None and loaded.speech_hits == []
    db.save_clip(loaded)
    assert db.get_clip(old.id).speech_score is None


# ---- the pipeline ----------------------------------------------------------
def _pipeline_app(store: ConfigStore, tmp_path: Path, monkeypatch, transcriber) -> tuple[App, list]:
    app = App(store, demo=True, serve=False)
    app.speech = SpeechCheck(store, transcriber=transcriber)
    coros: list = []
    monkeypatch.setattr(app.clips, "_spawn", coros.append)
    monkeypatch.setattr(app.recorder, "status",
                        lambda s: StreamStatus(streamer=s, live=True, category="x"))
    monkeypatch.setattr(app.recorder, "window_ready", lambda s, ts: True)

    async def cut(streamer, start, end, clip_id):
        p = tmp_path / f"{clip_id}.mp4"
        p.write_bytes(b"raw")
        return p

    async def render(clip, layout):
        p = tmp_path / f"{clip.id}_{layout.layout}.mp4"
        p.write_bytes(b"out")
        return p

    monkeypatch.setattr(app.cutter, "cut", cut)
    monkeypatch.setattr(app.renderer, "render", render)
    return app, coros


def _peak(source: str) -> PeakEvent:
    now = time.time()
    return PeakEvent(streamer="alpha", ts_start=now, ts_peak=now, ts_end=now, source=source, rank=8.0)


async def test_auto_clip_gets_checked(store: ConfigStore, tmp_path: Path, monkeypatch) -> None:
    fake = FakeTranscriber("let's go no way")
    app, coros = _pipeline_app(store, tmp_path, monkeypatch, fake)
    clip = app.clips.on_peak(_peak("auto"))
    await coros.pop()
    saved = app.db.get_clip(clip.id)
    assert saved.status is ClipStatus.PENDING_REVIEW
    assert saved.transcript == "let's go no way" and saved.speech_score > 0 and saved.rank > 8.0
    assert len(fake.calls) == 1


async def test_manual_clip_skips_the_check(store: ConfigStore, tmp_path: Path, monkeypatch) -> None:
    fake = FakeTranscriber()
    app, coros = _pipeline_app(store, tmp_path, monkeypatch, fake)
    clip = app.clips.on_peak(_peak("manual"))
    await coros.pop()
    saved = app.db.get_clip(clip.id)
    assert saved.status is ClipStatus.PENDING_REVIEW and saved.speech_score is None
    assert fake.calls == []


async def test_gate_drops_a_calm_auto_clip(store: ConfigStore, tmp_path: Path, monkeypatch) -> None:
    store.update({"speech": {"gate_threshold": 1.0}})
    app, coros = _pipeline_app(store, tmp_path, monkeypatch, FakeTranscriber("we talked about the weather"))
    clip = app.clips.on_peak(_peak("auto"))
    await coros.pop()
    saved = app.db.get_clip(clip.id)
    assert saved.status is ClipStatus.DROPPED and "speech check" in saved.error
    assert saved.out_path  # rendered, so it can still be approved by hand
    assert app.clips.approve(clip.id).status is ClipStatus.QUEUED


async def test_failed_check_never_fails_the_clip(store: ConfigStore, tmp_path: Path, monkeypatch) -> None:
    store.update({"speech": {"gate_threshold": 1.0}})
    app, coros = _pipeline_app(store, tmp_path, monkeypatch, _Broken())
    clip = app.clips.on_peak(_peak("auto"))
    await coros.pop()
    saved = app.db.get_clip(clip.id)
    assert saved.status is ClipStatus.PENDING_REVIEW and saved.error == ""
    assert saved.speech_score is None and saved.rank == 8.0
