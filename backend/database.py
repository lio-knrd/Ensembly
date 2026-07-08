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
    ("scenes", "asset_version", "INTEGER DEFAULT 0"),
    ("projects", "failed_stage", "TEXT DEFAULT NULL"),
    ("projects", "cancel_requested", "BOOLEAN DEFAULT 0"),
    ("projects", "canceled_stage", "TEXT DEFAULT NULL"),
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


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
