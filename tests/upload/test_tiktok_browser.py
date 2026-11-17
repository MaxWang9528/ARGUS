"""Uploader tests. No real browser and no TikTok: a fake page stands in, keyed by the
selector specs in SELECTORS, so these test the flow and its safety rules, not
whether the selectors match TikTok's real page."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from livewatcher.upload import tiktok_browser as tb
from livewatcher.upload.tiktok_browser import (
    SELECTORS,
    SPEC_KINDS,
    TikTokUploader,
    build_locator,
    caption_matches,
    caption_tokens,
    check_video_file,
    classify_error,
    describe_spec,
    is_disabled,
    normalize_caption,
)


# ---- pure helpers ----------------------------------------------------------------------
def test_selectors_are_well_formed_with_fallbacks():
    required = {"file_input", "caption_editor", "post_button", "captcha", "success", "login_wall",
                "upload_done", "upload_error", "post_now_button", "dismiss"}
    assert required <= SELECTORS.keys()
    for name, specs in SELECTORS.items():
        assert specs, name
        for spec in specs:
            assert spec[0] in SPEC_KINDS and len(spec) == SPEC_KINDS[spec[0]], (name, spec)
            if spec[0].endswith("_re"):
                re.compile(spec[-1])
    for name in ("file_input", "caption_editor", "post_button", "captcha", "success"):
        assert len(SELECTORS[name]) >= 2, f"{name} needs a fallback"


def test_post_button_is_matched_by_exact_name():
    assert SELECTORS["post_button"][0] == ("role", "button", "Post")


class RecordingRoot:
    def __init__(self):
        self.calls = []

    def get_by_role(self, role, **kw):
        self.calls.append(("get_by_role", role, kw))
        return "loc"

    def get_by_text(self, text, **kw):
        self.calls.append(("get_by_text", text, kw))
        return "loc"

    def locator(self, css):
        self.calls.append(("locator", css))
        return "loc"


def test_build_locator():
    root = RecordingRoot()
    build_locator(root, ("role", "button", "Post"))
    build_locator(root, ("role_re", "button", r"^upload another video$"))
    build_locator(root, ("text", "Uploaded"))
    build_locator(root, ("text_re", r"verify"))
    build_locator(root, ("css", "input[type='file']"))
    assert root.calls[0] == ("get_by_role", "button", {"name": "Post", "exact": True})
    assert root.calls[1][2]["name"].pattern == r"^upload another video$"
    assert root.calls[1][2]["name"].flags & re.IGNORECASE
    assert root.calls[2] == ("get_by_text", "Uploaded", {"exact": True})
    assert root.calls[3][1].pattern == "verify"
    assert root.calls[4] == ("locator", "input[type='file']")
    with pytest.raises(ValueError):
        build_locator(root, ("xpath", "//div"))
    with pytest.raises(ValueError):
        build_locator(root, ("role", "button"))


def test_describe_spec():
    assert describe_spec(("role", "button", "Post")) == "button named 'Post'"
    assert describe_spec(("css", "#x")) == "css '#x'"
    assert describe_spec(("text_re", "a")) == "text 'a'"


def test_normalize_caption():
    assert normalize_caption("  hi   there \t you  ") == "hi there you"
    assert normalize_caption("a\r\nb\n\n\n\nc") == "a\nb\n\nc"
    assert normalize_caption("zero\u200bwidth\x00 ctl") == "zerowidth ctl"
    assert normalize_caption("Cafe\u0301") == "Caf\u00e9"
    assert normalize_caption("alpha on Twitch 🔴 #alpha") == "alpha on Twitch 🔴 #alpha"
    long = "word " * 1000
    out = normalize_caption(long, max_chars=100)
    assert len(out) <= 100 and out.endswith("word")
    assert normalize_caption("x" * 150, max_chars=100) == "x" * 100
    assert normalize_caption("") == ""


def test_caption_tokens():
    assert caption_tokens("alpha on Twitch 🔴 #alpha #twitch") == [
        "alpha on Twitch 🔴 ", "#alpha", " ", "#twitch"]
    assert caption_tokens("hi @bob!") == ["hi ", "@bob!"]
    assert caption_tokens("#a#b") == ["#a", "#b"]
    assert caption_tokens("plain") == ["plain"]
    assert "".join(caption_tokens("x #y z @w")) == "x #y z @w"


def test_caption_matches():
    assert caption_matches("a #b", "a  #b \n")
    assert caption_matches("a #b", "a\u00a0#b\u200b")
    assert not caption_matches("a #b", "clip.mp4a #b")
    assert not caption_matches("a #b", "a #bob")


def test_is_disabled():
    assert is_disabled({"disabled": True})
    assert is_disabled({"aria": "true"})
    assert is_disabled({"data": "TRUE"})
    assert is_disabled({"cls": "TUXButton TUXButton--disabled"})
    assert is_disabled({"cls": "btn disabled"})
    assert not is_disabled({"disabled": False, "aria": "false", "data": None, "cls": "TUXButton--primary"})
    assert not is_disabled({})


def test_check_video_file(tmp_path: Path):
    assert "no rendered file" in check_video_file(None)
    assert "not found" in check_video_file(tmp_path / "nope.mp4")
    (tmp_path / "x.txt").write_bytes(b"\0" * 50_000)
    assert "not a video" in check_video_file(tmp_path / "x.txt")
    (tmp_path / "small.mp4").write_bytes(b"fake clip (ffmpeg not installed)")
    assert "too small" in check_video_file(tmp_path / "small.mp4")
    (tmp_path / "ok.mp4").write_bytes(b"\0" * 50_000)
    assert check_video_file(tmp_path / "ok.mp4") is None
    assert "too big" in check_video_file(tmp_path / "ok.mp4", max_bytes=1000)


class PlaywrightTimeout(Exception):
    pass


PlaywrightTimeout.__name__ = "TimeoutError"
PlaywrightTimeout.__module__ = "playwright._impl._errors"


def test_classify_error():
    assert classify_error(RuntimeError("libnspr4.so: error while loading shared libraries"))[0] == "blocked"
    assert "install-deps" in classify_error(RuntimeError("error while loading shared libraries"))[1]
    assert classify_error(RuntimeError("Executable doesn't exist at /x"))[0] == "blocked"
    assert classify_error(RuntimeError("page.goto: net::ERR_INTERNET_DISCONNECTED"))[0] == "transient"
    assert classify_error(ConnectionResetError("reset"))[0] == "transient"
    assert classify_error(ValueError("weird"))[0] == "blocked"
    kind, msg = classify_error(RuntimeError("net::ERR_FAILED"), posted=True)
    assert kind == "blocked" and "Check TikTok" in msg


# ---- fake browser ----------------------------------------------------------------------
def key(spec):
    return spec if spec[0] != "role_re" else ("role", spec[1], spec[2])


class FakeLocator:
    def __init__(self, page, k):
        self.page, self.k = page, k

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self.k in self.page.present else 0

    async def is_visible(self):
        return self.k in self.page.present and self.k not in self.page.hidden

    async def evaluate(self, _js):
        return self.page.state.get(self.k, {})

    async def set_input_files(self, path):
        self.page.files.append(path)
        self.page.after_attach()

    async def click(self):
        self.page.clicks.append(self.k)
        if self.k in self.page.post_keys and self.page.post_shows_success:
            self.page.show("success")
        if self.k in self.page.click_shows:
            self.page.show(self.page.click_shows[self.k])

    async def inner_text(self):
        return self.page.editor_text


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    async def press(self, combo):
        if combo == "ControlOrMeta+A":
            self.page.selected = True
        elif combo == "Backspace" and self.page.selected:
            self.page.editor_text, self.page.selected = "", False

    async def type(self, text, delay=0):
        self.page.editor_text += text


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.present: set = set()
        self.hidden: set = set()
        self.state: dict = {}
        self.files: list = []
        self.clicks: list = []
        self.editor_text = ""
        self.selected = False
        self.keyboard = FakeKeyboard(self)
        self.post_keys = {key(s) for s in SELECTORS["post_button"] + SELECTORS["post_now_button"]}
        self.post_shows_success = True
        self.on_goto = lambda: self.show("file_input", hidden=True)
        self.after_attach = self.default_after_attach
        self.screenshots: list = []
        self.click_shows: dict = {}  # locator key -> selector name to show when it's clicked

    @property
    def frames(self):
        return [self]

    def show(self, name, *, hidden=False, index=0, state=None):
        k = key(SELECTORS[name][index])
        self.present.add(k)
        if hidden:
            self.hidden.add(k)
        if state is not None:
            self.state[k] = state

    def default_after_attach(self):
        self.editor_text = "c0_blur_fit.mp4"  # TikTok pre-fills the file name
        self.show("caption_editor")
        self.show("upload_done")
        self.show("post_button", state={"disabled": False})

    def locator(self, css):
        return FakeLocator(self, ("css", css))

    def get_by_role(self, role, name=None, exact=False):
        return FakeLocator(self, ("role", role, name if isinstance(name, str) else name.pattern))

    def get_by_text(self, text, exact=False):
        if isinstance(text, str):
            return FakeLocator(self, ("text", text))
        return FakeLocator(self, ("text_re", text.pattern))

    async def goto(self, url, **kw):
        self.url = url
        self.on_goto()

    async def screenshot(self, path, full_page=False):
        Path(path).write_bytes(b"png")
        self.screenshots.append(path)


class FakeContext:
    def __init__(self, page, logged_in=True):
        self.pages = [page]
        self.logged_in = logged_in
        self.closed = False

    async def cookies(self, url):
        return [{"name": "sessionid", "value": "x"}] if self.logged_in else []

    async def close(self):
        self.closed = True

    async def add_cookies(self, cookies):
        pass


class FakePlaywright:
    def __init__(self, context, launch_error=None):
        self.context, self.launch_error = context, launch_error
        self.launch_kwargs = None
        self.chromium = self

    async def launch_persistent_context(self, profile, **kw):
        self.launch_kwargs = {"profile": profile, **kw}
        if self.launch_error:
            raise self.launch_error
        return self.context

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.exited = True


@pytest.fixture
def page():
    return FakePage()


@pytest.fixture
def browser(page):
    ctx = FakeContext(page)
    return FakePlaywright(ctx)


@pytest.fixture
def uploader(store, bus, browser, clock):
    return TikTokUploader(store=store, bus=bus, playwright_factory=lambda: browser,
                          sleep=clock.sleep, monotonic=clock, element_timeout_s=5,
                          processing_timeout_s=60, confirm_timeout_s=20, enabled_grace_s=3, poll_s=0.5)


CAPTION = "alpha on Twitch 🔴 #alpha #clips"


async def test_missing_file_is_blocked_without_opening_a_browser(store, bus, make_clip):
    def no_browser():
        raise AssertionError("the browser must not be opened")

    up = TikTokUploader(store=store, bus=bus, playwright_factory=no_browser)
    res = await up.upload(make_clip(write=False), "cap", dry_run=True)
    assert res.kind == "blocked" and "not found" in res.error
    res = await up.upload(make_clip(size=10), "cap", dry_run=True)
    assert res.kind == "blocked" and "too small" in res.error
    clip = make_clip()
    clip.renders = {}
    res = await up.upload(clip, "cap", dry_run=True)
    assert res.kind == "blocked"


async def test_dry_run_fills_the_form_and_never_clicks_post(uploader, browser, page, make_clip, store):
    clip = make_clip()
    res = await uploader.upload(clip, CAPTION, dry_run=True)
    assert res.kind == "dry_run", res.error
    assert page.url == tb.UPLOAD_URL
    assert page.files == [str(Path(clip.out_path).resolve())]
    assert caption_matches(CAPTION, page.editor_text)
    assert "c0_blur_fit" not in page.editor_text
    assert not page.post_keys & set(page.clicks)
    assert res.screenshot and Path(res.screenshot).exists()
    assert Path(res.screenshot).parent == store.get().data_dir / "upload_debug"
    assert browser.context.closed
    # headless and the profile come from the config, through open_context
    assert browser.launch_kwargs["headless"] is False
    assert browser.launch_kwargs["profile"].startswith(str(Path(store.get().tiktok.browser_profile_dir)))


async def test_hashtags_are_closed_with_a_space_not_doubled(uploader, page, make_clip):
    await uploader.upload(make_clip(), "go #a #b", dry_run=True)
    assert page.editor_text == "go #a #b "


async def test_real_run_clicks_post_once_and_confirms(uploader, browser, page, make_clip):
    res = await uploader.upload(make_clip(), CAPTION, dry_run=False)
    assert res.kind == "ok", res.error
    assert page.clicks.count(key(SELECTORS["post_button"][0])) == 1
    assert browser.context.closed


async def test_not_logged_in_is_blocked(uploader, browser, page, make_clip):
    browser.context.logged_in = False
    res = await uploader.upload(make_clip(), CAPTION, dry_run=True)
    assert res.kind == "blocked" and "login-tiktok" in res.error
    assert res.screenshot and Path(res.screenshot).exists()
    assert page.files == []
    assert browser.context.closed


async def test_redirect_to_login_is_blocked(uploader, browser, page, make_clip):
    def to_login():
        page.url = "https://www.tiktok.com/login?redirect_url=..."
    page.on_goto = to_login
    res = await uploader.upload(make_clip(), CAPTION, dry_run=True)
    assert res.kind == "blocked" and "login" in res.error


async def test_captcha_is_blocked(uploader, browser, page, make_clip):
    def captcha():
        page.default_after_attach()
        page.show("captcha", index=1)
    page.after_attach = captcha
    res = await uploader.upload(make_clip(), CAPTION, dry_run=True)
    assert res.kind == "blocked" and "CAPTCHA" in res.error
    assert res.screenshot
    assert browser.context.closed


async def test_page_that_does_not_match_is_blocked(uploader, browser, page, make_clip):
    page.on_goto = lambda: None  # no file input anywhere
    res = await uploader.upload(make_clip(), CAPTION, dry_run=True)
    assert res.kind == "blocked" and "file input" in res.error
    assert res.screenshot and Path(res.screenshot).exists()
    assert browser.context.closed


async def test_missing_caption_editor_is_blocked(uploader, page, make_clip):
    def no_editor():
        page.show("upload_done")
        page.show("post_button", state={})
    page.after_attach = no_editor
    res = await uploader.upload(make_clip(), CAPTION, dry_run=True)
    assert res.kind == "blocked" and "caption editor" in res.error


async def test_caption_that_does_not_stick_is_blocked(uploader, page, make_clip):
    async def ignore(text, delay=0):
        pass
    page.keyboard.type = ignore
    res = await uploader.upload(make_clip(), CAPTION, dry_run=True)
    assert res.kind == "blocked" and "caption" in res.error


async def test_processing_waits_for_the_post_button(uploader, page, clock, make_clip):
    def attach():
        page.editor_text = ""
        page.show("caption_editor")
        page.show("post_button", state={"aria": "true"})
    page.after_attach = attach
    polls = {"n": 0}

    def tick():
        polls["n"] += 1
        if polls["n"] == 10:
            page.show("post_button", state={"aria": "false"})
            page.show("upload_done")
    clock.on_sleep = tick
    res = await uploader.upload(make_clip(), CAPTION, dry_run=True)
    assert res.kind == "dry_run", res.error
    assert polls["n"] >= 10


async def test_processing_without_uploaded_mark_uses_grace(uploader, page, make_clip):
    def attach():
        page.show("caption_editor")
        page.show("post_button", state={})
    page.after_attach = attach
    res = await uploader.upload(make_clip(), CAPTION, dry_run=True)
    assert res.kind == "dry_run", res.error


async def test_processing_timeout_is_transient(uploader, page, make_clip):
    def attach():
        page.show("caption_editor")
        page.show("post_button", state={"disabled": True})
    page.after_attach = attach
    res = await uploader.upload(make_clip(), CAPTION, dry_run=True)
    assert res.kind == "transient" and "processed" in res.error


async def test_upload_error_is_transient(uploader, page, make_clip):
    page.after_attach = lambda: page.show("upload_error")
    res = await uploader.upload(make_clip(), CAPTION, dry_run=True)
    assert res.kind == "transient"


async def test_no_confirmation_after_post_is_blocked_not_transient(uploader, page, make_clip):
    page.post_shows_success = False
    res = await uploader.upload(make_clip(), CAPTION, dry_run=False)
    assert res.kind == "blocked" and "check TikTok" in res.error


async def test_error_after_post_is_blocked_not_transient(uploader, page, make_clip):
    page.post_shows_success = False

    class Boom(FakeLocator):  # the network drops right after Post
        async def count(self):
            if post in page.clicks:
                raise RuntimeError("net::ERR_CONNECTION_RESET")
            return await super().count()
    post = key(SELECTORS["post_button"][0])
    page.locator = lambda css: Boom(page, ("css", css))
    res = await uploader.upload(make_clip(), CAPTION, dry_run=False)
    assert post in page.clicks
    assert res.kind == "blocked" and "Check TikTok" in res.error


async def test_post_now_dialog_is_confirmed_only_in_real_runs(uploader, page, make_clip):
    page.post_shows_success = False
    original_click = FakeLocator.click

    async def click(self):
        await original_click(self)
        if self.k == key(SELECTORS["post_button"][0]):
            page.show("post_now_button")
        elif self.k == key(SELECTORS["post_now_button"][0]):
            page.show("success")
    FakeLocator.click = click
    try:
        res = await uploader.upload(make_clip(), CAPTION, dry_run=False)
    finally:
        FakeLocator.click = original_click
    assert res.kind == "ok", res.error
    assert key(SELECTORS["post_now_button"][0]) in page.clicks


async def test_publishing_click_refused_in_dry_run(uploader, page):
    run = tb._Run(None, Path("."))  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        await uploader._click_publishing(run, FakeLocator(page, key(SELECTORS["post_button"][0])), True)
    assert page.clicks == []


async def test_navigation_timeout_is_transient(uploader, page, make_clip):
    async def slow(url, **kw):
        raise PlaywrightTimeout("Timeout 60000ms exceeded")
    page.goto = slow
    res = await uploader.upload(make_clip(), CAPTION, dry_run=True)
    assert res.kind == "transient"


async def test_network_error_before_post_is_transient(uploader, page, make_clip):
    async def offline(url, **kw):
        raise RuntimeError("page.goto: net::ERR_NAME_NOT_RESOLVED at https://www.tiktok.com")
    page.goto = offline
    res = await uploader.upload(make_clip(), CAPTION, dry_run=True)
    assert res.kind == "transient"


async def test_browser_that_cannot_start_is_blocked(store, bus, page, make_clip):
    pw = FakePlaywright(FakeContext(page), launch_error=RuntimeError(
        "BrowserType.launch_persistent_context: chrome: error while loading shared libraries: libnspr4.so"))
    up = TikTokUploader(store=store, bus=bus, playwright_factory=lambda: pw)
    res = await up.upload(make_clip(), CAPTION, dry_run=True)
    assert res.kind == "blocked" and "system libraries" in res.error


async def test_cancel_closes_the_browser(store, bus, browser, page, make_clip):
    started = asyncio.Event()

    async def hang(url, **kw):
        started.set()
        await asyncio.sleep(3600)
    page.goto = hang
    up = TikTokUploader(store=store, bus=bus, playwright_factory=lambda: browser)
    task = asyncio.create_task(up.upload(make_clip(), CAPTION, dry_run=True))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert browser.context.closed
    assert getattr(browser, "exited", False)


# ---- dry run in "draft" mode: Save draft instead of stopping ----------------------------

def _with_draft_button(page, *, saves=True):
    base = page.after_attach

    def after():
        base()
        page.show("draft_button", state={"disabled": False})
    page.after_attach = after
    if saves:
        page.click_shows[key(SELECTORS["draft_button"][0])] = "draft_saved"


async def test_draft_mode_saves_a_draft_and_never_posts(uploader, page, make_clip, store):
    store.update({"tiktok": {"dry_run_mode": "draft"}})
    _with_draft_button(page)
    res = await uploader.upload(make_clip(), CAPTION, dry_run=True)
    assert res.kind == "dry_run", res.error
    assert page.clicks.count(key(SELECTORS["draft_button"][0])) == 1
    assert not page.post_keys & set(page.clicks)
    assert res.screenshot and "draft" in Path(res.screenshot).name


async def test_stop_mode_never_clicks_save_draft(uploader, page, make_clip):
    _with_draft_button(page)
    res = await uploader.upload(make_clip(), CAPTION, dry_run=True)
    assert res.kind == "dry_run", res.error
    assert key(SELECTORS["draft_button"][0]) not in page.clicks


async def test_draft_mode_without_draft_button_is_blocked(uploader, page, make_clip, store):
    store.update({"tiktok": {"dry_run_mode": "draft"}})
    res = await uploader.upload(make_clip(), CAPTION, dry_run=True)
    assert res.kind == "blocked" and "draft button" in res.error
    assert not page.post_keys & set(page.clicks)


async def test_draft_without_confirmation_is_blocked_not_transient(uploader, page, make_clip, store):
    store.update({"tiktok": {"dry_run_mode": "draft"}})
    _with_draft_button(page, saves=False)
    res = await uploader.upload(make_clip(), CAPTION, dry_run=True)
    assert res.kind == "blocked" and "check TikTok's drafts" in res.error


async def test_real_run_ignores_draft_mode(uploader, page, make_clip, store):
    store.update({"tiktok": {"dry_run_mode": "draft"}})
    _with_draft_button(page)
    res = await uploader.upload(make_clip(), CAPTION, dry_run=False)
    assert res.kind == "ok", res.error
    assert key(SELECTORS["draft_button"][0]) not in page.clicks


def test_dry_run_mode_is_validated(store):
    import pytest as _pytest
    from pydantic import ValidationError
    with _pytest.raises(ValidationError):
        store.update({"tiktok": {"dry_run_mode": "publish"}})
