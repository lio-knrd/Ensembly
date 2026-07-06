"""Pipeline state machine + background orchestration (spec sections 8-9).

Long-running generation runs off the request thread (a small thread pool) so the
UI never blocks; progress is pushed over the WebSocket event bus. Any stage can
be re-run for a single scene or the whole project without restarting from IDEA.
"""
from __future__ import annotations

import json
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

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
    PlatformPreset,
    Project,
    ProjectCharacter,
    Scene,
    SceneType,
    Stage,
)
from .services import ffmpeg
from .storage import project_folder

_executor = ThreadPoolExecutor(max_workers=2)


def submit(fn, *args) -> None:
    """Enqueue a pipeline step to run off the request thread."""
    _executor.submit(_guarded, fn, *args)


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
    _touch(project)
    session.add(project)
    session.commit()
    bus.publish("project.stage", project_id=project.id, stage=stage.value, message=message)


def _fail(session: Session, project: Project, where: str, exc: Exception) -> None:
    project.stage = Stage.FAILED
    project.error = f"{where}: {exc}"
    project.status_message = None
    _touch(project)
    session.add(project)
    session.commit()
    bus.publish("project.failed", project_id=project.id, error=project.error)


def _folder(project: Project) -> Path:
    return Path(settings.projects_dir.parent.parent) / project.folder_path if project.folder_path else project_folder(project.title)


def _content_style(session: Session, project: Project) -> str:
    """The image style prompt from the project's content preset (may be empty)."""
    if not project.content_preset_id:
        return ""
    preset = session.get(ContentPreset, project.content_preset_id)
    return (preset.image_style_prompt if preset else "") or ""


def _apply_style(prompt: str, style: str) -> str:
    style = style.strip()
    return f"{prompt.strip()}\n\nStyle: {style}" if style else prompt.strip()


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
            _fail(session, project, "script generation", exc)
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
                    scene_type=SceneType(s.scene_type),
                    character_ids=matched,
                    suggested_characters=suggested,
                    status="pending",
                )
            )
        # Stash metadata in settings-like project field via script.json (already saved).
        session.commit()
        _write_project_snapshot(project, folder)
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
    enter_cast_review(project_id)


def enter_cast_review(project_id: str) -> None:
    """Pause for character-sheet review before scene images are generated.

    If the script references no characters, there is nothing to review, so we
    skip straight to storyboard generation.
    """
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project or project.stage == Stage.FAILED:
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
        if not project:
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
        session.add(char)
        session.commit()
        session.refresh(char)

        style = _content_style(session, project)
        ref_prompt = prompt.strip() if prompt else _default_sheet_prompt(char.name, char.description, style)
        try:
            folder = character_folder(char.name)
            out = get_image_generator().generate(ref_prompt, folder / "reference.png", None)
            char.reference_image_path = _rel(out)
            session.add(char)
            session.commit()
        except Exception as exc:  # noqa: BLE001
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
        bus.publish("cast.updated", project_id=project.id)


def generate_missing_sheets(project_id: str) -> None:
    """Auto-generate reference sheets for every character still missing one."""
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project:
            return
        missing = [c["name"] for c in compute_cast(session, project) if not c["has_sheet"]]
    for name in missing:
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
            tts = get_tts_generator()
            for scene in scenes:
                bus.publish("scene.status", project_id=project.id, scene_id=scene.id, status="generating", stage="audio")
                audio_out = folder / "audio" / f"scene_{scene.order_index + 1:02d}.mp3"
                ts_out = folder / "audio" / f"scene_{scene.order_index + 1:02d}.timestamps.json"
                result = tts.synthesize(scene.narration_text, audio_out, ts_out)
                scene.audio_path = _rel(audio_out)
                scene.timestamps_path = _rel(ts_out)
                scene.duration_seconds = result.duration_seconds
                session.add(scene)
                session.commit()
                bus.publish("scene.updated", project_id=project.id, scene_id=scene.id)

            # Concatenate full narration for the final mux.
            ordered_audio = [Path(settings.projects_dir.parent.parent) / s.audio_path for s in scenes if s.audio_path]
            ffmpeg.concat_audio(ordered_audio, folder / "audio" / "full_narration.mp3")
        except Exception as exc:  # noqa: BLE001
            _fail(session, project, "audio generation", exc)
            return


# --------------------------------------------------------------------------- #
# Stage 3 — Storyboard / images
# --------------------------------------------------------------------------- #
def generate_storyboard(project_id: str) -> None:
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project or project.stage == Stage.FAILED:
            return
        _set_stage(session, project, Stage.STORYBOARD_GENERATING, "Generating storyboard images…")
        folder = _folder(project)
        scenes = session.exec(
            select(Scene).where(Scene.project_id == project.id).order_by(Scene.order_index)
        ).all()
        try:
            img = get_image_generator()
            for scene in scenes:
                _generate_scene_image(session, project, scene, folder, img)
        except Exception as exc:  # noqa: BLE001
            _fail(session, project, "storyboard generation", exc)
            return
        _set_stage(session, project, Stage.STORYBOARD_READY, "Storyboard ready for review")


def _generate_scene_image(session, project, scene: Scene, folder: Path, img) -> None:
    bus.publish("scene.status", project_id=project.id, scene_id=scene.id, status="generating", stage="image")
    scene.status = "generating"
    session.add(scene)
    session.commit()

    refs: list[Path] = []
    for cid in scene.character_ids:
        char = session.get(Character, cid)
        if char and char.reference_image_path:
            p = Path(settings.projects_dir.parent.parent) / char.reference_image_path
            if p.exists():
                refs.append(p)

    out = folder / "images" / f"scene_{scene.order_index + 1:02d}.png"
    prompt = _apply_style(scene.image_prompt, _content_style(session, project))
    result = img.generate(prompt, out, refs or None)
    scene.image_path = _rel(result)
    scene.status = "ready"
    session.add(scene)
    session.commit()
    bus.publish("scene.updated", project_id=project.id, scene_id=scene.id)


# --------------------------------------------------------------------------- #
# Stage 4 — Video clips (only scenes marked "video")
# --------------------------------------------------------------------------- #
def generate_clips(project_id: str) -> None:
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project or project.stage == Stage.FAILED:
            return
        _set_stage(session, project, Stage.CLIPS_GENERATING, "Generating motion clips…")
        folder = _folder(project)
        scenes = session.exec(
            select(Scene).where(Scene.project_id == project.id).order_by(Scene.order_index)
        ).all()
        try:
            vid = get_video_generator()
            for scene in scenes:
                if scene.scene_type != SceneType.VIDEO or not scene.image_path:
                    continue
                _generate_scene_clip(session, project, scene, folder, vid)
        except Exception as exc:  # noqa: BLE001
            _fail(session, project, "clip generation", exc)
            return
        _set_stage(session, project, Stage.CLIPS_READY, "Clips ready for review")


def _scene_elements(session, scene: Scene) -> list[tuple[Path, list[Path]]]:
    """Identity refs for the scene's linked characters: (reference sheet, variants)."""
    root = Path(settings.projects_dir.parent.parent)
    elements: list[tuple[Path, list[Path]]] = []
    for cid in scene.character_ids:
        char = session.get(Character, cid)
        if not char or not char.reference_image_path:
            continue
        frontal = root / char.reference_image_path
        if not frontal.exists():
            continue
        variants = [root / v for v in (char.variant_paths or []) if (root / v).exists()]
        elements.append((frontal, variants))
    return elements


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


def _generate_scene_clip(session, project, scene: Scene, folder: Path, vid) -> None:
    bus.publish("scene.status", project_id=project.id, scene_id=scene.id, status="generating", stage="clip")
    image_abs = Path(settings.projects_dir.parent.parent) / scene.image_path
    out = folder / "clips" / f"scene_{scene.order_index + 1:02d}.mp4"
    duration = scene.duration_seconds or 3.0
    result = vid.generate(
        image_abs,
        scene.image_prompt,
        out,
        duration,
        elements=_scene_elements(session, scene) or None,
        end_image_path=_next_scene_image(session, project, scene),
    )
    scene.clip_path = _rel(result)
    session.add(scene)
    session.commit()
    bus.publish("scene.updated", project_id=project.id, scene_id=scene.id)


# --------------------------------------------------------------------------- #
# Stage 5 — Final render + metadata
# --------------------------------------------------------------------------- #
def render(project_id: str) -> None:
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project or project.stage == Stage.FAILED:
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
            ts_files = [root / s.timestamps_path for s in scenes if s.timestamps_path]
            durations = [s.duration_seconds or 0.0 for s in scenes]
            timeline = ffmpeg.merge_timestamps(ts_files, durations)
            (folder / "audio" / "timeline.json").write_text(json.dumps(timeline, indent=2), encoding="utf-8")
            captions = ffmpeg.build_ass_captions(timeline, folder / "final" / "captions.ass")

            narration = folder / "audio" / "full_narration.mp3"
            from .storage import slugify

            out = folder / "final" / f"{slugify(project.title)}.mp4"
            ffmpeg.render_final(segments, narration, captions, out)

            _write_metadata(folder, project)
        except Exception as exc:  # noqa: BLE001
            _fail(session, project, "final render", exc)
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
            tts = get_tts_generator()
            audio_out = folder / "audio" / f"scene_{scene.order_index + 1:02d}.mp3"
            ts_out = folder / "audio" / f"scene_{scene.order_index + 1:02d}.timestamps.json"
            result = tts.synthesize(scene.narration_text, audio_out, ts_out)
            scene.audio_path = _rel(audio_out)
            scene.timestamps_path = _rel(ts_out)
            scene.duration_seconds = result.duration_seconds
            session.add(scene)
            session.commit()
            ordered = session.exec(
                select(Scene).where(Scene.project_id == project.id).order_by(Scene.order_index)
            ).all()
            ffmpeg.concat_audio(
                [Path(settings.projects_dir.parent.parent) / s.audio_path for s in ordered if s.audio_path],
                folder / "audio" / "full_narration.mp3",
            )
            bus.publish("scene.updated", project_id=project.id, scene_id=scene.id)
        except Exception:  # noqa: BLE001
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
            traceback.print_exc()


def _rel(path: Path) -> str:
    return Path(path).resolve().relative_to(settings.projects_dir.parent.parent).as_posix()
