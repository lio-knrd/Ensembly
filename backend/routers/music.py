"""Project soundtrack search and selection."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from ..database import get_session
from ..models import MusicTrack, Project
from ..schemas import MusicTrackSelect, ProjectMusicUpdate
from ..services import jamendo

router = APIRouter(tags=["music"])


@router.get("/api/music/search")
def search_music(
    q: str = Query("ambient", min_length=0, max_length=80),
    limit: int = Query(20, ge=1, le=50),
    instrumental: bool = True,
):
    if not jamendo.configured():
        return {
            "configured": False,
            "results": [],
            "message": "Set JAMENDO_CLIENT_ID in .env to enable Jamendo music search.",
        }
    try:
        return {
            "configured": True,
            "results": jamendo.search_tracks(q, limit=limit, instrumental=instrumental),
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Jamendo search failed: {exc}") from exc


@router.get("/api/projects/{project_id}/music")
def get_project_music(project_id: str, session: Session = Depends(get_session)):
    project = _project(session, project_id)
    track = session.get(MusicTrack, project.music_track_id) if project.music_track_id else None
    return {
        "enabled": project.music_enabled,
        "volume": project.music_volume,
        "track": _track(track) if track else None,
    }


@router.patch("/api/projects/{project_id}/music")
def update_project_music(
    project_id: str,
    body: ProjectMusicUpdate,
    session: Session = Depends(get_session),
):
    project = _project(session, project_id)
    if body.enabled is not None:
        project.music_enabled = bool(body.enabled)
    if body.volume is not None:
        project.music_volume = max(0.0, min(float(body.volume), 0.3))
    session.add(project)
    session.commit()
    return get_project_music(project_id, session)


@router.post("/api/projects/{project_id}/music/select")
def select_project_music(
    project_id: str,
    body: MusicTrackSelect,
    session: Session = Depends(get_session),
):
    project = _project(session, project_id)
    data = body.model_dump()
    existing = session.exec(
        select(MusicTrack).where(
            MusicTrack.provider == "jamendo",
            MusicTrack.provider_track_id == body.provider_track_id,
        )
    ).first()
    track = jamendo.upsert_track(existing, data)
    try:
        jamendo.ensure_downloaded(project, track)
    except jamendo.MusicLicenseError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Could not download Jamendo track: {exc}") from exc
    session.add(track)
    session.commit()
    session.refresh(track)

    project.music_track_id = track.id
    project.music_enabled = True
    session.add(project)
    session.commit()
    return {
        "enabled": project.music_enabled,
        "volume": project.music_volume,
        "track": _track(track),
    }


@router.delete("/api/projects/{project_id}/music", status_code=204)
def clear_project_music(project_id: str, session: Session = Depends(get_session)):
    project = _project(session, project_id)
    project.music_track_id = None
    project.music_enabled = False
    session.add(project)
    session.commit()


def _project(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def _track(track: MusicTrack) -> dict:
    return {
        **track.model_dump(),
        "downloaded": bool(jamendo.local_track_path(track)),
    }
