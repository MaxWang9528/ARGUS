"""In-process async pub/sub. FROZEN after Phase 0.

Publish from the event loop thread only. From another thread use
``loop.call_soon_threadsafe(bus.publish, topic, payload)``.

Payloads are the pydantic models named beside each topic (or a plain dict where
noted). See docs/CONTRACTS.md.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Any, Self


class Topic(StrEnum):
    CHAT = "chat"                      # ChatMessage
    RATE = "rate"                      # RateSample
    PEAK = "peak"                      # PeakEvent (published when the peak ENDS)
    CLIP = "clip"                      # Clip (published on every status change)
    STREAM_STATUS = "stream_status"    # StreamStatus
    SNAPSHOT = "snapshot"              # {"streamer": str, "ts": float}
    CONFIG_CHANGED = "config_changed"  # {"patch": dict}
    LOG = "log"                        # LogLine
    STREAMERS_CHANGED = "streamers_changed"  # {"discovered": [names]} (stream discovery)


class Subscription:
    """An async iterator of (topic, payload). Slow readers drop their oldest items."""

    def __init__(self, bus: EventBus, topics: set[Topic], maxsize: int) -> None:
        self._bus = bus
        self.topics = topics
        self.queue: asyncio.Queue[tuple[Topic, Any]] = asyncio.Queue(maxsize=maxsize)

    def _push(self, topic: Topic, payload: Any) -> None:
        if self.queue.full():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self.queue.put_nowait((topic, payload))

    async def get(self) -> tuple[Topic, Any]:
        return await self.queue.get()

    def close(self) -> None:
        self._bus._subs.discard(self)

    def __aiter__(self) -> AsyncIterator[tuple[Topic, Any]]:
        return self

    async def __anext__(self) -> tuple[Topic, Any]:
        return await self.queue.get()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class EventBus:
    def __init__(self) -> None:
        self._subs: set[Subscription] = set()

    def subscribe(self, *topics: Topic, maxsize: int = 5000) -> Subscription:
        """Subscribe to the given topics (all topics if none are given)."""
        sub = Subscription(self, set(topics) or set(Topic), maxsize)
        self._subs.add(sub)
        return sub

    def publish(self, topic: Topic, payload: Any) -> None:
        for sub in list(self._subs):
            if topic in sub.topics:
                sub._push(topic, payload)
