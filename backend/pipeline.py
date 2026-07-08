"""Pipeline state machine + background orchestration (spec sections 8-9).

Long-running generation runs off the request thread (a small thread pool) so the
UI never blocks; progress is pushed over the WebSocket event bus. Any stage can
be re-run for a single scene or the whole project without restarting from IDEA.
"""
from __future__ import annotations

import json
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from sqlmodel import Session, select

from . import prompts
from .adapters import (
    get_image_generator,
    get_script_generator,
    get_tts_generator,
    get_video_generator,
)
from .adapters.llm import ai_character_description
from .config import settings
from .database import engine
from .events import bus
from .models import (
    Character,
    ContentPreset,
    MusicTrack,
    PlatformPreset,
    Project,
    ProjectCharacter,
    Scene,
    SceneType,
    Stage,
)
from .services import ffmpeg
from .services.jamendo import ensure_downloaded, local_track_path
from .storage import project_folder

_executor = ThreadPoolExecutor(max_workers=2)
_jobs_by_project: dict[str, set[Future]] = {}
_jobs_lock = Lock()
_MAX_VIDEO_PROMPT_CHARS = 2400

def submit(fn, *args) -> None:
    """Enqueue a pipeline step to run off the request thread."""
    project_id = args[0] if args and isinstance(args[0], str) else None
    future = _executor.submit(_guarded, fn, *args)
    if project_id:
        with _jobs_lock:
            _jobs_by_project.setdefault(project_id, set()).add(future)
        future.add_done_callback(lambda done, pid=project_id: _forget_job(pid, done))


def _forget_job(project_id: str, future: Future) -> None:
    with _jobs_lock:
        jobs = _jobs_by_project.get(project_id)
        if not jobs:
            return
        jobs.discard(future)
        if not jobs:
            _jobs_by_project.pop(project_id, None)


def _guarded(fn, *args) -> None:
    try:
        fn(*args)
    except Exception:  # noqa: BLE001 — surface failure to the UI, keep worker alive
        traceback.print_exc()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _touch(project: Project) -> None:
    project.updated_at = datetime.now(timezone.utc)


def _set_stage(session: Session, project: Project, stage: Stage, message: str | None = None) -> None:
    project.stage = stage
    project.status_message = message
    if stage != Stage.FAILED:
        project.error = None
        project.failed_stage = None
    if stage != Stage.CANCELED:
        project.cancel_requested = False
        project.canceled_stage = None
    _touch(project)
    session.add(project)
    session.commit()
    bus.publish("project.stage", project_id=project.id, stage=stage.value, message=message)


def _fail(session: Session, project: Project, where: str, exc: Exception) -> None:
    project.failed_stage = project.stage.value
    project.stage = Stage.FAILED
    project.error = f"{where}: {exc}"
    project.status_message = None
    project.cancel_requested = False
    _touch(project)
    session.add(project)
    session.commit()
    bus.publish("project.failed", project_id=project.id, error=project.error)


def cancel_project(project_id: str) -> None:
    """Request cancellation for queued/running work for one project."""
    with _jobs_lock:
        for future in list(_jobs_by_project.get(project_id, set())):
            future.cancel()
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project:
            return
        _mark_canceled(session, project)


def _mark_canceled(session: Session, project: Project) -> None:
    if project.stage != Stage.CANCELED:
        project.canceled_stage = project.stage.value
    project.stage = Stage.CANCELED
    project.cancel_requested = True
    project.error = None
    project.status_message = "Canceled"
    _touch(project)
    session.add(project)
    for scene in session.exec(select(Scene).where(Scene.project_id == project.id, Scene.status == "generating")):
        scene.status = "pending"
        session.add(scene)
        bus.publish("scene.updated", project_id=project.id, scene_id=scene.id)
    session.commit()
    bus.publish("project.stage", project_id=project.id, stage=Stage.CANCELED.value, message="Canceled")


def _stop_if_canceled(session: Session, project: Project) -> bool:
    session.refresh(project)
    if project.cancel_requested or project.stage == Stage.CANCELED:
        _mark_canceled(session, project)
        return True
    return False


def _folder(project: Project) -> Path:
    return Path(settings.projects_dir.parent.parent) / project.folder_path if project.folder_path else project_folder(project.title)


def _asset_path(path: str | None) -> Path | None:
    if not path:
        return None
    return Path(settings.projects_dir.parent.parent) / path


def _asset_exists(path: str | None) -> bool:
    resolved = _asset_path(path)
    return bool(resolved and resolved.exists())


def _content_style(session: Session, project: Project) -> str:
    """The image style prompt from the project's content preset (may be empty)."""
    if not project.content_preset_id:
        return ""
    preset = session.get(ContentPreset, project.content_preset_id)
    return (preset.image_style_prompt if preset else "") or ""


def _content_voice_id(session: Session, project: Project) -> str | None:
    """The TTS voice from the project's content preset, falling back to config."""
    if not project.content_preset_id:
        return None
    preset = session.get(ContentPreset, project.content_preset_id)
    return (preset.voice_id if preset else "") or None


def _apply_style(prompt: str, style: str) -> str:
    style = style.strip()
    return (
        f"{prompt.strip()}\n\n"
        f"Visual style directive (mandatory, overrides any generic render look): {style}"
        if style
        else prompt.strip()
    )


def _write_project_snapshot(project: Project, folder: Path) -> None:
    (folder / "project.json").write_text(
        json.dumps(
            {
                "id": project.id,
                "title": project.title,
                "topic_prompt": project.topic_prompt,
                "platform_preset_id": project.platform_preset_id,
                "content_preset_id": project.content_preset_id,
                "target_duration_seconds": project.target_duration_seconds,
                "stage": project.stage.value,
                "created_at": project.created_at.isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# Stage 1 — Script generation
# --------------------------------------------------------------------------- #
def generate_script(project_id: str) -> None:
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project:
            return
        _set_stage(session, project, Stage.SCRIPT_GENERATING, "Writing script…")

        platform = session.get(PlatformPreset, project.platform_preset_id) if project.platform_preset_id else None
        content = session.get(ContentPreset, project.content_preset_id) if project.content_preset_id else None

        folder = Path(project_folder(project.title))
        project.folder_path = folder.relative_to(settings.projects_dir.parent.parent).as_posix()
        session.add(project)
        session.commit()

        try:
            gen = get_script_generator()
            user_prompt = prompts.build_script_prompt(
                platform.format_prompt if platform else "",
                content.content_prompt if content else "",
                project.topic_prompt,
                project.target_duration_seconds,
            )
            script = gen.generate(
                prompts.CORE_SYSTEM_PROMPT,
                user_prompt,
                prompts.SCRIPT_JSON_SCHEMA,
                project.topic_prompt,
                project.target_duration_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            if _stop_if_canceled(session, project):
                return
            _fail(session, project, "script generation", exc)
            return
        if _stop_if_canceled(session, project):
            return

        # Persist raw script.json snapshot.
        (folder / "script.json").write_text(
            json.dumps(
                {
                    "scenes": [vars(s) for s in script.scenes],
                    "metadata": script.metadata,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        # Replace existing scenes for this project.
        for old in session.exec(select(Scene).where(Scene.project_id == project.id)):
            session.delete(old)
        session.commit()

        characters = session.exec(select(Character)).all()
        by_name = {c.name.lower(): c for c in characters}

        for idx, s in enumerate(script.scenes):
            matched, suggested = [], []
            for cname in s.characters:
                found = by_name.get(cname.lower())
                if found:
                    matched.append(found.id)
                    link_exists = session.get(ProjectCharacter, (project.id, found.id))
                    if not link_exists:
                        session.add(ProjectCharacter(project_id=project.id, character_id=found.id))
                else:
                    suggested.append(cname)
            session.add(
                Scene(
                    project_id=project.id,
                    order_index=idx,
                    narration_text=s.narration_text,
                    image_prompt=s.image_prompt,
                    continuity_context=s.continuity_context,
                    scene_type=SceneType(s.scene_type),
                    character_ids=matched,
                    suggested_characters=suggested,
                    status="pending",
                )
            )
        # Stash metadata in settings-like project field via script.json (already saved).
        session.commit()
        _write_project_snapshot(project, folder)
        if _stop_if_canceled(session, project):
            return
        _set_stage(session, project, Stage.SCRIPT_READY, "Script ready for review")


# --------------------------------------------------------------------------- #
# Approvals & chaining
# --------------------------------------------------------------------------- #
def approve_script(project_id: str) -> None:
    """SCRIPT_APPROVED -> AUDIO (auto) -> CAST_REVIEW (or straight to storyboard)."""
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project:
            return
        _set_stage(session, project, Stage.SCRIPT_APPROVED, "Script approved")
    generate_audio(project_id)
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project or project.stage in (Stage.CANCELED, Stage.FAILED):
            return
    enter_cast_review(project_id)


def enter_cast_review(project_id: str) -> None:
    """Pause for character-sheet review before scene images are generated.

    If the script references no characters, there is nothing to review, so we
    skip straight to storyboard generation.
    """
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project or project.stage in (Stage.CANCELED, Stage.FAILED):
            return
        cast = compute_cast(session, project)
        if not cast:
            _set_stage(session, project, Stage.CAST_REVIEW, "No characters — continuing")
        else:
            missing = sum(1 for c in cast if not c["has_sheet"])
            msg = (
                f"{missing} of {len(cast)} character sheet(s) still needed"
                if missing
                else "All character sheets ready"
            )
            _set_stage(session, project, Stage.CAST_REVIEW, msg)
            return
    # empty cast -> proceed automatically
    generate_storyboard(project_id)


def approve_cast(project_id: str) -> None:
    """Character sheets reviewed -> generate the storyboard images."""
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project:
            return
        if project.stage not in (Stage.CAST_REVIEW,):
            return
    generate_storyboard(project_id)


def approve_storyboard(project_id: str) -> None:
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project:
            return
        _set_stage(session, project, Stage.STORYBOARD_APPROVED, "Storyboard approved")
    generate_clips(project_id)


def approve_clips(project_id: str) -> None:
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project:
            return
        _set_stage(session, project, Stage.CLIPS_APPROVED, "Clips approved")
    render(project_id)


def step_back(project_id: str) -> None:
    """Move one review checkpoint back without deleting generated assets."""
    previous = {
        Stage.SCRIPT_READY: Stage.IDEA,
        Stage.CAST_REVIEW: Stage.SCRIPT_READY,
        Stage.STORYBOARD_READY: Stage.CAST_REVIEW,
        Stage.CLIPS_READY: Stage.STORYBOARD_READY,
        Stage.DONE: Stage.CLIPS_READY,
    }
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project or project.stage not in previous:
            return
        _set_stage(session, project, previous[project.stage], "Moved back one step")


def step_forward(project_id: str) -> None:
    """Move to the next completed checkpoint without regenerating assets."""
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project:
            return

        scenes = session.exec(
            select(Scene).where(Scene.project_id == project.id).order_by(Scene.order_index)
        ).all()

        if project.stage == Stage.IDEA and scenes:
            _set_stage(session, project, Stage.SCRIPT_READY, "Moved forward one step")
        elif project.stage == Stage.SCRIPT_READY and _audio_ready(scenes):
            _set_stage(session, project, Stage.CAST_REVIEW, "Moved forward one step")
        elif project.stage == Stage.CAST_REVIEW and _storyboard_ready(scenes):
            _set_stage(session, project, Stage.STORYBOARD_READY, "Moved forward one step")
        elif project.stage == Stage.STORYBOARD_READY and _clips_ready(scenes):
            _set_stage(session, project, Stage.CLIPS_READY, "Moved forward one step")
        elif project.stage == Stage.CLIPS_READY and _final_ready(project):
            _set_stage(session, project, Stage.DONE, "Moved forward one step")


def retry_failed_step(project_id: str) -> None:
    """Retry the pipeline step that failed or was canceled."""
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project or project.stage not in (Stage.CANCELED, Stage.FAILED):
            return
        failed_stage = _interrupted_stage(project)
        resume_stage = failed_stage or Stage.SCRIPT_GENERATING
        message = "Resuming canceled step" if project.stage == Stage.CANCELED else "Retrying failed step"
        _set_stage(session, project, resume_stage, message)

    if failed_stage == Stage.SCRIPT_GENERATING:
        generate_script(project_id)
    elif failed_stage == Stage.SCRIPT_APPROVED:
        generate_audio(project_id)
        enter_cast_review(project_id)
    elif failed_stage == Stage.AUDIO_GENERATING:
        generate_audio(project_id)
        enter_cast_review(project_id)
    elif failed_stage == Stage.CAST_REVIEW:
        generate_missing_sheets(project_id)
    elif failed_stage == Stage.STORYBOARD_GENERATING:
        generate_storyboard(project_id)
    elif failed_stage == Stage.STORYBOARD_APPROVED:
        generate_clips(project_id)
    elif failed_stage == Stage.CLIPS_GENERATING:
        generate_clips(project_id)
    elif failed_stage == Stage.CLIPS_APPROVED:
        render(project_id)
    elif failed_stage == Stage.RENDERING:
        render(project_id)
    elif failed_stage in (
        Stage.IDEA,
        Stage.SCRIPT_READY,
        Stage.STORYBOARD_READY,
        Stage.CLIPS_READY,
        Stage.DONE,
    ):
        return
    else:
        generate_script(project_id)


def _interrupted_stage(project: Project) -> Stage | None:
    if project.canceled_stage:
        try:
            return Stage(project.canceled_stage)
        except ValueError:
            pass
    if project.failed_stage:
        try:
            return Stage(project.failed_stage)
        except ValueError:
            pass
    error = (project.error or "").lower()
    if "audio generation" in error:
        return Stage.AUDIO_GENERATING
    if "character sheet" in error:
        return Stage.CAST_REVIEW
    if "storyboard generation" in error:
        return Stage.STORYBOARD_GENERATING
    if "clip generation" in error:
        return Stage.CLIPS_GENERATING
    if "final render" in error:
        return Stage.RENDERING
    if "script generation" in error:
        return Stage.SCRIPT_GENERATING
    return None


def _audio_ready(scenes: list[Scene]) -> bool:
    return bool(scenes) and all(
        _asset_exists(scene.audio_path) and _asset_exists(scene.timestamps_path)
        for scene in scenes
    )


def _storyboard_ready(scenes: list[Scene]) -> bool:
    return bool(scenes) and all(_asset_exists(scene.image_path) for scene in scenes)


def _clips_ready(scenes: list[Scene]) -> bool:
    video_scenes = [scene for scene in scenes if scene.scene_type == SceneType.VIDEO]
    return bool(scenes) and all(_asset_exists(scene.clip_path) for scene in video_scenes)


def _final_ready(project: Project) -> bool:
    if not project.folder_path:
        return False
    folder = _folder(project)
    return (folder / "final" / "metadata.json").exists() and any((folder / "final").glob("*.mp4"))


# --------------------------------------------------------------------------- #
# Cast / character sheets (reviewed before scene images are generated)
# --------------------------------------------------------------------------- #
def compute_cast(session: Session, project: Project) -> list[dict]:
    """The distinct characters referenced by this project's scenes.

    Each entry reports whether a reference sheet exists yet, so the UI can flag
    what's missing before storyboard generation.
    """
    scenes = session.exec(select(Scene).where(Scene.project_id == project.id)).all()
    order: list[str] = []
    linked: dict[str, str] = {}  # name(lower) -> character_id
    for scene in scenes:
        for cid in scene.character_ids:
            char = session.get(Character, cid)
            if char:
                key = char.name.lower()
                linked[key] = char.id
                if key not in order:
                    order.append(key)
        for name in scene.suggested_characters:
            key = name.lower()
            if key not in order:
                order.append(key)

    # Resolve display names & sheet status.
    by_id = {c.id: c for c in session.exec(select(Character)).all()}
    by_name = {c.name.lower(): c for c in by_id.values()}
    cast: list[dict] = []
    for key in order:
        char = by_id.get(linked.get(key)) or by_name.get(key)
        if char:
            has_sheet = bool(char.reference_image_path)
            cast.append({
                "name": char.name,
                "character_id": char.id,
                "description": char.description,
                "has_sheet": has_sheet,
                "reference_image_path": char.reference_image_path,
                "reference_version": char.reference_version,
                "reference_prompt": char.reference_prompt,
                "reference_style_prompt": char.reference_style_prompt,
            })
        else:
            # Suggested-but-not-yet-created character.
            display = next(
                (n for s in scenes for n in s.suggested_characters if n.lower() == key),
                key.title(),
            )
            cast.append({
                "name": display,
                "character_id": None,
                "description": "",
                "has_sheet": False,
                "reference_image_path": None,
                "reference_version": 0,
                "reference_prompt": "",
                "reference_style_prompt": "",
            })
    return cast


def generate_character_sheet(
    project_id: str,
    name: str,
    description: str | None,
    prompt: str | None,
    generate_description: bool,
) -> None:
    """Create/attach a global character and generate its reference sheet.

    Uses the project's content-preset image style so sheets match the scenes.
    """
    from .storage import character_folder

    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project or project.stage in (Stage.CANCELED, Stage.FAILED):
            return
        _set_msg(session, project, f"Generating character sheet: {name}…")

        existing = session.exec(
            select(Character).where(Character.name == name)
        ).first() or next(
            (c for c in session.exec(select(Character)).all() if c.name.lower() == name.lower()),
            None,
        )
        char = existing or Character(name=name.strip())

        if description:
            char.description = description.strip()
        elif not char.description:
            if generate_description:
                content = (
                    session.get(ContentPreset, project.content_preset_id)
                    if project.content_preset_id
                    else None
                )
                char.description = ai_character_description(
                    char.name, project.topic_prompt, content.content_prompt if content else ""
                )
            else:
                char.description = f"{char.name}, a character in {project.topic_prompt}."
        if _stop_if_canceled(session, project):
            return
        session.add(char)
        session.commit()
        session.refresh(char)

        style = _content_style(session, project)
        ref_prompt = _apply_style(prompt, style) if prompt else _default_sheet_prompt(char.name, char.description, style)
        try:
            folder = character_folder(char.name)
            out = get_image_generator().generate(ref_prompt, folder / "reference.png", None)
            if _stop_if_canceled(session, project):
                return
            char.reference_image_path = _rel(out)
            char.reference_prompt = ref_prompt
            char.reference_style_prompt = style
            char.reference_version = (char.reference_version or 0) + 1
            session.add(char)
            session.commit()
        except Exception as exc:  # noqa: BLE001
            if _stop_if_canceled(session, project):
                return
            _fail(session, project, "character sheet", exc)
            return

        # Link to project and reconcile scenes (suggested name -> linked id).
        if not session.get(ProjectCharacter, (project.id, char.id)):
            session.add(ProjectCharacter(project_id=project.id, character_id=char.id))
        low = char.name.lower()
        for scene in session.exec(select(Scene).where(Scene.project_id == project.id)):
            referenced = any(n.lower() == low for n in scene.suggested_characters)
            if not referenced:
                continue
            scene.suggested_characters = [n for n in scene.suggested_characters if n.lower() != low]
            if char.id not in scene.character_ids:
                scene.character_ids = [*scene.character_ids, char.id]
            session.add(scene)
        session.commit()

        cast = compute_cast(session, project)
        missing = sum(1 for c in cast if not c["has_sheet"])
        _set_msg(
            session,
            project,
            f"{missing} of {len(cast)} character sheet(s) still needed"
            if missing
            else "All character sheets ready",
        )
        bus.publish("character.updated", character_id=char.id, project_ids=[project.id])
        bus.publish("cast.updated", project_id=project.id)


def generate_missing_sheets(project_id: str) -> None:
    """Auto-generate reference sheets for every character still missing one."""
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project:
            return
        missing = [c["name"] for c in compute_cast(session, project) if not c["has_sheet"]]
    for name in missing:
        with Session(engine) as session:
            project = session.get(Project, project_id)
            if not project or _stop_if_canceled(session, project):
                return
        generate_character_sheet(project_id, name, None, None, generate_description=True)


def _default_sheet_prompt(name: str, description: str, style: str) -> str:
    base = (
        f"Character reference sheet of {name}. {description} "
        f"Full-body, front view, neutral background, consistent identity, clean "
        f"lighting, high detail."
    )
    return _apply_style(base, style)


def _set_msg(session: Session, project: Project, message: str) -> None:
    project.status_message = message
    _touch(project)
    session.add(project)
    session.commit()
    bus.publish("project.stage", project_id=project.id, stage=project.stage.value, message=message)


# --------------------------------------------------------------------------- #
# Stage 2 — Audio + timestamps (auto, no approval)
# --------------------------------------------------------------------------- #
def generate_audio(project_id: str) -> None:
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project:
            return
        _set_stage(session, project, Stage.AUDIO_GENERATING, "Generating narration audio…")
        folder = _folder(project)
        scenes = session.exec(
            select(Scene).where(Scene.project_id == project.id).order_by(Scene.order_index)
        ).all()
        try:
            tts = get_tts_generator(_content_voice_id(session, project))
            for scene in scenes:
                if _stop_if_canceled(session, project):
                    return
                if _asset_exists(scene.audio_path) and _asset_exists(scene.timestamps_path):
                    if scene.status != "ready":
                        scene.status = "ready"
                        session.add(scene)
                        session.commit()
                        bus.publish("scene.updated", project_id=project.id, scene_id=scene.id)
                    continue
                scene.status = "generating"
                session.add(scene)
                session.commit()
                bus.publish("scene.status", project_id=project.id, scene_id=scene.id, status="generating", stage="audio")
                audio_out = folder / "audio" / f"scene_{scene.order_index + 1:02d}.mp3"
                ts_out = folder / "audio" / f"scene_{scene.order_index + 1:02d}.timestamps.json"
                result = tts.synthesize(scene.narration_text, audio_out, ts_out)
                if _stop_if_canceled(session, project):
                    return
                scene.audio_path = _rel(audio_out)
                scene.timestamps_path = _rel(ts_out)
                scene.duration_seconds = result.duration_seconds
                scene.asset_version = (scene.asset_version or 0) + 1
                scene.status = "ready"
                session.add(scene)
                session.commit()
                bus.publish("scene.updated", project_id=project.id, scene_id=scene.id)

            # Concatenate full narration for the final mux.
            if _stop_if_canceled(session, project):
                return
            ordered_audio = [Path(settings.projects_dir.parent.parent) / s.audio_path for s in scenes if s.audio_path]
            ffmpeg.concat_audio(ordered_audio, folder / "audio" / "full_narration.mp3")
        except Exception as exc:  # noqa: BLE001
            if _stop_if_canceled(session, project):
                return
            _fail(session, project, "audio generation", exc)
            return


# --------------------------------------------------------------------------- #
# Stage 3 — Storyboard / images
# --------------------------------------------------------------------------- #
def generate_storyboard(project_id: str) -> None:
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project or project.stage in (Stage.CANCELED, Stage.FAILED):
            return
        _set_stage(session, project, Stage.STORYBOARD_GENERATING, "Generating storyboard images…")
        folder = _folder(project)
        scenes = session.exec(
            select(Scene).where(Scene.project_id == project.id).order_by(Scene.order_index)
        ).all()
        try:
            img = get_image_generator()
            for scene in scenes:
                if _stop_if_canceled(session, project):
                    return
                if _asset_exists(scene.image_path):
                    if scene.status != "ready":
                        scene.status = "ready"
                        session.add(scene)
                        session.commit()
                        bus.publish("scene.updated", project_id=project.id, scene_id=scene.id)
                    continue
                if not _generate_scene_image(session, project, scene, folder, img):
                    return
        except Exception as exc:  # noqa: BLE001
            if _stop_if_canceled(session, project):
                return
            _fail(session, project, "storyboard generation", exc)
            return
        if _stop_if_canceled(session, project):
            return
        _set_stage(session, project, Stage.STORYBOARD_READY, "Storyboard ready for review")


def _generate_scene_image(session, project, scene: Scene, folder: Path, img) -> bool:
    if _stop_if_canceled(session, project):
        return False
    bus.publish("scene.status", project_id=project.id, scene_id=scene.id, status="generating", stage="image")
    scene.status = "generating"
    session.add(scene)
    session.commit()

    character_refs = _scene_character_refs(session, scene)
    continuity_refs = _scene_continuity_refs(session, project, scene)
    refs = [ref["path"] for ref in character_refs] + [ref["path"] for ref in continuity_refs]

    out = folder / "images" / f"scene_{scene.order_index + 1:02d}.png"
    ref_label = "style reference image" if getattr(img, "name", "") == "krea" else "reference image panel"
    prompt = _apply_continuity_context(
        _apply_character_context(
            _apply_style(scene.image_prompt, _content_style(session, project)),
            character_refs,
            ref_label,
        ),
        continuity_refs,
        ref_label,
        len(character_refs),
    )
    result = img.generate(prompt, out, refs or None)
    if _stop_if_canceled(session, project):
        return False
    scene.image_path = _rel(result)
    scene.status = "ready"
    scene.asset_version = (scene.asset_version or 0) + 1
    session.add(scene)
    session.commit()
    bus.publish("scene.updated", project_id=project.id, scene_id=scene.id)
    return True


def _scene_character_refs(session, scene: Scene) -> list[dict]:
    """Character identity refs for prompts, images, and video elements."""
    root = Path(settings.projects_dir.parent.parent)
    refs: list[dict] = []
    for cid in scene.character_ids:
        char = session.get(Character, cid)
        if not char or not char.reference_image_path:
            continue
        path = root / char.reference_image_path
        if not path.exists():
            continue
        variants = [root / v for v in (char.variant_paths or []) if (root / v).exists()]
        refs.append({
            "name": char.name,
            "description": char.description,
            "path": path,
            "variants": variants,
        })
    return refs


def _scene_continuity_refs(session, project: Project, scene: Scene, limit: int = 10) -> list[dict]:
    """Active earlier scene refs explicitly requested by the script output."""
    excluded = set(scene.excluded_context_scene_ids or [])
    candidates = [ref for ref in _scene_continuity_candidates(session, project, scene) if ref["scene_id"] not in excluded]
    selected = candidates[:limit]
    return sorted(selected, key=lambda ref: ref["order_index"])


def scene_context_refs(session, project: Project, scene: Scene, limit: int = 10) -> list[dict]:
    """Continuity refs surfaced to the UI, including explicitly excluded matches."""
    excluded = set(scene.excluded_context_scene_ids or [])
    candidates = _scene_continuity_candidates(session, project, scene)
    active = [ref for ref in candidates if ref["scene_id"] not in excluded][:limit]
    active_ids = {ref["scene_id"] for ref in active}
    visible = active + [ref for ref in candidates if ref["scene_id"] in excluded and ref["scene_id"] not in active_ids]
    return sorted(
        [{**ref, "excluded": ref["scene_id"] in excluded} for ref in visible],
        key=lambda ref: (ref["excluded"], ref["order_index"]),
    )


def _scene_continuity_candidates(session, project: Project, scene: Scene) -> list[dict]:
    """Earlier scene image candidates explicitly named in continuity_context."""
    root = Path(settings.projects_dir.parent.parent)
    if not scene.continuity_context:
        return []
    previous_scenes = session.exec(
        select(Scene)
        .where(
            Scene.project_id == project.id,
            Scene.order_index < scene.order_index,
            Scene.image_path.is_not(None),
        )
        .order_by(Scene.order_index)
    ).all()
    by_scene_number = {prev.order_index + 1: prev for prev in previous_scenes}

    candidates: list[tuple[int, dict]] = []
    seen_scene_ids: set[str] = set()
    for item in scene.continuity_context:
        try:
            source_scene = int(item.get("source_scene", 0))
        except (AttributeError, TypeError, ValueError):
            continue
        prev = by_scene_number.get(source_scene)
        if not prev or prev.id in seen_scene_ids:
            continue
        path = root / prev.image_path
        if not path.exists():
            continue
        anchor = str(item.get("visual_anchor", "")).strip()
        reason = str(item.get("reason", "")).strip()
        seen_scene_ids.add(prev.id)
        candidates.append((prev.order_index, {
            "scene_id": prev.id,
            "scene_number": prev.order_index + 1,
            "order_index": prev.order_index,
            "image_path": prev.image_path,
            "asset_version": prev.asset_version or 0,
            "prompt": prev.image_prompt,
            "path": path,
            "visual_anchor": anchor,
            "reason": reason,
            "matches": [anchor] if anchor else [],
        }))
    return [ref for _, ref in sorted(candidates, key=lambda item: item[0])]


def _apply_character_context(prompt: str, character_refs: list[dict], label: str) -> str:
    if not character_refs:
        return prompt
    lines = []
    for idx, ref in enumerate(character_refs, start=1):
        desc = f" - {ref['description']}" if ref.get("description") else ""
        lines.append(f"{idx}. {ref['name']}{desc}")
    mapping = "\n".join(lines)
    return (
        f"{prompt.strip()}\n\n"
        f"Character identity map (mandatory): attached {label}s are in this order:\n"
        f"{mapping}\n"
        f"Use the named characters exactly as mapped above, keep identities consistent, "
        f"and do not swap characters when multiple people appear."
    )


def _apply_continuity_context(
    prompt: str,
    continuity_refs: list[dict],
    label: str,
    start_index: int,
) -> str:
    if not continuity_refs:
        return prompt
    lines = []
    for offset, ref in enumerate(continuity_refs, start=1):
        attached_index = start_index + offset
        anchor = ref.get("visual_anchor") or ", ".join(ref.get("matches", [])[:5])
        reason = ref.get("reason", "")
        anchor_note = f" anchor: {anchor} -" if anchor else ""
        reason_note = f" Reason: {reason}" if reason else ""
        lines.append(
            f"{attached_index}. Scene {ref['scene_number']} continuity reference -{anchor_note} "
            f"{ref['prompt']}{reason_note}"
        )
    mapping = "\n".join(lines)
    return (
        f"{prompt.strip()}\n\n"
        f"Visual continuity map (mandatory): attached {label}s after the character "
        f"references show earlier scenes in this story:\n"
        f"{mapping}\n"
        f"Carry forward any recurring context-bound props, costumes, architecture, "
        f"landmarks, lighting logic, and spatial relationships that still belong in "
        f"this moment. Do not introduce contradictions to established objects unless "
        f"the current scene explicitly changes them."
    )


def _build_clip_prompt(scene_prompt: str, element_refs: list[dict]) -> str:
    prompt = (
        "Animate the provided start image. Preserve the exact rendered style, "
        "character appearances, props, environment, composition, and lighting from "
        "the image; do not redesign the scene. Motion direction: "
        f"{scene_prompt.strip()}"
    )
    return _limit_prompt(_apply_kling_element_context(prompt, element_refs), _MAX_VIDEO_PROMPT_CHARS)


def _apply_kling_element_context(prompt: str, element_refs: list[dict]) -> str:
    if not element_refs:
        return prompt
    lines = []
    for idx, ref in enumerate(element_refs, start=1):
        desc = _short_text(ref.get("description", ""), 90)
        desc = f" - {desc}" if desc else ""
        lines.append(f"@Element{idx} = {ref['name']}{desc}")
    mapping = "\n".join(lines)
    return (
        f"{prompt.strip()}\n\n"
        f"Kling element identity map (mandatory):\n"
        f"{mapping}\n"
        f"When describing these characters, reference the exact @Element handles above. "
        f"Keep each @Element identity consistent and do not swap them."
    )


def _short_text(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _limit_prompt(prompt: str, limit: int) -> str:
    prompt = prompt.strip()
    return prompt if len(prompt) <= limit else prompt[: limit - 3].rstrip() + "..."


# --------------------------------------------------------------------------- #
# Stage 4 — Video clips (only scenes marked "video")
# --------------------------------------------------------------------------- #
def generate_clips(project_id: str) -> None:
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project or project.stage in (Stage.CANCELED, Stage.FAILED):
            return
        _set_stage(session, project, Stage.CLIPS_GENERATING, "Generating motion clips…")
        folder = _folder(project)
        scenes = session.exec(
            select(Scene).where(Scene.project_id == project.id).order_by(Scene.order_index)
        ).all()
        try:
            vid = get_video_generator()
            for scene in scenes:
                if _stop_if_canceled(session, project):
                    return
                if scene.scene_type != SceneType.VIDEO or not scene.image_path:
                    continue
                if _asset_exists(scene.clip_path):
                    if scene.status != "ready":
                        scene.status = "ready"
                        session.add(scene)
                        session.commit()
                        bus.publish("scene.updated", project_id=project.id, scene_id=scene.id)
                    continue
                if not _generate_scene_clip(session, project, scene, folder, vid):
                    return
        except Exception as exc:  # noqa: BLE001
            if _stop_if_canceled(session, project):
                return
            _fail(session, project, "clip generation", exc)
            return
        if _stop_if_canceled(session, project):
            return
        _set_stage(session, project, Stage.CLIPS_READY, "Clips ready for review")


def _scene_elements(session, scene: Scene) -> list[tuple[Path, list[Path]]]:
    """Identity refs for the scene's linked characters: (reference sheet, variants)."""
    return [(ref["path"], ref["variants"]) for ref in _scene_character_refs(session, scene)]


def _next_scene_image(session, project, scene: Scene) -> Path | None:
    """The following scene's still, used as the clip's end frame for continuity."""
    root = Path(settings.projects_dir.parent.parent)
    nxt = session.exec(
        select(Scene)
        .where(Scene.project_id == project.id, Scene.order_index > scene.order_index)
        .order_by(Scene.order_index)
    ).first()
    if nxt and nxt.image_path:
        p = root / nxt.image_path
        if p.exists():
            return p
    return None


def _generate_scene_clip(session, project, scene: Scene, folder: Path, vid) -> bool:
    if _stop_if_canceled(session, project):
        return False
    bus.publish("scene.status", project_id=project.id, scene_id=scene.id, status="generating", stage="clip")
    scene.status = "generating"
    session.add(scene)
    session.commit()
    image_abs = Path(settings.projects_dir.parent.parent) / scene.image_path
    out = folder / "clips" / f"scene_{scene.order_index + 1:02d}.mp4"
    duration = scene.duration_seconds or 3.0
    character_refs = _scene_character_refs(session, scene)
    element_limit = getattr(vid, "_MAX_ELEMENTS", len(character_refs))
    element_refs = character_refs[:element_limit]
    prompt = _build_clip_prompt(scene.image_prompt, element_refs)
    result = vid.generate(
        image_abs,
        prompt,
        out,
        duration,
        elements=[(ref["path"], ref["variants"]) for ref in element_refs] or None,
        end_image_path=_next_scene_image(session, project, scene),
    )
    if _stop_if_canceled(session, project):
        return False
    scene.clip_path = _rel(result)
    scene.asset_version = (scene.asset_version or 0) + 1
    scene.status = "ready"
    session.add(scene)
    session.commit()
    bus.publish("scene.updated", project_id=project.id, scene_id=scene.id)
    return True


# --------------------------------------------------------------------------- #
# Stage 5 — Final render + metadata
# --------------------------------------------------------------------------- #
def render(project_id: str) -> None:
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project or project.stage in (Stage.CANCELED, Stage.FAILED):
            return
        _set_stage(session, project, Stage.RENDERING, "Rendering final video…")
        folder = _folder(project)
        scenes = session.exec(
            select(Scene).where(Scene.project_id == project.id).order_by(Scene.order_index)
        ).all()
        root = Path(settings.projects_dir.parent.parent)
        try:
            segments: list[Path] = []
            seg_dir = folder / "final" / "segments"
            seg_dir.mkdir(parents=True, exist_ok=True)
            for scene in scenes:
                if _stop_if_canceled(session, project):
                    return
                duration = scene.duration_seconds or 3.0
                seg = seg_dir / f"seg_{scene.order_index + 1:02d}.mp4"
                if scene.scene_type == SceneType.VIDEO and scene.clip_path and (root / scene.clip_path).exists():
                    ffmpeg._normalize_clip(root / scene.clip_path, seg, duration)
                elif scene.image_path and (root / scene.image_path).exists():
                    ffmpeg.ken_burns_clip(root / scene.image_path, seg, duration)
                else:
                    continue
                segments.append(seg)

            # Merge timestamps into a global timeline & build burned captions.
            if _stop_if_canceled(session, project):
                return
            ts_files = [root / s.timestamps_path for s in scenes if s.timestamps_path]
            durations = [s.duration_seconds or 0.0 for s in scenes]
            timeline = ffmpeg.merge_timestamps(ts_files, durations)
            (folder / "audio" / "timeline.json").write_text(json.dumps(timeline, indent=2), encoding="utf-8")
            captions = ffmpeg.build_ass_captions(timeline, folder / "final" / "captions.ass")

            narration = folder / "audio" / "full_narration.mp3"
            music_path = None
            if project.music_enabled and project.music_track_id:
                music_track = session.get(MusicTrack, project.music_track_id)
                music_path = local_track_path(music_track)
                if music_track and music_path is None:
                    music_path = ensure_downloaded(project, music_track)
                    session.add(music_track)
                    session.add(project)
                    session.commit()
            from .storage import slugify

            out = folder / "final" / f"{slugify(project.title)}.mp4"
            ffmpeg.render_final(segments, narration, captions, out, music_path, project.music_volume)
            if _stop_if_canceled(session, project):
                return

            _write_metadata(folder, project)
        except Exception as exc:  # noqa: BLE001
            if _stop_if_canceled(session, project):
                return
            _fail(session, project, "final render", exc)
            return
        if _stop_if_canceled(session, project):
            return
        _set_stage(session, project, Stage.DONE, "Done")


def _write_metadata(folder: Path, project: Project) -> None:
    """Reuse the script-generation metadata for final/metadata.json."""
    meta = {}
    script_path = folder / "script.json"
    if script_path.exists():
        try:
            meta = json.loads(script_path.read_text(encoding="utf-8")).get("metadata", {})
        except json.JSONDecodeError:
            meta = {}
    meta.setdefault("title", project.title)
    meta.setdefault("description", "")
    meta.setdefault("hashtags", [])
    meta.setdefault(
        "suggested_caption",
        f"{meta.get('title', project.title)} {' '.join(meta.get('hashtags', []))}".strip(),
    )
    (folder / "final" / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Per-scene regeneration (any stage, single scene, no full restart)
# --------------------------------------------------------------------------- #
def regenerate_scene_audio(project_id: str, scene_id: str) -> None:
    with Session(engine) as session:
        project = session.get(Project, project_id)
        scene = session.get(Scene, scene_id)
        if not project or not scene:
            return
        folder = _folder(project)
        try:
            if _stop_if_canceled(session, project):
                return
            bus.publish("scene.status", project_id=project.id, scene_id=scene.id, status="generating", stage="audio")
            scene.status = "generating"
            session.add(scene)
            session.commit()
            tts = get_tts_generator(_content_voice_id(session, project))
            audio_out = folder / "audio" / f"scene_{scene.order_index + 1:02d}.mp3"
            ts_out = folder / "audio" / f"scene_{scene.order_index + 1:02d}.timestamps.json"
            result = tts.synthesize(scene.narration_text, audio_out, ts_out)
            if _stop_if_canceled(session, project):
                return
            scene.audio_path = _rel(audio_out)
            scene.timestamps_path = _rel(ts_out)
            scene.duration_seconds = result.duration_seconds
            scene.asset_version = (scene.asset_version or 0) + 1
            scene.status = "ready"
            session.add(scene)
            session.commit()
            ordered = session.exec(
                select(Scene).where(Scene.project_id == project.id).order_by(Scene.order_index)
            ).all()
            if _stop_if_canceled(session, project):
                return
            ffmpeg.concat_audio(
                [Path(settings.projects_dir.parent.parent) / s.audio_path for s in ordered if s.audio_path],
                folder / "audio" / "full_narration.mp3",
            )
            bus.publish("scene.updated", project_id=project.id, scene_id=scene.id)
        except Exception:  # noqa: BLE001
            if _stop_if_canceled(session, project):
                return
            traceback.print_exc()


def regenerate_scene_image(project_id: str, scene_id: str) -> None:
    with Session(engine) as session:
        project = session.get(Project, project_id)
        scene = session.get(Scene, scene_id)
        if not project or not scene:
            return
        folder = _folder(project)
        try:
            _generate_scene_image(session, project, scene, folder, get_image_generator())
        except Exception:  # noqa: BLE001
            if _stop_if_canceled(session, project):
                return
            traceback.print_exc()


def regenerate_scene_clip(project_id: str, scene_id: str) -> None:
    with Session(engine) as session:
        project = session.get(Project, project_id)
        scene = session.get(Scene, scene_id)
        if not project or not scene or not scene.image_path:
            return
        folder = _folder(project)
        try:
            _generate_scene_clip(session, project, scene, folder, get_video_generator())
        except Exception:  # noqa: BLE001
            if _stop_if_canceled(session, project):
                return
            traceback.print_exc()


def _rel(path: Path) -> str:
    return Path(path).resolve().relative_to(settings.projects_dir.parent.parent).as_posix()
