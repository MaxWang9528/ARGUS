"""Config models and the ConfigStore.

FROZEN after Phase 0. Everything the user can tune lives in ``config.toml`` (see
``config.example.toml``). Read settings through ``ConfigStore.get()`` every time you
need them, so changes made in the dashboard apply at once.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any, Literal

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .events import EventBus, Topic

# Relative paths in the config (data_dir, profile folders) are relative to the project folder.
PROJECT_DIR = Path(__file__).resolve().parents[2]


def resolve_path(path: str) -> Path:
    p = Path(os.path.expanduser(path))
    return p if p.is_absolute() else PROJECT_DIR / p


LayoutName = Literal["blur_fit", "crop", "stacked"]
LAYOUTS: tuple[str, ...] = ("blur_fit", "crop", "stacked")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class General(_Model):
    data_dir: str = "clips"  # relative = inside the project folder
    auto_upload: bool = False
    # Clip peaks the detector finds. Off: peaks are still detected and logged, not clipped.
    # "Clip now" works either way.
    auto_clip: bool = True
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8787
    max_concurrent_renders: int = 1
    retention_days: int = 7
    # Streams in these Twitch categories (case-insensitive) are recorded first when
    # max_streamers limits how many are recorded. Not a filter: any category is clipped.
    priority_categories: list[str] = Field(default_factory=list)
    # Record at most this many live streamers at once (0 = no limit). Picked in order:
    # streamers in priority_categories first, then by viewer count, highest first. The rest
    # are still checked and their chat is still read, but they aren't recorded or clipped.
    max_streamers: int = Field(0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _renamed_keys(cls, data: Any) -> Any:
        # clip_categories (a clip filter) became priority_categories (recording priority)
        if isinstance(data, dict) and "clip_categories" in data:
            data = dict(data)
            old = data.pop("clip_categories")
            data.setdefault("priority_categories", old)
        return data


class Twitch(_Model):
    client_id: str = ""
    client_secret: str = ""
    live_check_interval_s: int = 20  # a stream can take a minute or two to show as live


class Detection(_Model):
    bin_s: float = Field(1, gt=0)
    smooth_s: float = Field(5, gt=0)
    baseline_window_s: float = Field(300, gt=0)  # "normal chat" = median rate over this long
    recent_baseline_s: float = Field(90, ge=0)   # a higher median over this long lifts it (0 = off)
    multiplier: float = 3.0
    z_threshold: float = 4.0
    min_msgs_per_sec: float = 2.0
    min_unique_chatters: int = 5
    end_ratio: float = 1.5
    cooldown_s: float = 90
    # A peak counts only if chat stays raised at least this long (from its rise to its
    # fall); shorter ones are one-off spikes and aren't clipped. Smoothing stretches a lone
    # burst to about smooth_s + 1 s.
    min_sustain_s: float = Field(10, ge=0)
    preroll_s: float = 8          # kept before chat starts rising (it reacts a few s late)
    postroll_s: float = 3         # kept after chat falls back...
    max_after_peak_s: float = 12  # ...but never more than this after the busiest moment
    max_clip_s: float = 35
    manual_preroll_s: float = 20  # "Clip now": seconds before the press
    manual_postroll_s: float = 11  # and after it
    ignore_users: list[str] = Field(
        default_factory=lambda: ["nightbot", "streamelements", "moobot", "fossabot"]
    )


class Capture(_Model):
    quality: str = "720p60,720p,best"
    buffer_minutes: float = 3
    snapshot_interval_s: float = 2
    low_latency: bool = True
    filter_ads: bool = True
    sync_offset_s: float = -2.0  # signed; + moves clips later. -2.0 is an estimate: tune it live


class Render(_Model):
    default_layout: LayoutName = "blur_fit"
    fit_zoom: float = 1.0
    resolution: str = "1080x1920"
    burn_title: bool = True


class TikTokAccount(_Model):
    """One TikTok account to post to. Each has its own logged-in browser profile."""

    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")  # also the profile folder name
    enabled: bool = True
    browser_profile_dir: str = ""  # "" = <data_dir>/accounts/<name>/browser_profile
    cookies_file: str = ""
    max_uploads_per_day: int = Field(6, ge=0)
    min_minutes_between_uploads: int = Field(45, ge=0)


class CategoryCaption(_Model):
    """The caption for clips from streams in one Twitch category."""

    category: str = Field(min_length=1)  # exact Twitch name, case-insensitive
    caption: str = Field(min_length=1)


class TikTok(_Model):
    browser_profile_dir: str = ""  # "" = <data_dir>/browser_profile (the "default" account)
    browser_channel: str = "chromium"
    cookies_file: str = ""
    headless: bool = False
    dry_run: bool = True
    # What a dry run does at the end: "stop" before Post (screenshot only), or "draft":
    # click Save draft, so the clip lands in TikTok's drafts but is never posted.
    dry_run_mode: Literal["stop", "draft"] = "stop"
    # {streamer} = channel name, {category} = the stream's Twitch category ("" if unknown)
    caption_template: str = "{streamer} 🔴 {category} on Twitch #{streamer} #twitch #clips"
    # Captions for particular categories; the first match wins, else caption_template.
    category_captions: list[CategoryCaption] = Field(default_factory=list)
    max_uploads_per_day: int = 6           # used by the implicit "default" account only
    min_minutes_between_uploads: int = 45  # used by the implicit "default" account only
    # Accounts to post to. Empty = one implicit account "default" that uses
    # browser_profile_dir, cookies_file and the two limits above.
    accounts: list[TikTokAccount] = Field(default_factory=list)
    # On: each upload goes to the next enabled account with a free slot, in turn.
    # Off: always the first enabled account with a free slot.
    cycle_accounts: bool = False
    # A queued clip that hasn't found a slot within this many hours is dropped (0 = never).
    max_queue_age_h: float = Field(6, ge=0)


class Region(_Model):
    """A rectangle in fractions (0-1) of the source frame."""

    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0


class Streamer(BaseModel):
    """One [[streamers]] entry. Extra keys are per-streamer detection overrides."""

    model_config = ConfigDict(extra="allow")

    name: str
    enabled: bool = True
    layout: LayoutName | None = None
    fit_zoom: float | None = None
    crop_x: float = 0.5
    stack_top: Region = Field(default_factory=lambda: Region(x=0.0, y=0.6, w=0.25, h=0.4))
    stack_bottom: Region = Field(default_factory=lambda: Region(x=0.15, y=0.0, w=0.7, h=1.0))
    stack_split: float = 0.4  # share of the output height given to stack_top


class LayoutConfig(_Model):
    """Everything the renderer needs to lay out one streamer's clip."""

    layout: LayoutName = "blur_fit"
    fit_zoom: float = 1.0
    crop_x: float = 0.5
    stack_top: Region = Field(default_factory=Region)
    stack_bottom: Region = Field(default_factory=Region)
    stack_split: float = 0.4


class Config(_Model):
    general: General = Field(default_factory=General)
    twitch: Twitch = Field(default_factory=Twitch)
    detection: Detection = Field(default_factory=Detection)
    capture: Capture = Field(default_factory=Capture)
    render: Render = Field(default_factory=Render)
    tiktok: TikTok = Field(default_factory=TikTok)
    streamers: list[Streamer] = Field(default_factory=list)

    # ---- helpers -------------------------------------------------------------
    @property
    def data_dir(self) -> Path:
        return resolve_path(self.general.data_dir)

    def caption_for(self, streamer: str, category: str = "") -> str:
        """The caption for a new clip of ``streamer`` while it streams ``category``."""
        tk = self.tiktok
        template = next((c.caption for c in tk.category_captions
                         if c.category.strip().lower() == category.strip().lower() and category.strip()),
                        tk.caption_template)
        try:
            text = template.format(streamer=streamer, category=category.strip())
        except (KeyError, IndexError, ValueError):
            text = template  # a template with stray braces is used as written
        return re.sub(r"[ \t]{2,}", " ", text).strip()

    def tiktok_accounts(self) -> list[TikTokAccount]:
        """The configured accounts, or the implicit "default" one when none are listed.
        ``browser_profile_dir`` is always filled in (expanded, absolute)."""
        tk = self.tiktok
        accounts = tk.accounts or [TikTokAccount(
            name="default", browser_profile_dir=tk.browser_profile_dir or str(self.data_dir / "browser_profile"),
            cookies_file=tk.cookies_file,
            max_uploads_per_day=tk.max_uploads_per_day,
            min_minutes_between_uploads=tk.min_minutes_between_uploads)]
        out = []
        for a in accounts:
            d = a.browser_profile_dir or str(self.data_dir / "accounts" / a.name / "browser_profile")
            out.append(a.model_copy(update={"browser_profile_dir": str(resolve_path(d))}))
        return out

    def enabled_streamers(self) -> list[Streamer]:
        return [s for s in self.streamers if s.enabled]

    def streamer(self, name: str) -> Streamer | None:
        low = name.lower()
        return next((s for s in self.streamers if s.name.lower() == low), None)

    def detection_for(self, name: str) -> Detection:
        """Global detection settings with this streamer's overrides merged in."""
        s = self.streamer(name)
        if s is None or not s.model_extra:
            return self.detection
        overrides = {k: v for k, v in s.model_extra.items() if k in Detection.model_fields}
        return Detection.model_validate({**self.detection.model_dump(), **overrides})

    def layout_for(self, name: str, layout: str | None = None) -> LayoutConfig:
        """Layout settings for a streamer. ``layout`` overrides the configured one."""
        s = self.streamer(name) or Streamer(name=name)
        return LayoutConfig(
            layout=layout or s.layout or self.render.default_layout,
            fit_zoom=s.fit_zoom if s.fit_zoom is not None else self.render.fit_zoom,
            crop_x=s.crop_x,
            stack_top=s.stack_top,
            stack_bottom=s.stack_bottom,
            stack_split=s.stack_split,
        )


def _deep_merge(doc: Any, patch: dict[str, Any]) -> None:
    """Merge ``patch`` into a tomlkit document in place, keeping comments."""
    for key, value in patch.items():
        if isinstance(value, dict) and key in doc and isinstance(doc[key], dict):
            _deep_merge(doc[key], value)
        else:
            doc[key] = value


class ConfigStore:
    """Loads, serves and saves the config.

    ``update(patch)`` takes a nested dict shaped like the TOML file, for example
    ``{"general": {"auto_upload": True}}``. The ``streamers`` list is replaced as a
    whole when present. The file is saved with tomlkit so comments survive.
    """

    def __init__(self, path: str | Path | None = None, bus: EventBus | None = None,
                 config: Config | None = None) -> None:
        self.path = Path(path) if path else None
        self.bus = bus
        self._doc: Any = tomlkit.document()
        if config is not None:
            self._config = config
        elif self.path and self.path.exists():
            text = self.path.read_text(encoding="utf-8")
            self._doc = tomlkit.parse(text)
            self._config = Config.model_validate(tomllib.loads(text))
        else:
            self._config = Config()

    def get(self) -> Config:
        return self._config

    def update(self, patch: dict[str, Any]) -> Config:
        """Validate, apply, save and announce a change. Raises ValidationError if bad."""
        merged = self._config.model_dump(exclude_none=True)
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        new = Config.model_validate(merged)
        self._config = new
        if self.path:
            _deep_merge(self._doc, patch)
            tmp = self.path.with_suffix(".toml.tmp")
            tmp.write_text(tomlkit.dumps(self._doc), encoding="utf-8")
            tmp.replace(self.path)
        if self.bus:
            self.bus.publish(Topic.CONFIG_CHANGED, {"patch": patch})
        return new
