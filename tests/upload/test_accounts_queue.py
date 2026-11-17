"""The queue with several TikTok accounts: clip ranking, expiry, account choice,
per-account limits and pauses. Only fakes; no browser."""

from __future__ import annotations

import pytest

from livewatcher.core.events import Topic
from livewatcher.core.fakes import FakeUploader
from livewatcher.core.models import ClipStatus, PeakEvent, UploadResult
from livewatcher.upload import login
from livewatcher.upload.queue import ClipUploadQueue, account_slot, choose_account
from livewatcher.upload.tiktok_browser import (
    CAPTCHA,
    LOGIN_PROMPT,
    NOT_LOGGED_IN,
    SENT_TO_LOGIN,
    blocked_scope,
)

T0 = 1_800_000_000.0
H = 3600


def R(kind: str, error: str = "") -> UploadResult:
    return UploadResult(kind=kind, error=error)


@pytest.fixture
def make_queue(store, bus, db, clock):
    def _make(results=None, **kw):
        uploader = FakeUploader(store=store, bus=bus, delay_s=0, results=results)
        q = ClipUploadQueue(store=store, bus=bus, db=db, uploader=uploader, clock=clock,
                            sleep=clock.sleep, retry_pause_s=60, **kw)
        return q, uploader
    return _make


@pytest.fixture
def accounts(store, tmp_path):
    """Configure accounts: accounts(("a", {...}), "b", dry_run=False, cycle_accounts=True)."""
    def _set(*specs, **tiktok):
        rows = []
        for spec in specs:
            name, extra = (spec, {}) if isinstance(spec, str) else spec
            rows.append({"name": name, "browser_profile_dir": str(tmp_path / "profiles" / name),
                         "max_uploads_per_day": 6, "min_minutes_between_uploads": 0, **extra})
        store.update({"tiktok": {"accounts": rows, "dry_run": False, **tiktok}})
    return _set


def queue_clips(db, make_clip, n, **kw):
    clips = [make_clip(**kw) for _ in range(n)]
    for c in clips:
        db.save_clip(c)
    return clips


async def run_steps(q, n):
    for _ in range(n):
        await q.step()


# ---- which clip ----------------------------------------------------------------------
async def test_manual_first_then_score_then_oldest(make_queue, db, make_clip, accounts):
    accounts("a", dry_run=True)
    old_low, old_high, new_high, manual_low = (make_clip(created_at=T0 + i) for i in range(4))
    old_low.score, old_high.score, new_high.score, manual_low.score = 1.0, 5.0, 5.0, 0.0
    peak = PeakEvent(streamer="alpha", ts_start=T0 + 20, ts_peak=T0 + 20, ts_end=T0 + 20, source="manual")
    auto = PeakEvent(streamer="alpha", ts_start=T0 + 5, ts_peak=T0 + 5, ts_end=T0 + 9, score=1.0)
    db.save_peak(peak)
    db.save_peak(auto)
    manual_low.peak_id, old_low.peak_id = peak.id, auto.id
    for c in (old_low, old_high, new_high, manual_low):
        db.save_clip(c)
    q, up = make_queue()
    await run_steps(q, 4)
    assert [c[0] for c in up.calls] == [manual_low.id, old_high.id, new_high.id, old_low.id]


async def test_a_queued_clip_expires_after_max_queue_age(make_queue, db, make_clip, clock, accounts):
    accounts(("a", {"max_uploads_per_day": 0}), max_queue_age_h=2)
    q, up = make_queue()
    (clip,) = queue_clips(db, make_clip, 1, created_at=clock())
    await q.step()
    assert db.get_clip(clip.id).status is ClipStatus.QUEUED
    clock.t += 2 * H - 1
    await q.step()
    assert db.get_clip(clip.id).status is ClipStatus.QUEUED
    clock.t += 2
    await q.step()
    got = db.get_clip(clip.id)
    assert got.status is ClipStatus.DROPPED
    assert got.error == "expired: no upload slot within 2 h"
    assert up.calls == []


async def test_expiry_counts_from_the_approval_not_the_creation(make_queue, db, bus, make_clip, clock,
                                                                accounts):
    accounts(("a", {"max_uploads_per_day": 0}), max_queue_age_h=1)
    q, _ = make_queue()
    (clip,) = queue_clips(db, make_clip, 1, status=ClipStatus.PENDING_REVIEW, created_at=clock())
    bus.publish(Topic.CLIP, clip.model_copy())
    clock.t += 5 * H  # reviewed hours later
    clip.status = ClipStatus.QUEUED
    db.save_clip(clip)
    bus.publish(Topic.CLIP, clip.model_copy())
    await q.step()
    assert db.get_clip(clip.id).status is ClipStatus.QUEUED
    clock.t += H + 1
    await q.step()
    assert db.get_clip(clip.id).status is ClipStatus.DROPPED


async def test_expiry_also_runs_while_paused_and_zero_means_never(make_queue, db, make_clip, clock,
                                                                  accounts, store):
    accounts("a", max_queue_age_h=1)
    q, _ = make_queue()
    a, = queue_clips(db, make_clip, 1, created_at=clock())
    q.pause("test")
    clock.t += H + 1
    await q.step()
    assert db.get_clip(a.id).status is ClipStatus.DROPPED
    store.update({"tiktok": {"max_queue_age_h": 0, "accounts": [{"name": "a", "max_uploads_per_day": 0}]}})
    b, = queue_clips(db, make_clip, 1, created_at=clock())
    clock.t += 1000 * H
    await q.step()
    assert db.get_clip(b.id).status is ClipStatus.QUEUED


# ---- which account -------------------------------------------------------------------
async def test_cycle_accounts_round_robin(make_queue, db, make_clip, accounts):
    accounts("a", "b", "c", cycle_accounts=True)
    q, up = make_queue()
    clips = queue_clips(db, make_clip, 5)
    await run_steps(q, 5)
    assert up.accounts == ["a", "b", "c", "a", "b"]
    assert [db.get_clip(c.id).account for c in clips] == ["a", "b", "c", "a", "b"]


async def test_without_cycle_the_first_free_account_is_used(make_queue, db, make_clip, accounts):
    accounts(("a", {"max_uploads_per_day": 2}), "b", "c", cycle_accounts=False)
    q, up = make_queue()
    queue_clips(db, make_clip, 4)
    await run_steps(q, 4)
    assert up.accounts == ["a", "a", "b", "b"]


async def test_cycle_skips_accounts_without_a_free_slot(make_queue, db, make_clip, accounts):
    accounts("a", ("b", {"max_uploads_per_day": 1}), "c", cycle_accounts=True)
    q, up = make_queue()
    queue_clips(db, make_clip, 5)
    await run_steps(q, 5)
    assert up.accounts == ["a", "b", "c", "a", "c"]


async def test_disabled_accounts_are_never_used(make_queue, db, make_clip, accounts):
    accounts(("a", {"enabled": False}), "b", cycle_accounts=True)
    q, up = make_queue()
    queue_clips(db, make_clip, 3)
    await run_steps(q, 3)
    assert up.accounts == ["b", "b", "b"]


async def test_rotation_resumes_after_a_restart(make_queue, db, make_clip, accounts, clock):
    accounts("a", "b", "c", cycle_accounts=True)
    db.add_upload("old", "ok", ts=clock() - 10 * H, account="b")
    q, up = make_queue()
    queue_clips(db, make_clip, 2)
    await run_steps(q, 2)
    assert up.accounts == ["c", "a"]


async def test_last_used_account_removed_from_the_list(make_queue, db, make_clip, accounts, clock):
    accounts("a", "b", cycle_accounts=True)
    db.add_upload("old", "ok", ts=clock() - H, account="gone")
    q, up = make_queue()
    queue_clips(db, make_clip, 1)
    await q.step()
    assert up.accounts == ["a"]


async def test_per_account_spacing(make_queue, db, make_clip, accounts, clock):
    accounts(("a", {"min_minutes_between_uploads": 45}), ("b", {"min_minutes_between_uploads": 45}))
    q, up = make_queue()
    queue_clips(db, make_clip, 3)
    await run_steps(q, 2)
    assert up.accounts == ["a", "b"]  # a is spaced, so the second goes to b at once
    clock.t += 44 * 60
    assert await q.step() is False
    clock.t += 61
    assert await q.step() is True
    assert up.accounts == ["a", "b", "a"]


async def test_all_accounts_full_waits_with_a_reason(make_queue, db, make_clip, accounts, clock):
    accounts(("a", {"max_uploads_per_day": 1}), ("b", {"max_uploads_per_day": 1,
                                                       "min_minutes_between_uploads": 30}),
             max_queue_age_h=0)
    q, _up = make_queue()
    clips = queue_clips(db, make_clip, 3)
    await run_steps(q, 2)
    assert await q.step() is False
    assert db.get_clip(clips[2].id).status is ClipStatus.QUEUED
    reason = q.waiting_reason
    assert "account a: daily limit reached (1 of 1" in reason
    assert "account b: daily limit reached" in reason
    assert q.next_account() == (None, reason)
    clock.t += 24 * H + 1
    assert await q.step() is True
    assert q.waiting_reason is None


async def test_dry_runs_rotate_but_do_not_count(make_queue, db, make_clip, accounts, store):
    accounts(("a", {"max_uploads_per_day": 1, "min_minutes_between_uploads": 45}),
             ("b", {"max_uploads_per_day": 1, "min_minutes_between_uploads": 45}),
             cycle_accounts=True, dry_run=True)
    q, up = make_queue()
    queue_clips(db, make_clip, 5)
    await run_steps(q, 4)  # far past both limits: dry runs aren't limited
    assert up.accounts == ["a", "b", "a", "b"]
    assert all(dry for _, _, dry in up.calls)
    assert db.count_uploads_since(0, kind="ok") == 0
    assert db.count_uploads_since(0, kind="dry_run", account="a") == 2
    store.update({"tiktok": {"dry_run": False}})
    assert await q.step() is True  # a real upload right after: both accounts still free
    assert up.accounts[-1] == "a"


async def test_clip_account_is_set_when_the_upload_starts(make_queue, db, bus, make_clip, accounts):
    accounts("a", "b")
    q, _ = make_queue()
    (clip,) = queue_clips(db, make_clip, 1)
    with bus.subscribe(Topic.CLIP) as sub:
        await q.step()
        events = []
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait()[1])
    uploading = [c for c in events if c.status is ClipStatus.UPLOADING]
    assert uploading and uploading[0].account == "a"
    assert db.get_clip(clip.id).account == "a"
    assert db.count_uploads_since(0, account="a") == 1


async def test_implicit_default_account(make_queue, store, db, make_clip, tmp_path):
    store.update({"tiktok": {"dry_run": False}})
    q, up = make_queue()
    (clip,) = queue_clips(db, make_clip, 1)
    await q.step()
    assert up.accounts == ["default"]
    assert db.get_clip(clip.id).account == "default"
    assert db.count_uploads_since(0, account="default") == 1
    (acc,) = store.get().tiktok_accounts()
    assert acc.browser_profile_dir == str(tmp_path / "profile")


async def test_old_upload_rows_count_for_the_default_account(make_queue, store, db, make_clip, clock):
    store.update({"tiktok": {"dry_run": False, "max_uploads_per_day": 2}})
    db.add_upload("x1", "ok", "", clock() - 60)  # positional, as the old callers do
    db.add_upload("x2", "ok", "", clock() - 30)
    q, _up = make_queue()
    queue_clips(db, make_clip, 1)
    assert await q.step() is False
    assert "daily limit" in q.waiting_reason


# ---- pauses --------------------------------------------------------------------------
@pytest.mark.parametrize("error", [
    f"{NOT_LOGGED_IN} (account a): run `./lw login-tiktok --account a` and log in",
    f"{CAPTCHA} (account a); solve it in the browser",
    f"{SENT_TO_LOGIN} (account a): run `./lw login-tiktok --account a`",
    f"{LOGIN_PROMPT} (account a): run `./lw login-tiktok --account a`",
    "captcha", "logged out",
])
def test_blocked_scope_account(error):
    assert blocked_scope(error) == "account"


@pytest.mark.parametrize("error", [
    "the page didn't respond as expected (Timeout); TikTok may have changed its upload page",
    "couldn't find the caption editor on the upload page (tried css 'x'); TikTok may have changed the page",
    "no Post button after attaching the file; the upload page doesn't look as expected",
    "the Post button is disabled after filling in the form",
    "Post was clicked but no confirmation appeared; check TikTok before retrying",
    "Post or Save draft was clicked, then Error: boom. Check TikTok before retrying.",
    "the browser can't start: system libraries are missing",
    "uploader error: RuntimeError: boom",
    "",
])
def test_blocked_scope_global(error):
    assert blocked_scope(error) == "global"


async def test_logged_out_pauses_only_that_account(make_queue, db, make_clip, accounts, bus):
    accounts("a", "b", cycle_accounts=False)
    err = f"{NOT_LOGGED_IN} (account a): run `./lw login-tiktok --account a` and log in"
    q, up = make_queue(results=[R("blocked", err)])
    clips = queue_clips(db, make_clip, 3)
    await run_steps(q, 3)
    assert db.get_clip(clips[0].id).status is ClipStatus.FAILED  # never retried elsewhere
    assert up.accounts == ["a", "b", "b"]
    assert set(q.account_paused) == {"a"}
    assert "account a paused" in q.paused_reason
    assert q.login_seen["a"][0] is False
    q.note_login("a", True)
    assert q.account_paused == {} and q.paused_reason is None


async def test_captcha_pauses_only_that_account(make_queue, db, make_clip, accounts):
    accounts("a", "b", cycle_accounts=True)
    q, up = make_queue(results=[R("ok"), R("blocked", f"{CAPTCHA} (account b)")])
    queue_clips(db, make_clip, 4)
    await run_steps(q, 4)
    assert up.accounts == ["a", "b", "a", "a"]
    assert set(q.account_paused) == {"b"}
    assert "b" not in q.login_seen or q.login_seen["b"][0] is not False  # a CAPTCHA isn't a logout


async def test_all_accounts_paused_waits_and_reports(make_queue, db, make_clip, accounts, bus):
    accounts("a", "b")
    q, up = make_queue(results=[R("blocked", "captcha"), R("blocked", "logged out")])
    clips = queue_clips(db, make_clip, 3)
    await run_steps(q, 3)
    assert len(up.calls) == 2
    assert db.get_clip(clips[2].id).status is ClipStatus.QUEUED
    assert "paused" in q.waiting_reason
    assert "account a paused" in q.paused_reason and "account b paused" in q.paused_reason
    # a hand approval lifts every pause
    clip = db.get_clip(clips[0].id)
    clip.status = ClipStatus.QUEUED
    db.save_clip(clip)
    bus.publish(Topic.CLIP, clip)
    await q.step()
    assert q.paused_reason is None
    assert len(up.calls) == 3


async def test_page_changed_pauses_everything(make_queue, db, make_clip, accounts, store):
    accounts("a", "b")
    q, up = make_queue(results=[R("blocked", "couldn't find the post button; TikTok may have changed the page")])
    queue_clips(db, make_clip, 2)
    await run_steps(q, 3)
    assert up.accounts == ["a"]
    assert q.account_paused == {}
    assert "changed the page" in q.paused_reason
    store.update({"tiktok": {"cycle_accounts": True}})  # any config change resumes
    await q.step()
    assert q.paused_reason is None and len(up.calls) == 2


async def test_a_busy_profile_is_skipped(make_queue, db, make_clip, accounts, store):
    accounts("a", "b")
    q, up = make_queue()
    queue_clips(db, make_clip, 1)
    a = store.get().tiktok_accounts()[0]
    with login.claim_profile(a):
        assert q.next_account()[0].name == "b"
        await q.step()
    assert up.accounts == ["b"]


async def test_no_enabled_account(make_queue, db, make_clip, accounts):
    accounts(("a", {"enabled": False}))
    q, _up = make_queue()
    queue_clips(db, make_clip, 1)
    assert await q.step() is False
    assert q.waiting_reason == "no TikTok account is enabled"


# ---- pure helpers --------------------------------------------------------------------
def test_account_slot_next_free(store, db, accounts, clock):
    accounts(("a", {"max_uploads_per_day": 2, "min_minutes_between_uploads": 60}))
    a = store.get().tiktok_accounts()[0]
    now = clock()
    slot = account_slot(a, db, now)
    assert (slot.uploads_24h, slot.last_upload_ts, slot.next_free_ts, slot.limit_reason) == (0, None, None, None)
    db.add_upload("1", "ok", ts=now - 5 * H, account="a")
    db.add_upload("2", "dry_run", ts=now - 60, account="a")  # not counted
    db.add_upload("3", "ok", ts=now - 30 * 60, account="a")
    db.add_upload("4", "ok", ts=now - 60, account="b")       # another account
    slot = account_slot(a, db, now)
    assert slot.uploads_24h == 2 and slot.last_upload_ts == now - 30 * 60
    assert slot.next_free_ts == now - 5 * H + 24 * H  # the daily limit outlasts the spacing
    assert "daily limit" in slot.limit_reason
    acc, why = choose_account(store.get(), db, now, dry_run=False)
    assert acc is None and why.startswith("account a: daily limit")
    acc, why = choose_account(store.get(), db, now, dry_run=True)
    assert acc.name == "a" and why is None
