"""FastAPI application entrypoint."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import init_db
from .events import bus
from .routers import characters, ideas, media, presets, projects, voices
from .routers import settings as settings_router
from .seed import seed

app = FastAPI(title="AI Video Pipeline Studio")

# Dev convenience: Vite dev server (5173) talks to this backend directly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects.router)
app.include_router(characters.router)
app.include_router(presets.router)
app.include_router(settings_router.router)
app.include_router(ideas.router)
app.include_router(voices.router)
app.include_router(media.router)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    seed()
    bus.bind_loop(asyncio.get_event_loop())


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await bus.connect(ws)
    try:
        while True:
            await ws.receive_text()  # keep-alive; client may ping
    except WebSocketDisconnect:
        bus.disconnect(ws)
    except Exception:  # noqa: BLE001
        bus.disconnect(ws)


# --------------------------------------------------------------------------- #
# Serve the built frontend (single-command production run). During development
# the frontend runs under Vite; this block is a no-op until `npm run build`.
# --------------------------------------------------------------------------- #
_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        candidate = _DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_DIST / "index.html")
