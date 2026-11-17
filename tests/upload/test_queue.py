from __future__ import annotations

import asyncio

import pytest

from argus.core.events import Topic
from argus.core.fakes import FakeUploader
from argus.core.models import ClipStatus, UploadResult
from argus.upload.queue import INTERRUPTED, ClipUploadQueue

T0 = 1_800_000_000.0  # same as conftest.T0


def R(kind: str, error: str = "", screenshot: str | None = None) -> UploadResult:
    return UploadResult(kind=kind, error=error, screenshot=screenshot)


@pytest.fixture
def make_queue(store, bus, db, clock):
    def _make(results=None, **kw):
        uploader = FakeUploader(store=store, bus=bus, delay_s=0, results=results)
        q = ClipUploadQueue(store=store, bus=bus, db=db, uploader=uploader, clock=clock,
                            sleep=clock.sleep, retry_pause_s=60, **kw)
        return q, uploader
    return _make


def real(store, **tiktok):
    store.update({"tiktok": {"dry_run": False, **tiktok}})


def status(db, clip):
    return db.get_clip(clip.id).status


async def test_dry_run_uploads_and_maps_to_dry_run_ok(make_queue, db, make_clip):
    q, up = make_queue()
    clip = make_clip()
    db.save_clip(clip)
    assert await q.step() is True
    got = db.get_clip(clip.id)
    assert got.status is ClipStatus.DRY_RUN_OK and got.error == ""
    assert up.calls == [(clip.id, clip.caption, True)]
    assert db.count_uploads_since(0, kind="dry_run") == 1
    assert db.count_uploads_since(0, kind="ok") == 0


async def test_dry_runs_bypass_the_limits(make_queue, db, make_clip, clock):
    for i in range(6):
        db.add_upload(f"old{i}", "ok", ts=clock() - 60)  # daily limit hit, last one a minute ago
    q, up = make_queue()
    clips = [make_clip() for _ in range(3)]
    for c in clips:
        db.save_clip(c)
    for _ in clips:
        assert await q.step() is True
    assert all(status(db, c) is ClipStatus.DRY_RUN_OK for c in clips)
    assert len(up.calls) == 3


async def test_real_upload_ok(make_queue, store, db, make_clip, clock):
    real(store)
    q, up = make_queue()
    clip = make_clip()
    db.save_clip(clip)
    assert await q.step() is True
    assert status(db, clip) is ClipStatus.UPLOADED
    assert up.calls == [(clip.id, clip.caption, False)]
    assert db.last_upload_ts() == clock()  # recorded with the injected clock


async def test_daily_limit_is_respected(make_queue, store, db, make_clip, clock):
    real(store, max_uploads_per_day=2, min_minutes_between_uploads=0, max_queue_age_h=0)
    q, up = make_queue()
    clips = [make_clip() for _ in range(3)]
    for c in clips:
        db.save_clip(c)
    assert await q.step() and await q.step()
    clock.t += 60
    assert await q.step() is False
    assert status(db, clips[2]) is ClipStatus.QUEUED
    assert "daily limit" in q.waiting_reason
    assert len(up.calls) == 2
    clock.t += 23 * 3600 + 58 * 60  # still inside 24 h of both uploads
    assert await q.step() is False
    clock.t += 3600  # the first two fall out of the window
    assert await q.step() is True
    assert status(db, clips[2]) is ClipStatus.UPLOADED
    assert q.waiting_reason is None


async def test_spacing_is_respected(make_queue, store, db, make_clip, clock):
    real(store, min_minutes_between_uploads=45)
    q, up = make_queue()
    a, b = make_clip(), make_clip()
    db.save_clip(a)
    db.save_clip(b)
    assert await q.step() is True
    clock.t += 44 * 60
    assert await q.step() is False
    assert status(db, b) is ClipStatus.QUEUED
    assert "45 min" in q.waiting_reason
    clock.t += 61
    assert await q.step() is True
    assert status(db, b) is ClipStatus.UPLOADED
    assert [c[0] for c in up.calls] == [a.id, b.id]


async def test_dry_runs_do_not_count_towards_limits(make_queue, store, db, make_clip):
    q, _ = make_queue()
    a, b = make_clip(), make_clip()
    db.save_clip(a)
    db.save_clip(b)
    assert await q.step()  # dry run
    real(store, max_uploads_per_day=1, min_minutes_between_uploads=45)
    assert await q.step()  # real upload right after the dry run: allowed
    assert status(db, b) is ClipStatus.UPLOADED


async def test_oldest_queued_first_and_dry_run_read_at_the_moment(make_queue, store, db, make_clip):
    q, up = make_queue()
    newer = make_clip(created_at=T0 + 100)
    older = make_clip(created_at=T0 + 1)
    db.save_clip(newer)
    db.save_clip(older)
    await q.step()
    real(store, min_minutes_between_uploads=0)
    await q.step()
    assert up.calls == [(older.id, older.caption, True), (newer.id, newer.caption, False)]


async def test_transient_is_retried_twice_then_fails(make_queue, store, db, make_clip, clock):
    real(store)
    q, up = make_queue(results=[R("transient", "net down")] * 3)
    clip = make_clip()
    db.save_clip(clip)
    assert await q.step() is True
    got = db.get_clip(clip.id)
    assert got.status is ClipStatus.FAILED
    assert "net down" in got.error and "3 attempts" in got.error
    assert len(up.calls) == 3
    assert clock.sleeps == [60, 60]
    assert db.count_uploads_since(0, kind="transient") == 3
    assert q.paused_reason is None  # transient never pauses


async def test_transient_then_ok(make_queue, store, db, make_clip):
    real(store)
    q, up = make_queue(results=[R("transient", "timeout"), R("ok")])
    clip = make_clip()
    db.save_clip(clip)
    await q.step()
    assert status(db, clip) is ClipStatus.UPLOADED
    assert db.get_clip(clip.id).error == ""
    assert len(up.calls) == 2


async def test_rejected_during_retry_pause_is_not_retried(make_queue, store, db, make_clip, clock):
    real(store)
    q, up = make_queue(results=[R("transient")] * 3)
    clip = make_clip()
    db.save_clip(clip)

    def reject():
        c = db.get_clip(clip.id)
        c.status = ClipStatus.REJECTED
        db.save_clip(c)

    clock.on_sleep = reject
    await q.step()
    assert len(up.calls) == 1
    assert status(db, clip) is ClipStatus.REJECTED


async def test_blocked_is_not_retried_and_pauses_the_queue(make_queue, store, db, bus, make_clip):
    real(store)
    q, up = make_queue(results=[R("blocked", "captcha", "/tmp/shot.png")])
    a, b = make_clip(), make_clip()
    db.save_clip(a)
    db.save_clip(b)
    assert await q.step() is True
    got = db.get_clip(a.id)
    assert got.status is ClipStatus.FAILED and got.error == "captcha"
    assert got.debug_screenshot == "/tmp/shot.png"
    assert len(up.calls) == 1
    assert "captcha" in q.paused_reason
    # paused: the next clip waits
    assert await q.step() is False
    assert await q.step() is False
    assert status(db, b) is ClipStatus.QUEUED
    assert len(up.calls) == 1
    assert db.count_uploads_since(0, kind="blocked") == 1


async def test_hand_approval_resumes(make_queue, store, db, bus, make_clip):
    real(store, min_minutes_between_uploads=0)
    q, _ = make_queue(results=[R("blocked", "logged out")])
    a = make_clip()
    db.save_clip(a)
    await q.step()
    assert q.paused_reason
    # what ClipService.approve does
    c = db.get_clip(a.id)
    c.status, c.error = ClipStatus.QUEUED, ""
    db.save_clip(c)
    bus.publish(Topic.CLIP, c)
    assert await q.step() is True
    assert q.paused_reason is None
    assert status(db, a) is ClipStatus.UPLOADED


async def test_hand_approval_of_a_clip_older_than_the_queue_resumes(store, bus, db, make_clip, clock):
    old = make_clip(status=ClipStatus.PENDING_REVIEW)
    db.save_clip(old)
    q = ClipUploadQueue(store=store, bus=bus, db=db, uploader=FakeUploader(store=store, bus=bus, delay_s=0),
                        clock=clock, sleep=clock.sleep)
    q.pause("test")
    c = db.get_clip(old.id)
    c.status = ClipStatus.QUEUED
    db.save_clip(c)
    bus.publish(Topic.CLIP, c)
    assert await q.step() is True
    assert status(db, old) is ClipStatus.DRY_RUN_OK


async def test_config_change_resumes(make_queue, store, db, make_clip):
    real(store, min_minutes_between_uploads=0)
    q, _ = make_queue(results=[R("blocked", "captcha")])
    a, b = make_clip(), make_clip()
    db.save_clip(a)
    db.save_clip(b)
    await q.step()
    assert q.paused_reason
    store.update({"tiktok": {"headless": False}})
    assert await q.step() is True
    assert status(db, b) is ClipStatus.UPLOADED


async def test_auto_queued_clip_does_not_resume(make_queue, store, db, bus, make_clip):
    real(store)
    q, _ = make_queue(results=[R("blocked", "captcha")])
    a = make_clip()
    db.save_clip(a)
    await q.step()
    assert q.paused_reason
    # ClipService._process publishes the same object through each status; read late,
    # every event shows the final status.
    new = make_clip(status=ClipStatus.RENDERING)
    db.save_clip(new)
    bus.publish(Topic.CLIP, new)
    new.status = ClipStatus.QUEUED
    db.save_clip(new)
    bus.publish(Topic.CLIP, new)
    assert await q.step() is False
    assert q.paused_reason
    assert status(db, new) is ClipStatus.QUEUED


async def test_missing_render_fails_that_clip_without_pausing(make_queue, db, make_clip):
    q, up = make_queue()
    gone = make_clip(write=False)
    tiny = make_clip(size=100)
    ok = make_clip()
    for c in (gone, tiny, ok):
        db.save_clip(c)
    await q.step()
    await q.step()
    await q.step()
    assert status(db, gone) is ClipStatus.FAILED and "not found" in db.get_clip(gone.id).error
    assert status(db, tiny) is ClipStatus.FAILED and "too small" in db.get_clip(tiny.id).error
    assert status(db, ok) is ClipStatus.DRY_RUN_OK
    assert [c[0] for c in up.calls] == [ok.id]
    assert q.paused_reason is None


async def test_uploader_that_raises_counts_as_blocked(store, bus, db, make_clip, clock):
    class Broken:
        async def upload(self, clip, caption, dry_run, account=None):
            raise ValueError("bug")

    q = ClipUploadQueue(store=store, bus=bus, db=db, uploader=Broken(), clock=clock, sleep=clock.sleep)
    clip = make_clip()
    db.save_clip(clip)
    await q.step()
    assert status(db, clip) is ClipStatus.FAILED
    assert "bug" in db.get_clip(clip.id).error
    assert q.paused_reason


async def test_every_status_change_is_saved_and_published(make_queue, bus, db, make_clip):
    q, _ = make_queue()
    clip = make_clip()
    db.save_clip(clip)
    with bus.subscribe(Topic.CLIP) as sub:
        await q.step()
        seen = []
        while not sub.queue.empty():
            seen.append(sub.queue.get_nowait()[1].status)
    assert seen == [ClipStatus.UPLOADING, ClipStatus.DRY_RUN_OK]


async def test_recover_interrupted_uploads(make_queue, db, make_clip):
    q, _ = make_queue()
    stuck = make_clip(status=ClipStatus.UPLOADING)
    db.save_clip(stuck)
    q.recover_interrupted()
    got = db.get_clip(stuck.id)
    assert got.status is ClipStatus.FAILED and got.error == INTERRUPTED


async def test_cancel_during_upload_leaves_clip_failed(store, bus, db, make_clip):
    started = asyncio.Event()

    class Slow:
        async def upload(self, clip, caption, dry_run, account=None):
            started.set()
            await asyncio.sleep(3600)

    q = ClipUploadQueue(store=store, bus=bus, db=db, uploader=Slow(), poll_s=0.01)
    clip = make_clip()
    db.save_clip(clip)
    task = asyncio.create_task(q.run())
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    got = db.get_clip(clip.id)
    assert got.status is ClipStatus.FAILED and got.error == INTERRUPTED


async def test_run_loop_processes_and_stops_cleanly(store, bus, db, make_clip):
    q = ClipUploadQueue(store=store, bus=bus, db=db,
                        uploader=FakeUploader(store=store, bus=bus, delay_s=0), poll_s=0.01)
    clip = make_clip()
    db.save_clip(clip)
    task = asyncio.create_task(q.run())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if status(db, clip) is ClipStatus.DRY_RUN_OK:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert status(db, clip) is ClipStatus.DRY_RUN_OK
    assert q._sub not in bus._subs


async def test_approval_during_a_blocked_upload_does_not_resume(store, bus, db, make_clip, clock):
    """Approvals and config changes made while the blocked upload ran are old news."""
    real(store, min_minutes_between_uploads=0)
    other = make_clip(status=ClipStatus.PENDING_REVIEW, created_at=T0 + 500)
    calls = []

    class BlockedWhileUserActs:
        async def upload(self, clip, caption, dry_run, account=None):
            calls.append(clip.id)
            if len(calls) > 1:
                return R("ok")
            c = db.get_clip(other.id)
            c.status = ClipStatus.QUEUED  # approved by hand during the upload
            db.save_clip(c)
            bus.publish(Topic.CLIP, c)
            store.update({"tiktok": {"headless": True}})
            return R("blocked", "captcha")

    db.save_clip(other)
    q = ClipUploadQueue(store=store, bus=bus, db=db, uploader=BlockedWhileUserActs(),
                        clock=clock, sleep=clock.sleep)
    first = make_clip()
    db.save_clip(first)
    assert await q.step() is True
    assert q.paused_reason
    assert await q.step() is False
    assert status(db, other) is ClipStatus.QUEUED
    assert calls == [first.id]
    # a fresh approval after the pause does resume
    c = db.get_clip(first.id)
    c.status, c.error = ClipStatus.QUEUED, ""
    db.save_clip(c)
    bus.publish(Topic.CLIP, c)
    assert await q.step() is True
    assert q.paused_reason is None
