# livewatcher

livewatcher watches Twitch streams live and works out when chat reacts to something. It then clips that moment from a rolling video buffer, renders a 9:16 vertical version and uploads it to TikTok. A local web dashboard shows the streams, the chat-rate graphs and a review queue for the clips.

It is currently set up for **GTA 6** (launching November 19, 2026): GTA VI streams are recorded first and get a GTA 6 caption. Peaks in any game are clipped.

```
Twitch chat (IRC) ──> chat rate per second ──> peak detector ──> peak (start … end)
                                                                   │
Twitch video (streamlink) ──> rolling 3-min buffer of 2 s segments │
        │                                    │                     ▼
        └─> live video in the dashboard      └──────> cut the window once it is recorded
                                                                   │
                                                     render 1080×1920 (blur_fit / crop / stacked)
                                                                   │
                                   auto-upload on ──> upload queue ──> TikTok (Playwright browser)
                                   auto-upload off ─> review queue in the dashboard
```

---

## Quick start

All commands run **inside WSL/Linux**, not in PowerShell. `lw` is a bash script, and Windows will ask which app to open it with if you double-click it.

```bash
cd livewatcher                       # the repo folder
cp config.example.toml config.toml   # then edit the [[streamers]] list
./lw run                             # dashboard at http://localhost:8787
```

Other commands:

| Command | What it does |
|---|---|
| `./lw run` | Watches the streamers in `config.toml`. Ctrl+C stops everything, including the recorder processes. |
| `./lw demo` | Starts the dashboard with fake streams, chat and uploads. Nothing touches Twitch or TikTok. |
| `./lw replay <chatlog.jsonl> [--streamer name]` | Runs the peak detector over a recorded chat log and prints the peaks it finds. Use it to tune the thresholds. |
| `./lw login-tiktok [--account NAME]` | Opens a browser window so you can log in to a TikTok account once. The login is saved in that account's browser profile. The Accounts page has a Log in button that does the same. |
| `./lw --config other.toml run` | Uses a different config file. |

### One-time setup

| What | How |
|---|---|
| Python environment | `~/.venvs/livewatcher` (Python 3.14). `lw` uses it automatically. Rebuild it with `UV_PROJECT_ENVIRONMENT=~/.venvs/livewatcher uv sync`. |
| ffmpeg | `sudo apt install ffmpeg`. Use the apt build: static builds without `drawtext` can't burn in titles. |
| Browser libraries | `sudo ~/.venvs/livewatcher/bin/playwright install-deps chromium`, then `playwright install chromium`. |
| TikTok login | `./lw login-tiktok` |

`sudo` has to run in a real terminal. It can't ask for your password through Claude Code's `!` prefix.

---

## How it works

### 1. Chat → chat rate (`livewatcher/chat/`, `livewatcher/detect/`)

- One anonymous IRC connection (`justinfanNNNNN`) to `wss://irc-ws.chat.twitch.tv` joins every enabled channel.
  - Joins are paced at 18 per 10 s.
  - It reconnects with backoff from 1 to 60 s, and also reconnects after 6 minutes of silence or a server `RECONNECT`.
- Every message is also appended to `chatlogs/<streamer>/<YYYY-MM-DD>.jsonl`, which `replay` can read.
- Messages are counted in **1 s bins** (`bin_s`).
  - Commands (`!…`) and bots (`ignore_users`) don't count.
  - One account counts for at most 2 messages per second, so a single spammer can't make a peak.
- The rate is the trailing mean over **5 s** (`smooth_s`), in messages per second.

### 2. Peak detection (`livewatcher/detect/peak.py`)

- **Baseline:** the rolling **median** of the rate over the last **10 minutes** (`baseline_window_s`), leaving out bins that were inside a peak.
  - The spread is the MAD × 1.4826, never below 0.25 msg/s, so a quiet chat can't produce an infinite score.
  - If the median of the last 2 minutes is higher, it becomes the baseline. That way a chat that permanently gets busier isn't treated as one long peak.
- **Score:** `z = (rate − baseline) / spread`.
- A peak **starts** when all of these hold:
  - `rate > baseline × multiplier` (default 3.0)
  - `z > z_threshold` (default 4.0)
  - `rate > min_msgs_per_sec` (default 2.0)
  - at least `min_unique_chatters` (default 5) different people are chatting
  - the `cooldown_s` (90 s) since the last peak has passed
  - there is at least 60 s of chat history (the warm-up, which restarts after 2 minutes of total silence)
- A peak **ends** when the rate falls below `baseline × end_ratio` (1.5), or when the clip would exceed `max_clip_s` (60 s).
- Thresholds are re-read on every bin, so changes from the dashboard's Debug sliders apply immediately. Any `[detection]` key can be overridden per streamer.
- The detector publishes one `RateSample` per bin, which feeds the graphs, and one `PeakEvent` when a peak ends.

### 3. Auto-clip and game captions

- **Auto-clip** (`general.auto_clip`, a switch in the dashboard header): on, every peak the detector finds is clipped. Off, peaks are still detected and logged but not clipped. **Clip now** always works.
- The live check reports each stream's **Twitch category** (the game). It doesn't filter anything; it chooses the caption:
  - `[[tiktok.category_captions]]` gives a caption for one category (exact Twitch name, ignoring case), e.g. "Grand Theft Auto VI" → `{streamer} playing GTA 6 🔴 #gta6 …`.
  - Any other category uses `tiktok.caption_template`, where `{category}` becomes the game: "xqc 🔴 Valorant on Twitch #xqc …".
  - Both are editable in Settings.
- `general.priority_categories` (GTA VI in the example) only decides which streams are recorded first when `max_streamers` limits recording.
- Each card shows the category, and the log notes each change ("xqc is playing Grand Theft Auto VI").

### 4. Video buffer (`livewatcher/capture/`)

- **Live check:** every `live_check_interval_s` (20 s), and at once when chat spikes on a channel that looks offline (a stream that just started can take a minute or two to show as live). It uses Helix if `[twitch]` keys are set. Otherwise it sends one keyless query to Twitch's public web API, the same one streamlink uses, per 100 channels; this takes about 0.1 s for 62 channels. It falls back to streamlink, which is slower and gives no viewer counts. Each check gives live/offline, title, category and **viewer count**.
- **Max streamers:** at most `general.max_streamers` (12 in the example) live streamers are recorded at once. They are ranked with streams in `priority_categories` first, then by viewers, highest first. A recorded streamer is only replaced by one with at least 20% more viewers, so the set doesn't keep changing. The others are marked "live, not recorded": their chat is still read, but they have no buffer and aren't clipped. 0 = no limit.
- **Recording:** each live streamer gets one recorder worker process.
  - The worker uses streamlink's Twitch HLS reader with low latency on, and writes Twitch's own **2 s segments** to `buffer/<streamer>/` without re-encoding them. They are `.ts`, or self-contained `.mp4` when Twitch serves fMP4.
  - Every segment is dated with Twitch's `PROGRAM-DATE-TIME`, not with the time it arrived.
  - Segments older than `buffer_minutes` (3) are deleted.
  - The worker rewrites `live.m3u8`, which the dashboard plays with hls.js, and saves a JPEG snapshot every `snapshot_interval_s`.
  - It costs about 0.8% of one CPU core, 80 MB of RAM and 0.43 MB/s of disk per stream.
- **Ads:** ad segments are filtered out, and that leaves a **gap** in the buffer. A clip window that overlaps a gap is dropped, with the reason logged.
- **Restarts:** the parent process restarts workers with backoff, and also when one goes silent for 60 s or the config changes. On shutdown it kills all of them.

### 5. Timing: chat time vs video time

- Every timestamp in the app is **chat wall-clock time** (UTC).
- Chat reacts *after* viewers see something, and viewers watch a little behind live. So the moment chat reacts to sits earlier in our buffer than the chat timestamp.
- The cutter shifts every window by `capture.sync_offset_s`, which is **−2.0 s** by default. A positive value moves clips later.
  - −2.0 is an estimate of viewer delay, not a measurement.
  - To tune it, clip a moment you recognise on a live stream and check it's in the clip. The Debug tab has a slider for this.
- **Clip window** = `peak_start − preroll_s` (25 s) to `peak_end + postroll_s` (6 s), capped at `max_clip_s` (60 s).
- **Clip now** shifts that window 5 s later: 20 s before the press to 11 s after it, so the clip includes what happens just after you press.

### 6. Clip life cycle (`livewatcher/app.py`, `ClipService`)

```
waiting ──> cutting ──> rendering ──> pending_review ──(approve)──> queued ──> uploading ──> uploaded
                                  └─> queued (auto-upload on)                           ├─> dry_run_ok
                                                                                        └─> failed
plus: rejected (by you), dropped (window not recordable: ad gap, too old, stream ended)
```

1. **waiting:** the post-roll hasn't been recorded yet. The clip waits, up to 120 s past the window end, until the whole window is in closed segments.
2. **cutting:** the segments covering the window are hard-linked into `clips/tmp/`, so buffer trimming can't delete them mid-cut. They are then joined, trimmed exactly and re-encoded to H.264/AAC in `clips/raw/<id>.mp4`. Twitch has a keyframe only every 2 s, so a stream copy would start up to 2 s off.
3. **rendering:** produces a 1080×1920 render in the streamer's layout, `clips/out/<id>_<layout>.mp4` (libx264 veryfast, CRF 20, AAC 128k). The streamer's name is burned in as a title.
4. The clip's chat-rate samples are saved to `clips/rates/<id>.json`, so the dashboard can replay the graph with the video.
5. **pending_review** if auto-upload is off, otherwise **queued**.

### 7. Vertical layouts (`livewatcher/render/`)

| Layout | Result | Settings |
|---|---|---|
| `blur_fit` (default) | The full 16:9 frame over a blurred, zoomed copy of itself. Nothing is cut off. | `fit_zoom` > 1.0 enlarges the picture and trims the sides |
| `crop` | A 9:16 slice at full height. Fills the screen. | `crop_x`: 0 = left edge, 1 = right edge |
| `stacked` | Two regions stacked, for example facecam on top and gameplay below | `stack_top`, `stack_bottom` (fractions of the frame), `stack_split` (top's share of the height) |

You can re-render any clip in another layout from the Clips tab; each layout's file is kept. The Debug tab's layout tool draws the regions over a live snapshot, so you can set them by eye and save them to the config.

### 8. Uploading (`livewatcher/upload/`)

- **Accounts:** `[[tiktok.accounts]]`, or the Accounts page, lists the TikTok accounts to post to. Each one has its own browser profile (login), `max_uploads_per_day` and `min_minutes_between_uploads`. With no accounts listed there is one built-in account, `default`, that uses the `[tiktok]` profile and limits.
- The **upload queue** uploads one clip at a time.
  - **Which clip:** clips you made with Clip now go first, then the highest **chat-to-baseline ratio** (`clip.score`, the peak's rate ÷ normal chat), then the oldest. So when there are more clips than upload slots, the biggest chat reactions get posted. A clip that finds no slot within `tiktok.max_queue_age_h` (6) hours is dropped as expired; you can queue it again from the Failed tab.
  - **Which account:** only enabled accounts with a free slot count: fewer than their daily maximum in the last 24 h, and at least their minimum gap since that account's last upload. With **Cycle** (`tiktok.cycle_accounts`) on, uploads go to each account in turn; with it off, to the first account in the list with a free slot. With no free account the clip waits, and the queue shows why.
  - Dry runs don't count toward the limits, but they still rotate accounts, so a dry run with Cycle on tests each account's login in turn.
  - A logged-out account or a CAPTCHA pauses only that account; a page change pauses the whole queue.
- The **uploader** drives a real Chromium through Playwright, using the chosen account's profile saved by `login-tiktok`:
  1. Open TikTok's upload page and attach the file.
  2. Wait for processing.
  3. Type the caption, then read it back to check it stuck.
  4. Click **Post**.
- **`tiktok.dry_run = true` (the default)** never clicks Post. `tiktok.dry_run_mode` sets what happens instead; switch it in the Debug tab under Uploads:
  - `"stop"` (default): stop on the filled-in form and save a screenshot to `upload_debug/`. Nothing reaches your account.
  - `"draft"`: click **Save draft**, so the clip lands in your TikTok drafts, private and unposted, and you can post it from the TikTok app later. If no confirmation appears, the clip is marked `failed` and not retried, so it can't make duplicate drafts.

  Either way the clip becomes `dry_run_ok`. Dry runs don't count toward the upload limits.
- **Results:**
  - **blocked** (CAPTCHA, logged out, page changed, caption didn't stick) → `failed` with a screenshot. The whole queue pauses until you approve a clip by hand or change the config.
  - **transient** (network error, processing timeout) → retried up to 2 times.
  - Nothing after the Post click is ever retried automatically, so a clip can't be posted twice.
- Caption: `tiktok.caption_template`, with `{streamer}` filled in. You can edit each clip's caption in the review queue.

### 9. Dashboard (`livewatcher/api/`, `web/`)

FastAPI serves a static page (Alpine.js, uPlot, hls.js, no build step) on `127.0.0.1:8787`. It is bound to localhost because it can change the config and trigger uploads. Live updates come over a WebSocket.

- **Streams:**
  - Recorded streamers are shown as cards, sorted by viewers. Each card shows live video (or a snapshot), the viewer count, the category, the buffer length and a chat-rate graph with the baseline, threshold and peaks. Each has a **Clip now** button.
  - "Live, not recorded" (outside max streamers) and "Offline" streamers are in collapsed lists.
  - Click a card to open it large; the arrow keys switch streamer.
- **Clips:**
  - Tabs: To review, Queued, Uploaded, and Failed/Dropped.
  - Each clip plays in a large 9:16 player, with its chat/s graph beside it. During playback a playhead moves across the graph and the line fills in up to it, so you watch chat react in sync. Clicking the graph seeks the video.
  - Each clip shows its chat ratio (for example "8.2× chat") or "manual". The Queued tab is in upload order, and posted clips show which account they went to.
  - You can edit the caption, switch layout and re-render, and Approve/Retry or Reject.
- **Accounts:** add, remove and enable TikTok accounts; set each one's limits; **Log in** (opens a browser window for that account) and **Check**; see uploads in the last 24 h and the next free slot; and the **Cycle** switch.
- **Debug:**
  - Log feed and peak log.
  - Detection sliders, global or per streamer.
  - Sync-offset slider, layout tool, dry-run switch.
  - Health table, which also shows which components are real or fake.
- **Settings:** the auto-upload switch (also in the header), the streamer list, upload limits, the caption template and the theme.

Saving from the dashboard writes `config.toml` with `tomlkit`, so your comments and formatting are kept.

### 10. Retention

Every hour, the raw, rendered and screenshot files and the saved chat rates of clips older than `general.retention_days` (7) are deleted. Clips still in progress are skipped. The clip rows stay in the database, so the history is kept. Leftover `clips/tmp/` folders older than a day are removed. Chat logs are kept. Set `retention_days = 0` to keep everything.

---

## Configuration (`config.toml`)

`config.toml` is gitignored. `config.example.toml` documents every key. The keys you're most likely to change:

| Key | Default | Meaning |
|---|---|---|
| `general.auto_upload` | `false` | Upload clips without review |
| `general.auto_clip` | `true` | Clip the peaks the detector finds (off: only Clip now) |
| `general.priority_categories` | `["Grand Theft Auto VI"]` in the example | Record these categories first when `max_streamers` applies |
| `tiktok.category_captions` | GTA VI in the example | Caption per Twitch category; others use `caption_template` (`{streamer}`, `{category}`) |
| `general.retention_days` | `7` | Delete clip files after this many days |
| `twitch.client_id` / `client_secret` | empty | Optional Helix keys: faster batched live checks |
| `detection.*` | see [Peak detection](#2-peak-detection-livewatcherdetectpeakpy) | Thresholds; also adjustable live in the Debug tab |
| `capture.buffer_minutes` | `3` | How far back a clip can reach |
| `capture.sync_offset_s` | `-2.0` | Chat-to-video offset (tune it live) |
| `capture.quality` | `720p60,720p,best` | streamlink quality preference |
| `render.default_layout` | `blur_fit` | `blur_fit`, `crop` or `stacked` |
| `tiktok.dry_run` | `true` | Never click Post |
| `tiktok.dry_run_mode` | `"stop"` | Dry run: `"stop"` before Post, or `"draft"` to save to TikTok drafts |
| `tiktok.caption_template` | GTA 6 tags in the example | `{streamer}` is replaced |
| `general.max_streamers` | `12` in the example | Most live streamers recorded at once (0 = no limit) |
| `tiktok.accounts` | none (= built-in `default`) | TikTok accounts: `name`, `enabled`, `browser_profile_dir`, `cookies_file`, `max_uploads_per_day`, `min_minutes_between_uploads` |
| `tiktok.cycle_accounts` | `false` | Post to each account in turn |
| `tiktok.max_queue_age_h` | `6` | Drop queued clips that find no slot within this many hours (0 = never) |
| `tiktok.max_uploads_per_day` / `min_minutes_between_uploads` | `6` / `45` | Limits of the built-in `default` account |

Each `[[streamers]]` entry has `name` and `enabled`, plus optional `layout`, `fit_zoom`, `crop_x`, `stack_top`, `stack_bottom` and `stack_split`. It can also override any `[detection]` key (for example `multiplier = 2.5` for a slower chat).

### Files (under `general.data_dir`, default `clips/` in the project folder)

| Path | What |
|---|---|
| `livewatcher.db` | SQLite database: clips, peaks, upload history |
| `buffer/<streamer>/` | Rolling segments and `live.m3u8`, wiped at start-up |
| `snapshots/<streamer>.jpg` | Latest frame |
| `clips/raw/<id>.mp4` | Cut clip, source aspect ratio |
| `clips/out/<id>_<layout>.mp4` | 1080×1920 render |
| `clips/rates/<id>.json` | Chat rate over the clip window |
| `chatlogs/<streamer>/<date>.jsonl` | Every chat message |
| `upload_debug/` | Uploader screenshots |
| `browser_profile/` | TikTok login |

A relative `data_dir` (and relative profile folders) is resolved against the project folder, whatever directory you start from. `clips/` is gitignored. The TikTok logins live in `clips/browser_profile` and `clips/accounts/<name>/browser_profile`, so treat that folder as private.

---

## Before relying on it unattended

1. **TikTok login:** add your accounts on the Accounts page, then click **Log in** on each one (or run `./lw login-tiktok --account NAME`).
2. **Selector check:** with `dry_run = true`, approve one clip and check the screenshot in `upload_debug/` shows a filled-in Post screen. Then try `dry_run_mode = "draft"` and check the clip appears in your TikTok drafts. TikTok changes its site; the selectors are all in `livewatcher/upload/tiktok_browser.py`.
3. **One real upload,** on purpose.
4. **Sync:** tune `sync_offset_s` on a live stream.
5. Then turn on auto-upload.

Each recorded stream costs about 80 MB of RAM and 0.4 MB/s of download, so keep `max_streamers` to roughly a dozen on a 7 GB machine. The streamer list itself can be long: unrecorded streamers only cost a chat connection.

**Risks:**
- Automated posting can get a TikTok account limited. The daily limit, the spacing between uploads and stopping on CAPTCHAs keep this small, but don't remove it.
- Reposting other streamers' footage generally needs their permission, and music in a stream can get a clip muted or removed.

---

## Development

```bash
~/.venvs/livewatcher/bin/python -m pytest -q   # about 620 tests; real-ffmpeg and browser tests skip if those are missing
~/.venvs/livewatcher/bin/ruff check .
```

| Path | Contents |
|---|---|
| `livewatcher/core/` | Config, models, SQLite, event bus, interfaces, fakes for the demo |
| `livewatcher/app.py` | Wires the components together; `ClipService`, retention |
| `livewatcher/chat/`, `detect/` | IRC reader, chat logs, peak detector, replay |
| `livewatcher/capture/` | Live check, recorder, buffer index, snapshots, cutter |
| `livewatcher/render/` | Filtergraphs, renderer, layout previews |
| `livewatcher/upload/` | TikTok uploader, upload queue (ranking, accounts), login |
| `livewatcher/api/`, `web/` | REST and WebSocket API, dashboard |
| `docs/CONTRACTS.md` | Interfaces, events, API table, file layout |
| `agents/` | The build plan and the original briefs |

Components talk through an in-process async event bus. `app.py` loads each real component by name and falls back to the fake if one can't be imported. `demo` always uses the fakes.
