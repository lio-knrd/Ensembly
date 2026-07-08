"""SQLModel table definitions — the fixed data model (spec section 6).

SQLite has no native array type, so list-valued columns (character_ids,
variant_paths) are stored as JSON via `sa_column=Column(JSON)`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import Column, JSON
from sqlmodel import Field, SQLModel


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Stage(str, Enum):
    """Pipeline stages (state machine, spec section 8)."""

    IDEA = "IDEA"
    SCRIPT_GENERATING = "SCRIPT_GENERATING"
    SCRIPT_READY = "SCRIPT_READY"
    SCRIPT_APPROVED = "SCRIPT_APPROVED"
    AUDIO_GENERATING = "AUDIO_GENERATING"
    CAST_REVIEW = "CAST_REVIEW"
    STORYBOARD_GENERATING = "STORYBOARD_GENERATING"
    STORYBOARD_READY = "STORYBOARD_READY"
    STORYBOARD_APPROVED = "STORYBOARD_APPROVED"
    CLIPS_GENERATING = "CLIPS_GENERATING"
    CLIPS_READY = "CLIPS_READY"
    CLIPS_APPROVED = "CLIPS_APPROVED"
    RENDERING = "RENDERING"
    DONE = "DONE"
    CANCELED = "CANCELED"
    FAILED = "FAILED"


class SceneType(str, Enum):
    STILL = "still"
    VIDEO = "video"


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #
class Project(SQLModel, table=True):
    __tablename__ = "projects"

    id: str = Field(default_factory=_uuid, primary_key=True)
    title: str
    topic_prompt: str
    platform_preset_id: Optional[str] = Field(default=None, foreign_key="platform_presets.id")
    content_preset_id: Optional[str] = Field(default=None, foreign_key="content_presets.id")
    target_duration_seconds: int = 75
    stage: Stage = Field(default=Stage.IDEA)
    folder_path: Optional[str] = None
    # Free-form status text surfaced during long-running generation, plus any error.
    status_message: Optional[str] = None
    error: Optional[str] = None
    failed_stage: Optional[str] = None
    cancel_requested: bool = False
    canceled_stage: Optional[str] = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class Scene(SQLModel, table=True):
    __tablename__ = "scenes"

    id: str = Field(default_factory=_uuid, primary_key=True)
    project_id: str = Field(foreign_key="projects.id", index=True)
    order_index: int = 0
    narration_text: str = ""
    image_prompt: str = ""
    continuity_context: list[dict] = Field(default_factory=list, sa_column=Column(JSON))
    scene_type: SceneType = Field(default=SceneType.STILL)
    character_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # Names the LLM flagged that don't yet exist in the global library.
    suggested_characters: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    excluded_context_scene_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    audio_path: Optional[str] = None
    timestamps_path: Optional[str] = None
    image_path: Optional[str] = None
    clip_path: Optional[str] = None
    duration_seconds: Optional[float] = None
    asset_version: int = 0
    approved: bool = False
    # Per-scene generation status, so the UI can show a spinner on one card.
    status: str = "pending"  # pending | generating | ready | failed


class Character(SQLModel, table=True):
    __tablename__ = "characters"

    id: str = Field(default_factory=_uuid, primary_key=True)
    name: str
    description: str = ""
    reference_image_path: Optional[str] = None
    reference_prompt: str = ""
    reference_style_prompt: str = ""
    reference_version: int = 0
    variant_paths: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)


class ProjectCharacter(SQLModel, table=True):
    __tablename__ = "project_characters"

    project_id: str = Field(foreign_key="projects.id", primary_key=True)
    character_id: str = Field(foreign_key="characters.id", primary_key=True)


class PlatformPreset(SQLModel, table=True):
    __tablename__ = "platform_presets"

    id: str = Field(default_factory=_uuid, primary_key=True)
    name: str
    format_prompt: str = ""
    is_default: bool = False


class ContentPreset(SQLModel, table=True):
    __tablename__ = "content_presets"

    id: str = Field(default_factory=_uuid, primary_key=True)
    name: str
    content_prompt: str = ""
    # Extra, separate option: a visual style applied to image generation only
    # (character reference sheets + scene images). NOT sent to the script LLM,
    # so scene image_prompts stay clean and the look stays consistent.
    image_style_prompt: str = ""
    voice_id: str = ""
    is_default: bool = False


class Setting(SQLModel, table=True):
    __tablename__ = "settings"

    key: str = Field(primary_key=True)
    value: str = ""  # JSON-encoded value


class Idea(SQLModel, table=True):
    """Idea backlog — queued topics waiting to become projects (spec section 10.4)."""

    __tablename__ = "ideas"

    id: str = Field(default_factory=_uuid, primary_key=True)
    text: str
    target_duration_seconds: int = 75
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=_now)
