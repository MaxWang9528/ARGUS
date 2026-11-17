"""Chat logs: one ``ChatMessage`` JSON per line in
``data_dir/chatlogs/<streamer>/<YYYY-MM-DD>.jsonl`` (UTC date of the message).

``ChatLogRecorder`` subscribes to ``Topic.CHAT`` and appends; ``read_chat_log``
yields the messages back (for ``./lw replay``).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import IO

from pydantic import ValidationError

from ..core.config import ConfigStore
from ..core.events import EventBus, Topic
from ..core.models import ChatMessage

log = logging.getLogger("argus.chat")


def chatlog_path(data_dir: Path, streamer: str, ts: float) -> Path:
    day = time.strftime("%Y-%m-%d", time.gmtime(ts))
    return data_dir / "chatlogs" / streamer.lower() / f"{day}.jsonl"


class ChatLogRecorder:
    """Appends every chat message to its streamer's log for the day."""

    def __init__(self, *, store: ConfigStore, bus: EventBus) -> None:
        self.store, self.bus = store, bus
        self._files: dict[str, tuple[Path, IO[str]]] = {}
        self.written = 0

    def write(self, msg: ChatMessage) -> None:
        path = chatlog_path(self.store.get().data_dir, msg.streamer, msg.ts)
        current = self._files.get(msg.streamer)
        if current is None or current[0] != path:
            if current is not None:
                current[1].close()
            path.parent.mkdir(parents=True, exist_ok=True)
            current = (path, path.open("a", encoding="utf-8"))
            self._files[msg.streamer] = current
        current[1].write(msg.model_dump_json() + "\n")
        self.written += 1

    def flush(self) -> None:
        for _, fh in self._files.values():
            fh.flush()

    def close(self) -> None:
        for _, fh in self._files.values():
            fh.close()
        self._files.clear()

    async def run(self) -> None:
        sub = self.bus.subscribe(Topic.CHAT)
        try:
            while True:
                _, msg = await sub.get()
                try:
                    self.write(msg)
                    while not sub.queue.empty():
                        self.write(sub.queue.get_nowait()[1])
                    self.flush()
                except OSError as exc:
                    log.warning("can't write chat log: %s", exc, extra={"streamer": msg.streamer})
        finally:
            sub.close()
            self.close()


def read_chat_log(path: str | Path) -> Iterator[ChatMessage]:
    """Yield the messages in a JSONL chat log, skipping blank or broken lines."""
    with Path(path).open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield ChatMessage.model_validate(json.loads(line))
            except (json.JSONDecodeError, ValidationError):
                log.debug("skipping bad line %d in %s", n, path)
