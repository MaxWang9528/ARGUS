# livewatcher contracts

FROZEN after Phase 0. If you need a change, describe it in your report and work around it in your own files.

Phase 2 (integration) made the additions marked **(Phase 2)** below.

The code is the source of truth for shapes: `livewatcher/core/models.py`, `interfaces.py`, `config.py`, `events.py`, `db.py`. This file adds the conventions the code can't show.

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
| Chat | `livewatcher.chat.twitch_irc:TwitchChatSource` | `store, bus` |
| Detector | `livewatcher.detect.peak:ChatPeakDetector` | `store, bus` |
| Recorder | `livewatcher.capture.recorder:Recorder` | `store, bus` |
| Cutter | `livewatcher.capture.cutter:Cutter` | `store, bus, recorder` **(Phase 2)** the real `Recorder`: it also calls `recorder.buffer_index(name)` and `recorder.buffer_dir(name)`, which are not in the `StreamRecorder` protocol, and raises `WindowUnavailable` without them |
| Renderer | `livewatcher.render.renderer:FfmpegRenderer` | `store, bus` |
| Uploader | `livewatcher.upload.tiktok_browser:TikTokUploader` | `store, bus` |
| Upload queue | `livewatcher.upload.queue:ClipUploadQueue` | `store, bus, db, uploader` |
| Dashboard | `livewatcher.api.server:create_app(app) -> FastAPI` | the `App` object |
| CLI replay | `livewatcher.detect.replay:main(args) -> int` | `args.file`, `args.streamer`, `args.store` |

A class may take extra keyword arguments with defaults (useful for tests). `run()` coroutines run for the life of the app and must stop cleanly when cancelled, killing any child processes.

## Config
Read settings with `store.get()` each time you need them. Never cache a `Config` across awaits that matter, because the dashboard can change it.
- `cfg.enabled_streamers()`, `cfg.detection_for(name)`, `cfg.layout_for(name, layout=None)`, `cfg.data_dir`.
- Streamer names are compared in lower case. Use `name.lower()` in events, file names and the database.

## Files under `data_dir`
| Path | Owner | What |
|---|---|---|
| `livewatcher.db` | core | SQLite database |
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

**Logging:** use `logging.getLogger("livewatcher.<area>")`. The app forwards those records to the `log` topic. Tag a line with `extra={"streamer": name}`.

**Clips:** after changing a clip, always `db.save_clip(clip)` then `bus.publish(Topic.CLIP, clip)`.

## Game filter (Phase 2)
`StreamStatus.category` is the Twitch category from the last live check (streamlink or Helix `game_name`), `""` when unknown. When `general.clip_categories` is non-empty, `ClipService` ignores **auto** peaks unless the category matches one of them (case-insensitive); an unknown category counts as no match. Manual clips are never filtered.

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
