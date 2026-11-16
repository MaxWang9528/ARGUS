# Agent B — `feat/capture`: live check, rolling buffer, snapshots, clip cutting

Read first, in this order: `docs/CONTRACTS.md`, `livewatcher/core/` (models, interfaces, config, events, fakes), then "Clip window and timing" in `agents/PLAN.md`.

## Goal
Keep a rolling buffer of the last few minutes of every live streamer, save frequent snapshots, expose the buffer as a playable playlist, and cut a raw clip for any chat-time window.

## Files you own (edit only these)
- `livewatcher/capture/` — `live_check.py`, `recorder.py`, `buffer.py`, `snapshot.py`, `cutter.py`
- `tests/capture/`

Everything else is frozen: `livewatcher/core/`, `app.py`, `cli.py`, `tests/conftest.py`, `docs/`, other branches' folders. If you need a change there, describe it in your report and work around it.

## Rules
- Use the shared environment (`~/.venvs/livewatcher/bin/python`). The `streamlink` command is at `~/.venvs/livewatcher/bin/streamlink`; find it relative to `sys.executable`, not on `PATH`.
- **ffmpeg may not be installed.** Check with `which ffmpeg ffprobe`. If it's missing, write the code and the tests anyway (mark ffmpeg tests with the `needs_ffmpeg` marker from `tests/conftest.py`), and say clearly in your report which checks you could not run. Don't try to install it.
- No command in this brief has been run on this machine. Treat each as a starting point: run it, confirm it does what the brief says, and adjust.
- Keep test videos under 15 s. Other agents share this machine's 7 GB of RAM.
- Never hard-code paths. Everything goes under `store.get().data_dir`.
- Commit to your branch when done. Don't push, and don't merge.

## Tasks
1. **First: prove the recorder command on a real live stream** (pick any large live channel). Starting point, with no Python in the data path:
   `streamlink --stdout [--twitch-low-latency] [--twitch-disable-ads] twitch.tv/<name> <quality>` piped into
   `ffmpeg -i pipe:0 -c copy -f hls -hls_time 6 -hls_list_size <n> -hls_flags delete_segments+program_date_time -strftime 1 -hls_segment_filename 'seg_%Y%m%d_%H%M%S.ts' live.m3u8`.
   Confirm the flag names against the installed streamlink (8.6). If ffmpeg's HLS output doesn't hold up, fall back to `-f segment` plus your own trimming and playlist.
2. **Live check** (`live_check.py`): decide who is live every `twitch.live_check_interval_s`. Without API keys, use the streamlink Python API (no streams means offline); confirm this works and how long a check takes. With `twitch.client_id` and `client_secret` set, use Helix `GET /streams` with an app token. Run blocking calls in a thread.
3. **`Recorder(store, bus)`** (`recorder.py`), implementing `StreamRecorder`. `run()` starts and stops one recording pipeline per live, enabled streamer, restarts a dead one with backoff, reacts to config changes, and kills every child process when cancelled. Required behaviour:
   - **Accurate time mapping.** Chat-time to buffer position must stay right, including just after start-up, when streamlink delivers several seconds of video in a burst (so a segment's file time is not when its content was live). Say in your report how you anchor it.
   - **Ad gaps.** With `capture.filter_ads` on, ad segments are filtered and the buffer has holes. Record where the holes are.
   - **Clean restarts.** Segments left over from an earlier run or a crashed pipeline are removed.
   - `status()` returns `StreamStatus` with the chat-time range the buffer covers. Publish `Topic.STREAM_STATUS` on change and at least every 30 s.
   - `window_ready(streamer, end_ts)` is true only when everything up to `end_ts` (after applying `sync_offset_s`) is in closed segments. It raises `WindowUnavailable` if that can never happen (offline, recorder stopped).
   - `playlist_path()` returns the `.m3u8`, with its segments in the same folder, so the dashboard can play it. `None` if there isn't one.
4. **Snapshots** (`snapshot.py`): a JPEG at `data_dir/snapshots/<streamer>.jpg` every `capture.snapshot_interval_s`, written to a temp file and renamed. Publish `Topic.SNAPSHOT`. Prefer a second output on the same ffmpeg process that decodes keyframes only; fall back to grabbing a frame from the last closed segment. Report the CPU cost.
5. **`Cutter(store, bus, recorder)`** (`cutter.py`), implementing `ClipCutter`. `cut()` takes a chat-time window, applies `capture.sync_offset_s` (positive moves the clip later), joins the matching segments, trims to within about half a second, and writes `data_dir/clips/raw/<clip_id>.mp4` with audio and video in sync. It raises `WindowUnavailable` when the window overlaps an ad gap or starts before the buffer.
6. **Measure the real chat-to-video offset.** On a live stream with low latency on, work out how far our buffer runs behind (or ahead of) what chat is reacting to, and recommend a default for `sync_offset_s`. Explain how you measured it.

## Done criteria
- A fake buffer built from the `sample_video` fixture: a cut for a known window has the right length (within 0.5 s) and starts at the right moment; `window_ready` is false before the last segment closes and true after; a window over a recorded gap raises `WindowUnavailable`.
- Live-check logic tested with streamlink mocked.
- A real run against one live channel for at least 3 minutes: segments rotate, old ones are deleted, snapshots refresh, the playlist plays, and one cut clip looks right. Put CPU, memory and disk numbers per stream in your report.
- `pytest tests/capture` passes and `ruff check .` is clean.

## Report back
The recorder command that worked, the live-detect method, how time is anchored, the measured offset and your recommended `sync_offset_s`, resource use per stream, what you couldn't verify, and any contract change you need (described, not made).
