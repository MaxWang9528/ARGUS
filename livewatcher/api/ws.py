"""``WS /api/ws``: relays bus events to the page as ``{"topic", "data"}``.

Every topic except ``chat`` is sent (chat is far too busy and the page only needs
the rate). The bus subscription is made before the socket is accepted, so nothing
published after the handshake is missed, and closed when the socket closes.
"""

from __future__ import annotations

import contextlib
from typing import Any

import anyio
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
        # A task group (not bare asyncio tasks) so that a cancel from the server,
        # on shutdown, reaches both halves and ends the handler cleanly.
        async with anyio.create_task_group() as tg:

            async def send_events() -> None:
                try:
                    async for topic, payload in sub:
                        await websocket.send_json(encode(topic, payload))
                except (WebSocketDisconnect, RuntimeError, OSError):
                    pass  # the socket went away mid-send
                tg.cancel_scope.cancel()

            tg.start_soon(send_events)
            # The page sends nothing we need; reading is how a disconnect is noticed.
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        break
            tg.cancel_scope.cancel()
