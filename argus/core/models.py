"""Shared data models. FROZEN after Phase 0.

All timestamps (``ts``, ``ts_start``, ``window_start`` ...) are chat wall-clock time:
UTC seconds since the epoch, as ``time.time()`` returns. Branch B converts them to
positions in the video buffer.
"""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class ChatMessage(BaseModel):
    streamer: str  # channel name, lower case
    ts: float
    user_id: str = ""
    user: str = ""
    text: str = ""


class RateSample(BaseModel):
    """One point of the chat-rate signal, published every bin."""

    streamer: str
    ts: float
    rate: float            # smoothed messages per second
    baseline: float        # rolling median
    threshold: float       # the rate a peak has to beat right now
    z: float
    unique_chatters: int = 0


class PeakEvent(BaseModel):
    id: str = Field(default_factory=new_id)
    streamer: str
    ts_start: float        # when chat first rose above baseline
    ts_peak: float         # when the rate was highest
    ts_end: float          # when it fell back (or hit max_clip_s)
    score: float = 0.0     # peak rate / baseline
    source: Literal["auto", "manual"] = "auto"


class StreamStatus(BaseModel):
    streamer: str
    live: bool = False
    title: str = ""
    category: str = ""  # Twitch category (game) from the last live check, "" if unknown
    viewers: int | None = None  # viewer count from the last live check, None if unknown
    # False when the streamer is live but outside general.max_streamers: not recorded or clipped.
    watched: bool = True
    recorder_state: Literal["stopped", "starting", "recording", "error"] = "stopped"
    buffer_start: float | None = None  # chat-time range the buffer covers
    buffer_end: float | None = None
    last_error: str = ""


class ClipStatus(StrEnum):
    WAITING = "waiting"                # waiting for the window to be recorded
    CUTTING = "cutting"
    RENDERING = "rendering"
    PENDING_REVIEW = "pending_review"  # auto-upload off: waits for the user
    QUEUED = "queued"                  # approved, waiting for the upload queue
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    DRY_RUN_OK = "dry_run_ok"          # dry run reached the Post button
    FAILED = "failed"
    REJECTED = "rejected"
    DROPPED = "dropped"                # never cut (ad gap, stream ended, ...)


class Clip(BaseModel):
    id: str = Field(default_factory=new_id)
    streamer: str
    peak_id: str | None = None
    window_start: float
    window_end: float
    raw_path: str | None = None
    renders: dict[str, str] = Field(default_factory=dict)  # layout name -> file path
    active_layout: str = "blur_fit"    # the render that gets uploaded
    status: ClipStatus = ClipStatus.WAITING
    caption: str = ""
    created_at: float = Field(default_factory=time.time)
    error: str = ""
    debug_screenshot: str | None = None
    score: float = 0.0  # the peak's rate / baseline; ranks clips when upload slots are short
    account: str = ""   # TikTok account it was (or is being) uploaded to

    @property
    def out_path(self) -> str | None:
        return self.renders.get(self.active_layout)


class UploadResult(BaseModel):
    """ok: posted. dry_run: stopped before Post. transient: worth a retry.
    blocked: CAPTCHA, logged out or unknown page - never retry automatically."""

    kind: Literal["ok", "dry_run", "transient", "blocked"]
    error: str = ""
    screenshot: str | None = None


class LogLine(BaseModel):
    ts: float = Field(default_factory=time.time)
    level: Literal["debug", "info", "warning", "error"] = "info"
    source: str = ""       # e.g. "detect", "capture", "upload"
    streamer: str = ""
    message: str = ""
