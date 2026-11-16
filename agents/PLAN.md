# livewatcher — Implementation Plan (revision 2)

## Context
The goal is an app that watches several Twitch streamers at once. When chat activity spikes above its normal level, it saves the moment as a clip, turns it into a vertical 9:16 video, and uploads it to TikTok through browser automation. Every setting and secret lives in one config file that is easy to edit. A local web dashboard shows what the program is seeing: you can scroll through each stream's live video, view the chat-rate graph, read a log of detected peaks, and work through a clip review queue. It also has an on/off switch for auto-upload. The project folder holds only `spec.txt` and the `agents/` folder, so this starts from scratch.

**Decisions made:** Twitch only. TikTok upload through Playwright browser automation. Local web dashboard. Vertical output with a choice of layouts per streamer.

## How the work is split
- **One shared plan** (this file, copied to `agents/PLAN.md`) holds the design and the shared contracts.
- **Exactly 5 agents**, one per branch, each with its own brief in `agents/`. The number is fixed by this plan.
- **I do Phase 0 and Phase 2 myself** in the main session. No other agents are started.

| Agent | Branch | Brief | Builds |
|---|---|---|---|
| A | `feat/chat-detect` | `agents/A-chat-detect.md` | Twitch chat reader and peak detector |
| B | `feat/capture` | `agents/B-capture.md` | Live check, rolling video buffer, snapshots, clip cutting |
| C | `feat/render` | `agents/C-render.md` | Vertical 9:16 rendering and layout preview |
| D | `feat/uploader` | `agents/D-uploader.md` | TikTok browser uploader and upload queue |
| E | `feat/dashboard` | `agents/E-dashboard.md` | Web API and dashboard |

**The five briefs in `agents/` are out of date.** They were written before this revision. Step 1 of Phase 0 rewrites them to match the "Brief changes" section below.

## Environment facts (checked on this machine)
- Ubuntu 26.04 on WSL2, Python 3.14.4, git 2.53, Node. 8 cores, 7 GB RAM.
- **Missing:** ffmpeg, `uv`, `pip`, and Python's `venv` module. `sudo` needs your password, so I can't install system packages.
- Every Python package this plan uses lists support for Python 3.14 on PyPI (streamlink 8.6, playwright 1.65, pydantic 2.14, fastapi 0.163, tomlkit, pillow, websockets). An actual install is still a Phase 0 check.
- WSLg works (`DISPLAY=:0`), so a headed browser window can open for the TikTok login.
- The repo is on `/mnt/c`, where disk access is slow. The repo stays where it is. The Python environment, video buffers, clips and browser profile all go on the Linux filesystem.

## Setup gate (in this order, before any agent starts)
| # | Who | Step |
|---|---|---|
| 1 | **You** | In a WSL terminal: `sudo apt update && sudo apt install -y ffmpeg` |
| 2 | Me | Install `uv` to `~/.local/bin` with its official installer (no sudo needed) |
| 3 | Me | Phase 0 steps 1–8 below, which create the shared environment at `~/.venvs/livewatcher` |
| 4 | **You** | `sudo ~/.venvs/livewatcher/bin/playwright install-deps chromium` (system libraries for the browser) |
| 5 | Me | `playwright install chromium` |
| 6 | **You** | `livewatcher login-tiktok`: a browser window opens and you log in to TikTok once |
| 7 | Me | Rest of Phase 0, then start the 5 agents |

Step 6 can be skipped for now. If it is, agent D builds against selectors it can't check, and checking them moves to Phase 2 with you present.

## Stack
- **Backend:** Python 3.14, asyncio, FastAPI + uvicorn (REST + WebSocket), pydantic v2, SQLite through stdlib `sqlite3` (WAL mode, writes off the event loop), `streamlink`, `websockets`, `playwright`, `pillow`.
- **Frontend:** static HTML + vanilla JS, with Alpine.js, uPlot and hls.js vendored into `web/vendor/`. No build step.
- **Config:** `config.toml` (gitignored, holds secrets) plus `config.example.toml` (committed). Read with `tomllib`. The dashboard saves with **`tomlkit`**, which keeps your comments and formatting.
- **Tooling:** `uv`, `pytest`, `ruff`.
- **One shared environment for all worktrees:** `~/.venvs/livewatcher` holds only the third-party packages (`uv sync --no-install-project` with `UV_PROJECT_ENVIRONMENT` set). pytest is configured with `pythonpath = ["."]`, so each worktree runs its own copy of the code with `~/.venvs/livewatcher/bin/python -m pytest`. Agents never create environments.
- **Adding a dependency:** Phase 0 declares all of them. An agent that needs one more installs it into the shared environment, adds it to `pyproject.toml`, and says so in its report. I regenerate `uv.lock` when merging.

## How it works
```
Twitch IRC (anonymous) ──> ChatSource ──> chat-rate bins ──> PeakDetector ──> PeakEvent (start…end)
                                                                                 │
streamlink ─pipe─> ffmpeg (copy) ─> rolling buffer of ~6s segments               │
                        │              + live playlist + snapshots               ▼
                        │                      │               ClipService waits until the
                        │                      └────────────>  window is in the buffer, then cuts
                        │                                              │
                        └─> dashboard live video               Renderer (9:16 layout)
                                                                       │
                              auto_upload on ──> UploadQueue ──> Playwright TikTok uploader
                              auto_upload off ─> review queue in dashboard (approve / re-render / reject)
```
Components talk through an in-process async **EventBus** and record state in SQLite. The dashboard reads SQLite and gets live events over a WebSocket. The dashboard binds to `127.0.0.1` only, because it can change config and trigger uploads.

### Peak detection
- Count chat messages in 1 s bins and smooth them over 5 s. Messages starting with `!` and messages from accounts in `ignore_users` (bots) are not counted.
- The baseline is the rolling median of the last 10 minutes. Spread is MAD (median absolute deviation) with a floor, so a silent chat can't produce an infinite score.
- A peak **starts** when all of these hold: `rate > baseline * multiplier`, `z > z_threshold`, `rate > min_msgs_per_sec`, at least `min_unique_chatters` different people are chatting, the cooldown has passed, and there is at least 60 s of history.
- A peak **ends** when the rate falls back below `baseline * end_ratio`, or when it reaches `max_clip_s`.
- Thresholds are read from the config store on every bin, so changes made in the dashboard apply at once. Each streamer can override them.
- Peaks are ignored when the streamer is offline or the buffer doesn't yet cover the pre-roll.

### Clip window and timing
- Clip = `peak_start − preroll_s` (default 25) to `peak_end + postroll_s` (default 6), capped at `max_clip_s` (default 60).
- **Chat and video are on different clocks.** Chat arrives in real time. Our video copy arrives several seconds behind the broadcast, and viewers react a few seconds after seeing something. So the moment chat reacts to can sit earlier or later in our buffer than the chat timestamp.
- All timestamps in events and the database are chat wall-clock time (UTC). Branch B converts them to buffer positions using `capture.sync_offset_s`, a signed number (positive moves clips later). Agent B measures the real value on a live stream and sets the default. The dashboard shows each clip's peak marker so you can tune it.
- Low-latency mode is on by default to keep the offset small and steady.
- **The clip can't be cut when the peak fires, because the post-roll hasn't been recorded yet.** ClipService creates the clip with status `waiting`, asks the recorder `window_ready()`, and cuts only when the whole window is in closed segments.
- **Ad breaks:** Twitch inserts ads into the stream for viewers who aren't subscribed. Ad segments are filtered out, which leaves gaps in the buffer. A clip whose window overlaps a gap is dropped with a logged reason.

### Vertical layouts
Each streamer sets one of three layouts in config:
- `blur_fit`: **the default.** The full 16:9 frame over a blurred, zoomed copy of itself. With `fit_zoom = 1.0` nothing is cut off, but the picture fills only about a third of the screen height. Raising `fit_zoom` (for example 1.3) makes it bigger and trims the left and right edges.
- `crop`: a 9:16 crop positioned with `crop_x` (0–1). Fills the screen and cuts off the sides.
- `stacked`: two regions you set per streamer (for example, facecam on top and gameplay below). Best for streamers whose overlay stays the same.

In the review queue you can re-render any clip in another layout. Each layout's render is kept as its own file. The dashboard's layout tool draws the regions over a snapshot so you can set them by eye.

## Config sketch (`config.example.toml`)
```toml
[general]
data_dir = "~/.local/share/livewatcher"
auto_upload = false            # also switchable from the dashboard
dashboard_host = "127.0.0.1"
dashboard_port = 8787
max_concurrent_renders = 1
retention_days = 7             # delete old raw and rendered clips

[twitch]                       # optional: Helix keys for faster live checks and titles
client_id = ""
client_secret = ""
live_check_interval_s = 60

[detection]                    # global defaults; any [[streamers]] entry can override
bin_s = 1
smooth_s = 5
baseline_window_s = 600
multiplier = 3.0
z_threshold = 4.0
min_msgs_per_sec = 2.0
min_unique_chatters = 5
end_ratio = 1.5
cooldown_s = 90
preroll_s = 25
postroll_s = 6
max_clip_s = 60
ignore_users = ["nightbot", "streamelements", "moobot", "fossabot"]

[capture]
quality = "720p60,720p,best"
buffer_minutes = 3
snapshot_interval_s = 2
low_latency = true
filter_ads = true
sync_offset_s = 0.0            # signed; + moves clips later. Agent B sets the measured default.

[render]
default_layout = "blur_fit"
fit_zoom = 1.0
resolution = "1080x1920"
burn_title = true

[tiktok]
browser_profile_dir = "~/.local/share/livewatcher/browser_profile"
browser_channel = "chromium"   # or "chrome" if installed
cookies_file = ""              # fallback if TikTok blocks the in-app login
headless = false
dry_run = true                 # stop before clicking Post, save a screenshot
caption_template = "{streamer} on Twitch 🔴 #{streamer} #twitch #clips"
max_uploads_per_day = 6
min_minutes_between_uploads = 45

[[streamers]]
name = "somestreamer"
enabled = true
layout = "stacked"
stack_top = { x = 0.0, y = 0.6, w = 0.25, h = 0.4 }   # fractions of the frame
stack_bottom = { x = 0.15, y = 0.0, w = 0.7, h = 1.0 }
# detection overrides go here, e.g. multiplier = 2.5
```

## Repo layout and who owns each part
```
livewatcher/
  core/      config.py models.py db.py events.py interfaces.py fakes.py   ← Phase 0 (frozen)
  app.py     ClipService + pipeline wiring                                ← Phase 0, Phase 2
  cli.py     run | demo | replay | login-tiktok                           ← Phase 0 (frozen)
  upload/login.py                                                         ← Phase 0
  chat/      twitch_irc.py chatlog.py                                     ← A
  detect/    peak.py replay.py                                            ← A
  capture/   live_check.py recorder.py buffer.py snapshot.py cutter.py    ← B
  render/    layouts.py ffmpeg_graph.py renderer.py                       ← C
  upload/    tiktok_browser.py queue.py                                   ← D
  api/       server.py routes.py ws.py                                    ← E
web/         index.html app.js styles.css vendor/                         ← E
tests/       conftest.py (Phase 0) + one folder per branch
docs/        CONTRACTS.md                                                 ← Phase 0 (frozen)
agents/      PLAN.md + five briefs
```
**Rule:** each branch edits only the files it owns plus its own tests. `core/`, `app.py`, `cli.py`, `tests/conftest.py` and `docs/CONTRACTS.md` are frozen after Phase 0. A branch that needs a contract change describes it in its report and works around it locally.

## Phase 0: Foundation (serial, on `main`, done by me)
1. **Rewrite the five briefs** in `agents/` to match this revision (see "Brief changes").
2. `git init`, with `core.filemode false` and a `.gitattributes` forcing LF line endings (needed on `/mnt/c`). `.gitignore` covers `config.toml`, data folders and worktrees.
3. `pyproject.toml` with every dependency, the shared environment, and a real install on Python 3.14. If a package fails to install, pin the project to Python 3.13 through `uv`.
4. `core/config.py`: pydantic models for the sketch above and a **ConfigStore**. `get()` returns current settings with per-streamer overrides merged. `update(patch)` validates, saves with tomlkit, and publishes `config_changed`.
5. `core/models.py`:
   - `ChatMessage(streamer, ts, user_id, user, text)`
   - `RateSample(streamer, ts, rate, baseline, threshold, z, unique_chatters)`
   - `PeakEvent(id, streamer, ts_start, ts_peak, ts_end, score, source)` where `source` is `auto` or `manual`
   - `StreamStatus(streamer, live, title, recorder_state, buffer_start, buffer_end, last_error)`: this is what the dashboard's health list shows
   - `Clip(id, streamer, peak_id, window_start, window_end, raw_path, renders, active_layout, status, caption, created_at, error, debug_screenshot)` where `renders` maps layout name to file path
   - Clip `status`: `waiting → cutting → rendering → pending_review | queued → uploading → uploaded | dry_run_ok | failed | rejected | dropped`
   - `UploadResult(kind, error, screenshot)` where `kind` is `ok`, `dry_run`, `transient` or `blocked`
6. `core/db.py` (schema and data access) and `core/events.py` (EventBus topics: `chat`, `rate`, `peak`, `clip`, `stream_status`, `snapshot`, `config_changed`, `log`).
7. `core/interfaces.py`:
   - `ChatSource.run()`
   - `PeakDetector.run()`
   - `StreamRecorder`: `start`, `stop`, `status`, `window_ready(streamer, end_ts)`, `snapshot_path`, `playlist_path`
   - `ClipCutter.cut(streamer, start_ts, end_ts, clip_id) -> path`, which raises a named error if the window overlaps a gap
   - `Renderer`: `render(clip, layout) -> path`, `preview(snapshot_path, layout_cfg) -> jpeg bytes`
   - `Uploader.upload(clip, caption, dry_run) -> UploadResult`
8. `core/fakes.py`: a fake for every interface (replayed chat, a recorder looping a sample video, instant cutter and renderer, no-op uploader).
9. `app.py`: **ClipService and the whole pipeline, written against the interfaces and running on fakes.** It handles: peak → `waiting` → wait for `window_ready` → cut → render → review queue or upload queue. It also provides the actions the dashboard calls: `clip_now`, `approve`, `reject`, `rerender`, `set_caption`, `upload_now`. Phase 2 then only swaps fakes for real modules.
10. `cli.py` with all four subcommands registered. `replay` calls `livewatcher.detect.replay:main` (A writes it) and `login-tiktok` calls `livewatcher.upload.login:main`.
11. `upload/login.py`: opens a headed browser with the saved profile so you can log in. If `cookies_file` is set, it loads those cookies instead. It confirms the session is logged in.
12. `tests/conftest.py`: fixtures that generate a short sample video with ffmpeg and a chat log containing one planted spike. Nothing binary is committed.
13. `docs/CONTRACTS.md`: event payloads, every REST endpoint and the WebSocket message format.
14. **Exit checks:** `livewatcher demo` runs the whole pipeline on fakes and produces a clip. `pytest` and `ruff` pass on `main`. A throwaway worktree can run `pytest` using the shared environment.
15. Commit, then start the 5 agents, each with `isolation: "worktree"` and its brief.

## Phase 1: Five agents in parallel
Each agent reads its brief, this plan and `docs/CONTRACTS.md`. It works only in its own files, runs its tests and `ruff`, commits to its branch, and reports what it built, how it tested it, and any contract change it wants. The table at the top lists the branches.

### Brief changes (applied in Phase 0 step 1)
**All briefs**
- Use the shared environment. Don't create one.
- No edits to frozen files. The CLI subcommands are already registered, so nothing needs a hook.
- Keep test videos under 15 s. Five agents share 7 GB of RAM.
- Any ffmpeg or streamlink command in the brief is a starting point. Run it and confirm it does what the brief says before building on it.

**A — chat and detection**
- Add: `!` commands and `ignore_users` are not counted, MAD floor, `min_unique_chatters`, peak end detection (`end_ratio`, `max_clip_s`), and `ts_end` on `PeakEvent`.
- Thresholds come from ConfigStore on every bin.
- Space out channel JOINs to stay under Twitch's limit for anonymous connections (about 20 per 10 s).
- Add a check: connect to one busy live channel for 2 minutes and confirm messages arrive.

**B — capture**
- **First task, before anything else: prove the recorder command on a live stream.** Starting point: the `streamlink` command-line tool piped straight into ffmpeg's HLS output (stream copy, ~6 s segments, a rolling playlist, automatic deletion of old segments). No Python in the data path. Fallback if that doesn't hold up: ffmpeg's plain segment output plus the brief's own trimming code.
- Required behaviour, however it's done:
  - Wall-clock to buffer mapping stays accurate, including just after start-up, when streamlink delivers several seconds of video in a burst.
  - Ad segments are filtered and the gaps are recorded. `cut()` raises the gap error for a window that overlaps one.
  - Old or orphaned segments are cleaned up after a recorder restart.
  - `window_ready()` is true only when the whole window is in closed segments.
  - `cut()` takes chat-time and applies `sync_offset_s`.
- **Measure the real chat-to-video offset** on a live stream with low latency on, and report the value to use as the default.
- Snapshots: prefer a second output on the same ffmpeg process that decodes keyframes only. Fall back to grabbing a frame from the last closed segment.
- Expose `playlist_path` so the dashboard can play the buffer as live video.

**C — render**
- Add `fit_zoom` to `blur_fit`.
- Output goes to `renders[layout]`, one file per layout. Re-rendering never overwrites another layout's file.
- `preview()` is part of the `Renderer` interface.

**D — uploader**
- `login.py` is now Phase 0's. Remove it from this brief.
- Selector discovery uses the logged-in profile from setup step 6, always with `dry_run` on. **Never click Post.** If the profile isn't logged in, build from the best-known page structure, mark the selectors as unchecked in the report, and stop there.
- Return `UploadResult` kinds: `dry_run` sets the clip to `dry_run_ok`, `blocked` (CAPTCHA, logged out, unknown page) is never retried, `transient` is retried up to 2 times.
- Honour `browser_channel` and `cookies_file`.
- The "manual check" in the done criteria moves to Phase 2.

**E — dashboard**
- Calls `ClipService` actions and `ConfigStore.update()`. It doesn't import other branches' modules.
- The streams view plays the live buffer with hls.js when a playlist exists, and falls back to the refreshing snapshot when it doesn't.
- The health list is built from `StreamStatus`.
- Each clip shows its peak marker and window, to help tune `sync_offset_s`.
- Server binds to `dashboard_host`.

## Phase 2: Integration (serial, done by me)
- Merge **A → B → C → D → E**. The branches own separate files, so conflicts should be limited to `pyproject.toml`.
- Swap the fakes in `app.py` for the real modules. Apply contract changes the agents reported. Add `retention_days` cleanup and clean shutdown of the recorder processes.
- With you present: check the TikTok selectors against the real upload page, then run the end-to-end checks below.

## Risks and notes
- **Upload automation is fragile.** TikTok changes its site, can show CAPTCHAs, and sometimes blocks logins from automated browsers. Selectors kept in one place, dry-run on by default, low daily limits, stopping on any CAPTCHA, and the cookie-file fallback keep the damage small. Automated posting carries some risk to the account.
- **Rights.** Posting other streamers' footage generally needs their permission, and music in a stream can get a clip muted or removed. The caption template credits the streamer.
- **Not yet tested here.** ffmpeg isn't installed, so no ffmpeg or streamlink command in this plan has been run on this machine. Also unconfirmed: Playwright's system libraries on Ubuntu 26.04, live detection without Twitch API keys, and TikTok's current upload page. Each is the first task of the agent or phase that depends on it.
- **Disk.** About 70–150 MB per stream for a 3-minute buffer at 720p, plus clips until `retention_days` removes them.

## Verification
1. `pytest` and `ruff check` pass on every branch and after the merge.
2. **Detector:** `livewatcher replay` on the fixture chat log reports the planted spike and nothing else. Repeat on a log recorded from a real busy stream.
3. **Clip and render:** in `livewatcher demo`, "clip now" produces a raw clip and a 1080×1920 render in each layout (check with ffprobe and by watching them).
4. **Timing:** on a live stream, trigger a manual clip at a moment you can recognise and confirm the clip contains it. Adjust `sync_offset_s` if it doesn't.
5. **Uploader:** a `dry_run` upload reaches the filled-in Post screen (check the screenshot). Then one real upload, on purpose.
6. **End to end:** `livewatcher run` with 2 or 3 live streamers and auto-upload **off**. Live video and rate graphs update, peaks appear, and clips land in the review queue. Approve one and confirm it uploads. Then switch auto-upload on and confirm the next peak uploads on its own.
