"""On-disk folder-structure helpers (spec section 7).

Characters are global (under CHARACTERS_DIR); projects reference them and never
copy character images into project folders.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .config import settings


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-") or "untitled"


def project_folder(title: str, created: date | None = None) -> Path:
    created = created or date.today()
    name = f"{created.isoformat()}_{slugify(title)}"
    folder = settings.projects_dir / name
    for sub in ("audio", "images", "clips", "final"):
        (folder / sub).mkdir(parents=True, exist_ok=True)
    return folder


def character_folder(name: str) -> Path:
    folder = settings.characters_dir / slugify(name)
    (folder / "variants").mkdir(parents=True, exist_ok=True)
    return folder


def relative_to_root(path: Path | str) -> str:
    """Store paths relative to the project root for portability."""
    p = Path(path).resolve()
    try:
        return str(p.relative_to(settings.projects_dir.parent.parent))
    except ValueError:
        return str(p)
