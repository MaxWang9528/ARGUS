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
| `DELETE /api/clips/{id}` | `{files}`: deletes the clip's files and row; 409 while it is waiting, cutting, rendering, queued or uploading | `app.delete_clip` |
| `POST /api/clips/purge` body `{statuses?: [..], older_than_h?: 0}` | `{clips, files, kept_in_progress}`: deletes matching clips (no statuses = every status not in progress); in-progress clips are always kept; 422 for an unknown status | `app.purge_clips` |
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
- **Choosing the clip:** among `queued` clips, manual clips go first, then the highest priority, then the oldest. Priority = `clip.rank` (or `clip.score`, peak rate / baseline, for clips made before ranks existed) halved every `tiktok.rank_half_life_h` hours of age (0 = no decay). `rank` = score * sqrt(peak unique chatters) * a chat-text factor (0.25 + 1.5 * reaction share - answer share, within 0.25-1.5; see `argus/detect/peak.py`). With the speech check (below), the rank of an auto clip is also multiplied by its speech factor. A clip queued longer than `tiktok.max_queue_age_h` (0 = never) becomes `dropped` with "expired: no upload slot within N h".
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

## Speech check (`feat/stream-signals`)
- `argus.detect.speech`: `speech_score(words, duration) -> SpeechScore(score, hits)` (pure; exclamation phrases, swears, laughter, doubled words and "!" over the busiest 5 s; no words/s or loudness), `rank_factor(score, weight)`, `FasterWhisperTranscriber` (implements `interfaces.Transcriber`), `SpeechCheck`.
- `Transcriber.transcribe(path, start=0, duration=None) -> Transcript(text, words: [Word(w, t, e, p)], duration, model, wall_s)`: any file ffmpeg reads (a raw cut or a buffer `.m3u8`); word times from `start`. Runs in a worker thread; the model loads once, lazily.
- `app.speech` (`SpeechCheck`; the demo uses `fakes.FakeTranscriber`). `ClipService._process` runs `app.speech.check(clip)` on **auto** clips after cutting, alongside the render; manual clips and resumed renders skip it. It sets `clip.transcript`, `clip.speech_score` (None = not checked), `clip.speech_hits`, multiplies `clip.rank` by `max(0.5, min(2, 1 + speech.rank_weight * (min(score, 3) - 0.5)))`, and returns a drop reason when `speech.gate_threshold > 0` and the score is below it: the clip becomes `dropped` with that error, keeping its render.
- Never fails a clip: an exception or a missing `faster-whisper` is logged (`argus.speech`) and the clip goes on unchanged. At most `speech.max_concurrent` (1-2) transcriptions at a time.
- Config `[speech]`: `enabled` (true), `model` ("small.en"), `threads` (2), `max_concurrent` (1), `download_root` ("" = Hugging Face cache), `gate_threshold` (0 = off), `rank_weight` (0.25).
- Optional dependency: `pip install argus[speech]` (`faster-whisper`).

## Moment judge (`feat/stream-signals`)
- `argus.detect.judge`: `build_prompt(clip, transcript, chat_lines, streamer, category) -> (system, user)` (pure; `system` is the fixed `SYSTEM_PROMPT`, cached; clip data, transcript in `<transcript>` and chat in `<chat>` tags, marked untrusted, go in `user`), `chat_for_window` (chat log lines from `window_start` to 8 s past `window_end`, no `!commands` or `detection.ignore_users`, the 200 most recent), `Verdict(is_moment, score 0-10, reason, caption)`, `MomentJudge`.
- `app.judge` (`MomentJudge`). For **auto** clips `ClipService._checks` runs `app.speech.check` and then, unless speech dropped the clip, `app.judge.judge(clip, category)`, alongside the render. It calls `AsyncAnthropic().messages.parse(..., output_format=Verdict)` with `output_config={"effort": judge.effort}`, a 30 s timeout and the SDK's two retries. It sets `clip.judge_score` (None = not judged), `judge_moment`, `judge_reason`, `judge_caption`, multiplies `clip.rank` by `max(0.25, min(2, 1 + judge.rank_weight * (score / 10 - 0.5)))`, and with `use_caption` sets `clip.caption` to the judge's caption plus the `#tags` of `cfg.caption_for(...)`. When `gate_score > 0` and the score is below it, the clip is `dropped` with that reason, keeping its render.
- Never fails a clip: no package, no credential (one warning), a refusal, `max_tokens`, an API error or a timeout is logged (`argus.judge`) and the clip goes on unchanged. At most `judge.max_concurrent` requests at a time.
- Config `[judge]`: `enabled` (false), `model` ("claude-opus-5-5"), `effort` ("low"), `rank_weight` (1.0), `gate_score` (0 = off), `use_caption` (true), `max_concurrent` (2).
- Optional dependency: `pip install argus[judge]` (`anthropic`), plus `ANTHROPIC_API_KEY` or `ant auth login`.

## Stream discovery (`argus/discover/`)
- `ranking` (pure): `rank_streams(items, top_n, min_viewers, exclude, languages, category_ids) -> [DiscoveredStream]` (Helix `/streams` items, most viewers first, one per channel), `hold(last_in_top, top, now, keep_s, exclude)` (the `keep_minutes` hold), `watch_order(top, held)`.
- `helix.HelixClient(client_id, secret, http=, clock=)`: official Helix only. App access token (client credentials) cached until 60 s before expiry, renewed once on 401. `top_streams(game_ids, languages, limit, min_viewers)` pages `/helix/streams?type=live&first=100` with `after`; `game_ids(names)` -> `{lower name: (id, name)}` via `/helix/games?name=` (cached, unknown names warned once). A 429 raises `RateLimited(wait_s)` from `Ratelimit-Reset`. The secret is only sent in the token request body and never logged, put in an error or returned.
- `service.StreamDiscovery(store=, bus=)` is `app.discovery`; `run()` polls every `discovery.poll_s` (sooner when `discovery`, `twitch` or `streamers` is patched). Off, or without keys, it calls `store.set_discovered(None)` and reports `helix.SETUP_HINT`. A failed poll keeps the current list.
- **Seam:** `ConfigStore.set_discovered(names | None)` keeps a runtime-only list on the `Config` (`_discovered`, never saved, kept across `update()`), and publishes `Topic.STREAMERS_CHANGED` `{"discovered": [...]}` when the set changes. Then:
  - `cfg.enabled_streamers()` = enabled `[[streamers]]` + discovered (`mode = "merge"`), or just discovered (`"only"`, only while discovery runs). A configured entry with `enabled = false` is never added.
  - `cfg.streamer(name)` returns the `[[streamers]]` entry, else a default `Streamer(name)` for a discovered channel, so `detection_for`/`layout_for` give the defaults and per-streamer overrides still apply to configured ones.
  - `cfg.discovered_names()`, `cfg.is_discovered(name)`, `cfg.twitch_credentials()` (env `TWITCH_CLIENT_ID`/`TWITCH_CLIENT_SECRET` win over `[twitch]`; the live checker uses it too).
  - Chat and the recorder also wake on `streamers_changed` (chat joins/parts through its join limiter). The WebSocket relays it; the page reloads `/api/state`.
  - `LiveChecker.hints(name) -> LiveInfo | None` (set by the app to `discovery.live_hint`): channels in the last poll (younger than `poll_s + 30` s) aren't checked again.
- `GET /api/state`: each streamer row gains `discovered` and `configured` (discovered-only channels are listed after the configured ones; `enabled` is false for a configured channel `mode = "only"` doesn't watch), and the body gains `discovery: {enabled, active, has_credentials, setup, mode, error, last_poll, streams: [{name, rank, in_top, viewers, category, held_until}]}`.
- CLI: `./lw discover [--category NAME]... [--top N]` -> `argus.discover.cli:main(args)` (exit 2 without keys).
- Config `[discovery]`: `enabled` (false), `categories` ([] = all), `languages` (["en"]), `top_n` (12), `min_viewers` (0), `poll_s` (90, 30-3600), `exclude`, `keep_minutes` (10), `mode` ("merge" | "only").
