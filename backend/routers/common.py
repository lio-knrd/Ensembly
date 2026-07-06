"""Shared router helpers."""
from __future__ import annotations

import json
from pathlib import Path

from sqlmodel import Session, select

from ..config import settings
from ..models import ContentPreset, PlatformPreset, Setting


def get_setting(session: Session, key: str, default=None):
    row = session.get(Setting, key)
    if row is None:
        return default
    try:
        return json.loads(row.value)
    except json.JSONDecodeError:
        return row.value


def set_setting(session: Session, key: str, value) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=json.dumps(value)))
    else:
        row.value = json.dumps(value)
        session.add(row)


def default_platform_preset(session: Session) -> PlatformPreset | None:
    row = session.exec(select(PlatformPreset).where(PlatformPreset.is_default == True)).first()  # noqa: E712
    return row or session.exec(select(PlatformPreset)).first()


def default_content_preset(session: Session) -> ContentPreset | None:
    row = session.exec(select(ContentPreset).where(ContentPreset.is_default == True)).first()  # noqa: E712
    return row or session.exec(select(ContentPreset)).first()


def project_metadata(folder_path: str | None) -> dict:
    if not folder_path:
        return {}
    root = settings.projects_dir.parent.parent
    script = root / folder_path / "script.json"
    if script.exists():
        try:
            return json.loads(script.read_text(encoding="utf-8")).get("metadata", {})
        except json.JSONDecodeError:
            return {}
    return {}
