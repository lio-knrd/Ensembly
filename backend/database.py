"""SQLite engine + session helpers."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from .config import settings

# Lightweight additive migrations for SQLite (create_all won't add columns to
# existing tables). Each entry: table, column, SQL type + default.
_ADDED_COLUMNS = [
    ("content_presets", "image_style_prompt", "TEXT DEFAULT ''"),
    ("content_presets", "animation_style_prompt", "TEXT DEFAULT ''"),
    ("content_presets", "voice_id", "TEXT DEFAULT ''"),
    ("characters", "reference_prompt", "TEXT DEFAULT ''"),
    ("characters", "reference_style_prompt", "TEXT DEFAULT ''"),
    ("characters", "reference_version", "INTEGER DEFAULT 0"),
    ("characters", "reference_variants", "JSON DEFAULT '[]'"),
    ("characters", "content_preset_id", "TEXT DEFAULT NULL"),
    ("character_forms", "reference_variants", "JSON DEFAULT '[]'"),
    ("scenes", "asset_version", "INTEGER DEFAULT 0"),
    ("scenes", "continuity_context", "JSON DEFAULT '[]'"),
    ("scenes", "excluded_context_scene_ids", "JSON DEFAULT '[]'"),
    ("scenes", "character_assignments", "JSON DEFAULT '[]'"),
    ("scenes", "use_next_scene_as_end_frame", "BOOLEAN DEFAULT 1"),
    ("scenes", "image_variants", "JSON DEFAULT '[]'"),
    ("scenes", "clip_variants", "JSON DEFAULT '[]'"),
    ("scenes", "video_request_id", "TEXT DEFAULT NULL"),
    ("scenes", "video_request_status", "TEXT DEFAULT NULL"),
    ("scenes", "video_request_status_url", "TEXT DEFAULT NULL"),
    ("scenes", "video_request_response_url", "TEXT DEFAULT NULL"),
    ("scenes", "audio_variants", "JSON DEFAULT '[]'"),
    ("scenes", "animation_spec", "JSON DEFAULT NULL"),
    ("scenes", "animation_path", "TEXT DEFAULT NULL"),
    ("scenes", "animation_variants", "JSON DEFAULT '[]'"),
    ("content_presets", "enable_animations", "BOOLEAN DEFAULT 0"),
    ("content_presets", "tiktok_account_id", "TEXT DEFAULT NULL"),
    ("projects", "failed_stage", "TEXT DEFAULT NULL"),
    ("projects", "cancel_requested", "BOOLEAN DEFAULT 0"),
    ("projects", "canceled_stage", "TEXT DEFAULT NULL"),
    ("projects", "music_track_id", "TEXT DEFAULT NULL"),
    ("projects", "music_enabled", "BOOLEAN DEFAULT 1"),
    ("projects", "music_volume", "REAL DEFAULT 0.075"),
    ("projects", "subtitles_enabled", "BOOLEAN DEFAULT 1"),
    ("projects", "subtitle_position", "REAL DEFAULT 0.128"),
    ("projects", "title_is_custom", "BOOLEAN DEFAULT 0"),
    ("projects", "title_card_path", "TEXT DEFAULT NULL"),
    ("projects", "title_card_source_path", "TEXT DEFAULT NULL"),
    ("projects", "title_card_kicker", "TEXT DEFAULT ''"),
    ("projects", "title_card_text", "TEXT DEFAULT ''"),
    ("projects", "title_card_part_label", "TEXT DEFAULT ''"),
    ("projects", "title_card_prompt", "TEXT DEFAULT ''"),
    ("projects", "title_card_status", "TEXT DEFAULT 'pending'"),
    ("projects", "title_card_version", "INTEGER DEFAULT 0"),
    ("projects", "title_card_variants", "JSON DEFAULT '[]'"),
    ("ideas", "plan_id", "TEXT DEFAULT NULL"),
]

# check_same_thread=False so background tasks (different thread) can use sessions.
engine = create_engine(
    f"sqlite:///{settings.db_file}",
    echo=False,
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    settings.ensure_dirs()
    # Import models so their tables register on SQLModel.metadata.
    from . import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    _run_migrations()
    _rename_legacy_values()


def _rename_legacy_values() -> None:
    """Carry data written under the app's former name (Mythforge) forward.

    ``source_type`` marks which editorial items were produced in this app, and
    the value is shown in the UI, so old rows would otherwise read "mythforge"
    forever. The UPDATE is idempotent, so it can run on every start.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE editorial_items SET source_type = 'ensembly' "
                "WHERE source_type = 'mythforge'"
            )
        )


def _run_migrations() -> None:
    with engine.begin() as conn:
        for table, column, coldef in _ADDED_COLUMNS:
            cols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            if column not in cols:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}"))
                # Titles that predate this flag may have been explicitly chosen;
                # preserve them instead of treating every legacy row as AI-owned.
                if table == "projects" and column == "title_is_custom":
                    conn.execute(text("UPDATE projects SET title_is_custom = 1"))
                if table == "characters" and column == "content_preset_id":
                    _backfill_character_groups(conn)


def _backfill_character_groups(conn) -> None:
    """Give every pre-existing (global) character a group.

    A character's group is inferred from the projects that already use it: both
    the explicit project_characters links and the per-scene character_ids lists.
    Where a character was used across groups the most-used one wins; characters
    no project ever used fall back to the default content preset.
    """
    import json
    from collections import Counter

    project_group = {
        row[0]: row[1]
        for row in conn.execute(text("SELECT id, content_preset_id FROM projects"))
        if row[1]
    }
    votes: dict[str, Counter] = {}

    def vote(character_id: str, project_id: str) -> None:
        group = project_group.get(project_id)
        if group:
            votes.setdefault(character_id, Counter())[group] += 1

    for character_id, project_id in conn.execute(
        text("SELECT character_id, project_id FROM project_characters")
    ):
        vote(character_id, project_id)
    for project_id, character_ids in conn.execute(
        text("SELECT project_id, character_ids FROM scenes")
    ):
        for character_id in json.loads(character_ids or "[]"):
            vote(character_id, project_id)

    fallback_row = conn.execute(
        text("SELECT id FROM content_presets ORDER BY is_default DESC, name LIMIT 1")
    ).first()
    fallback = fallback_row[0] if fallback_row else None

    for row in conn.execute(text("SELECT id FROM characters")):
        character_id = row[0]
        counted = votes.get(character_id)
        group = counted.most_common(1)[0][0] if counted else fallback
        if group:
            conn.execute(
                text("UPDATE characters SET content_preset_id = :group WHERE id = :id"),
                {"group": group, "id": character_id},
            )


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
