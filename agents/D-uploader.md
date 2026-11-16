# Agent D — `feat/uploader`: Playwright TikTok uploader

Read first, in this order: `docs/CONTRACTS.md`, `livewatcher/core/` (models, interfaces, config, db, fakes), `livewatcher/upload/login.py`, then "Risks and notes" in `agents/PLAN.md`.

## Goal
Upload rendered clips to TikTok by driving a logged-in Chromium browser, safely: rate limits, a dry-run mode, and stopping instead of retrying when something unexpected happens.

## Files you own (edit only these)
- `livewatcher/upload/tiktok_browser.py`, `livewatcher/upload/queue.py`
- `tests/upload/`

Everything else is frozen, including `livewatcher/upload/login.py` (reuse its `open_context`, `is_logged_in` and `UPLOAD_URL`). If you need a change there, describe it in your report and work around it.

## Hard rules
- **Never click Post, and never publish anything to a real TikTok account.** Every browser run you do uses `dry_run=True`.
- **Don't log in, and don't enter credentials.** Only the user does that.
- Use the shared environment (`~/.venvs/livewatcher/bin/python`). Don't create one.
- Commit to your branch when done. Don't push, and don't merge.

## What may not be available
The Chromium browser, its system libraries, and a logged-in TikTok profile each need a step from the user and may not be done yet. Check, in order:
1. Does `~/.venvs/livewatcher/bin/python -m playwright install chromium` work, and does a headless Chromium launch? (This step needs no sudo. If the launch fails on missing system libraries, stop here.)
2. Is the profile at `tiktok.browser_profile_dir` logged in (`is_logged_in`)?

If both hold, inspect the real upload page and check your selectors against it, in dry run. If either fails, build from the best-known structure of the page, mark every selector as **unchecked** in your report, and don't keep trying.

## Tasks
1. **`TikTokUploader(store, bus)`** (`tiktok_browser.py`), implementing `Uploader`. It must never raise for an expected failure; it returns an `UploadResult`.
   - Keep every selector in one `SELECTORS` dict at the top of the file, preferring role and text locators, with a list of fallbacks per element.
   - Steps: open the persistent profile (`open_context`) → go to `UPLOAD_URL` → attach the file with `set_input_files` → wait until processing finishes → replace the caption with the given text → leave visibility at its default → click Post → wait for the success confirmation. Random human-like pauses between steps.
   - `dry_run=True`: do everything except clicking Post, save a screenshot, return `kind="dry_run"`.
   - Not logged in, a CAPTCHA or verification prompt, or a page that doesn't match: save a screenshot to `data_dir/upload_debug/` and return `kind="blocked"` with a clear message.
   - Network errors and timeouts: return `kind="transient"`.
   - Honour `tiktok.headless`, `browser_channel` and `cookies_file` (all through `open_context`). Always close the browser, including on cancel.
   - Check the file exists and is a sane size before opening the browser.
2. **`ClipUploadQueue(store, bus, db, uploader)`** (`queue.py`), implementing `UploadQueue`:
   - Take clips with status `queued`, oldest first, one at a time. Upload `clip.out_path` with `clip.caption` and `tiktok.dry_run` as read at that moment.
   - Before each real upload enforce `max_uploads_per_day` (successful uploads in the last 24 h) and `min_minutes_between_uploads` (since the last success), using `db.count_uploads_since` and `db.last_upload_ts`. A clip that has to wait stays `queued`. Dry runs don't count and aren't limited.
   - Map the result as in `docs/CONTRACTS.md`: `ok → uploaded`, `dry_run → dry_run_ok`, `blocked → failed` with no retry, `transient` → up to 2 retries with a pause, then `failed`.
   - After a `blocked` result, pause the whole queue (it can't succeed until the user acts) and log why. Resume when a clip is next approved by hand or the config changes.
   - Record every attempt with `db.add_upload`. Save and publish the clip on every status change. Set `clip.error` and `clip.debug_screenshot`.
   - Take an injectable clock and sleep so tests don't wait in real time.

## Done criteria
- Queue tests with `FakeUploader` and scripted results: the daily limit and the spacing are respected, dry runs bypass them, `transient` is retried twice then fails, `blocked` is never retried and pauses the queue, attempts are recorded.
- Uploader tests that don't need TikTok: a missing file returns `blocked` without opening a browser; the caption and selector helpers are tested as pure functions.
- If the browser and login were available: a dry run reached the filled-in Post screen. Give the screenshot path.
- `pytest tests/upload` passes and `ruff check .` is clean.

## Report back
Which of the three (browser, system libraries, login) were available, the selectors and whether each was checked against the live page, screenshot paths, and any contract change you need (described, not made).
