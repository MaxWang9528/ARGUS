# ARGUS contracts

FROZEN after Phase 0. If you need a change, describe it in your report and work around it in your own files.

Phase 2 (integration) made the additions marked **(Phase 2)** below.

The code is the source of truth for shapes: `argus/core/models.py`, `interfaces.py`, `config.py`, `events.py`, `db.py`. This file adds the conventions the code can't show.

## Running things
- Python: `~/.venvs/livewatcher/bin/python` (shared, already has every dependency). Never create a venv or run `uv sync`.
- Tests: `~/.venvs/livewatcher/bin/python -m pytest tests/<your area>` from the repo (or worktree) root.
- Lint: `~/.venvs/livewatcher/bin/ruff check .`
- App: `./lw demo` (fakes only) or `./lw run`.
- Need one more package: `~/.local/bin/uv pip install --python ~/.venvs/livewatcher/bin/python <pkg>`, add it to `pyproject.toml` `dependencies`, and say so in your report. Don't touch `uv.lock`.

## Time
Every timestamp is chat wall-clock time: UTC epoch seconds (`time.time()`). Only branch B converts to buffer positions.

## Construction (how `app.py` builds your class)
`app.py` imports these exact names. Keyword arguments only.

| Component | Module : class | Arguments |
|---|---|---|
| Chat | `argus.chat.twitch_irc:TwitchChatSource` | `store, bus` |
| Detector | `argus.detect.peak:ChatPeakDetector` | `store, bus` |
| Recorder | `argus.capture.recorder:Recorder` | `store, bus` |
| Cutter | `argus.capture.cutter:Cutter` | `store, bus, recorder` **(Phase 2)** the real `Recorder`: it also calls `recorder.buffer_index(name)` and `recorder.buffer_dir(name)`, which are not in the `StreamRecorder` protocol, and raises `WindowUnavailable` without them |
| Renderer | `argus.render.renderer:FfmpegRenderer` | `store, bus` |
| Uploader | `argus.upload.tiktok_browser:TikTokUploader` | `store, bus` |
| Upload queue | `argus.upload.queue:ClipUploadQueue` | `store, bus, db, uploader` |
| Dashboard | `argus.api.server:create_app(app) -> FastAPI` | the `App` object |
| CLI replay | `argus.detect.replay:main(args) -> int` | `args.file`, `args.streamer`, `args.store` |

A class may take extra keyword arguments with defaults (useful for tests). `run()` coroutines run for the life of the app and must stop cleanly when cancelled, killing any child processes.

## Config
Read settings with `store.get()` each time you need them. Never cache a `Config` across awaits that matter, because the dashboard can change it.
- `cfg.enabled_streamers()`, `cfg.detection_for(name)`, `cfg.layout_for(name, layout=None)`, `cfg.data_dir`.
- Streamer names are compared in lower case. Use `name.lower()` in events, file names and the database.

## Files under `data_dir`
| Path | Owner | What |
|---|---|---|
| `argus.db` | core | SQLite database |
| `buffer/<streamer>/` | B | rolling segments and `live.m3u8` |
| `snapshots/<streamer>.jpg` | B | latest frame, replaced atomically |
| `clips/tmp/<clip_id>/` | B | cut scratch (hard links into `buffer/`, so it must share its filesystem) |
| `clips/raw/<clip_id>.mp4` | B | cut clip, source aspect ratio |
| `clips/out/<clip_id>_<layout>.mp4` | C | 1080x1920 render |
| `clips/rates/<clip_id>.json` | core | chat rate over the clip window |
| `chatlogs/<streamer>/<YYYY-MM-DD>.jsonl` | A | one `ChatMessage` JSON per line |
| `upload_debug/` | D | screenshots |

## Events (`Topic` -> payload)
| Topic | Payload | Publisher |
|---|---|---|
| `chat` | `ChatMessage` | A |
| `rate` | `RateSample`, once per bin per live-or-not streamer | A |
| `peak` | `PeakEvent`, published once when the peak ends | A (and `clip_now`, with `source="manual"`) |
| `clip` | `Clip`, on every status change | core, D |
| `stream_status` | `StreamStatus`, on change and at least every 30 s | B |
| `snapshot` | `{"streamer", "ts"}` | B |
| `config_changed` | `{"patch"}` (the WebSocket blanks `twitch.client_secret` in it) **(Phase 2)** | core |
| `log` | `LogLine` | core (see below) |

**Logging:** use `logging.getLogger("argus.<area>")`. The app forwards those records to the `log` topic. Tag a line with `extra={"streamer": name}`.

**Clips:** after changing a clip, always `db.save_clip(clip)` then `bus.publish(Topic.CLIP, clip)`.

## Auto-clip and captions
`StreamStatus.category` is the Twitch category from the last live check, `""` when unknown. It does not filter clips. `general.auto_clip = false` makes `ClipService` ignore **auto** peaks; manual clips always work. A new clip's caption is `cfg.caption_for(streamer, category)`: the first `tiktok.category_captions` entry whose category matches (case-insensitive), else `tiktok.caption_template`, formatted with `{streamer}` and `{category}`. The old key `general.clip_categories` still loads, as `priority_categories`.

## Retention (Phase 2)
Every hour the app deletes the raw, rendered and screenshot files of clips older than `general.retention_days` (0 keeps everything), except clips still in progress. The clip row stays, with its file paths cleared. `clips/tmp/` folders older than a day go too. Chat logs are kept.

## Clip life cycle
`waiting -> cutting -> rendering -> pending_review | queued -> uploading -> uploaded | dry_run_ok | failed`, plus `rejected` and `dropped`.
- `ClipService` (core) owns everything up to `pending_review`/`queued`.
- The upload queue (D) owns `queued -> uploading -> uploaded | dry_run_ok | failed`. It uploads `clip.out_path` (the render for `clip.active_layout`) with `clip.caption`.
- `UploadResult.kind`: `ok -> uploaded`, `dry_run -> dry_run_ok`, `blocked -> failed` (never retried), `transient` -> retried up to 2 times, then `failed`.

## Dashboard API (branch E implements, all under `/api`)
JSON bodies are the pydantic models dumped with `model_dump(mode="json")`. Errors: `404` for unknown ids or streamers, `409` for `ClipError`, `422` for a bad config patch, `501` when a feature has no data yet.

| Method and path | Does | Backed by |
|---|---|---|
| `GET /api/state` | `{auto_upload, dry_run, components, streamers:[{name, enabled, status: StreamStatus, has_playlist, has_snapshot}]}` (`has_snapshot` **Phase 2**) | `app.store`, `app.recorder.status`, `app.components` |
| `GET /api/streamers/{name}/rates` | `RateSample[]`, up to 15 min | `app.rate_history` |
| `GET /api/streamers/{name}/snapshot.jpg` | latest frame (404 if none), `Cache-Control: no-store` | `app.recorder.snapshot_path` |
| `GET /api/streamers/{name}/live/{file}` | `live.m3u8` (always the recorder's playlist, whatever its file name) and its segments (404 if no playlist) | `app.recorder.playlist_path` (serve only files in that folder) |
| `GET /api/streamers/{name}/layout-preview.jpg?layout=&crop_x=&fit_zoom=&stack_top=x,y,w,h&stack_bottom=x,y,w,h&stack_split=` | snapshot with regions drawn; query values override the config | `app.renderer.preview` |
| `POST /api/streamers/{name}/clip-now` | `Clip` | `app.clips.clip_now` |
| `GET /api/peaks?streamer=&since=&limit=` (`limit` **Phase 2**, default 200, max 2000) | `PeakEvent[]` | `app.db.list_peaks` |
| `GET /api/clips?status=a,b&streamer=&limit=` | `Clip[]`, newest first | `app.db.list_clips` |
| `GET /api/clips/{id}` | `Clip` | `app.db.get_clip` |
| `GET /api/clips/{id}/video?layout=` | rendered mp4 (default: active layout), supports Range | `clip.renders` |
| `GET /api/clips/{id}/rates` **(Phase 2)** | `RateSample[]` over the clip window ±5 s, oldest first; video time t = chat time `window_start + t` | `app.clip_rates` (saved to `clips/rates/<id>.json` when the clip is ready) |
| `GET /api/clips/{id}/raw` | raw mp4 | `clip.raw_path` |
| `GET /api/clips/{id}/screenshot` | upload debug screenshot | `clip.debug_screenshot` |
| `POST /api/clips/{id}/approve` | `Clip` | `app.clips.approve` |
| `POST /api/clips/{id}/reject` | `Clip` | `app.clips.reject` |
| `POST /api/clips/{id}/rerender` body `{layout}` | `Clip` (returns when the render is done) | `app.clips.rerender` |
| `PATCH /api/clips/{id}` body `{caption}` | `Clip` | `app.clips.set_caption` |
| `GET /api/config` | the whole config, with `twitch.client_secret` blanked | `app.store.get` |
| `PATCH /api/config` body = nested patch | new config | `app.store.update` (a blank `client_secret` in the patch is dropped) |
| `GET /api/logs` | `LogLine[]`, last 500 | `app.recent_logs` |
| `WS /api/ws` | server sends `{"topic": "<topic>", "data": {...}}` for `rate`, `peak`, `clip`, `stream_status`, `snapshot`, `config_changed`, `log` (never `chat`) | `app.bus.subscribe` |

The static dashboard is served from `web/` at `/`.

## Popularity, max streamers, multiple TikTok accounts (Phase 3)

### Streamers (branch `feat/popular`)
- `StreamStatus.viewers` (int or None) comes from every live check: Helix `viewer_count` when `[twitch]` keys are set, otherwise looked up without keys. Unknown is `None`, and sorts last.
- `general.max_streamers` (0 = no limit): at each live check the recorder ranks enabled live streamers as **(category in `priority_categories` first, then viewers, highest first)** and records only the top N. The others get `StreamStatus.watched = False`, no recorder process and no buffer. Their chat is still read and their rate is still published.
  - To avoid churn, a watched streamer is only displaced by one with at least 20% more viewers.
  - `ClipService` ignores peaks from unwatched streamers.
- `config.example.toml` lists many streamers, the most popular first.

### Accounts (branch `feat/accounts`)
- `cfg.tiktok_accounts()` gives the accounts with `browser_profile_dir` filled in. An empty `tiktok.accounts` means one implicit account, `default`, that uses the old `[tiktok]` profile, cookies and limits.
- `Uploader.upload(clip, caption, dry_run, account=None)`: `account` picks the profile.
- **Choosing the clip:** among `queued` clips, manual clips go first, then the highest `clip.score` (peak rate / baseline), then the oldest. A clip queued longer than `tiktok.max_queue_age_h` (0 = never) becomes `dropped` with "expired: no upload slot within N h".
- **Choosing the account:**
  - Candidates are the enabled accounts with a free slot: fewer than `max_uploads_per_day` real uploads in the last 24 h, and at least `min_minutes_between_uploads` since that account's last one.
  - With `tiktok.cycle_accounts` on, it takes the next candidate after the account used last (round robin); with it off, the first candidate in list order.
  - With no candidate the clip stays `queued` and the queue reports why.
- Dry runs don't count toward limits and aren't limited, but they still pick an account the same way, so each account's login can be tested.
- `clip.account` is set when the upload starts. The `uploads` table gains an `account` column, and old rows count as `default`.
- Accounts are added, edited and removed through `PATCH /api/config` with the whole `tiktok.accounts` list. Removing an account leaves its profile folder on disk.

| Method and path | Does |
|---|---|
| `GET /api/accounts` | `[{name, enabled, profile_dir, logged_in: bool|null, login_open: bool, uploads_24h, max_uploads_per_day, min_minutes_between_uploads, last_upload_ts: float|null, next_free_ts: float|null}]`. `logged_in` is the last known check, `null` if never checked. |
| `POST /api/accounts/{name}/login` | Opens a visible browser window on that account's profile at TikTok's login page; 202 `{login_open: true}`, 409 if one is already open, 404 if the account is unknown. When the window is closed the app checks the login again. |
| `POST /api/accounts/{name}/check` | Headless login check: `{logged_in: bool}` |
| `GET /api/upload` | `{cycle: bool, next_account: str|null, queued: int, waiting_reason: str|null, paused_reason: str|null}` |
| CLI | `./lw login-tiktok [--account NAME]` (default: the first account) |
