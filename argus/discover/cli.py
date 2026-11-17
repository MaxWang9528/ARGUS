"""``./lw discover [--category NAME] [--top N]``: print the top live streams once.

Uses the same settings as ``[discovery]`` (languages, min_viewers, exclude); the flags
override the categories and the count. Handy to test the Twitch keys.
"""

from __future__ import annotations

import argparse
import sys

import httpx

from ..core.config import ConfigStore
from .helix import SETUP_HINT, HelixClient, HelixError
from .ranking import rank_streams


async def main(args: argparse.Namespace, *, http: httpx.AsyncClient | None = None) -> int:
    store: ConfigStore = args.store
    cfg = store.get()
    d = cfg.discovery
    cid, secret = cfg.twitch_credentials()
    if not (cid and secret):
        print(SETUP_HINT, file=sys.stderr)
        return 2
    categories = args.category if args.category else d.categories
    top = args.top if args.top else d.top_n
    exclude = list(d.exclude)
    client = HelixClient(cid, secret, http=http)
    try:
        ids: list[str] = []
        if categories:
            found = await client.game_ids(categories)
            for name in categories:
                if name.lower() not in found:
                    print(f"unknown category (use the exact Twitch name): {name}", file=sys.stderr)
            ids = [gid for gid, _ in found.values()]
            if not ids:
                return 1
        items = await client.top_streams(game_ids=ids, languages=d.languages, min_viewers=d.min_viewers,
                                         limit=top + len(exclude) + 20)
    except (HelixError, httpx.HTTPError) as exc:
        print(f"Twitch request failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.aclose()
    ranked = rank_streams(items, top_n=top, min_viewers=d.min_viewers, exclude=exclude,
                          languages=d.languages, category_ids=ids)
    scope = ", ".join(categories) if categories else "all categories"
    langs = ", ".join(d.languages) if d.languages else "any language"
    print(f"Top {len(ranked)} live streams ({scope}; {langs}):")
    for i, s in enumerate(ranked, 1):
        title = s.title if len(s.title) <= 60 else s.title[:57] + "..."
        print(f"{i:>3}. {s.login:<25} {s.viewers:>8,} viewers  {s.category[:30]:<30} {title}")
    if not ranked:
        print("  (none)")
    return 0
