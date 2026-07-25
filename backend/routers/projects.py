"""Project + scene endpoints and pipeline controls."""
from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..adapters.llm import analyze_project_scope
from ..adapters.registry import tts_voice_label
from ..animation import normalize_spec
from .. import pipeline
from ..config import (
    SUBTITLE_POSITION_DEFAULT,
    SUBTITLE_POSITION_MAX,
    SUBTITLE_POSITION_MIN,
    clamp_subtitle_position,
    settings,
)
from ..database import get_session
from ..models import (
    Character,
    CharacterForm,
    ContentPreset,
    PlatformPreset,
    Project,
    ProjectCharacter,
    Scene,
    SceneType,
    Stage,
    TikTokAccount,
)
from ..schemas import (
    CastSheetGenerate,
    ProjectCreate,
    ProjectDetail,
    ProjectScopeAnalyze,
    ProjectSplitCreate,
    ProjectSubtitlesUpdate,
    SceneAssetSelect,
    SceneUpdate,
    TikTokPublishIn,
    TitleCardGenerate,
    TitleCardSelect,
    TitleCardUpdate,
)
from ..events import bus
from ..services import tiktok as tiktok_service
from ..services.music_library import apply_default as apply_default_music
from .tiktok import ensure_access_token as ensure_tiktok_token
from ..storage import slugify
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
    title_is_custom: bool = False,
) -> Project:
    project = Project(
        title=_unique_title(session, title),
        title_is_custom=title_is_custom,
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
        thumb = p.title_card_path or (first.image_path if first and first.image_path else None)
        version = p.title_card_version if p.title_card_path else (first.asset_version if first else 0)
        out.append({**p.model_dump(), "thumbnail": thumb, "thumbnail_version": version})
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
        title_is_custom=body.title_is_custom if body.title_is_custom is not None else bool(body.title.strip()),
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
            title_is_custom=bool(body.title.strip()),
        ),
        _new_project(
            session,
            title=part_titles[1],
            topic_prompt=prompt_2,
            duration=duration,
            platform_id=platform.id if platform else None,
            content_id=content.id if content else None,
            title_is_custom=bool(body.title.strip()),
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
    project_data["title_card_variants"] = _title_card_options(project)
    project_data["final_video_version"] = _final_video_version(project)
    # Placement bounds/default travel with the project so the editor never has
    # to hard-code the render geometry.
    project_data["subtitle_position_default"] = SUBTITLE_POSITION_DEFAULT
    project_data["subtitle_position_min"] = SUBTITLE_POSITION_MIN
    project_data["subtitle_position_max"] = SUBTITLE_POSITION_MAX
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


# --- TikTok publishing (posts as the group's linked account) ---
def _final_video_path(project: Project) -> Path | None:
    if not project.folder_path:
        return None
    path = (
        Path(settings.projects_dir.parent.parent)
        / project.folder_path
        / "final"
        / f"{slugify(project.title)}.mp4"
    )
    return path if path.is_file() else None


def _publish_account(session: Session, project: Project) -> TikTokAccount:
    """The account this project posts as — its group's, and only its group's."""
    preset = (
        session.get(ContentPreset, project.content_preset_id)
        if project.content_preset_id
        else None
    )
    if not preset:
        raise HTTPException(400, "This project has no group, so there is no TikTok account.")
    if not preset.tiktok_account_id:
        raise HTTPException(
            400, f"The group \"{preset.name}\" has no TikTok account selected yet."
        )
    account = session.get(TikTokAccount, preset.tiktok_account_id)
    if not account:
        raise HTTPException(400, "The group's TikTok account is no longer linked.")
    return account


@router.get("/{project_id}/publish/tiktok")
def tiktok_publish_target(project_id: str, session: Session = Depends(get_session)):
    """What this project would post as, so the UI can show it before publishing."""
    project = _require(session, project_id)
    preset = (
        session.get(ContentPreset, project.content_preset_id)
        if project.content_preset_id
        else None
    )
    account = (
        session.get(TikTokAccount, preset.tiktok_account_id)
        if preset and preset.tiktok_account_id
        else None
    )
    metadata = project_metadata(project.folder_path)
    video = _final_video_path(project)
    return {
        "configured": tiktok_service.is_configured(),
        "group": {"id": preset.id, "name": preset.name} if preset else None,
        "account": (
            {
                "id": account.id,
                "display_name": account.display_name,
                "open_id": account.open_id,
                "avatar_url": account.avatar_url,
            }
            if account
            else None
        ),
        "video_ready": bool(video),
        "video_bytes": video.stat().st_size if video else 0,
        "suggested_caption": metadata.get("suggested_caption", "") or project.title,
        "privacy_levels": list(tiktok_service.PRIVACY_LEVELS),
    }


@router.post("/{project_id}/publish/tiktok")
def publish_to_tiktok(
    project_id: str,
    body: TikTokPublishIn,
    session: Session = Depends(get_session),
):
    """Direct-post the finished render to the group's TikTok account."""
    project = _require(session, project_id)
    video = _final_video_path(project)
    if not video:
        raise HTTPException(400, "This project has no finished video to publish yet.")
    account = _publish_account(session, project)
    token = ensure_tiktok_token(session, account)
    metadata = project_metadata(project.folder_path)
    caption = (body.caption or metadata.get("suggested_caption") or project.title).strip()
    try:
        init = tiktok_service.init_direct_post(
            token,
            video_bytes=video.stat().st_size,
            title=caption,
            privacy_level=body.privacy_level,
            disable_comment=body.disable_comment,
            disable_duet=body.disable_duet,
            disable_stitch=body.disable_stitch,
        )
        tiktok_service.upload_video(
            init["upload_url"], video, init["chunk_size"], init["total_chunk_count"]
        )
    except tiktok_service.TikTokError as exc:
        raise HTTPException(502, str(exc))
    return {
        "publish_id": init["publish_id"],
        "account_id": account.id,
        "caption": caption,
        "privacy_level": body.privacy_level,
    }


@router.get("/{project_id}/publish/tiktok/status")
def tiktok_publish_status(
    project_id: str,
    publish_id: str,
    session: Session = Depends(get_session),
):
    project = _require(session, project_id)
    account = _publish_account(session, project)
    token = ensure_tiktok_token(session, account)
    try:
        return tiktok_service.publish_status(token, publish_id)
    except tiktok_service.TikTokError as exc:
        raise HTTPException(502, str(exc))


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


@router.patch("/{project_id}/subtitles")
def update_subtitles(
    project_id: str,
    body: ProjectSubtitlesUpdate,
    session: Session = Depends(get_session),
):
    """Burned-in subtitle switch + placement for this project's next render."""
    project = _require(session, project_id)
    if body.enabled is not None:
        project.subtitles_enabled = bool(body.enabled)
    if body.position is not None:
        project.subtitle_position = clamp_subtitle_position(body.position)
    session.add(project)
    session.commit()
    return {
        "enabled": project.subtitles_enabled,
        "position": project.subtitle_position,
    }


@router.post("/{project_id}/title-card")
def generate_title_card(
    project_id: str,
    body: TitleCardGenerate,
    session: Session = Depends(get_session),
):
    project = _require(session, project_id)
    if body.mode == "reuse":
        if body.scene_id:
            scene = _require_scene(session, project_id, body.scene_id)
        else:
            scene = session.exec(
                select(Scene)
                .where(Scene.project_id == project_id, Scene.image_path.is_not(None))
                .order_by(Scene.order_index)
            ).first()
        if not scene or not scene.image_path:
            raise HTTPException(400, "Choose a scene with a generated image")
        source_path = scene.image_path
    else:
        source_path = None

    project.title_card_status = "generating"
    session.add(project)
    session.commit()
    pipeline.submit(
        pipeline.generate_title_card,
        project_id,
        body.mode,
        source_path,
        body.kicker,
        body.text,
        body.part_label,
        body.prompt,
    )
    return {"ok": True}


@router.patch("/{project_id}/title-card")
def update_title_card(
    project_id: str,
    body: TitleCardUpdate,
    session: Session = Depends(get_session),
):
    _require(session, project_id)
    pipeline.update_title_card_overlay(
        project_id,
        body.kicker,
        body.text,
        body.part_label,
        body.prompt,
    )
    return {"ok": True}


@router.post("/{project_id}/title-card/select")
def select_title_card(
    project_id: str,
    body: TitleCardSelect,
    session: Session = Depends(get_session),
):
    project = _require(session, project_id)
    options = _title_card_options(project)
    if body.path not in {item["path"] for item in options}:
        raise HTTPException(400, "Unknown title-image candidate")
    selected = pipeline.select_title_card_variant(project_id, body.path)
    if not selected:
        raise HTTPException(400, "Unknown title-image candidate")
    return {"ok": True, "project": selected}


# --- Scenes ---
@router.patch("/{project_id}/scenes/{scene_id}")
def update_scene(project_id: str, scene_id: str, body: SceneUpdate, session: Session = Depends(get_session)):
    scene = session.get(Scene, scene_id)
    if not scene or scene.project_id != project_id:
        raise HTTPException(404, "Scene not found")
    data = body.model_dump(exclude_unset=True)
    if "animation_spec" in data:
        # Validate/clean before storing so a bad edit can never reach the renderer.
        data["animation_spec"] = normalize_spec(data["animation_spec"])
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


@router.post("/{project_id}/scenes/{scene_id}/regenerate-animation")
def regen_animation(project_id: str, scene_id: str, session: Session = Depends(get_session)):
    scene = _require_scene(session, project_id, scene_id)
    if scene.scene_type != SceneType.ANIMATION:
        raise HTTPException(400, "Scene is not marked as animation")
    if not normalize_spec(scene.animation_spec):
        raise HTTPException(400, "Scene has no valid animation spec")
    pipeline.submit(pipeline.regenerate_scene_animation, project_id, scene_id)
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
        changed = body.path != scene.image_path
        scene.image_path = body.path
        if changed and scene.scene_type == SceneType.VIDEO:
            scene.clip_variants = _paths_with_current(scene.clip_variants, scene.clip_path)
            scene.clip_path = None
    elif body.kind == "clip":
        options = _paths_with_current(scene.clip_variants, scene.clip_path)
        if body.path not in options:
            raise HTTPException(400, "Unknown clip candidate")
        scene.clip_path = body.path
    elif body.kind == "animation":
        options = _paths_with_current(scene.animation_variants, scene.animation_path)
        if body.path not in options:
            raise HTTPException(400, "Unknown animation candidate")
        scene.animation_path = body.path
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
    data["animation_variants"] = _paths_with_current(scene.animation_variants, scene.animation_path)
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


def _title_card_options(project: Project) -> list[dict]:
    result = []
    seen = set()
    for raw in list(project.title_card_variants or []) + [
        {"path": project.title_card_path, "source_path": project.title_card_source_path}
    ]:
        item = raw if isinstance(raw, dict) else {"path": str(raw), "source_path": ""}
        path = str(item.get("path", "") or "")
        if not path or path in seen:
            continue
        seen.add(path)
        result.append({"path": path, "source_path": str(item.get("source_path", "") or "")})
    return result


def _final_video_version(project: Project) -> int | None:
    path = _final_video_path(project)
    return int(path.stat().st_mtime) if path else None
