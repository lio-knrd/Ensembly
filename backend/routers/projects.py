"""Project + scene endpoints and pipeline controls."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..adapters.registry import tts_voice_label
from .. import pipeline
from ..database import get_session
from ..models import Character, ContentPreset, Project, ProjectCharacter, Scene, SceneType, Stage
from ..schemas import CastSheetGenerate, ProjectCreate, ProjectDetail, SceneUpdate
from .common import (
    default_content_preset,
    default_platform_preset,
    get_setting,
    project_metadata,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("")
def list_projects(session: Session = Depends(get_session)):
    projects = session.exec(select(Project).order_by(Project.updated_at.desc())).all()
    out = []
    for p in projects:
        first = session.exec(
            select(Scene).where(Scene.project_id == p.id).order_by(Scene.order_index)
        ).first()
        thumb = first.image_path if first and first.image_path else None
        out.append({**p.model_dump(), "thumbnail": thumb, "thumbnail_version": first.asset_version if first else 0})
    return out


@router.post("", status_code=201)
def create_project(body: ProjectCreate, session: Session = Depends(get_session)):
    platform = default_platform_preset(session)
    content = default_content_preset(session)
    duration = body.target_duration_seconds or get_setting(session, "default_duration_seconds", 75)

    project = Project(
        title=body.title.strip() or "Untitled",
        topic_prompt=body.topic_prompt.strip(),
        target_duration_seconds=int(duration),
        platform_preset_id=body.platform_preset_id or (platform.id if platform else None),
        content_preset_id=body.content_preset_id or (content.id if content else None),
        stage=Stage.IDEA,
    )
    session.add(project)
    session.commit()
    session.refresh(project)

    if body.start:
        pipeline.submit(pipeline.generate_script, project.id)
    return project.model_dump()


@router.get("/{project_id}", response_model=ProjectDetail)
def get_project(project_id: str, session: Session = Depends(get_session)):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    scenes = session.exec(
        select(Scene).where(Scene.project_id == project_id).order_by(Scene.order_index)
    ).all()
    char_ids = session.exec(
        select(ProjectCharacter.character_id).where(ProjectCharacter.project_id == project_id)
    ).all()
    characters = [session.get(Character, cid).model_dump() for cid in char_ids if session.get(Character, cid)]
    project_data = project.model_dump()
    project_data["voice_name"] = tts_voice_label(None)
    project_data["voice_id"] = ""
    if project.content_preset_id:
        content = session.get(ContentPreset, project.content_preset_id)
        if content:
            project_data["content_preset_name"] = content.name
            project_data["visual_style_prompt"] = content.image_style_prompt
            project_data["voice_id"] = content.voice_id
            project_data["voice_name"] = tts_voice_label(content.voice_id)
    return ProjectDetail(
        project=project_data,
        scenes=[s.model_dump() for s in scenes],
        metadata=project_metadata(project.folder_path),
        characters=characters,
    )


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: str, session: Session = Depends(get_session)):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    for s in session.exec(select(Scene).where(Scene.project_id == project_id)):
        session.delete(s)
    for link in session.exec(select(ProjectCharacter).where(ProjectCharacter.project_id == project_id)):
        session.delete(link)
    session.delete(project)
    session.commit()


# --- Pipeline controls ---
@router.post("/{project_id}/generate-script")
def regen_script(project_id: str, session: Session = Depends(get_session)):
    _require(session, project_id)
    pipeline.submit(pipeline.generate_script, project_id)
    return {"ok": True}


@router.post("/{project_id}/approve-script")
def approve_script(project_id: str, session: Session = Depends(get_session)):
    _require(session, project_id)
    pipeline.submit(pipeline.approve_script, project_id)
    return {"ok": True}


# --- Cast / character sheets (pre-storyboard review) ---
@router.get("/{project_id}/cast")
def get_cast(project_id: str, session: Session = Depends(get_session)):
    project = _require(session, project_id)
    return pipeline.compute_cast(session, project)


@router.post("/{project_id}/cast/generate")
def generate_cast_sheet(project_id: str, body: CastSheetGenerate, session: Session = Depends(get_session)):
    _require(session, project_id)
    pipeline.submit(
        pipeline.generate_character_sheet,
        project_id,
        body.name,
        body.description,
        body.prompt,
        body.generate_description,
    )
    return {"ok": True}


@router.post("/{project_id}/cast/generate-missing")
def generate_missing_sheets(project_id: str, session: Session = Depends(get_session)):
    _require(session, project_id)
    pipeline.submit(pipeline.generate_missing_sheets, project_id)
    return {"ok": True}


@router.post("/{project_id}/approve-cast")
def approve_cast(project_id: str, session: Session = Depends(get_session)):
    _require(session, project_id)
    pipeline.submit(pipeline.approve_cast, project_id)
    return {"ok": True}


@router.post("/{project_id}/approve-storyboard")
def approve_storyboard(project_id: str, session: Session = Depends(get_session)):
    _require(session, project_id)
    pipeline.submit(pipeline.approve_storyboard, project_id)
    return {"ok": True}


@router.post("/{project_id}/approve-clips")
def approve_clips(project_id: str, session: Session = Depends(get_session)):
    _require(session, project_id)
    pipeline.submit(pipeline.approve_clips, project_id)
    return {"ok": True}


@router.post("/{project_id}/step-back")
def step_back(project_id: str, session: Session = Depends(get_session)):
    _require(session, project_id)
    pipeline.submit(pipeline.step_back, project_id)
    return {"ok": True}


@router.post("/{project_id}/step-forward")
def step_forward(project_id: str, session: Session = Depends(get_session)):
    _require(session, project_id)
    pipeline.submit(pipeline.step_forward, project_id)
    return {"ok": True}


@router.post("/{project_id}/retry-failed-step")
def retry_failed_step(project_id: str, session: Session = Depends(get_session)):
    _require(session, project_id)
    pipeline.submit(pipeline.retry_failed_step, project_id)
    return {"ok": True}


@router.post("/{project_id}/cancel")
def cancel_project(project_id: str, session: Session = Depends(get_session)):
    _require(session, project_id)
    pipeline.cancel_project(project_id)
    return {"ok": True}


@router.post("/{project_id}/render")
def rerender(project_id: str, session: Session = Depends(get_session)):
    _require(session, project_id)
    pipeline.submit(pipeline.render, project_id)
    return {"ok": True}


# --- Scenes ---
@router.patch("/{project_id}/scenes/{scene_id}")
def update_scene(project_id: str, scene_id: str, body: SceneUpdate, session: Session = Depends(get_session)):
    scene = session.get(Scene, scene_id)
    if not scene or scene.project_id != project_id:
        raise HTTPException(404, "Scene not found")
    data = body.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(scene, field, value)
    session.add(scene)
    session.commit()
    return scene.model_dump()


@router.post("/{project_id}/scenes/{scene_id}/regenerate-image")
def regen_image(project_id: str, scene_id: str, session: Session = Depends(get_session)):
    _require_scene(session, project_id, scene_id)
    pipeline.submit(pipeline.regenerate_scene_image, project_id, scene_id)
    return {"ok": True}


@router.post("/{project_id}/scenes/{scene_id}/regenerate-audio")
def regen_audio(project_id: str, scene_id: str, session: Session = Depends(get_session)):
    _require_scene(session, project_id, scene_id)
    pipeline.submit(pipeline.regenerate_scene_audio, project_id, scene_id)
    return {"ok": True}


@router.post("/{project_id}/scenes/{scene_id}/regenerate-clip")
def regen_clip(project_id: str, scene_id: str, session: Session = Depends(get_session)):
    scene = _require_scene(session, project_id, scene_id)
    if scene.scene_type != SceneType.VIDEO:
        raise HTTPException(400, "Scene is not marked as video")
    pipeline.submit(pipeline.regenerate_scene_clip, project_id, scene_id)
    return {"ok": True}


def _require(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def _require_scene(session: Session, project_id: str, scene_id: str) -> Scene:
    scene = session.get(Scene, scene_id)
    if not scene or scene.project_id != project_id:
        raise HTTPException(404, "Scene not found")
    return scene
