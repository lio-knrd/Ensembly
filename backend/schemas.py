"""Request/response models for the REST API."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from .models import SceneType


# --- Projects ---
class ProjectCreate(BaseModel):
    title: str
    topic_prompt: str
    target_duration_seconds: Optional[int] = None
    platform_preset_id: Optional[str] = None
    content_preset_id: Optional[str] = None
    start: bool = True  # kick off script generation immediately


class ProjectDetail(BaseModel):
    project: dict
    scenes: list[dict]
    metadata: dict
    characters: list[dict]


# --- Scenes ---
class SceneUpdate(BaseModel):
    narration_text: Optional[str] = None
    image_prompt: Optional[str] = None
    scene_type: Optional[SceneType] = None
    approved: Optional[bool] = None
    character_ids: Optional[list[str]] = None


# --- Characters ---
class CharacterCreate(BaseModel):
    name: str
    description: str = ""
    generate_reference: bool = False  # generate a reference image from description


class CharacterUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


# --- Presets ---
class PlatformPresetIn(BaseModel):
    name: str
    format_prompt: str = ""
    is_default: bool = False


class ContentPresetIn(BaseModel):
    name: str
    content_prompt: str = ""
    image_style_prompt: str = ""
    voice_id: str = ""
    is_default: bool = False


# --- Cast / character sheets (pre-storyboard review) ---
class CastSheetGenerate(BaseModel):
    name: str
    description: Optional[str] = None  # appearance notes; blank = derive
    prompt: Optional[str] = None  # full override of the reference-image prompt
    generate_description: bool = False  # let the LLM write the description first


# --- Ideas ---
class IdeaIn(BaseModel):
    text: str
    target_duration_seconds: Optional[int] = None
    notes: Optional[str] = None


class IdeaConvert(BaseModel):
    title: Optional[str] = None
    platform_preset_id: Optional[str] = None
    content_preset_id: Optional[str] = None
    start: bool = True


# --- Settings ---
class SettingsUpdate(BaseModel):
    default_duration_seconds: Optional[int] = None
    default_platform_preset_name: Optional[str] = None
    default_content_preset_name: Optional[str] = None
    active_image_model: Optional[str] = None
