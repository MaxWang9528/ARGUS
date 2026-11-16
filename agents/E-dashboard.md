# Agent E — `feat/dashboard`: FastAPI and the web UI

Read first, in this order: `docs/CONTRACTS.md` (the "Dashboard API" table is your spec), `livewatcher/app.py`, `livewatcher/core/` (models, config, events, fakes).

## Goal
A local web dashboard that shows what the program is seeing and gives the user control: scroll through streams, watch chat rates, review clips, switch auto-upload, and use the debugging tools.

## Files you own (edit only these)
- `livewatcher/api/` — `server.py`, `routes.py`, `ws.py`
- `web/` — `index.html`, `app.js`, `styles.css`, `vendor/`
- `tests/api/`

Everything else is frozen: `livewatcher/core/`, `app.py`, `cli.py`, `tests/conftest.py`, `docs/`, other branches' folders. If you need a change there, describe it in your report and work around it.

## Rules
- Use the shared environment (`~/.venvs/livewatcher/bin/python`). Don't create one.
- Build against `./lw demo`, which runs everything on fakes and serves your dashboard at `http://127.0.0.1:8787`. In the demo there is no live playlist and clips may be placeholder files if ffmpeg is missing, so the page must cope with both.
- Talk to the rest of the app only through the `App` object (`app.store`, `app.bus`, `app.db`, `app.recorder`, `app.renderer`, `app.clips`, `app.rate_history()`, `app.recent_logs()`, `app.components`). Don't import other branches' modules.
- No build step and no CDN at run time: download Alpine.js, uPlot and hls.js once into `web/vendor/` and commit them.
- Other agents run on this machine at the same time. Use a spare port (not 8787) for your own test runs, and stop every server you start.
- Commit to your branch when done. Don't push, and don't merge.

## Tasks
1. **API** (`livewatcher/api/`): `create_app(app) -> FastAPI`, with every endpoint in the contract table, the error codes listed there, and the WebSocket. Details that matter:
   - Serve files only from the folders the contract names. Reject any path that escapes them.
   - `rerender` and `layout-preview` call async methods on `app`; other clip actions are plain calls that raise `ClipError`.
   - The WebSocket relays bus events as `{"topic", "data"}` and never sends `chat`. Close the bus subscription when the socket closes.
   - Never return `twitch.client_secret`.
2. **Dashboard** (`web/`), one page with four views:
   - **Streams.** A horizontally scrollable strip of streamer cards with a grid toggle. Each card has the live picture (hls.js on the buffer playlist when `has_playlist` is true, otherwise the snapshot, refreshed on `snapshot` events), live/offline and recorder state, a uPlot chart of chat rate with the baseline and threshold lines and a marker for each peak, and a "Clip now" button. Clicking a card opens it large. Left and right arrow keys move between streamers.
   - **Clips.** Tabs for To review, Queued, Uploaded and Failed/Dropped. Each clip has a video player, an editable caption, a layout picker with a re-render button, Approve and Reject, its window and peak time, and for failures the error and a link to the screenshot. The list updates live from `clip` events.
   - **Debug.** A live log feed with level and streamer filters. Sliders for the detection settings (global and per streamer) that apply as you move them and save to the config. A layout tool: pick a streamer and layout, adjust `crop_x`, `fit_zoom` or the stack regions, see the preview image update, and save. The dry-run switch. A health list from `StreamStatus` and `app.components` (which parts are real and which are fake).
   - **Settings.** A large auto-upload switch that is also visible in the header on every view. The streamer list (add, remove, enable). Upload limits.
   - Works in light and dark mode and on a small laptop screen. Show a clear banner when the WebSocket is disconnected, and reconnect by itself.
   - Per-streamer detection overrides are extra keys on the streamer's entry, so saving one means sending the whole `streamers` list in the config patch.

## Done criteria
- TestClient tests for every endpoint against `App(store, demo=True, serve=False)`: success cases, 404/409/422/501 cases, a config round trip, a path-escape attempt, and the secret never appearing.
- A WebSocket test: an event published on the bus reaches the client.
- A browser smoke test with Playwright if Chromium can launch here (`python -m playwright install chromium` needs no sudo; skip the test cleanly if the launch fails): the page loads without console errors, the three demo streamers appear, "Clip now" leads to a clip in To review, and the auto-upload switch survives a reload.
- `pytest tests/api` passes and `ruff check .` is clean.

## Report back
How to run it, screenshot paths for each view if you could take them, what you couldn't verify, and any contract change you need (described, not made).
