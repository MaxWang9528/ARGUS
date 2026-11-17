"""Upload one rendered clip to TikTok by driving the saved, logged-in browser profile.

Safety rules this module keeps:
- ``dry_run=True`` does everything except clicking Post. Every click that publishes
  goes through ``_click_publishing``, which refuses to run in a dry run. With
  ``tiktok.dry_run_mode = "draft"`` a dry run then clicks Save draft instead, so the
  clip lands in TikTok's drafts, private and unposted.
- It never logs in and never types credentials. A logged-out profile is ``blocked``.
- A CAPTCHA, a verification prompt or a page that doesn't look as expected is
  ``blocked`` (never retried). Network trouble before Post is ``transient``.
- Once Post has been clicked nothing is ``transient`` any more, so the queue can't
  retry and post the same clip twice.

Every selector is in ``SELECTORS``. They are the best-known structure of TikTok
Studio's upload page and have NOT been checked against the live page yet.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..core.config import ConfigStore
from ..core.events import EventBus
from ..core.models import Clip, UploadResult
from .login import UPLOAD_URL, is_logged_in, open_context

log = logging.getLogger("livewatcher.upload")

# --------------------------------------------------------------------------------------
# Selectors. One list of fallbacks per element, tried in order. Spec forms:
#   ("role", role, name)        get_by_role(role, name=name, exact=True)
#   ("role_re", role, pattern)  get_by_role(role, name=re.compile(pattern, re.IGNORECASE))
#   ("text", text)              get_by_text(text, exact=True)
#   ("text_re", pattern)        get_by_text(re.compile(pattern, re.IGNORECASE))
#   ("css", selector)           locator(selector)
# Role and text locators come first wherever the element has a usable one.
# ALL UNCHECKED against the live page (no browser could be started when written).
# --------------------------------------------------------------------------------------
Spec = tuple[str, ...]

SELECTORS: dict[str, list[Spec]] = {
    # The hidden <input type=file> behind the "Select video" drop zone. It has no
    # role or text, so CSS only. Matched when attached, not when visible.
    "file_input": [
        ("css", "input[type='file'][accept*='video']"),
        ("css", "input[type='file']"),
    ],
    # Shown once the file is uploaded and processed.
    "upload_done": [
        ("text_re", r"^\s*uploaded\b"),
        ("css", "[data-e2e='upload_status_container'] [class*='success']"),
    ],
    # Shown when the upload itself failed (worth a retry).
    "upload_error": [
        ("text_re", r"couldn.t upload|upload failed|failed to upload"),
        ("css", "[data-e2e='upload_status_container'] [class*='error']"),
    ],
    # The caption ("Description") editor, a Draft.js contenteditable. It has no
    # accessible name; a bare role="combobox" would also match the visibility
    # dropdown, so the role is pinned with contenteditable.
    "caption_editor": [
        ("css", "div[contenteditable='true'][role='combobox']"),
        ("css", ".public-DraftEditor-content[contenteditable='true']"),
        ("css", "[data-e2e='caption_container'] [contenteditable='true']"),
    ],
    # The Post button. Exact name, so it can't match "Posts" or "Post now".
    "post_button": [
        ("role", "button", "Post"),
        ("css", "button[data-e2e='post_video_button']"),
    ],
    # "Continue to post?" dialog TikTok may show after Post while its content check
    # is still running. Publishing click: real runs only.
    "post_now_button": [
        ("role", "button", "Post now"),
    ],
    # Save draft: used only by a dry run in "draft" mode. Never publishes.
    "draft_button": [
        ("role", "button", "Save draft"),
        ("css", "button[data-e2e='save_draft_button']"),
    ],
    # After Save draft: the confirmation (or TikTok moves to the content list).
    "draft_saved": [
        ("text_re", r"draft (saved|has been saved)|saved (to|as|in) (your )?drafts?"),
    ],
    # After Post: the confirmation.
    "success": [
        ("text_re", r"your video (has been|is being) (uploaded|posted|published)"),
        ("text_re", r"^\s*video (posted|published)\b"),
        ("role_re", "button", r"^upload another video$"),
    ],
    # CAPTCHA / verification. Any match stops the upload as blocked.
    "captcha": [
        ("text_re", r"verify to continue|drag the slider|drag the puzzle|select .* same shape"),
        ("css", "#captcha-verify-image"),
        ("css", "#captcha_container"),
        ("css", ".captcha_verify_container"),
        ("css", "iframe[src*='captcha']"),
    ],
    # Signs that the profile is logged out.
    "login_wall": [
        ("text_re", r"^\s*log in to tiktok\s*$"),
        ("css", "[data-e2e='top-login-button']"),
    ],
    # Harmless tips/popups that may cover the form. Only these buttons are ever
    # clicked to dismiss something; anything else covering the page is a mismatch.
    "dismiss": [
        ("role", "button", "Got it"),
        ("role", "button", "Not now"),
    ],
}

SPEC_KINDS = {"role": 3, "role_re": 3, "text": 2, "text_re": 2, "css": 2}

# TikTok's web caption limit is larger (up to 4000), 2200 is the long-standing safe one.
CAPTION_MAX_CHARS = 2200
VIDEO_SUFFIXES = (".mp4", ".mov", ".webm")
MIN_VIDEO_BYTES = 10_000            # anything smaller is a placeholder, not a video
MAX_VIDEO_BYTES = 2 * 1024 ** 3     # conservative web-upload limit
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))


# --------------------------------------------------------------------------------------
# Pure helpers (tested without a browser)
# --------------------------------------------------------------------------------------
def build_locator(root: Any, spec: Spec) -> Any:
    """Turn one selector spec into a Playwright locator on a Page or Frame."""
    if spec[0] not in SPEC_KINDS or len(spec) != SPEC_KINDS[spec[0]]:
        raise ValueError(f"bad selector spec {spec!r}")
    kind = spec[0]
    if kind == "role":
        return root.get_by_role(spec[1], name=spec[2], exact=True)
    if kind == "role_re":
        return root.get_by_role(spec[1], name=re.compile(spec[2], re.IGNORECASE))
    if kind == "text":
        return root.get_by_text(spec[1], exact=True)
    if kind == "text_re":
        return root.get_by_text(re.compile(spec[1], re.IGNORECASE))
    return root.locator(spec[1])


def describe_spec(spec: Spec) -> str:
    """Short human-readable form of a spec, for logs and error messages."""
    kind = spec[0]
    if kind in ("role", "role_re"):
        return f"{spec[1]} named {spec[2]!r}"
    if kind in ("text", "text_re"):
        return f"text {spec[1]!r}"
    return f"css {spec[1]!r}"


def normalize_caption(text: str, max_chars: int = CAPTION_MAX_CHARS) -> str:
    """The caption as it should end up in the editor: NFC, no control or zero-width
    characters, single spaces within lines, trimmed, cut at a word to ``max_chars``."""
    text = unicodedata.normalize("NFC", text or "").translate(_INVISIBLE)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    text = "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch)[0] != "C")
    lines = [re.sub(r" {2,}", " ", line).strip() for line in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if len(text) > max_chars:
        cut = text[:max_chars]
        space = cut.rfind(" ")
        text = (cut[:space] if space > max_chars * 0.8 else cut).rstrip()
    return text


def caption_tokens(text: str) -> list[str]:
    """Split a caption into pieces to type. ``#tag`` and ``@user`` are their own
    pieces, because typing them opens a suggestion list that has to be closed."""
    return [t for t in re.split(r"([#@][^\s#@]+)", text) if t]


def caption_matches(expected: str, actual: str) -> bool:
    """True when the editor's text is the caption we meant to type (whitespace and
    invisible characters ignored)."""
    def squash(s: str) -> str:
        s = unicodedata.normalize("NFC", s or "").translate(_INVISIBLE).replace("\xa0", " ")
        return re.sub(r"\s+", " ", s).strip()
    return squash(expected) == squash(actual)


def is_disabled(state: dict[str, Any]) -> bool:
    """Whether a button looks disabled, from the attributes ``_button_state`` reads."""
    if state.get("disabled"):
        return True
    if str(state.get("aria") or "").lower() == "true" or str(state.get("data") or "").lower() == "true":
        return True
    classes = str(state.get("cls") or "").lower().split()
    return any(c == "disabled" or c.endswith("-disabled") for c in classes)


def check_video_file(path: str | Path | None, *, min_bytes: int = MIN_VIDEO_BYTES,
                     max_bytes: int = MAX_VIDEO_BYTES) -> str | None:
    """Return why this file can't be uploaded, or None if it looks fine."""
    if not path:
        return "the clip has no rendered file"
    p = Path(path)
    if not p.is_file():
        return f"rendered file not found: {p}"
    if p.suffix.lower() not in VIDEO_SUFFIXES:
        return f"not a video file TikTok takes ({p.suffix or 'no extension'}): {p}"
    size = p.stat().st_size
    if size < min_bytes:
        return f"rendered file is too small to be a video ({size} bytes): {p}"
    if size > max_bytes:
        return f"rendered file is too big to upload ({size / 1024 ** 2:.0f} MB): {p}"
    return None


def classify_error(exc: BaseException, *, posted: bool = False) -> tuple[str, str]:
    """Map an unexpected exception to (UploadResult.kind, message)."""
    msg = f"{type(exc).__name__}: {str(exc).strip().splitlines()[0] if str(exc).strip() else ''}".rstrip(": ")
    if posted:
        return "blocked", f"Post or Save draft was clicked, then {msg}. Check TikTok before retrying."
    text = str(exc)
    if "error while loading shared libraries" in text or "Host system is missing dependencies" in text:
        return "blocked", ("the browser can't start: system libraries are missing "
                           "(run `sudo ~/.venvs/livewatcher/bin/python -m playwright install-deps chromium`)")
    if "Executable doesn't exist" in text:
        return "blocked", ("the browser isn't installed "
                           "(run `~/.venvs/livewatcher/bin/python -m playwright install chromium`)")
    if re.search(r"net::ERR_|ECONNRESET|ECONNREFUSED|ENOTFOUND|NS_ERROR_NET", text):
        return "transient", f"network error: {msg}"
    if isinstance(exc, (ConnectionError, OSError)) and not isinstance(exc, FileNotFoundError):
        return "transient", f"network error: {msg}"
    return "blocked", f"unexpected error: {msg}"


def _is_playwright_timeout(exc: BaseException) -> bool:
    return type(exc).__name__ == "TimeoutError" and type(exc).__module__.startswith("playwright")


# --------------------------------------------------------------------------------------
# The uploader
# --------------------------------------------------------------------------------------
class _Stop(Exception):
    """Ends a run with a given UploadResult kind."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.shot: str | None = None


class _Run:
    """State of one upload attempt."""

    def __init__(self, clip: Clip, debug_dir: Path) -> None:
        self.clip, self.debug_dir = clip, debug_dir
        self.page: Any = None
        self.posted = False


def _default_playwright() -> Any:
    from playwright.async_api import async_playwright

    return async_playwright()


class TikTokUploader:
    """Implements ``Uploader`` with Playwright and the profile from ``login-tiktok``."""

    def __init__(self, *, store: ConfigStore, bus: EventBus,
                 playwright_factory: Callable[[], Any] | None = None,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 monotonic: Callable[[], float] = time.monotonic,
                 rng: random.Random | None = None,
                 upload_url: str = UPLOAD_URL,
                 nav_timeout_s: float = 60.0,
                 element_timeout_s: float = 30.0,
                 processing_timeout_s: float = 600.0,
                 confirm_timeout_s: float = 120.0,
                 enabled_grace_s: float = 15.0,
                 poll_s: float = 0.5,
                 min_bytes: int = MIN_VIDEO_BYTES,
                 max_bytes: int = MAX_VIDEO_BYTES) -> None:
        self.store, self.bus = store, bus
        self._playwright_factory = playwright_factory or _default_playwright
        self._sleep, self._monotonic = sleep, monotonic
        self._rng = rng or random.Random()
        self.upload_url = upload_url
        self.nav_timeout_s, self.element_timeout_s = nav_timeout_s, element_timeout_s
        self.processing_timeout_s, self.confirm_timeout_s = processing_timeout_s, confirm_timeout_s
        self.enabled_grace_s = enabled_grace_s
        self.poll_s = poll_s
        self.min_bytes, self.max_bytes = min_bytes, max_bytes

    # ---- entry point -------------------------------------------------------------------
    async def upload(self, clip: Clip, caption: str, dry_run: bool) -> UploadResult:
        extra = {"streamer": clip.streamer}
        problem = check_video_file(clip.out_path, min_bytes=self.min_bytes, max_bytes=self.max_bytes)
        if problem:
            log.warning("clip %s not uploaded: %s", clip.id, problem, extra=extra)
            return UploadResult(kind="blocked", error=problem)

        cfg = self.store.get()
        run = _Run(clip, cfg.data_dir / "upload_debug")
        try:
            async with self._playwright_factory() as pw:
                context = await open_context(pw, cfg)
                try:
                    result = await self._drive(context, run, caption, dry_run)
                finally:
                    await self._close(context)
        except asyncio.CancelledError:
            raise
        except _Stop as stop:
            result = UploadResult(kind=stop.kind, error=str(stop), screenshot=stop.shot)
        except Exception as exc:  # noqa: BLE001 - must not raise for expected failures
            kind, msg = classify_error(exc, posted=run.posted)
            result = UploadResult(kind=kind, error=msg)
        level = logging.INFO if result.kind in ("ok", "dry_run") else logging.WARNING
        log.log(level, "clip %s upload: %s%s", clip.id, result.kind,
                f" ({result.error})" if result.error else "", extra=extra)
        return result

    async def _close(self, context: Any) -> None:
        try:
            await asyncio.shield(context.close())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("closing the browser failed", exc_info=True)

    # ---- the flow ----------------------------------------------------------------------
    async def _drive(self, context: Any, run: _Run, caption: str, dry_run: bool) -> UploadResult:
        try:
            return await self._steps(context, run, caption, dry_run)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Attach a screenshot to any failure while the page is still open.
            if isinstance(exc, _Stop):
                exc.shot = await self._screenshot(run, exc.kind)
                raise
            if _is_playwright_timeout(exc) and not run.posted:
                first = str(exc).strip().splitlines()[0] if str(exc).strip() else "timeout"
                stop = _Stop("blocked", f"the page didn't respond as expected ({first}); "
                                        "TikTok may have changed its upload page")
            else:
                stop = _Stop(*classify_error(exc, posted=run.posted))
            stop.shot = await self._screenshot(run, stop.kind)
            raise stop from exc

    async def _steps(self, context: Any, run: _Run, caption: str, dry_run: bool) -> UploadResult:
        page = context.pages[0] if context.pages else await context.new_page()
        run.page = page
        if not await is_logged_in(context):
            raise _Stop("blocked", "not logged in to TikTok: run `./lw login-tiktok` and log in")

        try:
            await page.goto(self.upload_url, wait_until="domcontentloaded",
                            timeout=self.nav_timeout_s * 1000)
        except Exception as exc:
            if _is_playwright_timeout(exc):
                raise _Stop("transient", f"the upload page didn't load in {self.nav_timeout_s:.0f} s") from exc
            raise
        await self._pause(1.5, 3.5)
        await self._check_blockers(page)

        # 1. attach the file
        root, file_input = await self._find_in_frames(page, "file_input", visible=False)
        await file_input.set_input_files(str(Path(run.clip.out_path).resolve()))
        log.info("clip %s: file attached, waiting for TikTok to process it", run.clip.id,
                 extra={"streamer": run.clip.streamer})
        await self._pause(2.0, 4.0)

        # 2. wait for processing
        post_button = await self._wait_processed(page, root)
        await self._dismiss_popups(root)

        # 3. caption
        editor = await self._find(page, root, "caption_editor")
        wanted = normalize_caption(caption)
        await self._type_caption(page, editor, wanted)
        actual = await editor.inner_text()
        if not caption_matches(wanted, actual):
            raise _Stop("blocked", f"the caption didn't come out as typed (got {actual[:80]!r})")
        await self._pause(1.0, 2.5)
        await self._check_blockers(page)
        if is_disabled(await self._button_state(post_button)):
            raise _Stop("blocked", "the Post button is disabled after filling in the form")

        # 4. visibility is left at its default. Post, or stop here in a dry run.
        if dry_run:
            shot = await self._screenshot(run, "dry_run")
            if self.store.get().tiktok.dry_run_mode != "draft":
                return UploadResult(kind="dry_run", screenshot=shot)
            await self._pause(1.0, 2.5)
            return await self._save_draft(run, root)
        await self._pause(1.0, 2.5)
        await self._click_publishing(run, post_button, dry_run)
        return await self._wait_confirmed(run, root, dry_run)

    # ---- steps ---------------------------------------------------------------------------
    async def _wait_processed(self, page: Any, root: Any) -> Any:
        """Wait until the upload is processed: the Post button is enabled and the
        "Uploaded" mark shows. If the mark is never found (its selector may be stale)
        an enabled Post button for ``enabled_grace_s`` is taken as done. Returns the
        Post button locator."""
        deadline = self._monotonic() + self.processing_timeout_s
        element_deadline = self._monotonic() + self.element_timeout_s
        post_button = None
        enabled_since: float | None = None
        while True:
            await self._check_blockers(page)
            if await self._present(root, "upload_error") is not None:
                raise _Stop("transient", "TikTok says the upload failed")
            if post_button is None:
                post_button = await self._present(root, "post_button")
            if post_button is not None:
                if is_disabled(await self._button_state(post_button)):
                    enabled_since = None
                else:
                    now = self._monotonic()
                    enabled_since = now if enabled_since is None else enabled_since
                    if await self._present(root, "upload_done") is not None:
                        return post_button
                    if now - enabled_since >= self.enabled_grace_s:
                        log.warning("no 'Uploaded' mark found; taking the enabled Post button as done")
                        return post_button
            elif self._monotonic() > element_deadline:
                raise _Stop("blocked", "no Post button after attaching the file; the upload page "
                                       "doesn't look as expected")
            if self._monotonic() > deadline:
                raise _Stop("transient", f"the video wasn't processed within {self.processing_timeout_s:.0f} s")
            await self._sleep(self.poll_s)

    async def _type_caption(self, page: Any, editor: Any, caption: str) -> None:
        """Replace whatever TikTok pre-filled (the file name) with ``caption``."""
        await editor.click()
        await self._pause(0.3, 0.8)
        await page.keyboard.press("ControlOrMeta+A")
        await page.keyboard.press("Backspace")
        await self._pause(0.3, 0.8)
        tokens = caption_tokens(caption)
        for i, token in enumerate(tokens):
            await page.keyboard.type(token, delay=self._rng.uniform(30, 90))
            if token[0] in "#@":
                # A suggestion list opens. Close it by typing a space (never Enter,
                # which would pick a suggestion), unless a space comes next anyway.
                await self._pause(0.8, 1.6)
                if i + 1 == len(tokens) or not tokens[i + 1].startswith(" "):
                    await page.keyboard.type(" ")
        await self._pause(0.5, 1.0)

    async def _wait_confirmed(self, run: _Run, root: Any, dry_run: bool) -> UploadResult:
        """After Post: wait for the confirmation. Nothing here may be transient."""
        page = run.page
        deadline = self._monotonic() + self.confirm_timeout_s
        post_now_clicked = False
        while True:
            await self._check_blockers(page)
            if await self._present(root, "success") is not None or "/tiktokstudio/content" in (page.url or ""):
                shot = await self._screenshot(run, "posted")
                return UploadResult(kind="ok", screenshot=shot)
            if not post_now_clicked:
                post_now = await self._present(root, "post_now_button")
                if post_now is not None:
                    await self._click_publishing(run, post_now, dry_run)
                    post_now_clicked = True
            if self._monotonic() > deadline:
                raise _Stop("blocked", "Post was clicked but no confirmation appeared; "
                                       "check TikTok before retrying")
            await self._sleep(self.poll_s)

    async def _save_draft(self, run: _Run, root: Any) -> UploadResult:
        """Dry run in "draft" mode: click Save draft and wait for the confirmation.
        After the click nothing is transient, so a retry can't make a second draft."""
        page = run.page
        button = await self._find(page, root, "draft_button")
        if is_disabled(await self._button_state(button)):
            raise _Stop("blocked", "the Save draft button is disabled after filling in the form")
        run.posted = True  # an error from here on must not be retried
        await button.click()
        deadline = self._monotonic() + self.confirm_timeout_s
        while True:
            await self._check_blockers(page)
            if (await self._present(root, "draft_saved") is not None
                    or "/tiktokstudio/content" in (page.url or "")):
                shot = await self._screenshot(run, "draft")
                log.info("clip %s: saved to TikTok drafts (dry run)", run.clip.id,
                         extra={"streamer": run.clip.streamer})
                return UploadResult(kind="dry_run", screenshot=shot)
            if self._monotonic() > deadline:
                raise _Stop("blocked", "Save draft was clicked but no confirmation appeared; "
                                       "check TikTok's drafts before retrying")
            await self._sleep(self.poll_s)

    async def _click_publishing(self, run: _Run, locator: Any, dry_run: bool) -> None:
        """The only place a publishing click happens."""
        if dry_run:
            raise RuntimeError("refusing to click a publishing button in a dry run")
        run.posted = True
        await locator.click()

    async def _dismiss_popups(self, root: Any) -> None:
        for _ in range(3):
            button = await self._present(root, "dismiss")
            if button is None:
                return
            await button.click()
            await self._pause(0.5, 1.2)

    async def _check_blockers(self, page: Any) -> None:
        if "/login" in (page.url or ""):
            raise _Stop("blocked", "TikTok sent the browser to the login page: run `./lw login-tiktok`")
        for frame in self._frames(page):
            if await self._present(frame, "captcha") is not None:
                raise _Stop("blocked", "TikTok is showing a CAPTCHA or verification; solve it in the "
                                       "browser (run with headless off), then approve the clip again")
            if await self._present(frame, "login_wall") is not None:
                raise _Stop("blocked", "TikTok shows a login prompt: run `./lw login-tiktok`")

    # ---- locator helpers ----------------------------------------------------------------
    @staticmethod
    def _frames(page: Any) -> list[Any]:
        frames = list(getattr(page, "frames", None) or [])
        return frames or [page]

    async def _present(self, root: Any, name: str, *, visible: bool = True) -> Any | None:
        """The first fallback of ``name`` that is on the page now, or None. No waiting."""
        for spec in SELECTORS[name]:
            loc = build_locator(root, spec).first
            try:
                if await loc.count() and (not visible or await loc.is_visible()):
                    return loc
            except Exception as exc:  # a detached frame or a stale node is just "not here"
                if type(exc).__module__.startswith("playwright"):
                    continue
                raise
        return None

    async def _find(self, page: Any, root: Any, name: str, *, visible: bool = True) -> Any:
        """Wait up to element_timeout_s for ``name``; blocked if it never shows."""
        deadline = self._monotonic() + self.element_timeout_s
        while True:
            loc = await self._present(root, name, visible=visible)
            if loc is not None:
                return loc
            await self._check_blockers(page)
            if self._monotonic() > deadline:
                tried = "; ".join(describe_spec(s) for s in SELECTORS[name])
                raise _Stop("blocked", f"couldn't find the {name.replace('_', ' ')} on the upload page "
                                       f"(tried {tried}); TikTok may have changed the page")
            await self._sleep(self.poll_s)

    async def _find_in_frames(self, page: Any, name: str, *, visible: bool = True) -> tuple[Any, Any]:
        """Like ``_find`` but looks in every frame (older pages put the form in an
        iframe). Returns (frame, locator)."""
        deadline = self._monotonic() + self.element_timeout_s
        while True:
            for frame in self._frames(page):
                loc = await self._present(frame, name, visible=visible)
                if loc is not None:
                    return frame, loc
            await self._check_blockers(page)
            if self._monotonic() > deadline:
                tried = "; ".join(describe_spec(s) for s in SELECTORS[name])
                raise _Stop("blocked", f"couldn't find the {name.replace('_', ' ')} on the upload page "
                                       f"(tried {tried}); TikTok may have changed the page")
            await self._sleep(self.poll_s)

    @staticmethod
    async def _button_state(locator: Any) -> dict[str, Any]:
        return await locator.evaluate(
            "e => ({disabled: e.disabled === true, aria: e.getAttribute('aria-disabled'),"
            " data: e.getAttribute('data-disabled'), cls: String(e.className || '')})")

    async def _pause(self, lo: float, hi: float) -> None:
        await self._sleep(self._rng.uniform(lo, hi))

    async def _screenshot(self, run: _Run, tag: str) -> str | None:
        if run.page is None:
            return None
        try:
            run.debug_dir.mkdir(parents=True, exist_ok=True)
            path = run.debug_dir / f"{run.clip.id}-{time.strftime('%Y%m%d-%H%M%S')}-{tag}.png"
            await run.page.screenshot(path=str(path), full_page=True)
            return str(path)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("screenshot failed", exc_info=True)
            return None
