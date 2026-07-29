"""SQLModel table definitions — the fixed data model (spec section 6).

SQLite has no native array type, so list-valued columns (character_ids,
character_assignments, variant_paths) are stored as JSON via
`sa_column=Column(JSON)`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import Column, JSON
from sqlmodel import Field, SQLModel

from .config import SUBTITLE_POSITION_DEFAULT


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
    # A deterministic, programmatically-rendered animation (math/science
    # diagrams, curves, counters) instead of AI image/video. The visual is
    # produced from a typed spec whose cues sync to the narration timestamps.
    ANIMATION = "animation"


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #
class Project(SQLModel, table=True):
    __tablename__ = "projects"

    id: str = Field(default_factory=_uuid, primary_key=True)
    title: str
    # A generated script title may replace the initial working title only when
    # the creator did not explicitly provide one.
    title_is_custom: bool = False
    topic_prompt: str
    platform_preset_id: Optional[str] = Field(default=None, foreign_key="platform_presets.id")
    content_preset_id: Optional[str] = Field(default=None, foreign_key="content_presets.id")
    target_duration_seconds: int = 75
    music_track_id: Optional[str] = Field(default=None, foreign_key="music_tracks.id")
    music_enabled: bool = True
    music_volume: float = 0.075
    # Burned-in subtitles. Both live on the project (never on a preset or a
    # global setting) so a placement chosen here is scoped to this render only
    # and every new project starts from SUBTITLE_POSITION_DEFAULT again.
    subtitles_enabled: bool = True
    subtitle_position: float = SUBTITLE_POSITION_DEFAULT
    title_card_path: Optional[str] = None
    title_card_source_path: Optional[str] = None
    title_card_kicker: str = ""
    title_card_text: str = ""
    title_card_part_label: str = ""
    title_card_prompt: str = ""
    title_card_status: str = "pending"  # pending | generating | ready | failed
    title_card_version: int = 0
    title_card_variants: list[dict] = Field(default_factory=list, sa_column=Column(JSON))
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
    # For video scenes, optionally animate toward the following scene's still.
    use_next_scene_as_end_frame: bool = True
    character_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    character_assignments: list[dict] = Field(default_factory=list, sa_column=Column(JSON))
    # Names the LLM flagged that don't yet exist in the global library.
    suggested_characters: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    excluded_context_scene_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    audio_path: Optional[str] = None
    timestamps_path: Optional[str] = None
    image_path: Optional[str] = None
    clip_path: Optional[str] = None
    # For scene_type == animation: the typed, engine-agnostic animation spec
    # (template id + params + narration-anchored cues) and its rendered clip.
    animation_spec: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    animation_path: Optional[str] = None
    # Every generated candidate is retained. The singular paths above point at
    # the currently selected candidates used by downstream pipeline stages.
    image_variants: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    clip_variants: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    animation_variants: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # Persisted fal queue state so a timeout/restart can resume without
    # submitting a duplicate paid generation.
    video_request_id: Optional[str] = None
    video_request_status: Optional[str] = None
    video_request_status_url: Optional[str] = None
    video_request_response_url: Optional[str] = None
    audio_variants: list[dict] = Field(default_factory=list, sa_column=Column(JSON))
    duration_seconds: Optional[float] = None
    asset_version: int = 0
    approved: bool = False
    # Per-scene generation status, so the UI can show a spinner on one card.
    status: str = "pending"  # pending | generating | ready | failed


class Character(SQLModel, table=True):
    __tablename__ = "characters"

    id: str = Field(default_factory=_uuid, primary_key=True)
    # Characters belong to exactly one group (content preset), so a cast lookup
    # never crosses group lines — "Zeus" in the mythology group and a "Zeus" in
    # a cat-cartoon group are separate library entries. NULL means ungrouped:
    # reachable only from the library, never matched into a project's cast.
    content_preset_id: Optional[str] = Field(
        default=None, foreign_key="content_presets.id", index=True
    )
    name: str
    description: str = ""
    reference_image_path: Optional[str] = None
    reference_prompt: str = ""
    reference_style_prompt: str = ""
    reference_version: int = 0
    # Selectable generation history. `variant_paths` remains reserved for
    # supplementary identity views passed to the video model.
    reference_variants: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    variant_paths: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)


class CharacterForm(SQLModel, table=True):
    __tablename__ = "character_forms"

    id: str = Field(default_factory=_uuid, primary_key=True)
    character_id: str = Field(foreign_key="characters.id", index=True)
    name: str = "Default"
    state: str = ""
    description: str = ""
    reference_image_path: Optional[str] = None
    reference_prompt: str = ""
    reference_style_prompt: str = ""
    reference_version: int = 0
    reference_variants: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    variant_paths: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    trigger_phrases: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    is_default: bool = False
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
    # The same idea for animation scenes: a house style handed to the Manim
    # authoring call so diagrams carry the group's palette and layout instead of
    # the generic catalog defaults. Separate from image_style_prompt because the
    # two describe different media (a rendered photo vs. drawn geometry) and
    # neither reaches the script LLM.
    animation_style_prompt: str = ""
    voice_id: str = ""
    # When on, the script LLM may mark scenes as deterministic animations and
    # emit an animation spec for them. Off by default so narrative/photographic
    # presets (e.g. mythology) never get diagrams sprinkled in.
    enable_animations: bool = False
    # Which linked TikTok account this group publishes as. Groups pick from the
    # accounts already in the app, so one login can serve several groups.
    tiktok_account_id: Optional[str] = Field(default=None, foreign_key="tiktok_accounts.id")
    # The same, per destination: a group can publish to TikTok, to YouTube, to
    # both, or to neither, so the two selections are independent.
    youtube_account_id: Optional[str] = Field(default=None, foreign_key="youtube_accounts.id")
    is_default: bool = False


class TikTokAccount(SQLModel, table=True):
    """One authorized TikTok creator account, owned by the app, not by a group.

    Accounts are linked once and then *selected* by any number of groups, so a
    second group that posts as the same creator never re-runs the OAuth flow.
    """

    __tablename__ = "tiktok_accounts"

    id: str = Field(default_factory=_uuid, primary_key=True)
    # TikTok's stable per-app user identifier — the key we de-duplicate on.
    open_id: str = Field(index=True)
    union_id: str = ""
    display_name: str = ""
    avatar_url: str = ""
    access_token: str = ""
    refresh_token: str = ""
    # Access tokens last ~24h and refresh tokens ~365 days; both are refreshed
    # ahead of expiry, and the refresh token is replaced by whatever comes back.
    access_expires_at: Optional[datetime] = None
    refresh_expires_at: Optional[datetime] = None
    scopes: str = ""
    last_error: str = ""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class YouTubeAccount(SQLModel, table=True):
    """One authorized YouTube channel, owned by the app, not by a group.

    Mirrors ``TikTokAccount``: linked once, then selected by any number of
    groups. The differences are Google's, not ours — a refresh token has no
    advertised lifetime (so ``refresh_expires_at`` stays empty and a dead token
    only shows up as an ``invalid_grant`` on use), and a refresh response does
    not normally return a replacement refresh token.
    """

    __tablename__ = "youtube_accounts"

    id: str = Field(default_factory=_uuid, primary_key=True)
    # The channel id (UC...) — stable per channel, so it is what we de-duplicate
    # on. A Google account with several channels links each one separately.
    channel_id: str = Field(index=True)
    title: str = ""
    handle: str = ""
    avatar_url: str = ""
    access_token: str = ""
    refresh_token: str = ""
    access_expires_at: Optional[datetime] = None
    refresh_expires_at: Optional[datetime] = None
    scopes: str = ""
    # Set when Google rejects the refresh token, which is the one state the
    # creator has to fix by linking the channel again.
    needs_relink: bool = False
    last_error: str = ""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class MusicTrack(SQLModel, table=True):
    __tablename__ = "music_tracks"

    id: str = Field(default_factory=_uuid, primary_key=True)
    provider: str = "jamendo"
    provider_track_id: str = Field(index=True)
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
    local_path: Optional[str] = None
    created_at: datetime = Field(default_factory=_now)


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
    plan_id: Optional[str] = Field(default=None, foreign_key="editorial_plans.id", index=True)
    created_at: datetime = Field(default_factory=_now)


class EditorialPlan(SQLModel, table=True):
    """A reusable editorial context for an ordered body of work."""

    __tablename__ = "editorial_plans"

    id: str = Field(default_factory=_uuid, primary_key=True)
    name: str
    description: str = ""
    editorial_rules: str = ""
    ordering_mode: str = "custom"
    parent_plan_id: Optional[str] = Field(default=None, foreign_key="editorial_plans.id", index=True)
    platform_preset_id: Optional[str] = Field(default=None, foreign_key="platform_presets.id")
    content_preset_id: Optional[str] = Field(default=None, foreign_key="content_presets.id")
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class EditorialItem(SQLModel, table=True):
    """One planned, external, completed, or AI-suggested work in a plan."""

    __tablename__ = "editorial_items"

    id: str = Field(default_factory=_uuid, primary_key=True)
    plan_id: str = Field(foreign_key="editorial_plans.id", index=True)
    title: str
    summary: str = ""
    coverage_summary: str = ""
    notes: str = ""
    status: str = "planned"
    source_type: str = "manual"
    order_index: int = 0
    target_duration_seconds: int = 75
    project_id: Optional[str] = Field(default=None, foreign_key="projects.id")
    external_url: str = ""
    part_group_id: Optional[str] = Field(default=None, index=True)
    part_group_title: str = ""
    part_number: Optional[int] = None
    ai_rationale: str = ""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
