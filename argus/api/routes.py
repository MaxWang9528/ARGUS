"""The JSON API. See the "Dashboard API" table in docs/CONTRACTS.md.

Every route is ``async`` so that clip actions and config changes run on the event
loop thread, where the bus must be published from.

Status codes: 404 for an unknown streamer or clip id and for a file that isn't
there; 409 when ClipService refuses an action (``ClipError``); 422 for a bad
body, query or config patch; 501 when the layout preview can't be made yet (no
snapshot, or the renderer has no preview).
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import anyio
from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, ValidationError

from ..core.config import LAYOUTS, Config, Detection, LayoutConfig, Region
from ..core.models import Clip, ClipStatus

if TYPE_CHECKING:
    from ..app import App

router = APIRouter()

_NO_STORE = {"Cache-Control": "no-store"}
_HLS_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
    ".aac": "audio/aac",
}


# ---- helpers -------------------------------------------------------------------
def _lw(request: Request) -> App:
    return request.app.state.lw


def _streamer_name(lw: App, name: str) -> str:
    """The lower-case name of a configured streamer, or 404."""
    if lw.store.get().streamer(name) is None:
        raise HTTPException(404, f"no streamer called {name}")
    return name.lower()


def _clip(lw: App, clip_id: str) -> Clip:
    clip = lw.db.get_clip(clip_id)
    if clip is None:
        raise HTTPException(404, f"no clip with id {clip_id}")
    return clip


def _inside(path: str | Path | None, folder: Path) -> Path:
    """``path`` resolved, if it is an existing file inside ``folder``; else 404."""
    if not path:
        raise HTTPException(404, "no such file")
    try:
        resolved = Path(path).expanduser().resolve()
        root = folder.expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(404, "no such file") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise HTTPException(404, "no such file")
    return resolved


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _public_config(cfg: Config) -> dict[str, Any]:
    data = cfg.model_dump(mode="json")
    data["twitch"]["client_secret"] = ""
    return data


def scrub_patch(patch: Any) -> Any:
    """A copy of a config patch that is safe to show: no client secret."""
    if not isinstance(patch, dict):
        return patch
    out = copy.deepcopy(patch)
    twitch = out.get("twitch")
    if isinstance(twitch, dict) and "client_secret" in twitch:
        twitch["client_secret"] = ""
    return out


def _drop_none(value: Any) -> Any:
    """Remove ``None`` values: TOML has no null, and None means "use the default"."""
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_none(v) for v in value if v is not None]
    return value


def _validation_detail(exc: ValidationError) -> list[dict[str, Any]]:
    return [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]


# ---- state, streamers ----------------------------------------------------------
@router.get("/state")
async def get_state(request: Request) -> dict[str, Any]:
    lw = _lw(request)
    cfg = lw.store.get()
    streamers = []
    for s in cfg.streamers:
        name = s.name.lower()
        streamers.append({
            "name": name,
            "enabled": s.enabled,
            "status": _dump(lw.recorder.status(name)),
            "has_playlist": lw.recorder.playlist_path(name) is not None,
            # Not in the contract table: lets the page skip a snapshot request that would 404.
            "has_snapshot": lw.recorder.snapshot_path(name) is not None,
        })
    return {
        "auto_upload": cfg.general.auto_upload,
        "dry_run": cfg.tiktok.dry_run,
        "components": dict(lw.components),
        "streamers": streamers,
    }


@router.get("/streamers/{name}/rates")
async def get_rates(request: Request, name: str) -> list[dict[str, Any]]:
    lw = _lw(request)
    return [_dump(r) for r in lw.rate_history(_streamer_name(lw, name))]


@router.get("/streamers/{name}/snapshot.jpg")
async def get_snapshot(request: Request, name: str) -> Response:
    lw = _lw(request)
    low = _streamer_name(lw, name)
    path = _inside(lw.recorder.snapshot_path(low), lw.store.get().data_dir / "snapshots")
    return Response(await _read_whole(path), media_type="image/jpeg", headers=_NO_STORE)


async def _read_whole(path: Path) -> bytes:
    """Read a small file that is replaced by rename (snapshots, the live playlist) in one go.

    FileResponse sizes the file before sending it, so a rename in between gives the
    browser a Content-Length that doesn't match the body."""
    try:
        return await anyio.Path(path).read_bytes()
    except FileNotFoundError:
        raise HTTPException(404, "no such file") from None


@router.get("/streamers/{name}/live/{file}")
async def get_live(request: Request, name: str, file: str) -> Response:
    lw = _lw(request)
    low = _streamer_name(lw, name)
    playlist = lw.recorder.playlist_path(low)
    if playlist is None:
        raise HTTPException(404, f"{low} has no live playlist")
    playlist = Path(playlist)
    folder = playlist.parent
    if "/" in file or "\\" in file or file in ("", ".", ".."):
        raise HTTPException(404, "no such file")
    # The page always asks for live.m3u8, whatever the recorder named its playlist.
    target = playlist if file == "live.m3u8" else folder / file
    path = _inside(target, folder)
    media_type = _HLS_TYPES.get(path.suffix.lower(), "application/octet-stream")
    if path.suffix.lower() == ".m3u8":
        return Response(await _read_whole(path), media_type=media_type, headers=_NO_STORE)
    return FileResponse(path, media_type=media_type)


def _region(text: str | None, field: str) -> Region | None:
    if text is None or text == "":
        return None
    try:
        x, y, w, h = (float(v) for v in text.split(","))
    except ValueError as exc:
        raise HTTPException(422, f"{field} must be four numbers: x,y,w,h") from exc
    return Region(x=x, y=y, w=w, h=h)


@router.get("/streamers/{name}/layout-preview.jpg")
async def get_layout_preview(
    request: Request, name: str,
    layout: str | None = None,
    crop_x: float | None = Query(None, ge=0, le=1),
    fit_zoom: float | None = Query(None, gt=0, le=4),
    stack_top: str | None = None,
    stack_bottom: str | None = None,
    stack_split: float | None = Query(None, gt=0, lt=1),
) -> Response:
    lw = _lw(request)
    low = _streamer_name(lw, name)
    if layout and layout not in LAYOUTS:
        raise HTTPException(422, f"layout must be one of {', '.join(LAYOUTS)}")
    base = lw.store.get().layout_for(low, layout or None)
    overrides: dict[str, Any] = {
        "crop_x": crop_x, "fit_zoom": fit_zoom, "stack_split": stack_split,
        "stack_top": _region(stack_top, "stack_top"), "stack_bottom": _region(stack_bottom, "stack_bottom"),
    }
    cfg = LayoutConfig.model_validate({**base.model_dump(), **{k: v for k, v in overrides.items() if v is not None}})
    snapshot = lw.recorder.snapshot_path(low)
    if snapshot is None:
        raise HTTPException(501, f"no snapshot of {low} yet, so there is nothing to preview")
    preview = getattr(lw.renderer, "preview", None)
    if preview is None:
        raise HTTPException(501, "the renderer can't make previews")
    try:
        data = await preview(Path(snapshot), cfg)
    except NotImplementedError as exc:
        raise HTTPException(501, "the renderer can't make previews") from exc
    return Response(data, media_type="image/jpeg", headers=_NO_STORE)


@router.post("/streamers/{name}/clip-now")
async def post_clip_now(request: Request, name: str) -> dict[str, Any]:
    lw = _lw(request)
    return _dump(lw.clips.clip_now(_streamer_name(lw, name)))


# ---- peaks and clips -----------------------------------------------------------
@router.get("/peaks")
async def get_peaks(request: Request, streamer: str | None = None, since: float = 0.0,
                    limit: int = Query(200, ge=1, le=2000)) -> list[dict[str, Any]]:
    lw = _lw(request)
    return [_dump(p) for p in lw.db.list_peaks(streamer.lower() if streamer else None, since, limit)]


@router.get("/clips")
async def get_clips(request: Request, status: str | None = None, streamer: str | None = None,
                    limit: int = Query(200, ge=1, le=2000)) -> list[dict[str, Any]]:
    lw = _lw(request)
    statuses = None
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        bad = [s for s in statuses if s not in ClipStatus._value2member_map_]
        if bad:
            raise HTTPException(422, f"unknown status {', '.join(bad)}")
    clips = lw.db.list_clips(status=statuses, streamer=streamer.lower() if streamer else None, limit=limit)
    return [_dump(c) for c in clips]


@router.get("/clips/{clip_id}")
async def get_clip(request: Request, clip_id: str) -> dict[str, Any]:
    return _dump(_clip(_lw(request), clip_id))


@router.get("/clips/{clip_id}/video")
async def get_clip_video(request: Request, clip_id: str, layout: str | None = None) -> FileResponse:
    lw = _lw(request)
    clip = _clip(lw, clip_id)
    path = clip.renders.get(layout) if layout else clip.out_path
    path = _inside(path, lw.store.get().data_dir / "clips" / "out")
    return FileResponse(path, media_type="video/mp4")


@router.get("/clips/{clip_id}/rates")
async def get_clip_rates(request: Request, clip_id: str) -> list[dict[str, Any]]:
    """Chat rate over the clip window (padded 5 s), for replaying it with the video.
    Video time t corresponds to chat time ``clip.window_start + t``."""
    lw = _lw(request)
    return [_dump(r) for r in lw.clip_rates(_clip(lw, clip_id))]


@router.get("/clips/{clip_id}/raw")
async def get_clip_raw(request: Request, clip_id: str) -> FileResponse:
    lw = _lw(request)
    clip = _clip(lw, clip_id)
    return FileResponse(_inside(clip.raw_path, lw.store.get().data_dir / "clips" / "raw"), media_type="video/mp4")


@router.get("/clips/{clip_id}/screenshot")
async def get_clip_screenshot(request: Request, clip_id: str) -> FileResponse:
    lw = _lw(request)
    clip = _clip(lw, clip_id)
    path = _inside(clip.debug_screenshot, lw.store.get().data_dir / "upload_debug")
    return FileResponse(path)


class PurgeBody(BaseModel):
    statuses: list[str] | None = None  # None = every status that isn't in progress
    older_than_h: float = 0.0


@router.post("/clips/purge")
async def post_purge(request: Request, body: PurgeBody) -> dict[str, int]:
    """Delete clips (files and rows) by status and age. In-progress clips are kept."""
    lw = _lw(request)
    try:
        return lw.purge_clips(body.statuses, body.older_than_h)
    except ValueError as exc:  # unknown status name
        raise HTTPException(422, str(exc)) from None


@router.delete("/clips/{clip_id}")
async def delete_clip(request: Request, clip_id: str) -> dict[str, int]:
    lw = _lw(request)
    _clip(lw, clip_id)
    return {"files": lw.delete_clip(clip_id)}


@router.post("/clips/{clip_id}/approve")
async def post_approve(request: Request, clip_id: str) -> dict[str, Any]:
    lw = _lw(request)
    _clip(lw, clip_id)
    return _dump(lw.clips.approve(clip_id))


@router.post("/clips/{clip_id}/reject")
async def post_reject(request: Request, clip_id: str) -> dict[str, Any]:
    lw = _lw(request)
    _clip(lw, clip_id)
    return _dump(lw.clips.reject(clip_id))


class RerenderBody(BaseModel):
    layout: str


@router.post("/clips/{clip_id}/rerender")
async def post_rerender(request: Request, clip_id: str, body: RerenderBody) -> dict[str, Any]:
    lw = _lw(request)
    _clip(lw, clip_id)
    return _dump(await lw.clips.rerender(clip_id, body.layout))


class CaptionBody(BaseModel):
    caption: str = Field(max_length=2200)  # TikTok's caption limit


@router.patch("/clips/{clip_id}")
async def patch_clip(request: Request, clip_id: str, body: CaptionBody) -> dict[str, Any]:
    lw = _lw(request)
    _clip(lw, clip_id)
    return _dump(lw.clips.set_caption(clip_id, body.caption))


# ---- config and logs -----------------------------------------------------------
@router.get("/config")
async def get_config(request: Request) -> dict[str, Any]:
    return _public_config(_lw(request).store.get())


@router.patch("/config")
async def patch_config(request: Request, patch: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    lw = _lw(request)
    patch = _drop_none(patch)
    twitch = patch.get("twitch")
    if isinstance(twitch, dict) and twitch.get("client_secret") == "":
        twitch = {k: v for k, v in twitch.items() if k != "client_secret"}
        if twitch:
            patch["twitch"] = twitch
        else:
            del patch["twitch"]
    # Check the whole result before applying it, including the per-streamer detection
    # overrides, which Config accepts as free-form extra keys.
    try:
        merged = lw.store.get().model_dump(exclude_none=True)
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        candidate = Config.model_validate(merged)
        for s in candidate.streamers:
            extra = {k: v for k, v in (s.model_extra or {}).items() if k in Detection.model_fields}
            Detection.model_validate({**candidate.detection.model_dump(), **extra})
        names = [s.name.lower() for s in candidate.streamers]
        if len(names) != len(set(names)):
            raise HTTPException(422, "streamer names must be unique")
        new = lw.store.update(patch)
    except ValidationError as exc:
        raise HTTPException(422, _validation_detail(exc)) from exc
    return _public_config(new)


@router.get("/logs")
async def get_logs(request: Request) -> list[dict[str, Any]]:
    return [_dump(line) for line in _lw(request).recent_logs()[-500:]]
