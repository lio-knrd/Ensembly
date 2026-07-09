"""Project + scene endpoints and pipeline controls."""
from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..adapters.llm import analyze_project_scope
from ..adapters.registry import tts_voice_label
from .. import pipeline
from ..config import settings
from ..database import get_session
from ..models import Character, CharacterForm, ContentPreset, Project, ProjectCharacter, Scene, SceneType, Stage
from ..schemas import (
    CastSheetGenerate,
    ProjectCreate,
    ProjectDetail,
    ProjectScopeAnalyze,
    ProjectSplitCreate,
    SceneAssetSelect,
    SceneUpdate,
)
from ..events import bus
from ..services.music_library import apply_default as apply_default_music
from .common import (
    default_content_preset,
    default_platform_preset,
    get_setting,
    project_metadata,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _unique_title(session: Session, requested: str) -> str:
    base = (requested.strip() or "Untitled")[:120]
    existing = set(session.exec(select(Project.title)).all())
    if base not in existing:
        return base
    number = 2
    while True:
        suffix = f" ({number})"
        candidate = f"{base[:120 - len(suffix)].rstrip()}{suffix}"
        if candidate not in existing:
            return candidate
        number += 1


def _unique_series_titles(session: Session, requested: str) -> tuple[str, str]:
    """Reserve a readable pair without asking the LLM about existing projects."""
    base = (requested.strip() or "Untitled")[:108]
    existing = set(session.exec(select(Project.title)).all())
    series_number = 1
    while True:
        series_base = base if series_number == 1 else f"{base[:97].rstrip()} — Series {series_number}"
        titles = (f"{series_base} (1)", f"{series_base} (2)")
        if not any(title in existing for title in titles):
            return titles
        series_number += 1


def _presets_for_request(session: Session, platform_id: str | None, content_id: str | None):
    platform = session.get(PlatformPreset, platform_id) if platform_id else default_platform_preset(session)
    content = session.get(ContentPreset, content_id) if content_id else default_content_preset(session)
    return platform, content


def _new_project(
    session: Session,
    *,
    title: str,
    topic_prompt: str,
    duration: int,
    platform_id: str | None,
    content_id: str | None,
) -> Project:
    project = Project(
        title=_unique_title(session, title),
        topic_prompt=topic_prompt.strip(),
        target_duration_seconds=duration,
        platform_preset_id=platform_id,
        content_preset_id=content_id,
        stage=Stage.IDEA,
    )
    apply_default_music(session, project)
    session.add(project)
    session.flush()
    return project


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
    platform, content = _presets_for_request(session, body.platform_preset_id, body.content_preset_id)
    duration = body.target_duration_seconds or get_setting(session, "default_duration_seconds", 75)

    project = _new_project(
        session,
        title=body.title,
        topic_prompt=body.topic_prompt.strip(),
        duration=int(duration),
        platform_id=platform.id if platform else None,
        content_id=content.id if content else None,
    )
    session.commit()
    session.refresh(project)

    if body.start:
        pipeline.submit(pipeline.generate_script, project.id)
    return project.model_dump()


@router.post("/analyze-scope")
def analyze_scope(body: ProjectScopeAnalyze, session: Session = Depends(get_session)):
    duration = int(body.target_duration_seconds or get_setting(session, "default_duration_seconds", 75))
    platform, content = _presets_for_request(session, body.platform_preset_id, body.content_preset_id)
    return analyze_project_scope(
        body.topic_prompt,
        body.title,
        duration,
        platform.format_prompt if platform else "",
        content.content_prompt if content else "",
    )


@router.post("/split", status_code=201)
def create_split_projects(body: ProjectSplitCreate, session: Session = Depends(get_session)):
    analysis = body.analysis
    if not analysis.get("split_recommended"):
        raise HTTPException(400, "The supplied analysis does not recommend a split")
    duration = int(body.target_duration_seconds or get_setting(session, "default_duration_seconds", 75))
    platform, content = _presets_for_request(session, body.platform_preset_id, body.content_preset_id)
    base_title = (body.title.strip() or str(analysis.get("suggested_title", "")).strip() or body.topic_prompt)[:108]
    throughline = str(analysis.get("series_throughline", "")).strip()
    part_1 = analysis.get("part_1") or {}
    part_2 = analysis.get("part_2") or {}
    part_titles = _unique_series_titles(session, base_title)
    shared = (
        f"Original topic: {body.topic_prompt.strip()}\n"
        f"Series throughline: {throughline}\n"
        f"This is a two-part series. Each part has about {duration} seconds."
    )
    prompt_1 = (
        f"{shared}\n\nPART 1 OF 2\n"
        f"Cover: {str(part_1.get('focus', '')).strip()}\n"
        f"Hard ending boundary: {str(part_1.get('ending_boundary', '')).strip()}\n"
        f"Reserved for Part 2: {str(part_2.get('focus', '')).strip()}\n"
        "Make this part satisfying on its own. End at the stated natural hinge. "
        "Do not explain events, conclusions, or consequences reserved for Part 2."
    )
    prompt_2 = (
        f"{shared}\n\nPART 2 OF 2\n"
        f"Already covered in Part 1: {str(part_1.get('focus', '')).strip()}\n"
        f"Part 1 ended at this boundary: {str(part_1.get('ending_boundary', '')).strip()}\n"
        f"Begin with: {str(part_2.get('opening_bridge', '')).strip()}\n"
        f"Cover: {str(part_2.get('focus', '')).strip()}\n"
        "Continue from the hinge and complete the remaining arc. Use at most one "
        "short bridging sentence; do not summarize or retell Part 1."
    )
    projects = [
        _new_project(
            session,
            title=part_titles[0],
            topic_prompt=prompt_1,
            duration=duration,
            platform_id=platform.id if platform else None,
            content_id=content.id if content else None,
        ),
        _new_project(
            session,
            title=part_titles[1],
            topic_prompt=prompt_2,
            duration=duration,
            platform_id=platform.id if platform else None,
            content_id=content.id if content else None,
        ),
    ]
    session.commit()
    for project in projects:
        session.refresh(project)
    if body.start:
        for project in projects:
            pipeline.submit(pipeline.generate_script, project.id)
    return {"projects": [project.model_dump() for project in projects]}


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
    characters = []
    for cid in char_ids:
        char = session.get(Character, cid)
        if not char:
            continue
        forms = session.exec(select(CharacterForm).where(CharacterForm.character_id == cid)).all()
        characters.append({**char.model_dump(), "forms": [f.model_dump() for f in forms]})
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
        scenes=[_scene_payload(session, project, s) for s in scenes],
        metadata=project_metadata(project.folder_path),
        characters=characters,
    )


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: str, session: Session = Depends(get_session)):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    folder_path = project.folder_path
    pipeline.cancel_project(project_id)
    for s in session.exec(select(Scene).where(Scene.project_id == project_id)):
        session.delete(s)
    for link in session.exec(select(ProjectCharacter).where(ProjectCharacter.project_id == project_id)):
        session.delete(link)
    session.delete(project)
    session.commit()
    _delete_project_folder(folder_path)


def _delete_project_folder(folder_path: str | None) -> None:
    if not folder_path:
        return
    folder = (Path(settings.projects_dir.parent.parent) / folder_path).resolve()
    projects_root = settings.projects_dir.resolve()
    try:
        folder.relative_to(projects_root)
    except ValueError:
        return
    if folder.exists() and folder.is_dir():
        shutil.rmtree(folder)


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
        body.state,
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
    session.refresh(scene)
    project = session.get(Project, project_id)
    return _scene_payload(session, project, scene) if project else scene.model_dump()


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


@router.post("/{project_id}/scenes/{scene_id}/select-asset")
def select_scene_asset(
    project_id: str,
    scene_id: str,
    body: SceneAssetSelect,
    session: Session = Depends(get_session),
):
    scene = _require_scene(session, project_id, scene_id)
    project = _require(session, project_id)

    if body.kind == "image":
        options = _paths_with_current(scene.image_variants, scene.image_path)
        if body.path not in options:
            raise HTTPException(400, "Unknown image candidate")
        scene.image_path = body.path
    elif body.kind == "clip":
        options = _paths_with_current(scene.clip_variants, scene.clip_path)
        if body.path not in options:
            raise HTTPException(400, "Unknown clip candidate")
        scene.clip_path = body.path
    else:
        options = _audio_options(scene)
        selected = next((item for item in options if item.get("path") == body.path), None)
        if not selected:
            raise HTTPException(400, "Unknown audio candidate")
        scene.audio_path = selected["path"]
        scene.timestamps_path = selected.get("timestamps_path")
        scene.duration_seconds = selected.get("duration_seconds")

    scene.asset_version = (scene.asset_version or 0) + 1
    session.add(scene)
    session.commit()
    session.refresh(scene)
    if body.kind == "audio":
        pipeline.submit(pipeline.rebuild_narration, project_id)
    bus.publish("scene.updated", project_id=project_id, scene_id=scene_id)
    return _scene_payload(session, project, scene)


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


def _scene_payload(session: Session, project: Project, scene: Scene) -> dict:
    data = scene.model_dump()
    data["image_variants"] = _paths_with_current(scene.image_variants, scene.image_path)
    data["clip_variants"] = _paths_with_current(scene.clip_variants, scene.clip_path)
    data["audio_variants"] = _audio_options(scene)
    refs = []
    for ref in pipeline.scene_context_refs(session, project, scene):
        refs.append({
            "scene_id": ref["scene_id"],
            "scene_number": ref["scene_number"],
            "image_path": ref["image_path"],
            "asset_version": ref["asset_version"],
            "prompt": ref["prompt"],
            "matches": ref["matches"],
            "visual_anchor": ref["visual_anchor"],
            "reason": ref["reason"],
            "excluded": ref["excluded"],
        })
    data["context_refs"] = refs
    return data


def _paths_with_current(paths: list[str] | None, current: str | None) -> list[str]:
    result = list(paths or [])
    if current and current not in result:
        result.append(current)
    return result


def _audio_options(scene: Scene) -> list[dict]:
    result = list(scene.audio_variants or [])
    if scene.audio_path and not any(item.get("path") == scene.audio_path for item in result):
        result.append({
            "path": scene.audio_path,
            "timestamps_path": scene.timestamps_path,
            "duration_seconds": scene.duration_seconds,
        })
    return result
