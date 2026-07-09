"""Request/response models for the REST API."""
from __future__ import annotations

from typing import Literal, Optional

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


class ProjectMusicUpdate(BaseModel):
    enabled: Optional[bool] = None
    volume: Optional[float] = None


class MusicTrackSelect(BaseModel):
    provider_track_id: str
    title: str
    artist_name: str = ""
    album_name: str = ""
    duration_seconds: int = 0
    license_url: str = ""
    audio_url: str = ""
    download_url: str = ""
    download_allowed: bool = False
    image_url: str = ""
    share_url: str = ""


# --- Scenes ---
class SceneUpdate(BaseModel):
    narration_text: Optional[str] = None
    image_prompt: Optional[str] = None
    continuity_context: Optional[list[dict]] = None
    scene_type: Optional[SceneType] = None
    use_next_scene_as_end_frame: Optional[bool] = None
    approved: Optional[bool] = None
    character_ids: Optional[list[str]] = None
    character_assignments: Optional[list[dict]] = None
    excluded_context_scene_ids: Optional[list[str]] = None


class SceneAssetSelect(BaseModel):
    kind: Literal["image", "clip", "audio"]
    path: str


# --- Characters ---
class CharacterCreate(BaseModel):
    name: str
    description: str = ""
    generate_reference: bool = False  # generate a reference image from description


class CharacterUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class CharacterReferenceSelect(BaseModel):
    path: str
    form_id: Optional[str] = None


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


class StyleSuggestionIn(BaseModel):
    content_prompt: str = ""
    current_style_prompt: str = ""
    guidelines: str = ""


# --- Cast / character sheets (pre-storyboard review) ---
class CastSheetGenerate(BaseModel):
    name: str
    state: Optional[str] = None
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
