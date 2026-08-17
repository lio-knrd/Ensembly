"""Shared local soundtrack library and project music defaults."""
from __future__ import annotations

import json
from pathlib import Path

from sqlmodel import Session, select

from ..config import settings
from ..models import MusicTrack, Project, Setting

DEFAULT_MUSIC_SETTING = "default_music_track_id"
WHISPER_TRACK_ID = "builtin-whisper-in-the-deep"
WHISPER_PROVIDER_ID = "whisper-in-the-deep"
WHISPER_PATH = "data/music/whisper-in-the-deep.mp3"
# Real provenance for the bundled track, so the rendered credit names where it
# actually came from instead of the placeholder "Local library".
WHISPER_TITLE = "Whisper In The Deep"
WHISPER_ARTIST = "Royalty Free Zone - Epic Journey"
WHISPER_SOURCE_URL = "https://www.youtube.com/watch?v=cqMbxcC5LMU"
WHISPER_CREDIT_SETTING = "whisper_credit_corrected"


def seed_local_library(session: Session) -> None:
    """Register bundled files without overwriting user-editable metadata."""
    path = settings.root_dir / WHISPER_PATH
    track = session.get(MusicTrack, WHISPER_TRACK_ID)
    if track is None and path.is_file():
        session.add(
            MusicTrack(
                id=WHISPER_TRACK_ID,
                provider="local",
                provider_track_id=WHISPER_PROVIDER_ID,
                title=WHISPER_TITLE,
                artist_name=WHISPER_ARTIST,
                duration_seconds=192,
                license_url="royalty-free",
                download_allowed=True,
                local_path=WHISPER_PATH,
                share_url=WHISPER_SOURCE_URL,
            )
        )

    if session.get(Setting, DEFAULT_MUSIC_SETTING) is None:
        session.add(Setting(key=DEFAULT_MUSIC_SETTING, value=json.dumps(WHISPER_TRACK_ID)))


def correct_whisper_credit(session: Session) -> None:
    """Replace the placeholder attribution on already-seeded databases, once."""
    if session.get(Setting, WHISPER_CREDIT_SETTING) is not None:
        return
    track = session.get(MusicTrack, WHISPER_TRACK_ID)
    if track and track.artist_name in ("", "Local library"):
        track.title = WHISPER_TITLE
        track.artist_name = WHISPER_ARTIST
        track.share_url = WHISPER_SOURCE_URL
        session.add(track)
    session.add(Setting(key=WHISPER_CREDIT_SETTING, value=json.dumps(True)))


def default_track(session: Session) -> MusicTrack | None:
    setting = session.get(Setting, DEFAULT_MUSIC_SETTING)
    if not setting:
        return None
    try:
        track_id = json.loads(setting.value)
    except (TypeError, json.JSONDecodeError):
        return None
    track = session.get(MusicTrack, track_id) if isinstance(track_id, str) else None
    return track if local_track_path(track) else None


def apply_default(session: Session, project: Project) -> None:
    track = default_track(session)
    if track:
        project.music_track_id = track.id
        project.music_enabled = True


def backfill_legacy_defaults(session: Session) -> None:
    """Give the default to old projects that never explicitly chose no music."""
    track = default_track(session)
    if not track:
        return
    projects = session.exec(
        select(Project).where(
            Project.music_track_id.is_(None),
            Project.music_enabled.is_(True),
        )
    ).all()
    for project in projects:
        project.music_track_id = track.id
        session.add(project)


def local_tracks(session: Session) -> list[MusicTrack]:
    tracks = session.exec(
        select(MusicTrack)
        .where(MusicTrack.provider == "local")
        .order_by(MusicTrack.title)
    ).all()
    return [track for track in tracks if local_track_path(track)]


def local_track_path(track: MusicTrack | None) -> Path | None:
    if not track or not track.local_path:
        return None
    path = (settings.root_dir / track.local_path).resolve()
    data_root = (settings.root_dir / "data").resolve()
    if data_root not in path.parents and path != data_root:
        return None
    return path if path.is_file() else None


def playback_url(track: MusicTrack) -> str:
    if track.provider == "local" and local_track_path(track):
        return f"/media/{track.local_path.replace(chr(92), '/')}"
    return track.audio_url
