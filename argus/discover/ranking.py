"""Pure functions: Helix ``/streams`` items plus settings -> who to watch."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DiscoveredStream:
    login: str            # lower case
    viewers: int
    category: str = ""    # Twitch game name
    category_id: str = ""
    title: str = ""
    language: str = ""
    started_at: str = ""


def parse_stream(item: Mapping[str, Any]) -> DiscoveredStream | None:
    """One Helix ``/streams`` item, or None when it isn't a usable live stream."""
    login = str(item.get("user_login") or "").strip().lower()
    if not login or item.get("type", "live") != "live":
        return None
    try:
        viewers = int(item.get("viewer_count") or 0)
    except (TypeError, ValueError):
        viewers = 0
    return DiscoveredStream(
        login=login, viewers=viewers, category=str(item.get("game_name") or ""),
        category_id=str(item.get("game_id") or ""), title=str(item.get("title") or ""),
        language=str(item.get("language") or "").lower(), started_at=str(item.get("started_at") or ""))


def eligible_streams(items: Iterable[Mapping[str, Any]], *, min_viewers: int = 0,
                     exclude: Iterable[str] = (), languages: Iterable[str] = (),
                     category_ids: Iterable[str] = ()) -> list[DiscoveredStream]:
    """Every usable stream, most viewers first (ties by name), one per channel.

    Helix already filters by language and category; they are checked again here so a
    stale page (or a test) can't slip anything through. Empty ``languages`` or
    ``category_ids`` = any."""
    banned = {_name(n) for n in exclude}
    langs = {lang.strip().lower() for lang in languages if lang.strip()}
    cats = {str(c) for c in category_ids if str(c)}
    best: dict[str, DiscoveredStream] = {}
    for item in items:
        s = parse_stream(item)
        if s is None or s.login in banned or s.viewers < min_viewers:
            continue
        if langs and s.language not in langs:
            continue
        if cats and s.category_id not in cats:
            continue
        if s.login not in best or s.viewers > best[s.login].viewers:
            best[s.login] = s  # paging can return a channel twice as counts move
    return sorted(best.values(), key=lambda s: (-s.viewers, s.login))


def rank_streams(items: Iterable[Mapping[str, Any]], *, top_n: int, min_viewers: int = 0,
                 exclude: Iterable[str] = (), languages: Iterable[str] = (),
                 category_ids: Iterable[str] = ()) -> list[DiscoveredStream]:
    """The top ``top_n`` of ``eligible_streams``."""
    return eligible_streams(items, min_viewers=min_viewers, exclude=exclude, languages=languages,
                            category_ids=category_ids)[:max(0, top_n)]


def hold(last_in_top: Mapping[str, float], top: Iterable[str], now: float, keep_s: float,
         exclude: Iterable[str] = ()) -> dict[str, float]:
    """``keep_minutes``: {channel: last time it was in the top N}. A channel stays until
    ``keep_s`` after it left the top N (or went offline); excluded ones go at once."""
    banned = {_name(n) for n in exclude}
    out = {n: t for n, t in last_in_top.items() if now - t <= keep_s and n not in banned}
    for n in top:
        if n not in banned:
            out[n] = now
    return out


def watch_order(top: Iterable[str], held: Mapping[str, float]) -> list[str]:
    """The top N in rank order, then the held ones, most recently in the top first."""
    first = list(dict.fromkeys(top))
    rest = sorted((n for n in held if n not in first), key=lambda n: (-held[n], n))
    return first + rest


def _name(n: str) -> str:
    return n.strip().lstrip("@").lower()
