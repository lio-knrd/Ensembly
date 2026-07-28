"""Request/response models for the REST API."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from .models import SceneType


# --- Projects ---
class ProjectCreate(BaseModel):
    title: str = ""
    title_is_custom: Optional[bool] = None
    topic_prompt: str
    target_duration_seconds: Optional[int] = None
    platform_preset_id: Optional[str] = None
    content_preset_id: Optional[str] = None
    start: bool = True  # kick off script generation immediately


class ProjectScopeAnalyze(BaseModel):
    title: str = ""
    topic_prompt: str
    target_duration_seconds: Optional[int] = None
    platform_preset_id: Optional[str] = None
    content_preset_id: Optional[str] = None


class ProjectSplitCreate(ProjectScopeAnalyze):
    analysis: dict
    start: bool = True


class TitleCardGenerate(BaseModel):
    mode: Literal["reuse", "generate"] = "reuse"
    scene_id: Optional[str] = None
    kicker: str = ""
    text: str = ""
    part_label: str = ""
    prompt: str = ""


class TitleCardUpdate(BaseModel):
    kicker: Optional[str] = None
    text: Optional[str] = None
    part_label: Optional[str] = None
    prompt: Optional[str] = None


class TitleCardSelect(BaseModel):
    path: str


class ProjectDetail(BaseModel):
    project: dict
    scenes: list[dict]
    metadata: dict
    characters: list[dict]


class ProjectMusicUpdate(BaseModel):
    enabled: Optional[bool] = None
    volume: Optional[float] = None


class ProjectSubtitlesUpdate(BaseModel):
    enabled: Optional[bool] = None
    # Fraction of the frame height from the bottom edge; clamped server-side.
    position: Optional[float] = None


class MusicTrackSelect(BaseModel):
    provider: Literal["jamendo", "local"] = "jamendo"
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
    animation_spec: Optional[dict] = None
    use_next_scene_as_end_frame: Optional[bool] = None
    approved: Optional[bool] = None
    character_ids: Optional[list[str]] = None
    character_assignments: Optional[list[dict]] = None
    excluded_context_scene_ids: Optional[list[str]] = None


class SceneAssetSelect(BaseModel):
    kind: Literal["image", "clip", "audio", "animation"]
    path: str


# --- Characters ---
class CharacterCreate(BaseModel):
    name: str
    description: str = ""
    # The group (content preset) the character belongs to. Omitted -> the
    # default group, so a caller that doesn't care still lands somewhere real.
    content_preset_id: Optional[str] = None
    generate_reference: bool = False  # generate a reference image from description


class CharacterUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    content_preset_id: Optional[str] = None


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
    enable_animations: bool = False
    is_default: bool = False


# --- TikTok ---
# The group's account is deliberately absent from ContentPresetIn: preset saves
# send a whole body, so a form that doesn't know about TikTok would clear the
# selection. It is set through /api/tiktok/groups/{id}/account instead.
class TikTokLinkStart(BaseModel):
    # When set, the newly linked account is attached to this group right away.
    content_preset_id: Optional[str] = None


class TikTokLinkComplete(BaseModel):
    # Either paste the whole URL TikTok redirected to, or pass code + state.
    redirected_url: Optional[str] = None
    code: Optional[str] = None
    state: Optional[str] = None


class TikTokAccountSelect(BaseModel):
    account_id: Optional[str] = None


class TikTokPublishIn(BaseModel):
    # "draft" sends the video to the TikTok inbox for the creator to publish
    # (works without TikTok's audit); "direct" posts straight to the profile
    # (audited apps only, otherwise forced to private).
    mode: Literal["draft", "direct"] = "draft"
    privacy_level: str = "SELF_ONLY"
    caption: Optional[str] = None
    disable_comment: bool = False
    disable_duet: bool = False
    disable_stitch: bool = False


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
    plan_id: Optional[str] = None


class IdeaConvert(BaseModel):
    title: Optional[str] = None
    platform_preset_id: Optional[str] = None
    content_preset_id: Optional[str] = None
    start: bool = True


class EditorialPlanIn(BaseModel):
    name: str
    description: str = ""
    editorial_rules: str = ""
    ordering_mode: str = "custom"
    parent_plan_id: Optional[str] = None
    platform_preset_id: Optional[str] = None
    content_preset_id: Optional[str] = None


class EditorialPlanUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    editorial_rules: Optional[str] = None
    ordering_mode: Optional[str] = None
    parent_plan_id: Optional[str] = None
    platform_preset_id: Optional[str] = None
    content_preset_id: Optional[str] = None


class EditorialItemIn(BaseModel):
    title: str
    summary: str = ""
    coverage_summary: str = ""
    notes: str = ""
    status: str = "planned"
    source_type: str = "manual"
    target_duration_seconds: Optional[int] = None
    project_id: Optional[str] = None
    external_url: str = ""
    part_group_id: Optional[str] = None
    part_group_title: str = ""
    part_number: Optional[int] = None


class EditorialItemUpdate(BaseModel):
    title: Optional[str] = None
    summary: Optional[str] = None
    coverage_summary: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None
    target_duration_seconds: Optional[int] = None
    external_url: Optional[str] = None
    part_group_title: Optional[str] = None
    part_number: Optional[int] = None


class EditorialSuggestIn(BaseModel):
    instruction: str
    count: int = 5


class EditorialReorderIn(BaseModel):
    item_ids: list[str]


class EditorialItemConvert(BaseModel):
    start: bool = True


# --- Settings ---
class SettingsUpdate(BaseModel):
    default_duration_seconds: Optional[int] = None
    default_platform_preset_name: Optional[str] = None
    default_content_preset_name: Optional[str] = None
    active_image_model: Optional[str] = None
