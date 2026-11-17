"""Command line. FROZEN for branch agents.

    ./lw run                 watch the streamers in config.toml
    ./lw demo                run everything on fake parts (no Twitch, no TikTok)
    ./lw replay <chat.jsonl> run a recorded chat log through the peak detector
    ./lw login-tiktok [--account NAME]  open a browser and log in to TikTok once
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import sys
from pathlib import Path

from .core.config import Config, ConfigStore, General, Streamer

DEMO_STREAMERS = ["demo_alpha", "demo_beta", "demo_gamma"]


def demo_store(data_dir: str = "clips/demo") -> ConfigStore:
    """An in-memory config for the demo. Nothing is written to config.toml."""
    return ConfigStore(config=Config(
        general=General(data_dir=data_dir),
        streamers=[Streamer(name=n) for n in DEMO_STREAMERS]))


def _load_store(path: str) -> ConfigStore:
    if not Path(path).exists():
        sys.exit(f"{path} not found. Copy config.example.toml to {path} and edit it.")
    return ConfigStore(path)


def _delegate(target: str, args: argparse.Namespace) -> int:
    """Call ``module:function(args)``; branches supply the function."""
    module, func = target.split(":")
    try:
        fn = getattr(importlib.import_module(module), func)
    except (ImportError, AttributeError) as exc:
        sys.exit(f"this command isn't built yet ({exc})")
    result = fn(args)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    return int(result or 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="argus")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="watch the streamers in the config file")
    sub.add_parser("demo", help="run on fake parts")
    p = sub.add_parser("replay", help="run a chat log through the detector")
    p.add_argument("file")
    p.add_argument("--streamer", default=None, help="only this channel")
    p = sub.add_parser("login-tiktok", help="log in to TikTok in an account's saved browser profile")
    p.add_argument("--account", default=None, help="the [[tiktok.accounts]] name (default: the first)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    if args.command in ("run", "demo"):
        from .app import App

        store = demo_store() if args.command == "demo" else _load_store(args.config)
        try:
            asyncio.run(App(store, demo=args.command == "demo").run())
        except KeyboardInterrupt:
            pass
        return 0
    if args.command == "replay":
        args.store = _load_store(args.config) if Path(args.config).exists() else ConfigStore()
        return _delegate("argus.detect.replay:main", args)
    if args.command == "login-tiktok":
        args.store = _load_store(args.config) if Path(args.config).exists() else ConfigStore()
        return _delegate("argus.upload.login:main", args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
