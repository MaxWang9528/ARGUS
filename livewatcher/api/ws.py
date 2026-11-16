"""``WS /api/ws``: relays bus events to the page as ``{"topic", "data"}``.

Every topic except ``chat`` is sent (chat is far too busy and the page only needs
the rate). The bus subscription is made before the socket is accepted, so nothing
published after the handshake is missed, and closed when the socket closes.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from ..core.events import Topic
from .routes import scrub_patch

router = APIRouter()

RELAYED: tuple[Topic, ...] = tuple(t for t in Topic if t is not Topic.CHAT)


def encode(topic: Topic, payload: Any) -> dict[str, Any]:
    if isinstance(payload, BaseModel):
        data = payload.model_dump(mode="json")
    else:
        data = jsonable_encoder(payload)
    if topic is Topic.CONFIG_CHANGED and isinstance(data, dict):
        data = {**data, "patch": scrub_patch(data.get("patch"))}
    return {"topic": topic.value, "data": data}


@router.websocket("/ws")
async def events(websocket: WebSocket) -> None:
    lw = websocket.app.state.lw
    with lw.bus.subscribe(*RELAYED, maxsize=2000) as sub:
        await websocket.accept()

        async def send_events() -> None:
            async for topic, payload in sub:
                await websocket.send_json(encode(topic, payload))

        async def wait_for_close() -> None:
            # The page sends nothing we need; reading is how a disconnect is noticed.
            with contextlib.suppress(WebSocketDisconnect):
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return

        sender = asyncio.create_task(send_events())
        closer = asyncio.create_task(wait_for_close())
        try:
            done, _ = await asyncio.wait({sender, closer}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is sender and not task.cancelled() and task.exception() is not None:
                    exc = task.exception()
                    if not isinstance(exc, (WebSocketDisconnect, RuntimeError, OSError)):
                        raise exc
        finally:
            for task in (sender, closer):
                task.cancel()
            await asyncio.gather(sender, closer, return_exceptions=True)
