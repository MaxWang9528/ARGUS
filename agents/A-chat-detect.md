# Agent A — `feat/chat-detect`: Twitch chat input and peak detection

Read first, in this order: `docs/CONTRACTS.md`, `livewatcher/core/` (models, interfaces, config, events, fakes), then "Peak detection" in `agents/PLAN.md`.

## Goal
Read chat from every enabled Twitch channel, turn it into a chat-rate signal, and publish a `PeakEvent` whenever chat spikes well above its baseline.

## Files you own (edit only these)
- `livewatcher/chat/twitch_irc.py`, `livewatcher/chat/chatlog.py`
- `livewatcher/detect/peak.py`, `livewatcher/detect/replay.py`
- `tests/chat/`, `tests/detect/`

Everything else is frozen: `livewatcher/core/`, `app.py`, `cli.py`, `tests/conftest.py`, `docs/`, other branches' folders. If you need a change there, describe it in your report and work around it.

## Rules
- Use the shared environment (`~/.venvs/livewatcher/bin/python`). Don't create one.
- Commit to your branch when done. Don't push, and don't merge.

## Tasks
1. **`TwitchChatSource(store, bus)`** in `twitch_irc.py`, implementing `ChatSource`:
   - Connect anonymously to `wss://irc-ws.chat.twitch.tv:443` with nick `justinfan<random 5 digits>`. Send `CAP REQ :twitch.tv/tags`.
   - One connection joins every enabled channel. Space out JOINs to stay under Twitch's limit for anonymous connections (about 20 per 10 s).
   - Answer `PING` with `PONG`. Reconnect with backoff (1 s up to 60 s, with jitter) and rejoin.
   - Parse `PRIVMSG` into `ChatMessage` (`user-id` and `display-name` from the tags) and publish `Topic.CHAT`.
   - When the config changes (`Topic.CONFIG_CHANGED`), join new channels and part removed ones.
   - Keep the line parser a pure function so it's easy to test.
2. **Chat log** in `chatlog.py`: a small recorder that subscribes to `Topic.CHAT` and appends to `data_dir/chatlogs/<streamer>/<YYYY-MM-DD>.jsonl`. `TwitchChatSource.run()` starts it. Also provide a reader that yields `ChatMessage`s from a JSONL file.
3. **`ChatPeakDetector(store, bus)`** in `peak.py`, implementing `PeakDetector`. Put the maths in a pure, clock-free class (one instance per streamer: feed it messages and bin ticks, it returns `RateSample`s and finished `PeakEvent`s) so tests and replay can drive it without sleeping. The `run()` wrapper subscribes to `Topic.CHAT` and ticks on the wall clock.
   - Bin messages in `bin_s`, smooth over `smooth_s`.
   - Don't count messages that start with `!` or come from a user in `ignore_users` (compare lower case).
   - Baseline = rolling median over `baseline_window_s`. Spread = MAD scaled to a standard deviation, with a floor so a silent chat can't give an infinite score. Bins inside an active peak are left out of the baseline.
   - A peak **starts** when all hold: `rate > baseline * multiplier`, `z > z_threshold`, `rate > min_msgs_per_sec`, unique chatters over the smoothing window `>= min_unique_chatters`, the cooldown since the last peak's end has passed, and there are at least 60 s of history.
   - `ts_start` = when the rate first rose above baseline plus one spread on the way up. `ts_peak` = the highest bin.
   - A peak **ends** when the rate drops below `baseline * end_ratio`, or at `ts_start + max_clip_s`. Publish `Topic.PEAK` once, at the end. `score` = peak rate / baseline.
   - Publish a `RateSample` every bin, with `threshold` set to the rate a peak would need right now.
   - Read `store.get().detection_for(streamer)` on every bin so dashboard changes apply at once.
4. **`replay.main(args)`** in `replay.py`: read `args.file` (JSONL), run it through the pure detector as fast as possible using message timestamps, and print each peak (start, peak, end, score) plus a one-line summary. `args.streamer` filters to one channel. `args.store` is a `ConfigStore`. Return 0.

## Done criteria
- Unit tests on made-up series: flat gives 0 peaks, a slow ramp gives 0, a sharp spike gives 1, two spikes inside the cooldown give 1, nothing fires during warm-up, a silent chat with one message gives 0, a spike made by a single user gives 0, `!` commands are ignored.
- IRC parser tests on real-format `PRIVMSG`, `PING` and tagged lines.
- Replaying the `sample_chat_log` fixture finds exactly the planted spike (starts near +400 s).
- A live check: connect to one busy live Twitch channel for about 2 minutes and confirm messages arrive and rate samples look sane. Put the numbers in your report.
- `pytest tests/chat tests/detect` passes and `ruff check .` is clean.

## Report back
What you built, test results, the live-check numbers, threshold defaults you'd recommend if they differ from the config, and any contract change you need (described, not made).
