"""FastAPI application entrypoint."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import init_db
from .events import bus
from .routers import (
    characters,
    ideas,
    media,
    music,
    presets,
    projects,
    tiktok,
    voices,
    youtube,
)
from .routers import settings as settings_router
from .seed import seed

app = FastAPI(title="Ensembly")

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
app.include_router(music.router)
app.include_router(media.router)
app.include_router(tiktok.router)
app.include_router(youtube.router)


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
# the frontend runs under Vite; the index fallback is inert until `npm run build`.
# --------------------------------------------------------------------------- #
_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
# Files dropped in here are served from the site root. This is where a platform's
# domain-ownership proof goes — e.g. TikTok's URL-property signature file, which
# has to answer at `https://<host>/<their-filename>`. Kept outside frontend/dist
# because `npm run build` wipes that directory.
_VERIFICATION_DIR = settings.root_dir / "data" / "verification"

if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")


def _safe_file(root: Path, rel: str) -> Path | None:
    """Resolve ``rel`` inside ``root``, refusing anything that escapes it.

    Without the containment check a request for `../../.env` would be served
    straight off disk, since this handler matches every unclaimed path.
    """
    if not rel:
        return None
    try:
        candidate = (root / rel).resolve()
        candidate.relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    return candidate if candidate.is_file() else None


@app.get("/{full_path:path}")
def spa(full_path: str):
    proof = _safe_file(_VERIFICATION_DIR, full_path)
    if proof:
        return FileResponse(proof)
    asset = _safe_file(_DIST, full_path)
    if asset:
        return FileResponse(asset)
    index = _DIST / "index.html"
    if index.is_file():
        return FileResponse(index)
    raise HTTPException(
        503, "The frontend is not built yet. Run `npm run build` in frontend/."
    )
