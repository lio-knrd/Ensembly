"""Serve generated media (images, audio, clips, final videos).

Paths are stored relative to the project root and always live under /data, so
requests are confined to the data directory (no traversal outside it, and the
`.env` at the root is never reachable).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..config import settings

router = APIRouter(prefix="/media", tags=["media"])

_ROOT = settings.projects_dir.parent.parent
_DATA = (_ROOT / "data").resolve()


@router.get("/{rel_path:path}")
def serve(rel_path: str):
    target = (_ROOT / rel_path).resolve()
    if _DATA not in target.parents and target != _DATA:
        raise HTTPException(403, "Forbidden")
    if not target.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(target)
