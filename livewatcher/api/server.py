"""The dashboard's FastAPI app: the JSON API under ``/api`` and the page in ``web/``.

``app.py`` calls ``create_app(app)`` with the running ``App`` and serves the result
with uvicorn on ``general.dashboard_host:dashboard_port`` (127.0.0.1 by default).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..app import ClipError
from . import accounts, routes, ws

if TYPE_CHECKING:
    from ..app import App

WEB_DIR = Path(__file__).resolve().parents[2] / "web"

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class SameOriginGuard:
    """Refuses cross-site writes and WebSocket connections.

    The dashboard binds to localhost, but any web page the user has open could
    still send a simple POST (approve, clip now) or open the WebSocket. Browsers
    always send ``Origin`` on those, so a request whose Origin doesn't match the
    Host it was sent to is turned away. Tools that send no Origin (curl, tests)
    are let through.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            is_write = scope["type"] == "websocket" or scope.get("method", "GET") not in _SAFE_METHODS
            if is_write and not _same_origin(scope):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                response = JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _same_origin(scope: Scope) -> bool:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    origin = headers.get("origin")
    if not origin:
        return True
    return urlsplit(origin).netloc.lower() == headers.get("host", "").lower()


class NoCacheStatic:
    """Adds ``Cache-Control: no-cache`` to the page's own files so an updated
    ``app.js`` is picked up on reload (the browser still revalidates with ETags)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                headers.append((b"cache-control", b"no-cache"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_header)


def create_app(app: App, *, web_dir: Path | None = None) -> FastAPI:
    """Build the dashboard for one running ``App``."""
    api = FastAPI(title="livewatcher", docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    api.state.lw = app

    @api.exception_handler(ClipError)
    async def _clip_error(request: Request, exc: ClipError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    api.include_router(routes.router, prefix="/api")
    api.include_router(accounts.router, prefix="/api")
    api.include_router(ws.router, prefix="/api")
    folder = web_dir or WEB_DIR
    if folder.is_dir():
        api.mount("/", StaticFiles(directory=folder, html=True), name="web")
    api.add_middleware(NoCacheStatic)
    api.add_middleware(SameOriginGuard)
    return api
