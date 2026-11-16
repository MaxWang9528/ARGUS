# Agent C — `feat/render`: vertical 9:16 rendering

Read first, in this order: `docs/CONTRACTS.md`, `livewatcher/core/` (models, interfaces, config, fakes), then "Vertical layouts" in `agents/PLAN.md`.

## Goal
Turn a raw 16:9 clip into a 1080×1920 TikTok-ready MP4 in the layout asked for, and draw layout previews for the dashboard.

## Files you own (edit only these)
- `livewatcher/render/` — `ffmpeg_graph.py`, `renderer.py`, `layouts.py`
- `tests/render/`

Everything else is frozen: `livewatcher/core/`, `app.py`, `cli.py`, `tests/conftest.py`, `docs/`, other branches' folders. If you need a change there, describe it in your report and work around it.

## Rules
- Use the shared environment (`~/.venvs/livewatcher/bin/python`). Don't create one.
- **ffmpeg may not be installed.** Check with `which ffmpeg ffprobe`. If it's missing, write the code and tests anyway (mark ffmpeg tests with `needs_ffmpeg` from `tests/conftest.py`; the `sample_video` fixture skips by itself), and say clearly in your report which checks you could not run. Don't try to install it.
- No ffmpeg filter in this brief has been run on this machine. Run each one and look at the output before building on it.
- Keep test videos under 15 s.
- Commit to your branch when done. Don't push, and don't merge.

## Tasks
1. **Filtergraph builders** (`ffmpeg_graph.py`): pure functions, no I/O, taking a `LayoutConfig` (from `core/config.py`), the output size and an optional title, and returning the `-filter_complex` string. Don't assume the source is exactly 16:9 or 1280×720.
   - `blur_fit`: background = the frame scaled up to cover the output, cropped and strongly blurred. Foreground = the frame scaled to the output width times `fit_zoom`, centred, with anything wider than the output cropped off. `fit_zoom = 1.0` loses nothing.
   - `crop`: a 9:16 slice at full source height, placed with `crop_x` (0 = left, 0.5 = centre, 1 = right), scaled to the output.
   - `stacked`: `stack_top` and `stack_bottom` (fractions of the frame) are each cropped, then scaled to cover their share of the output (`stack_split` is the top's share of the height) and centre-cropped to fit exactly, one above the other.
   - Title banner when `render.burn_title` is on: the streamer's name, drawn readable over any background. Escape the text properly for `drawtext`, and handle a missing font without failing the render.
2. **`FfmpegRenderer(store, bus)`** (`renderer.py`), implementing `Renderer`:
   - `render(clip, layout)` reads `clip.raw_path` and writes `data_dir/clips/out/<clip.id>_<layout.layout>.mp4`, returning the path. It never touches another layout's file. Write to a temp name and rename when done.
   - Output: H.264 (yuv420p, `+faststart`, a quality around CRF 20 at a fast preset), AAC 128k stereo, frame rate matching the source up to 60, size from `render.resolution`.
   - At most `general.max_concurrent_renders` run at once (read when a render starts); the rest wait.
   - A failed ffmpeg run raises an error that carries the last lines of ffmpeg's output. Cancelling the task kills ffmpeg.
3. **`preview(snapshot_path, layout)`** on the renderer, with the drawing in `layouts.py` (Pillow): returns JPEG bytes of the snapshot with the layout's regions outlined and labelled (crop slice, or the two stack regions, or the `fit_zoom` trim lines).

## Done criteria
- Snapshot tests of the filtergraph strings for every layout, including odd inputs (`fit_zoom` 1.3, `crop_x` 0 and 1, a 4:3 source).
- Render the `sample_video` fixture in all three layouts and check with `ffprobe`: 1080×1920, has audio, duration within 0.2 s of the source.
- Concurrency: three renders queued with a limit of 1 run one after another.
- A failed render raises with ffmpeg's message; no partial file is left behind.
- `preview` returns a valid JPEG for each layout.
- `pytest tests/render` passes and `ruff check .` is clean.

## Report back
Render time per 10 s of clip for each layout on this machine, paths to example outputs the user can watch, what you couldn't verify, and any contract change you need (described, not made).
