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
    ("content_presets", "voice_id", "TEXT DEFAULT ''"),
    ("characters", "reference_prompt", "TEXT DEFAULT ''"),
    ("characters", "reference_style_prompt", "TEXT DEFAULT ''"),
    ("characters", "reference_version", "INTEGER DEFAULT 0"),
    ("characters", "reference_variants", "JSON DEFAULT '[]'"),
    ("character_forms", "reference_variants", "JSON DEFAULT '[]'"),
    ("scenes", "asset_version", "INTEGER DEFAULT 0"),
    ("scenes", "continuity_context", "JSON DEFAULT '[]'"),
    ("scenes", "excluded_context_scene_ids", "JSON DEFAULT '[]'"),
    ("scenes", "character_assignments", "JSON DEFAULT '[]'"),
    ("scenes", "use_next_scene_as_end_frame", "BOOLEAN DEFAULT 1"),
    ("scenes", "image_variants", "JSON DEFAULT '[]'"),
    ("scenes", "clip_variants", "JSON DEFAULT '[]'"),
    ("scenes", "audio_variants", "JSON DEFAULT '[]'"),
    ("projects", "failed_stage", "TEXT DEFAULT NULL"),
    ("projects", "cancel_requested", "BOOLEAN DEFAULT 0"),
    ("projects", "canceled_stage", "TEXT DEFAULT NULL"),
    ("projects", "music_track_id", "TEXT DEFAULT NULL"),
    ("projects", "music_enabled", "BOOLEAN DEFAULT 1"),
    ("projects", "music_volume", "REAL DEFAULT 0.075"),
    ("projects", "title_is_custom", "BOOLEAN DEFAULT 0"),
    ("projects", "title_card_path", "TEXT DEFAULT NULL"),
    ("projects", "title_card_source_path", "TEXT DEFAULT NULL"),
    ("projects", "title_card_kicker", "TEXT DEFAULT ''"),
    ("projects", "title_card_text", "TEXT DEFAULT ''"),
    ("projects", "title_card_part_label", "TEXT DEFAULT ''"),
    ("projects", "title_card_prompt", "TEXT DEFAULT ''"),
    ("projects", "title_card_status", "TEXT DEFAULT 'pending'"),
    ("projects", "title_card_version", "INTEGER DEFAULT 0"),
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


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
