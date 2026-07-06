"""Live progress broadcasting over WebSocket.

Background pipeline tasks push small JSON events here; connected browsers
receive them so the UI reflects generation state live instead of freezing.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket


class EventBus:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)

    async def _broadcast(self, message: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    def publish(self, event_type: str, **data: Any) -> None:
        """Thread-safe publish — callable from background worker threads."""
        message = {"type": event_type, **data}
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._broadcast(message), self._loop)


bus = EventBus()
