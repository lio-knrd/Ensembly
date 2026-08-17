"""SQLite engine + session helpers."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import MetaData, event, text
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlmodel import Session, SQLModel, create_engine

from .config import settings

# Lightweight additive migrations for SQLite (create_all won't add columns to
# existing tables). Each entry: table, column, SQL type + default.
_ADDED_COLUMNS = [
    ("content_presets", "image_style_prompt", "TEXT DEFAULT ''"),
    ("content_presets", "animation_style_prompt", "TEXT DEFAULT ''"),
    ("content_presets", "motion_style_prompt", "TEXT DEFAULT ''"),
    # Enum column: stored by member NAME, like scene_type and camera_move.
    ("content_presets", "visual_mode", "TEXT DEFAULT 'MIXED'"),
    ("content_presets", "panel_seconds", "REAL DEFAULT 4.0"),
    ("content_presets", "panel_parallax", "BOOLEAN DEFAULT 1"),
    ("content_presets", "voice_id", "TEXT DEFAULT ''"),
    ("content_presets", "voice_speed", "REAL DEFAULT 1.0"),
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
    ("scenes", "use_next_scene_as_end_frame", "BOOLEAN DEFAULT 0"),
    ("scenes", "motion_prompt", "TEXT DEFAULT ''"),
    # SQLAlchemy stores an Enum column by member NAME, not by value, so the
    # column default has to be the name (see scene_type, stored as "STILL").
    ("scenes", "camera_move", "TEXT DEFAULT 'PUSH_IN'"),
    ("scenes", "particles", "TEXT DEFAULT 'NONE'"),
    ("scenes", "transition", "TEXT DEFAULT 'CUT'"),
    # Plain string, not an enum: the script model invents the beat names.
    ("scenes", "beat_id", "TEXT DEFAULT ''"),
    ("scenes", "motion_fx", "TEXT DEFAULT 'NONE'"),
    ("scenes", "grade", "TEXT DEFAULT 'NONE'"),
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
    ("content_presets", "youtube_account_id", "TEXT DEFAULT NULL"),
    ("projects", "failed_stage", "TEXT DEFAULT NULL"),
    ("projects", "cancel_requested", "BOOLEAN DEFAULT 0"),
    ("projects", "canceled_stage", "TEXT DEFAULT NULL"),
    ("projects", "music_track_id", "TEXT DEFAULT NULL"),
    ("projects", "music_enabled", "BOOLEAN DEFAULT 1"),
    ("projects", "music_volume", "REAL DEFAULT 0.075"),
    ("projects", "subtitles_enabled", "BOOLEAN DEFAULT 1"),
    ("projects", "subtitle_position", "REAL DEFAULT 0.128"),
    ("projects", "title_is_custom", "BOOLEAN DEFAULT 0"),
    ("projects", "revision_notes", "TEXT DEFAULT ''"),
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


@event.listens_for(engine, "connect")
def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
    """SQLite does not enforce declared foreign keys unless every connection opts in."""
    dbapi_connection.execute("PRAGMA foreign_keys = ON")


# All of these links are optional. Deleting the record on the right must retain
# the record on the left and clear only the now-invalid reference. Non-nullable
# relationships intentionally keep SQLite's default NO ACTION policy; their
# delete behavior remains explicit application logic until a separate decision
# is made for each one.
_SET_NULL_FOREIGN_KEYS = {
    ("characters", "content_preset_id", "content_presets", "id"),
    ("content_presets", "tiktok_account_id", "tiktok_accounts", "id"),
    ("content_presets", "youtube_account_id", "youtube_accounts", "id"),
    ("ideas", "plan_id", "editorial_plans", "id"),
    ("editorial_items", "project_id", "projects", "id"),
    ("editorial_plans", "parent_plan_id", "editorial_plans", "id"),
    ("editorial_plans", "platform_preset_id", "platform_presets", "id"),
    ("editorial_plans", "content_preset_id", "content_presets", "id"),
    ("projects", "platform_preset_id", "platform_presets", "id"),
    ("projects", "content_preset_id", "content_presets", "id"),
    ("projects", "music_track_id", "music_tracks", "id"),
}
_FK_POLICY_TABLES = tuple(sorted({table for table, _, _, _ in _SET_NULL_FOREIGN_KEYS}))


def init_db() -> None:
    settings.ensure_dirs()
    # Import models so their tables register on SQLModel.metadata.
    from . import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    _run_migrations()
    _repair_nullable_orphans()
    _apply_foreign_key_policy()
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
                # End frames used to default on, which pointed every clip at the
                # NEXT scene's still — a different shot in a different place, so
                # the clip was spent morphing across a cut. The default is now
                # off; existing rows are flipped once, here, rather than on every
                # start, so a scene deliberately switched back on stays on.
                if table == "scenes" and column == "motion_prompt":
                    conn.execute(
                        text("UPDATE scenes SET use_next_scene_as_end_frame = 0")
                    )
        # Enum columns hold member names. A row written with a value instead
        # ("push_in" for PUSH_IN) fails to load at all, so normalize rather than
        # leave the project unopenable. Idempotent: every name is upper case.
        conn.execute(
            text(
                "UPDATE scenes SET camera_move = UPPER(camera_move) "
                "WHERE camera_move IS NOT NULL AND camera_move <> UPPER(camera_move)"
            )
        )
        conn.execute(
            text(
                "UPDATE content_presets SET visual_mode = UPPER(visual_mode) "
                "WHERE visual_mode IS NOT NULL AND visual_mode <> UPPER(visual_mode)"
            )
        )
        for column in ("particles", "transition"):
            conn.execute(
                text(
                    f"UPDATE scenes SET {column} = UPPER({column}) "
                    f"WHERE {column} IS NOT NULL AND {column} <> UPPER({column})"
                )
            )


def _repair_nullable_orphans() -> None:
    """Restore the only nullable relationship with domain-state recovery.

    The project board historically allowed a project to be deleted while its
    editorial item retained the project's id. An item is still valid without a
    project, so it returns to the planned queue rather than being deleted.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE editorial_items "
                "SET project_id = NULL, status = 'planned', updated_at = CURRENT_TIMESTAMP "
                "WHERE project_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM projects WHERE projects.id = editorial_items.project_id)"
            )
        )


def _apply_foreign_key_policy() -> None:
    """Upgrade legacy SQLite tables to the model's nullable-link policy.

    SQLite cannot add or alter a foreign-key constraint in place. Legacy
    additive migrations therefore need a one-time table rebuild, preserving all
    columns and rows while recreating the tables with the current constraints.
    """
    with engine.connect() as conn:
        if not _foreign_key_policy_needs_upgrade(conn):
            return

        # PRAGMA changes must happen outside a transaction. The tables are
        # copied before any originals are dropped, and all original names are
        # restored before foreign-key checks are turned back on.
        conn.commit()
        raw_connection = conn.connection.driver_connection
        raw_connection.execute("PRAGMA foreign_keys = OFF")
        try:
            with conn.begin():
                temporary_tables: dict[str, str] = {}
                # Keep original-name table definitions in this metadata solely
                # so cloned foreign keys can resolve their parent names. Only
                # the temporary tables below are emitted as DDL.
                temporary_metadata = MetaData()
                for table in SQLModel.metadata.tables.values():
                    table.to_metadata(temporary_metadata)
                for table_name in _FK_POLICY_TABLES:
                    source_table = SQLModel.metadata.tables[table_name]
                    existing_columns = {
                        row[1]
                        for row in conn.execute(text(f'PRAGMA table_info("{table_name}")'))
                    }
                    model_columns = set(source_table.columns.keys())
                    if existing_columns != model_columns:
                        raise RuntimeError(
                            f"Cannot safely rebuild {table_name}: database columns do not match the model"
                        )

                    temporary_name = f"__fk_policy_{table_name}"
                    temporary_table = source_table.to_metadata(
                        temporary_metadata, name=temporary_name
                    )
                    conn.execute(CreateTable(temporary_table))
                    columns = ", ".join(f'"{column}"' for column in source_table.columns.keys())
                    conn.execute(
                        text(
                            f'INSERT INTO "{temporary_name}" ({columns}) '
                            f'SELECT {columns} FROM "{table_name}"'
                        )
                    )
                    temporary_tables[table_name] = temporary_name

                for table_name in _FK_POLICY_TABLES:
                    conn.execute(text(f'DROP TABLE "{table_name}"'))
                for table_name, temporary_name in temporary_tables.items():
                    conn.execute(text(f'ALTER TABLE "{temporary_name}" RENAME TO "{table_name}"'))
                for table_name in _FK_POLICY_TABLES:
                    for index in SQLModel.metadata.tables[table_name].indexes:
                        conn.execute(CreateIndex(index))
        finally:
            raw_connection.execute("PRAGMA foreign_keys = ON")


def _foreign_key_policy_needs_upgrade(conn) -> bool:
    for table_name, column, parent_table, parent_column in _SET_NULL_FOREIGN_KEYS:
        foreign_keys = conn.execute(text(f'PRAGMA foreign_key_list("{table_name}")')).mappings()
        if not any(
            fk["from"] == column
            and fk["table"] == parent_table
            and fk["to"] == parent_column
            and fk["on_delete"].upper() == "SET NULL"
            for fk in foreign_keys
        ):
            return True
    return False


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
