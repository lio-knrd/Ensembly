"""Pipeline state machine + background orchestration (spec sections 8-9).

Long-running generation runs off the request thread (a small thread pool) so the
UI never blocks; progress is pushed over the WebSocket event bus. Any stage can
be re-run for a single scene or the whole project without restarting from IDEA.
"""
from __future__ import annotations

import hashlib
import json
import re
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps
from sqlmodel import Session, select

from . import prompts
from .adapters import (
    get_animation_generator,
    get_image_generator,
    get_script_generator,
    get_tts_generator,
    get_video_generator,
)
from .animation import normalize_spec
from .adapters.llm import ai_character_description, author_manim_code, repair_manim_code
from .config import settings
from .database import engine
from .events import bus
from .models import (
    CAMERA_MOVE_PHRASES,
    CameraMove,
    Character,
    CharacterForm,
    ContentPreset,
    MusicTrack,
    Grade,
    MotionFx,
    Particles,
    PlatformPreset,
    Project,
    ProjectCharacter,
    Scene,
    SceneType,
    Stage,
    Transition,
    VisualMode,
)
from .services import ffmpeg, parallax
from .services.jamendo import ensure_downloaded, local_track_path
from .services.pacing import panel_pacing_feedback
from .services.typography import dashless_copy
from .storage import project_folder

_executor = ThreadPoolExecutor(max_workers=2)
_jobs_by_project: dict[str, set[Future]] = {}
_jobs_lock = Lock()
_MAX_VIDEO_PROMPT_CHARS = 2400


def _candidate_path(folder: Path, subfolder: str, stem: str, suffix: str) -> Path:
    """Return a collision-free path so regeneration never overwrites a candidate."""
    return folder / subfolder / f"{stem}_{uuid.uuid4().hex[:10]}{suffix}"


def _paths_with(paths: list[str] | None, *candidates: str | None) -> list[str]:
    result = list(paths or [])
    for candidate in candidates:
        if candidate and candidate not in result:
            result.append(candidate)
    return result


def _audio_options_with(options: list[dict] | None, candidate: dict) -> list[dict]:
    result = list(options or [])
    result = [item for item in result if item.get("path") != candidate.get("path")]
    result.append(candidate)
    return result


def _title_card_variant(path: str | None, source_path: str | None) -> dict | None:
    if not path:
        return None
    return {"path": path, "source_path": source_path or ""}


def _title_card_variants_with(
    variants: list[dict] | None,
    *candidates: dict | None,
) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    for raw in list(variants or []) + [candidate for candidate in candidates if candidate]:
        item = raw if isinstance(raw, dict) else {"path": str(raw), "source_path": ""}
        path = str(item.get("path", "") or "")
        if not path or path in seen:
            continue
        seen.add(path)
        result.append({"path": path, "source_path": str(item.get("source_path", "") or "")})
    return result

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


def stop_project_jobs(project_id: str) -> None:
    """Stop queued work without persisting any project state.

    Deletion uses this path: writing CANCELED from a second database session
    before the delete transaction can lock/race SQLite and leave a durable
    canceled record when the actual deletion fails.
    """
    with _jobs_lock:
        for future in list(_jobs_by_project.get(project_id, set())):
            future.cancel()


def cancel_project(project_id: str) -> None:
    """Request cancellation for queued/running work for one project."""
    stop_project_jobs(project_id)
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


def _timestamp_duration(path: Path | None) -> float | None:
    if not path or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        duration = data.get("duration")
        return float(duration) if duration is not None else None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _save_scene_ready(session: Session, project: Project, scene: Scene) -> None:
    scene.status = "ready"
    session.add(scene)
    session.commit()
    bus.publish("scene.updated", project_id=project.id, scene_id=scene.id)


def _content_style(session: Session, project: Project) -> str:
    """The image style prompt from the project's content preset (may be empty)."""
    if not project.content_preset_id:
        return ""
    preset = session.get(ContentPreset, project.content_preset_id)
    return (preset.image_style_prompt if preset else "") or ""


def _content_motion_style(session: Session, project: Project) -> str:
    """The motion house style from the project's content preset (may be empty)."""
    if not project.content_preset_id:
        return ""
    preset = session.get(ContentPreset, project.content_preset_id)
    return (preset.motion_style_prompt if preset else "") or ""


def _content_animation_style(session: Session, project: Project) -> str:
    """The animation house style from the project's content preset (may be empty)."""
    if not project.content_preset_id:
        return ""
    preset = session.get(ContentPreset, project.content_preset_id)
    return (preset.animation_style_prompt if preset else "") or ""


def _content_voice_id(session: Session, project: Project) -> str | None:
    """The TTS voice from the project's content preset, falling back to config."""
    if not project.content_preset_id:
        return None
    preset = session.get(ContentPreset, project.content_preset_id)
    return (preset.voice_id if preset else "") or None


def _content_voice_speed(session: Session, project: Project) -> float:
    """Per-group narration speed, clamped to ElevenLabs' supported range."""
    if not project.content_preset_id:
        return 1.0
    preset = session.get(ContentPreset, project.content_preset_id)
    speed = (preset.voice_speed if preset else 1.0) or 1.0
    return max(0.7, min(1.2, float(speed)))


def _norm(text: str | None) -> str:
    return " ".join((text or "").strip().lower().replace("_", " ").replace("-", " ").split())


def _default_form(session: Session, char: Character) -> CharacterForm:
    form = session.exec(
        select(CharacterForm).where(
            CharacterForm.character_id == char.id,
            CharacterForm.is_default == True,  # noqa: E712
        )
    ).first()
    if form:
        return form
    form = CharacterForm(
        character_id=char.id,
        name="Default",
        description=char.description,
        reference_image_path=char.reference_image_path,
        reference_prompt=char.reference_prompt,
        reference_style_prompt=char.reference_style_prompt,
        reference_version=char.reference_version,
        variant_paths=char.variant_paths,
        is_default=True,
    )
    session.add(form)
    session.commit()
    session.refresh(form)
    return form


def _match_character_form(session: Session, char: Character, state: str) -> CharacterForm | None:
    if not _norm(state):
        return _default_form(session, char)
    target = _norm(state)
    forms = session.exec(select(CharacterForm).where(CharacterForm.character_id == char.id)).all()
    for form in forms:
        candidates = [form.name, form.state, *(form.trigger_phrases or [])]
        if any(_norm(candidate) == target for candidate in candidates):
            return form
    return None


def _form_for_assignment(
    session: Session,
    char: Character,
    assignment: dict,
) -> CharacterForm | None:
    """Resolve a scene assignment, including assignments saved before its form existed."""
    form_id = assignment.get("form_id")
    form = session.get(CharacterForm, form_id) if form_id else None
    if form and form.character_id == char.id:
        return form
    return _match_character_form(session, char, str(assignment.get("state", "") or ""))


def _assignment_for_character(session: Session, char: Character, raw: dict) -> dict:
    state = str(raw.get("state", "") or "").strip()
    importance = str(raw.get("state_importance", "default") or "default").strip()
    notes = str(raw.get("state_notes", "") or "").strip()
    form = _match_character_form(session, char, state)
    missing_form = bool(state and not form)
    if not form and not state:
        form = _default_form(session, char)
    return {
        "character_id": char.id,
        "character_name": char.name,
        "form_id": form.id if form and not missing_form else None,
        "form_name": form.name if form and not missing_form else "",
        "state": state,
        "state_importance": importance,
        "state_notes": notes,
        "missing_form": missing_form,
    }


def _apply_style(prompt: str, style: str) -> str:
    style = style.strip()
    prompt = prompt.strip()
    if not style:
        return prompt
    directive = (
        f"Visual style directive (mandatory, overrides any generic render look): {style}"
    )
    # Editing a saved prompt hands the whole stored string back, directive
    # included, so appending would stack a second copy on every round trip.
    if directive in prompt:
        return prompt
    return f"{prompt}\n\n{directive}"


def _unique_generated_title(session: Session, project: Project, requested: str) -> str:
    base = (requested.strip() or project.title or "Untitled")[:120]
    existing = {
        title
        for project_id, title in session.exec(select(Project.id, Project.title)).all()
        if project_id != project.id
    }
    if base not in existing:
        return base
    number = 2
    while True:
        suffix = f" ({number})"
        candidate = f"{base[:120 - len(suffix)].rstrip()}{suffix}"
        if candidate not in existing:
            return candidate
        number += 1


def _title_font(size: int, role: str = "headline") -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    windows = {
        "headline": ("impact.ttf", "arialbd.ttf", "bahnschrift.ttf"),
        "kicker": ("segoeprb.ttf", "seguisb.ttf", "arialbd.ttf"),
        "part": ("impact.ttf", "bahnschrift.ttf", "arialbd.ttf"),
    }
    candidates = tuple(Path("C:/Windows/Fonts") / name for name in windows.get(role, windows["headline"])) + (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    )
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _fit_title_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    role: str,
    max_size: int,
    min_size: int,
    max_width: int,
    stroke_width: int = 0,
):
    size = max_size
    while size > min_size:
        font = _title_font(size, role)
        box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
        if box[2] - box[0] <= max_width:
            return font
        size -= 4
    return _title_font(min_size, role)


def _series_part_label(project: Project) -> str:
    source = f"{project.topic_prompt}\n{project.title}"
    match = re.search(r"\bPART\s*(\d+)\s*(?:OF|/)\s*(\d+)\b", source, re.IGNORECASE)
    if match:
        number, total = int(match.group(1)), int(match.group(2))
        return "FINAL PART" if total > 1 and number == total else f"PART {number}"
    if re.search(r"\bFINAL\s+PART\b", source, re.IGNORECASE):
        return "FINAL PART"
    match = re.search(r"\bPART\s*(\d+)\b", source, re.IGNORECASE)
    return f"PART {match.group(1)}" if match else ""


def _fallback_cover_title(title: str) -> str:
    value = re.sub(r"^\s*the\s+story\s+of\s+", "", title, flags=re.IGNORECASE)
    value = re.sub(r"\s*(?:[-—:]\s*)?(?:final\s+part|part\s*\d+)\s*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*\(\d+\)\s*$", "", value).strip()
    return value or title


def _render_title_overlay(
    source: Path,
    output: Path,
    headline: str,
    kicker: str = "",
    part_label: str = "",
) -> Path:
    with Image.open(source) as opened:
        image = ImageOps.fit(opened.convert("RGB"), (1080, 1920), method=Image.Resampling.LANCZOS)
    image = ImageEnhance.Brightness(image).enhance(0.88)
    draw = ImageDraw.Draw(image, "RGBA")

    headline = headline.strip() or "UNTITLED"
    kicker = kicker.strip().upper()
    part_label = part_label.strip().upper()
    headline_font = _fit_title_font(draw, headline, "headline", 190, 82, 920)
    headline_box = draw.textbbox((0, 0), headline, font=headline_font)
    headline_width = headline_box[2] - headline_box[0]
    headline_height = headline_box[3] - headline_box[1]
    bar_width = min(1020, max(620, headline_width + 100))
    bar_top = 820
    bar_bottom = bar_top + headline_height + 82

    # A slight offset shadow and a saturated banner make the main subject read
    # instantly at feed-thumbnail size.
    draw.rounded_rectangle(
        ((1080 - bar_width) / 2 + 13, bar_top + 15, (1080 + bar_width) / 2 + 13, bar_bottom + 15),
        radius=16,
        fill=(0, 0, 0, 145),
    )
    draw.rounded_rectangle(
        ((1080 - bar_width) / 2, bar_top, (1080 + bar_width) / 2, bar_bottom),
        radius=16,
        fill=(255, 224, 0, 248),
    )
    draw.text(
        (540, (bar_top + bar_bottom) / 2),
        headline,
        font=headline_font,
        fill=(5, 5, 8, 255),
        anchor="mm",
    )

    if kicker:
        kicker_font = _fit_title_font(draw, kicker, "kicker", 68, 40, 860, 2)
        kicker_box = draw.textbbox((0, 0), kicker, font=kicker_font, stroke_width=2)
        kicker_width = kicker_box[2] - kicker_box[0]
        kicker_height = kicker_box[3] - kicker_box[1]
        kicker_y = bar_top - 66
        draw.rounded_rectangle(
            (
                540 - kicker_width / 2 - 34,
                kicker_y - kicker_height / 2 - 23,
                540 + kicker_width / 2 + 34,
                kicker_y + kicker_height / 2 + 23,
            ),
            radius=12,
            fill=(8, 9, 13, 165),
        )
        draw.text(
            (540, kicker_y),
            kicker,
            font=kicker_font,
            fill=(255, 255, 255, 255),
            stroke_width=2,
            stroke_fill=(0, 0, 0, 235),
            anchor="mm",
        )

    if part_label:
        part_font = _fit_title_font(draw, part_label, "part", 108, 66, 760, 5)
        draw.text(
            (540, bar_bottom + 92),
            part_label,
            font=part_font,
            fill=(255, 55, 28, 255),
            stroke_width=7,
            stroke_fill=(0, 0, 0, 235),
            anchor="mm",
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, "PNG", optimize=True)
    return output


def _build_title_card(
    session: Session,
    project: Project,
    mode: str,
    source_path: str | None,
    kicker: str,
    text: str,
    part_label: str,
    prompt: str,
) -> None:
    folder = _folder(project)
    title_text = text.strip() or _fallback_cover_title(project.title)
    kicker_text = kicker.strip()
    resolved_part_label = part_label.strip()
    previous = _title_card_variant(project.title_card_path, project.title_card_source_path)
    if mode == "generate":
        title_prompt = prompt.strip() or (
            f"Vertical cinematic cover image for a short-form story titled '{title_text}'. "
            f"Subject: {project.topic_prompt}. Strong single focal point, dramatic composition, "
            "clear negative space around the center for a title overlay. Do not render any words, "
            "letters, logos, captions, borders, or watermarks."
        )
        title_prompt = _apply_style(title_prompt, _content_style(session, project))
        background = folder / "images" / f"title_card_background_{project.title_card_version + 1:02d}.png"
        source = Path(get_image_generator().generate(title_prompt, background, None))
        project.title_card_prompt = prompt.strip()
    else:
        source = _asset_path(source_path)
        if not source or not source.exists():
            raise FileNotFoundError("The selected title-card source image is missing")
        project.title_card_prompt = ""

    output = _candidate_path(folder, "images", "title_card", ".png")
    _render_title_overlay(source, output, title_text, kicker_text, resolved_part_label)
    project.title_card_source_path = _rel(source)
    project.title_card_path = _rel(output)
    project.title_card_kicker = kicker_text
    project.title_card_text = title_text
    project.title_card_part_label = resolved_part_label
    project.title_card_variants = _title_card_variants_with(
        project.title_card_variants,
        previous,
        _title_card_variant(project.title_card_path, project.title_card_source_path),
    )
    project.title_card_status = "ready"
    project.title_card_version = (project.title_card_version or 0) + 1
    _touch(project)
    session.add(project)
    session.commit()
    bus.publish("project.title_card", project_id=project.id, status="ready")


def update_title_card_overlay(
    project_id: str,
    kicker: str | None = None,
    text: str | None = None,
    part_label: str | None = None,
    prompt: str | None = None,
) -> None:
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project:
            return

        if kicker is not None:
            project.title_card_kicker = kicker.strip()
        if text is not None:
            project.title_card_text = text.strip() or _fallback_cover_title(project.title)
        if part_label is not None:
            project.title_card_part_label = part_label.strip()
        if prompt is not None:
            project.title_card_prompt = prompt.strip()

        source = _asset_path(project.title_card_source_path)
        output = _asset_path(project.title_card_path)
        if source and output and source.exists():
            _render_title_overlay(
                source,
                output,
                project.title_card_text,
                project.title_card_kicker,
                project.title_card_part_label,
            )
            project.title_card_status = "ready"
            project.title_card_version = (project.title_card_version or 0) + 1
            project.title_card_variants = _title_card_variants_with(
                project.title_card_variants,
                _title_card_variant(project.title_card_path, project.title_card_source_path),
            )
        _touch(project)
        session.add(project)
        session.commit()
        bus.publish("project.title_card", project_id=project.id, status=project.title_card_status)


def select_title_card_variant(project_id: str, path: str) -> dict | None:
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project:
            return None
        options = _title_card_variants_with(
            project.title_card_variants,
            _title_card_variant(project.title_card_path, project.title_card_source_path),
        )
        selected = next((item for item in options if item["path"] == path), None)
        if not selected:
            return None
        project.title_card_path = selected["path"]
        if selected.get("source_path"):
            project.title_card_source_path = selected["source_path"]
        project.title_card_variants = options
        project.title_card_status = "ready"
        project.title_card_version = (project.title_card_version or 0) + 1
        _touch(project)
        session.add(project)
        session.commit()
        session.refresh(project)
        return {
            "title_card_path": project.title_card_path,
            "title_card_source_path": project.title_card_source_path,
            "title_card_status": project.title_card_status,
            "title_card_version": project.title_card_version,
            "title_card_variants": project.title_card_variants,
            "updated_at": project.updated_at.isoformat(),
        }


def generate_title_card(
    project_id: str,
    mode: str = "reuse",
    source_path: str | None = None,
    kicker: str = "",
    text: str = "",
    part_label: str = "",
    prompt: str = "",
) -> None:
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project:
            return
        project.title_card_status = "generating"
        session.add(project)
        session.commit()
        bus.publish("project.title_card", project_id=project.id, status="generating")
        try:
            _build_title_card(
                session, project, mode, source_path, kicker, text, part_label, prompt
            )
        except Exception as exc:  # noqa: BLE001
            project.title_card_status = "failed"
            project.status_message = f"Title image failed: {exc}"
            session.add(project)
            session.commit()
            bus.publish("project.title_card", project_id=project.id, status="failed")


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
def _merge_consecutive_animations(scenes: list) -> list:
    """Combine each maximal run of consecutive animation scenes into one scene.

    Done deterministically at the script level — before audio — so a continuous
    diagram explanation becomes a single animation scene with one narration and,
    later, one audio take + one authored Manim program. Nothing is merged at the
    code level because no code exists yet (it is authored after audio). Non-
    animation scenes pass through untouched; continuity ``source_scene`` numbers
    are remapped to the post-merge scene numbering.
    """
    from .adapters.base import GeneratedScene

    old_to_new: dict[int, int] = {}
    merged: list = []
    i = 0
    while i < len(scenes):
        new_number = len(merged) + 1
        if scenes[i].scene_type != "animation":
            old_to_new[i + 1] = new_number
            merged.append(scenes[i])
            i += 1
            continue
        run = []
        while i < len(scenes) and scenes[i].scene_type == "animation":
            old_to_new[i + 1] = new_number
            run.append(scenes[i])
            i += 1
        if len(run) == 1:
            merged.append(run[0])
            continue
        narration = " ".join(s.narration_text.strip() for s in run if s.narration_text.strip())
        image_prompt = next((s.image_prompt.strip() for s in run if s.image_prompt.strip()), "")
        characters: list[dict] = []
        seen: set[str] = set()
        continuity: list[dict] = []
        for s in run:
            for ch in s.characters:
                key = str(ch.get("name", "")).lower()
                if key and key not in seen:
                    seen.add(key)
                    characters.append(ch)
            continuity.extend(s.continuity_context)
        merged.append(
            GeneratedScene(
                narration_text=narration,
                image_prompt=image_prompt,
                scene_type="animation",
                characters=characters,
                continuity_context=continuity,
                animation=None,
            )
        )

    # Remap continuity references to the new numbering; drop self/dangling links.
    for index, scene in enumerate(merged, start=1):
        remapped = []
        for item in scene.continuity_context:
            src = old_to_new.get(item.get("source_scene"))
            if src and src != index:
                remapped.append({**item, "source_scene": src})
        scene.continuity_context = remapped
    return merged


def generate_script(project_id: str) -> None:
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project:
            return
        _set_stage(session, project, Stage.SCRIPT_GENERATING, "Writing script…")

        platform = session.get(PlatformPreset, project.platform_preset_id) if project.platform_preset_id else None
        content = session.get(ContentPreset, project.content_preset_id) if project.content_preset_id else None

        # A generated title may differ from the working title used when the
        # project folder was created. Regenerating the script must keep the
        # existing asset folder; otherwise a failed rewrite silently repoints a
        # finished project at a new empty directory.
        folder = _folder(project)
        if not project.folder_path:
            project.folder_path = folder.relative_to(
                settings.projects_dir.parent.parent
            ).as_posix()
            session.add(project)
            session.commit()

        enable_animations = bool(content and content.enable_animations)
        panels_mode = bool(content and content.visual_mode == VisualMode.PANELS)
        panel_seconds = (content.panel_seconds if content else 4.0) or 4.0
        try:
            gen = get_script_generator()
            existing_scenes = session.exec(
                select(Scene)
                .where(Scene.project_id == project.id)
                .order_by(Scene.order_index)
            ).all()
            pacing_feedback = ""
            if existing_scenes:
                from .adapters.base import GeneratedScene, GeneratedScript

                pacing_feedback = panel_pacing_feedback(
                    GeneratedScript(
                        scenes=[
                            GeneratedScene(
                                narration_text=scene.narration_text,
                                image_prompt=scene.image_prompt,
                                scene_type=scene.scene_type.value,
                            )
                            for scene in existing_scenes
                        ],
                        metadata={},
                    ),
                    panels_mode=panels_mode,
                    panel_seconds=panel_seconds,
                    target_seconds=project.target_duration_seconds,
                )
            for attempt in range(2):
                user_prompt = prompts.build_script_prompt(
                    platform.format_prompt if platform else "",
                    content.content_prompt if content else "",
                    project.topic_prompt,
                    project.target_duration_seconds,
                    enable_animations=enable_animations,
                    latex_available=_latex_available(),
                    revision_notes=project.revision_notes,
                    panels_mode=panels_mode,
                    panel_seconds=panel_seconds,
                    pacing_feedback=pacing_feedback,
                )
                script = gen.generate(
                    prompts.CORE_SYSTEM_PROMPT,
                    user_prompt,
                    prompts.build_script_schema(enable_animations, panels_mode),
                    project.topic_prompt,
                    project.target_duration_seconds,
                )
                pacing_feedback = panel_pacing_feedback(
                    script,
                    panels_mode=panels_mode,
                    panel_seconds=panel_seconds,
                    target_seconds=project.target_duration_seconds,
                )
                if not pacing_feedback or attempt == 1:
                    break
        except Exception as exc:  # noqa: BLE001
            if _stop_if_canceled(session, project):
                return
            _fail(session, project, "script generation", exc)
            return
        if _stop_if_canceled(session, project):
            return

        # Collapse runs of consecutive animation scenes into one scene so each
        # renders as a single continuous animation with one voice take. Code is
        # authored later (post-audio), so this is a pure narration/structure merge.
        if enable_animations:
            script.scenes = _merge_consecutive_animations(script.scenes)

        # Everything below is copy the audience reads rather than words the
        # voice speaks, so the dashes come out here — before the title, the
        # cover text and script.json are all derived from it. The narration on
        # script.scenes is deliberately left alone: the voice needs the beat.
        script.metadata = dashless_copy(script.metadata)

        generated_title = str(script.metadata.get("title", "")).strip()
        if generated_title and not project.title_is_custom:
            project.title = _unique_generated_title(session, project, generated_title)
        project.title_card_kicker = str(
            script.metadata.get("cover_kicker")
            or script.metadata.get("hook_text")
            or ""
        ).strip()
        project.title_card_text = str(
            script.metadata.get("cover_title")
            or _fallback_cover_title(project.title)
        ).strip()
        project.title_card_part_label = _series_part_label(project)
        session.add(project)
        session.commit()

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

        by_name = {c.name.lower(): c for c in _group_characters(session, project)}

        for idx, s in enumerate(script.scenes):
            matched, suggested, assignments = [], [], []
            for raw_char in s.characters:
                if isinstance(raw_char, dict):
                    cname = str(raw_char.get("name", "")).strip()
                    char_payload = raw_char
                else:
                    cname = str(raw_char).strip()
                    char_payload = {
                        "name": cname,
                        "state": "",
                        "state_importance": "default",
                        "state_notes": "",
                    }
                if not cname:
                    continue
                found = by_name.get(cname.lower())
                if found:
                    matched.append(found.id)
                    assignments.append(_assignment_for_character(session, found, char_payload))
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
                    motion_prompt=s.motion_prompt,
                    camera_move=CameraMove(s.camera_move),
                    particles=Particles(s.particles),
                    transition=Transition(s.transition),
                    motion_fx=MotionFx(s.motion_fx),
                    grade=Grade(s.grade),
                    beat_id=s.beat_id,
                    continuity_context=s.continuity_context,
                    scene_type=SceneType(s.scene_type),
                    animation_spec=s.animation,
                    character_ids=matched,
                    character_assignments=assignments,
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
    def ready(scene: Scene) -> bool:
        if scene.scene_type == SceneType.ANIMATION:
            return _asset_exists(scene.animation_path)
        return _asset_exists(scene.image_path)

    return bool(scenes) and all(ready(scene) for scene in scenes)


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
def _group_characters(session: Session, project: Project) -> list[Character]:
    """The character library visible to this project — its group's, and only its.

    A project without a group sees the ungrouped characters, so cast matching is
    never a cross-group lookup in either direction.
    """
    return session.exec(
        select(Character).where(Character.content_preset_id == project.content_preset_id)
    ).all()


def compute_cast(session: Session, project: Project) -> list[dict]:
    """The distinct characters referenced by this project's scenes.

    Each entry reports whether the selected/default form has a reference sheet,
    so the UI can flag what's missing before storyboard generation.
    """
    scenes = session.exec(select(Scene).where(Scene.project_id == project.id)).all()
    order: list[str] = []
    entries: dict[str, dict] = {}
    for scene in scenes:
        assignments = scene.character_assignments or []
        assigned_character_ids: set[str] = set()
        if assignments:
            for assignment in assignments:
                cid = assignment.get("character_id")
                char = session.get(Character, cid) if cid else None
                if not char:
                    continue
                assigned_character_ids.add(char.id)
                state = str(assignment.get("state", "") or "").strip()
                form_id = assignment.get("form_id")
                key = f"{char.id}:{form_id or _norm(state) or 'default'}"
                if key not in entries:
                    entries[key] = {"character": char, "assignment": assignment}
                    order.append(key)
        # character_ids is the broad scene membership list. Older/mixed scenes
        # can have structured assignments for only some members, so merge in
        # every unrepresented character instead of treating the two fields as
        # mutually exclusive.
        for cid in scene.character_ids:
            if cid in assigned_character_ids:
                continue
            char = session.get(Character, cid)
            if not char:
                continue
            key = f"{char.id}:default"
            if key not in entries:
                entries[key] = {"character": char, "assignment": {}}
                order.append(key)
        for name in scene.suggested_characters:
            key = name.lower()
            if key not in entries:
                entries[key] = {"suggested_name": name}
                order.append(key)

    cast: list[dict] = []
    for key in order:
        entry = entries[key]
        char = entry.get("character")
        if char:
            assignment = entry.get("assignment") or {}
            state = str(assignment.get("state", "") or "").strip()
            form = _form_for_assignment(session, char, assignment)
            has_sheet = bool(form.reference_image_path if form else False)
            form_name = form.name if form else (state or "Default")
            description = form.description if form and form.description else char.description
            # Show the prompt that WOULD be sent for a character that has never
            # been generated, not just the one a past run stored, so the wording
            # can be corrected before a moderated model rejects it rather than
            # only afterwards.
            stored_prompt = form.reference_prompt if form else ""
            cast.append({
                "key": key,
                "name": char.name,
                "character_id": char.id,
                "form_id": form.id if form else None,
                "form_name": form_name,
                "state": state,
                "state_importance": assignment.get("state_importance", "default"),
                "state_notes": assignment.get("state_notes", ""),
                "missing_form": bool(state and not form),
                "description": description,
                "has_sheet": has_sheet,
                "reference_image_path": form.reference_image_path if form else None,
                "reference_version": form.reference_version if form else 0,
                "reference_prompt": stored_prompt
                or _default_sheet_prompt(
                    f"{char.name} ({state})" if state else char.name,
                    description,
                    _content_style(session, project),
                ),
                "reference_style_prompt": form.reference_style_prompt if form else "",
                "reference_variants": _paths_with(
                    form.reference_variants if form else [],
                    form.reference_image_path if form else None,
                ),
            })
        else:
            # Suggested-but-not-yet-created character.
            display = entry.get("suggested_name") or key.title()
            cast.append({
                "key": key,
                "name": display,
                "character_id": None,
                "form_id": None,
                "form_name": "Default",
                "state": "",
                "state_importance": "default",
                "state_notes": "",
                "missing_form": False,
                "description": "",
                "has_sheet": False,
                "reference_image_path": None,
                "reference_version": 0,
                "reference_prompt": "",
                "reference_style_prompt": "",
                "reference_variants": [],
            })
    return cast


def generate_character_sheet(
    project_id: str,
    name: str,
    description: str | None,
    prompt: str | None,
    generate_description: bool,
    state: str | None = None,
) -> None:
    """Create/attach a global character or form and generate its reference sheet.

    Uses the project's content-preset image style so sheets match the scenes.
    """
    from .storage import character_folder, slugify

    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project or project.stage == Stage.CANCELED:
            return
        # A rejected sheet (a moderated image model refusing the prompt) leaves
        # the project FAILED at cast review, and editing the prompt and asking
        # again IS the fix. Re-open the review here rather than making the
        # creator find "retry failed step" first, which would only replay the
        # same prompt. Any other failure still has to be retried at its own step.
        if project.stage == Stage.FAILED:
            if _interrupted_stage(project) != Stage.CAST_REVIEW:
                return
            _set_stage(session, project, Stage.CAST_REVIEW, "Retrying character sheet")
        _set_msg(session, project, f"Generating character sheet: {name}…")

        # Reuse only a character from this project's own group; a same-named
        # character in another group is a different library entry.
        in_group = _group_characters(session, project)
        existing = next(
            (c for c in in_group if c.name.lower() == name.lower()),
            None,
        )
        char = existing or Character(
            name=name.strip(), content_preset_id=project.content_preset_id
        )

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

        form = _match_character_form(session, char, state)
        if not form:
            form = CharacterForm(
                character_id=char.id,
                name=state or "Default",
                state=state,
                is_default=not bool(state),
            )
        if description:
            form.description = description.strip()
        elif state and not form.description and generate_description:
            content = (
                session.get(ContentPreset, project.content_preset_id)
                if project.content_preset_id
                else None
            )
            form.description = ai_character_description(
                f"{char.name} ({state})",
                project.topic_prompt,
                content.content_prompt if content else "",
            )
        elif not form.description:
            form.description = char.description

        style = _content_style(session, project)
        form_label = f"{char.name} ({state})" if state else char.name
        form_description = form.description or char.description
        ref_prompt = (
            _apply_style(prompt, style)
            if prompt
            else _default_sheet_prompt(form_label, form_description, style)
        )

        # Save the form and the prompt we are about to send BEFORE sending it.
        # When a moderated image model rejects the prompt, that prompt is the
        # one thing the creator needs back in the cast panel to edit and retry;
        # a form with no image still reads as "no sheet" there. Linking the
        # character to the project and its scenes now (rather than only on
        # success) is what puts the failed member on screen at all — until the
        # link exists it is only a suggested name with no prompt to show.
        form.reference_prompt = ref_prompt
        form.reference_style_prompt = style
        session.add(form)
        session.commit()
        session.refresh(form)
        _link_character(session, project, char, form, state)
        session.commit()

        try:
            folder = character_folder(char.name)
            (folder / "forms").mkdir(parents=True, exist_ok=True)
            stem = "reference" if not state else slugify(state)
            subfolder = "variants" if not state else "forms"
            out_path = _candidate_path(folder, subfolder, stem, ".png")
            previous = form.reference_image_path
            out = get_image_generator().generate(ref_prompt, out_path, None)
            if _stop_if_canceled(session, project):
                return
            form.reference_image_path = _rel(out)
            form.reference_variants = _paths_with(
                form.reference_variants, previous, form.reference_image_path
            )
            form.reference_version = (form.reference_version or 0) + 1
            if form.is_default:
                char.reference_image_path = form.reference_image_path
                char.reference_prompt = form.reference_prompt
                char.reference_style_prompt = form.reference_style_prompt
                char.reference_version = form.reference_version
                char.reference_variants = form.reference_variants
            session.add(char)
            session.add(form)
            session.commit()
            session.refresh(form)
        except Exception as exc:  # noqa: BLE001
            if _stop_if_canceled(session, project):
                return
            # Name the member in the error: a cast can be a dozen sheets deep and
            # "character sheet failed" alone leaves nothing to act on. The
            # project.failed event refreshes the cast query along with it.
            _fail(session, project, f"character sheet for {form_label}", exc)
            return

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


def _link_character(
    session: Session,
    project: Project,
    char: Character,
    form: CharacterForm,
    state: str | None,
) -> None:
    """Attach a library character to the project and its scenes.

    Turns the script's suggested name into a real linked id, so the cast panel
    shows the character with its form and prompt whether or not its reference
    image exists yet.
    """
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
    _reconcile_scene_assignments(session, project.id, char, form, state)


def _reconcile_scene_assignments(
    session: Session,
    project_id: str,
    char: Character,
    form: CharacterForm,
    state: str | None,
) -> None:
    target_state = _norm(state)
    for scene in session.exec(select(Scene).where(Scene.project_id == project_id)):
        # JSON columns do not track mutations inside their nested dictionaries.
        # Copy each assignment so assigning the list back is seen as a change.
        assignments = [dict(assignment) for assignment in (scene.character_assignments or [])]
        changed = False
        for assignment in assignments:
            if assignment.get("character_id") != char.id:
                continue
            if _norm(assignment.get("state")) != target_state:
                continue
            assignment["form_id"] = form.id
            assignment["form_name"] = form.name
            assignment["missing_form"] = False
            changed = True
        if not changed and char.id in (scene.character_ids or []) and not target_state:
            assignments.append(_assignment_for_character(session, char, {
                "state": "",
                "state_importance": "default",
                "state_notes": "",
            }))
            changed = True
        if changed:
            scene.character_assignments = assignments
            session.add(scene)


def generate_missing_sheets(project_id: str) -> None:
    """Auto-generate reference sheets for every character still missing one."""
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project:
            return
        # Re-open a cast review that a rejected sheet failed, so this can be run
        # straight from the panel. Failures at any other step still have to be
        # retried at that step.
        if project.stage == Stage.FAILED:
            if _interrupted_stage(project) != Stage.CAST_REVIEW:
                return
            _set_stage(session, project, Stage.CAST_REVIEW, "Retrying character sheets")
        missing = [
            {"name": c["name"], "state": c.get("state") or ""}
            for c in compute_cast(session, project)
            if not c["has_sheet"]
        ]
    for item in missing:
        with Session(engine) as session:
            project = session.get(Project, project_id)
            if not project or _stop_if_canceled(session, project):
                return
            # Stop on the first rejection instead of walking the rest of the
            # cast: a later member succeeding would clear the failed state and
            # bury the prompt that actually needs editing.
            if project.stage == Stage.FAILED:
                return
        generate_character_sheet(
            project_id,
            item["name"],
            None,
            None,
            generate_description=True,
            state=item["state"],
        )


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
            tts = get_tts_generator(
                _content_voice_id(session, project),
                _content_voice_speed(session, project),
            )
            for scene_index, scene in enumerate(scenes):
                if _stop_if_canceled(session, project):
                    return
                audio_out = folder / "audio" / f"scene_{scene.order_index + 1:02d}.mp3"
                ts_out = folder / "audio" / f"scene_{scene.order_index + 1:02d}.timestamps.json"

                existing_duration = scene.duration_seconds
                if existing_duration is None:
                    existing_duration = _timestamp_duration(_asset_path(scene.timestamps_path))
                if _asset_exists(scene.audio_path) and _asset_exists(scene.timestamps_path) and existing_duration is not None:
                    scene.duration_seconds = existing_duration
                    _save_scene_ready(session, project, scene)
                    continue

                # Files with the deterministic legacy name may belong to scenes
                # from a previous script. With no DB link there is no safe way
                # to prove the text matches, so preserve them and write a fresh
                # candidate instead of silently attaching stale narration.
                if audio_out.exists() or ts_out.exists():
                    audio_out = _candidate_path(
                        folder, "audio", f"scene_{scene.order_index + 1:02d}", ".mp3"
                    )
                    ts_out = audio_out.with_suffix(".timestamps.json")
                scene.status = "generating"
                session.add(scene)
                session.commit()
                bus.publish("scene.status", project_id=project.id, scene_id=scene.id, status="generating", stage="audio")
                result = tts.synthesize(
                    scene.narration_text,
                    audio_out,
                    ts_out,
                    previous_text=(
                        scenes[scene_index - 1].narration_text if scene_index else ""
                    ),
                    next_text=(
                        scenes[scene_index + 1].narration_text
                        if scene_index + 1 < len(scenes) else ""
                    ),
                )
                if _stop_if_canceled(session, project):
                    return
                scene.audio_path = _rel(audio_out)
                scene.timestamps_path = _rel(ts_out)
                scene.duration_seconds = result.duration_seconds
                scene.audio_variants = _audio_options_with(
                    scene.audio_variants,
                    {
                        "path": scene.audio_path,
                        "timestamps_path": scene.timestamps_path,
                        "duration_seconds": scene.duration_seconds,
                    },
                )
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
                # Animation scenes render deterministically from their spec here
                # (the audio stage already produced the timestamps the cues need)
                # instead of going through image generation.
                if scene.scene_type == SceneType.ANIMATION:
                    if _asset_exists(scene.animation_path):
                        _save_scene_ready(session, project, scene)
                        continue
                    if not _render_scene_animation(session, project, scene, folder):
                        return
                    continue
                if _asset_exists(scene.image_path):
                    _save_scene_ready(session, project, scene)
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
        if not project.title_card_path:
            first = next((scene for scene in scenes if _asset_exists(scene.image_path)), None)
            if first:
                try:
                    _build_title_card(
                        session,
                        project,
                        "reuse",
                        first.image_path,
                        project.title_card_kicker,
                        project.title_card_text,
                        project.title_card_part_label,
                        "",
                    )
                except Exception as exc:  # noqa: BLE001
                    project.title_card_status = "failed"
                    project.status_message = f"Storyboard ready; title image failed: {exc}"
                    session.add(project)
                    session.commit()
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
    krea_style_refs = getattr(img, "name", "") in {"krea", "krea-direct"}
    # Krea exposes every attached image as a *style* reference. A light character
    # sheet is still useful for recognition, but a previous scene leaks its cast,
    # pose and composition even at low strength. Keep continuity textual on Krea;
    # other adapters retain the existing image-reference behaviour.
    image_character_refs = (
        [] if krea_style_refs and _is_distant_character_shot(scene.image_prompt)
        else character_refs
    )
    image_continuity_refs = [] if krea_style_refs else continuity_refs
    refs = [ref["path"] for ref in image_character_refs] + [
        ref["path"] for ref in image_continuity_refs
    ]
    ref_strengths = [0.20] * len(image_character_refs) + [0.12] * len(image_continuity_refs)

    out = _candidate_path(
        folder, "images", f"scene_{scene.order_index + 1:02d}", ".png"
    )
    scene_prompt, image_style = _image_prompt_and_style_for_scale(
        scene.image_prompt,
        _content_style(session, project),
        character_refs,
    )
    prompt = _apply_continuity_context(
        _apply_character_context(
            _apply_composition_contract(
                _apply_style(scene_prompt, image_style),
                scene.image_prompt,
            ),
            image_character_refs,
            "character identity reference image",
        ),
        continuity_refs,
        None if krea_style_refs else "continuity reference image",
        len(image_character_refs),
    )
    result = img.generate(prompt, out, refs or None, ref_strengths or None)
    if _stop_if_canceled(session, project):
        return False
    previous = scene.image_path
    scene.image_path = _rel(result)
    scene.image_variants = _paths_with(
        scene.image_variants, previous, scene.image_path
    )
    if previous != scene.image_path and scene.scene_type == SceneType.VIDEO:
        scene.clip_variants = _paths_with(scene.clip_variants, scene.clip_path)
        scene.clip_path = None
    scene.status = "ready"
    scene.asset_version = (scene.asset_version or 0) + 1
    session.add(scene)
    session.commit()
    bus.publish("scene.updated", project_id=project.id, scene_id=scene.id)
    return True


def _is_distant_character_shot(prompt: str) -> bool:
    """Whether people are too small for a Krea identity style-ref to help.

    At these scales the reference cannot improve a visible face, but can still
    leak a giant portrait or hero pose into an otherwise environmental frame.
    """
    text = " ".join((prompt or "").lower().split())
    return any(phrase in text for phrase in (
        "extreme wide shot",
        "high aerial wide shot",
        "aerial extreme wide",
    )) or ("wide shot" in text and "tiny" in text)


def _apply_composition_contract(prompt: str, scene_prompt: str) -> str:
    """Keep Krea's 'action-panel' aesthetic inside one cinematic frame."""
    rules = (
        "Composition contract: render one continuous cinematic frame at one moment "
        "in time. Never create a collage, comic page, split-screen, montage, inset "
        "panel, border, or close-up overlay. Obey the requested shot scale and camera "
        "angle exactly."
    )
    if _is_distant_character_shot(scene_prompt):
        rules += (
            " This is an environmental distant shot: every named person must remain "
            "small in the frame. Do not add enlarged faces, bodies, portraits, or "
            "foreground character cutaways anywhere. Preserve the requested empty "
            "negative space. Do not add a sail, canopy, curtain, flag, wing, cliff, "
            "silhouette, or other large foreground shape unless explicitly requested."
        )
    return f"{prompt.strip()}\n\n{rules}"


def _image_prompt_and_style_for_scale(
    scene_prompt: str, style: str, character_refs: list[dict]
) -> tuple[str, str]:
    """Remove portrait attractors when the prompt explicitly dwarfs its cast."""
    if "dwarfed" not in (scene_prompt or "").lower():
        return scene_prompt, style

    prompt = scene_prompt
    replacements = (
        "one tiny indistinct human figure",
        "another tiny indistinct human figure",
    )
    for index, ref in enumerate(character_refs):
        name = str(ref.get("name", "")).strip()
        if name:
            prompt = re.sub(
                rf"\b{re.escape(name)}\b",
                replacements[min(index, len(replacements) - 1)],
                prompt,
                flags=re.IGNORECASE,
            )

    # Preserve the established rendering language but remove clauses that ask
    # Krea to turn a landscape into a character cover or comic-page montage.
    distant_style = style
    substitutions = {
        "cinematic action-panel composition": "cinematic environmental composition",
        "expressive faces": "naturalistic distant figures",
        "vertical webtoon cover quality": "polished vertical cinematic illustration quality",
        "consistent character rendering across scenes": "consistent world rendering across scenes",
    }
    for source, replacement in substitutions.items():
        distant_style = distant_style.replace(source, replacement)
    return prompt, distant_style


def _latex_available() -> bool:
    """Whether a LaTeX distribution is available (enables MathTex/Tex)."""
    from .animation.latex import latex_available

    return latex_available()


def _load_timestamps(scene: Scene) -> dict:
    """The scene's ElevenLabs word timings (empty if not generated yet)."""
    path = _asset_path(scene.timestamps_path)
    if path and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"words": [], "duration": scene.duration_seconds or 0.0}


# Initial attempt + this many LLM auto-repair retries for failing animation code.
_ANIMATION_REPAIR_ATTEMPTS = 2


def _persist_animation_code(session, scene: Scene, spec: dict, code: str) -> None:
    scene.animation_spec = {"code": code, "title": spec.get("title", "")}
    session.add(scene)
    session.commit()


def _render_scene_animation(session, project: Project, scene: Scene, folder: Path) -> bool:
    """Author (if needed) and render one animation scene, synced to its voice.

    The Manim code is authored HERE — in the storyboard stage, after narration
    audio + word timestamps exist — from the scene's (merged) narration, not
    during script generation. On a render failure the code + error are sent back
    to the LLM to fix, up to ``_ANIMATION_REPAIR_ATTEMPTS`` times; a successful
    author/repair is persisted so the UI shows the working code.
    """
    if _stop_if_canceled(session, project):
        return False

    duration = scene.duration_seconds or _timestamp_duration(_asset_path(scene.timestamps_path)) or 6.0
    words = _load_timestamps(scene).get("words", [])
    latex = _latex_available()

    spec = normalize_spec(scene.animation_spec)
    if not spec:
        # No code yet (the normal path now): write it against the real narration
        # and length. Any existing code (a user edit / older project) is reused.
        _set_msg(session, project, f"Writing animation for scene {scene.order_index + 1}…")
        authored = author_manim_code(
            scene.narration_text,
            duration,
            latex,
            _content_animation_style(session, project),
            project.revision_notes,
        )
        spec = normalize_spec({"code": authored, "title": ""}) if authored else None
        if spec:
            _persist_animation_code(session, scene, spec, spec["code"])
    if not spec:
        # Authoring refuses incomplete or unparseable programs, so landing here
        # means no usable code exists — never that half a program was written.
        _fail(
            session,
            project,
            "animation render",
            ValueError(
                f"Scene {scene.order_index + 1}: no complete animation program was "
                "returned. Retry the scene, or shorten its narration if it keeps failing."
            ),
        )
        return False

    bus.publish("scene.status", project_id=project.id, scene_id=scene.id, status="generating", stage="animation")
    scene.status = "generating"
    session.add(scene)
    session.commit()

    generator = get_animation_generator()
    title = spec.get("title", "")
    code = spec["code"]
    out = _candidate_path(folder, "animations", f"scene_{scene.order_index + 1:02d}", ".mp4")

    result = None
    last_error = ""
    for attempt in range(_ANIMATION_REPAIR_ATTEMPTS + 1):
        if _stop_if_canceled(session, project):
            return False
        try:
            result = generator.render({"code": code, "title": title}, words, out, duration)
            break
        except Exception as exc:  # noqa: BLE001 — capture to feed the repair loop
            last_error = str(exc)
        if attempt >= _ANIMATION_REPAIR_ATTEMPTS:
            break
        _set_msg(session, project, f"Animation code error — auto-repairing (try {attempt + 1})…")
        repaired = repair_manim_code(code, last_error, scene.narration_text, duration, latex)
        if not repaired or repaired.strip() == code.strip():
            break
        code = repaired
        _persist_animation_code(session, scene, spec, code)  # show the fix in the UI

    if result is None:
        if _stop_if_canceled(session, project):
            return False
        _fail(session, project, "animation render", RuntimeError(last_error or "render failed"))
        return False

    if code != spec["code"]:
        _persist_animation_code(session, scene, spec, code)
    previous = scene.animation_path
    scene.animation_path = _rel(result)
    scene.animation_variants = _paths_with(scene.animation_variants, previous, scene.animation_path)
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
    assigned_character_ids: set[str] = set()
    if scene.character_assignments:
        for assignment in scene.character_assignments:
            char = session.get(Character, assignment.get("character_id")) if assignment.get("character_id") else None
            if char:
                assigned_character_ids.add(char.id)
            form = _form_for_assignment(session, char, assignment) if char else None
            if not char or not form or not form.reference_image_path:
                continue
            path = root / form.reference_image_path
            if not path.exists():
                continue
            variants = [root / v for v in (form.variant_paths or []) if (root / v).exists()]
            label = char.name if not assignment.get("state") else f"{char.name} ({assignment.get('state')})"
            refs.append({
                "name": label,
                "description": form.description or char.description,
                "path": path,
                "variants": variants,
            })

    for cid in scene.character_ids:
        if cid in assigned_character_ids:
            continue
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


def _same_beat_predecessor(session, project: Project, scene: Scene) -> Scene | None:
    """The panel immediately before this one, when it belongs to the same beat.

    Immediately before, not merely earlier in the beat: a beat is continuous, so
    the picture worth carrying forward is the one the reader just left. Returns
    None for a panel that opens a beat or stands alone.
    """
    beat = (scene.beat_id or "").strip()
    if not beat:
        return None
    previous = session.exec(
        select(Scene)
        .where(Scene.project_id == project.id, Scene.order_index < scene.order_index)
        .order_by(Scene.order_index.desc())
    ).first()
    if previous is None or (previous.beat_id or "").strip() != beat:
        return None
    return previous


def _scene_continuity_candidates(session, project: Project, scene: Scene) -> list[dict]:
    """Earlier scene image candidates for this panel.

    Two sources: whatever ``continuity_context`` names, and — when this panel
    continues the beat before it — that panel's own image, attached whether the
    script asked for it or not. A beat is one moment shown from several angles,
    so its panels have to agree on the place, the light and the palette, and the
    surest way to get that is to hand the image model the picture it is
    continuing from. Both are only candidates: the callers still drop anything
    the user excluded.
    """
    root = Path(settings.projects_dir.parent.parent)
    beat_predecessor = _same_beat_predecessor(session, project, scene)
    if not scene.continuity_context and beat_predecessor is None:
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
    # First, so a long continuity_context can never crowd it out of the limit.
    if beat_predecessor is not None and beat_predecessor.image_path:
        path = root / beat_predecessor.image_path
        if path.exists():
            seen_scene_ids.add(beat_predecessor.id)
            anchor = "the same scene, continued"
            candidates.append((beat_predecessor.order_index, {
                "scene_id": beat_predecessor.id,
                "scene_number": beat_predecessor.order_index + 1,
                "order_index": beat_predecessor.order_index,
                "image_path": beat_predecessor.image_path,
                "asset_version": beat_predecessor.asset_version or 0,
                "prompt": beat_predecessor.image_prompt,
                "path": path,
                "visual_anchor": anchor,
                "reason": (
                    f"This panel continues beat '{scene.beat_id}': the place, the "
                    "light and the palette carry over from it."
                ),
                "matches": [anchor],
            }))
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
        lines.append(f"{idx}. {ref['name']}")
    mapping = "\n".join(lines)
    return (
        f"{prompt.strip()}\n\n"
        f"Character identity map: attached {label}s are in this order:\n"
        f"{mapping}\n"
        f"Use each reference only for stable physical identity: face, apparent age, "
        f"build, hair, and other distinguishing anatomy. The current scene description "
        f"is authoritative for shot scale, camera angle, pose, action, expression, "
        f"costume, props, effects, environment, lighting, and composition. Do not copy "
        f"those scene-specific elements or the framing/background from a reference. "
        f"Keep identities consistent and do not swap people when several appear."
    )


def _apply_continuity_context(
    prompt: str,
    continuity_refs: list[dict],
    label: str | None,
    start_index: int,
) -> str:
    if not continuity_refs:
        return prompt
    lines = []
    for offset, ref in enumerate(continuity_refs, start=1):
        attached_index = start_index + offset
        anchor = ref.get("visual_anchor") or ", ".join(ref.get("matches", [])[:5])
        reason = ref.get("reason", "")
        anchor_note = anchor or "the explicitly recurring element"
        reason_note = f" Purpose: {reason}" if reason else ""
        prefix = (
            f"{attached_index}. Attached {label} from scene {ref['scene_number']}"
            if label
            else f"{offset}. From scene {ref['scene_number']}"
        )
        lines.append(
            f"{prefix}, preserve only this "
            f"continuity anchor: {anchor_note}.{reason_note}"
        )
    mapping = "\n".join(lines)
    heading = (
        f"Visual continuity map: attached {label}s after the character references "
        f"correspond to these entries:\n"
        if label
        else "Visual continuity notes:\n"
    )
    return (
        f"{prompt.strip()}\n\n"
        f"{heading}"
        f"{mapping}\n"
        f"Use only the explicitly named visual anchors. The current scene description "
        f"remains authoritative: do not copy the earlier image's camera, framing, pose, "
        f"lighting, incidental people, or overall composition. Carry forward recurring "
        f"objects only where the current moment still requires them."
    )


def _build_clip_prompt(
    scene: Scene, motion_style: str = "", element_refs: list[dict] | None = None
) -> str:
    """The image-to-video prompt: motion, and nothing that describes the frame.

    An i2v model already has the picture. Handing it a description of that same
    picture — which is what ``image_prompt`` is — reads as "render this again",
    and the model satisfies it the cheapest way there is: hold the frame and add
    light effects. So the scene's ``motion_prompt`` is what goes in, the preamble
    pins the DESIGN rather than the composition (pinning composition is close to
    "do not move the camera"), and the group's motion house style follows.
    """
    motion = (scene.motion_prompt or "").strip()
    if not motion:
        # Scripts written before motion_prompt existed, or a cleared field. The
        # camera move alone still beats re-describing the frame.
        motion = _camera_phrase(scene.camera_move).capitalize() + "."
    parts = [
        "Animate the provided start image. Keep the rendered style, character "
        "designs, costumes, props, environment and lighting exactly as they "
        "already appear, and add nothing that is not already in the frame. The "
        "framing may move; the artwork may not be redesigned or re-rendered.",
        f"Motion: {motion}",
    ]
    motion_style = " ".join((motion_style or "").split())
    if motion_style:
        parts.append(f"House motion style: {_limit_prompt(motion_style, 900)}")
    prompt = "\n\n".join(parts)
    return _limit_prompt(
        _apply_kling_element_context(prompt, element_refs or []),
        _MAX_VIDEO_PROMPT_CHARS,
    )


# Bumped whenever the panel renderer's output changes for the same inputs, so a
# cached segment from an older version of the effects is not reused.
_SEGMENT_RENDERER_VERSION = 9


def _segment_key(
    image: Path, duration: float, move: str, particles: str, transition: str,
    parallax_on: bool, prev_token: str, motion_fx: str, grade: str,
) -> str:
    """Identity of a rendered segment: same key means the same pixels.

    ``prev_token`` is in here because a transition is rendered from the previous
    panel's final frame — change the panel before this one and this segment's
    opening genuinely differs, even though nothing about this scene moved.
    """
    stat = image.stat() if image.exists() else None
    parts = [
        str(_SEGMENT_RENDERER_VERSION), str(image), str(stat.st_mtime_ns if stat else 0),
        str(stat.st_size if stat else 0), f"{duration:.3f}", move, particles,
        transition, "P" if parallax_on else "F", prev_token, motion_fx, grade,
    ]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def _render_still_segment(
    session, project: Project, scene: Scene, image: Path, out: Path,
    duration: float, prev_clip: Path | None,
) -> Path:
    """One still panel as a video segment, cached, with its effect layers.

    Falls back to the flat camera move on any failure rather than aborting the
    render: a flatter panel is a far better outcome than a project that cannot
    finish because a depth model misbehaved on one image.
    """
    move = getattr(scene.camera_move, "value", scene.camera_move) or "push_in"
    particles = getattr(scene.particles, "value", scene.particles) or "none"
    transition = getattr(scene.transition, "value", scene.transition) or "cut"
    motion_fx = getattr(scene.motion_fx, "value", scene.motion_fx) or "none"
    grade = getattr(scene.grade, "value", scene.grade) or "none"

    # Depth parallax belongs to camera translation, not to every moving frame.
    # Pans/tilts are rotations and punch-in is an editorial zoom, so they use
    # the coherent flat renderer. Push/pull are the only dolly-style moves.
    parallax_on = parallax.available() and parallax.uses_depth(move)
    if project.content_preset_id:
        preset = session.get(ContentPreset, project.content_preset_id)
        parallax_on = parallax_on and bool(preset and preset.panel_parallax)

    # A transition only exists if there is a previous segment to arrive from.
    prev_token = ""
    if parallax.needs_prev_frame(transition) and prev_clip and prev_clip.exists():
        prev_stat = prev_clip.stat()
        prev_token = f"{prev_stat.st_mtime_ns}:{prev_stat.st_size}"
    elif parallax.needs_prev_frame(transition):
        # Nothing to arrive from, so the blend it asked for cannot happen.
        transition = "cut"

    key = _segment_key(
        image, duration, move, particles, transition, parallax_on, prev_token,
        motion_fx, grade,
    )
    key_file = out.with_suffix(".key")
    if out.exists() and key_file.exists() and key_file.read_text(encoding="utf-8").strip() == key:
        return out

    # Every panel goes through the Python renderer, effects or not. It samples
    # the camera move at sub-pixel positions; ffmpeg's zoompan quantises the
    # crop to whole input pixels, so an eased pan or tilt visibly stutters.
    try:
        prev_frame = (
            parallax.last_frame(prev_clip)
            if parallax.needs_prev_frame(transition) and prev_clip
            else None
        )
        parallax.parallax_clip(
            image, out, duration, move,
            particles=particles,
            transition=(
                transition
                if prev_frame is not None or not parallax.needs_prev_frame(transition)
                else "cut"
            ),
            prev_frame=prev_frame,
            use_depth=parallax_on,
            motion_fx=motion_fx,
            grade=grade,
        )
        key_file.write_text(key, encoding="utf-8")
        return out
    except Exception as exc:  # noqa: BLE001
        _set_msg(session, project, f"Effects unavailable for {image.name}; using a flat move")
        print(f"panel effects failed for {image}: {exc}")

    ffmpeg.ken_burns_clip(image, out, duration, move)
    key_file.write_text(key, encoding="utf-8")
    return out


def _clip_duration(scene_seconds: float) -> float:
    """How many seconds of video to actually generate for a scene.

    Capped by ``settings.max_clip_seconds``: the tail of a long generation is
    both the most expensive part and the part where the model has drifted
    furthest from the frame it started with. The render covers the remainder of
    the scene from the clip it has (services/ffmpeg._normalize_clip).
    """
    cap = settings.max_clip_seconds or 0
    return min(scene_seconds, cap) if cap > 0 else scene_seconds


def _camera_phrase(move) -> str:
    """The camera move as plain language for a prompt."""
    key = getattr(move, "value", move) or CameraMove.PUSH_IN.value
    return CAMERA_MOVE_PHRASES.get(key, CAMERA_MOVE_PHRASES[CameraMove.PUSH_IN.value])


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
        scene_ids = [
            scene.id
            for scene in session.exec(
                select(Scene).where(Scene.project_id == project.id).order_by(Scene.order_index)
            ).all()
            if scene.scene_type == SceneType.VIDEO and scene.image_path
        ]

    if not scene_ids:
        with Session(engine) as session:
            project = session.get(Project, project_id)
            if project:
                _set_stage(session, project, Stage.CLIPS_READY, "Clips ready for review")
        return

    # Video submissions are independent once the stills exist. Each worker has
    # its own DB session, and request IDs are committed before polling begins.
    errors: list[Exception] = []
    worker_count = min(4, len(scene_ids))
    with ThreadPoolExecutor(max_workers=worker_count) as video_executor:
        futures = [
            video_executor.submit(_generate_scene_clip_job, project_id, scene_id)
            for scene_id in scene_ids
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project:
            return
        if _stop_if_canceled(session, project):
            return
        if errors:
            _fail(session, project, "clip generation", errors[0])
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


def _generate_scene_clip_job(project_id: str, scene_id: str, force: bool = False) -> bool:
    """Submit or resume one queued video request, using short-lived sessions."""
    vid = get_video_generator()
    with Session(engine) as session:
        project = session.get(Project, project_id)
        scene = session.get(Scene, scene_id)
        if not project or not scene or not scene.image_path:
            return False
        folder = _folder(project)
        out = _candidate_path(folder, "clips", f"scene_{scene.order_index + 1:02d}", ".mp4")
        stable_out = folder / "clips" / f"scene_{scene.order_index + 1:02d}.mp4"
        if not force and _asset_exists(scene.clip_path):
            _save_scene_ready(session, project, scene)
            return True
        if not force and stable_out.exists():
            scene.clip_path = _rel(stable_out)
            scene.video_request_id = None
            scene.video_request_status = "completed"
            scene.video_request_status_url = None
            scene.video_request_response_url = None
            _save_scene_ready(session, project, scene)
            return True
        if _stop_if_canceled(session, project):
            return False

        image_abs = Path(settings.projects_dir.parent.parent) / scene.image_path
        duration = _clip_duration(scene.duration_seconds or 3.0)
        character_refs = (
            _scene_character_refs(session, scene)
            if settings.video_character_elements
            else []
        )
        element_limit = getattr(vid, "_MAX_ELEMENTS", len(character_refs))
        element_refs = character_refs[:element_limit]
        prompt = _build_clip_prompt(
            scene, _content_motion_style(session, project), element_refs
        )
        end_image = (
            _next_scene_image(session, project, scene)
            if scene.use_next_scene_as_end_frame
            else None
        )
        existing_request_id = None if force else scene.video_request_id
        existing_status_url = None if force else scene.video_request_status_url
        existing_response_url = None if force else scene.video_request_response_url
        scene.status = "generating"
        if force:
            scene.video_request_id = None
            scene.video_request_status = None
            scene.video_request_status_url = None
            scene.video_request_response_url = None
        session.add(scene)
        session.commit()
        bus.publish("scene.status", project_id=project.id, scene_id=scene.id, status="generating", stage="clip")

    elements = [(ref["path"], ref["variants"]) for ref in element_refs] or None
    if not hasattr(vid, "submit") or not hasattr(vid, "retrieve"):
        result = vid.generate(image_abs, prompt, out, duration, elements, end_image)
        with Session(engine) as session:
            project = session.get(Project, project_id)
            scene = session.get(Scene, scene_id)
            if not project or not scene:
                return False
            previous = scene.clip_path
            scene.clip_path = _rel(result)
            scene.clip_variants = _paths_with(scene.clip_variants, previous, scene.clip_path)
            scene.video_request_status = "completed"
            scene.asset_version = (scene.asset_version or 0) + 1
            scene.status = "ready"
            session.add(scene)
            session.commit()
            bus.publish("scene.updated", project_id=project.id, scene_id=scene.id)
        return True

    request_id = existing_request_id
    request_status_url = existing_status_url
    request_response_url = existing_response_url
    if not request_id:
        request = vid.submit(image_abs, prompt, duration, elements, end_image)
        request_id = request["request_id"]
        request_status_url = request.get("status_url")
        request_response_url = request.get("response_url")
        with Session(engine) as session:
            scene = session.get(Scene, scene_id)
            if not scene:
                return False
            scene.video_request_id = request_id
            scene.video_request_status = "queued"
            scene.video_request_status_url = request_status_url
            scene.video_request_response_url = request_response_url
            session.add(scene)
            session.commit()

    try:
        result = vid.retrieve(
            request_id,
            out,
            status_url=request_status_url,
            response_url=request_response_url,
        )
    except TimeoutError:
        with Session(engine) as session:
            scene = session.get(Scene, scene_id)
            if scene:
                scene.video_request_id = request_id
                scene.video_request_status = "timeout"
                scene.status = "failed"
                session.add(scene)
                session.commit()
        raise
    except Exception:
        with Session(engine) as session:
            scene = session.get(Scene, scene_id)
            if scene:
                scene.video_request_id = None
                scene.video_request_status = "failed"
                scene.video_request_status_url = None
                scene.video_request_response_url = None
                scene.status = "failed"
                session.add(scene)
                session.commit()
        raise

    with Session(engine) as session:
        project = session.get(Project, project_id)
        scene = session.get(Scene, scene_id)
        if not project or not scene:
            return False
        previous = scene.clip_path
        scene.clip_path = _rel(result)
        scene.clip_variants = _paths_with(scene.clip_variants, previous, scene.clip_path)
        scene.video_request_id = None
        scene.video_request_status = "completed"
        scene.video_request_status_url = None
        scene.video_request_response_url = None
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
                elif scene.scene_type == SceneType.ANIMATION and scene.animation_path and (root / scene.animation_path).exists():
                    # Already an MP4 sized to the scene; normalize to WxH/fps like a clip.
                    ffmpeg._normalize_clip(root / scene.animation_path, seg, duration)
                elif scene.image_path and (root / scene.image_path).exists():
                    _render_still_segment(
                        session, project, scene, root / scene.image_path, seg,
                        duration, segments[-1] if segments else None,
                    )
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
            # Left as None when subtitles are off, so a captions.ass written by
            # an earlier render is never picked back up.
            captions = None
            if project.subtitles_enabled:
                hook_text, hook_kicker = _hook_card(folder)
                captions = ffmpeg.build_ass_captions(
                    timeline,
                    folder / "final" / "captions.ass",
                    position=project.subtitle_position,
                    hook_text=hook_text,
                    hook_kicker=hook_kicker,
                )

            narration = folder / "audio" / "full_narration.mp3"
            # The selected take may have changed since the last TTS generation.
            ffmpeg.concat_audio(
                [root / scene.audio_path for scene in scenes if scene.audio_path],
                narration,
            )
            music_path = None
            music_track = None
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

            _write_metadata(folder, project, music_track if music_path else None)
        except Exception as exc:  # noqa: BLE001
            if _stop_if_canceled(session, project):
                return
            _fail(session, project, "final render", exc)
            return
        if _stop_if_canceled(session, project):
            return
        _set_stage(session, project, Stage.DONE, "Done")


def _hook_card(folder: Path) -> tuple[str, str]:
    """The script's on-screen hook and its kicker, read back at render time.

    Read from script.json rather than carried on the project row because it is
    copy the script model wrote, and a re-render should burn whatever the
    current script says rather than a value cached when the row was last saved.

    The kicker is ``cover_kicker``, shared with the title card rather than given
    a field of its own: both want the same thing, a short label naming the arc
    above the piece that carries the weight, and sharing it keeps the thumbnail
    and the opening two seconds saying the same words.
    """
    script_path = folder / "script.json"
    if not script_path.exists():
        return "", ""
    try:
        metadata = json.loads(script_path.read_text(encoding="utf-8")).get("metadata", {})
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return "", ""
    return (
        str(metadata.get("hook_text", "") or "").strip(),
        str(metadata.get("cover_kicker", "") or "").strip(),
    )


def _write_metadata(
    folder: Path,
    project: Project,
    music_track: MusicTrack | None = None,
) -> None:
    """Reuse script metadata and add required soundtrack attribution."""
    meta = {}
    script_path = folder / "script.json"
    if script_path.exists():
        try:
            meta = json.loads(script_path.read_text(encoding="utf-8")).get("metadata", {})
        except json.JSONDecodeError:
            meta = {}
    # Also cleaned here, not only where the script is generated, so re-rendering
    # a project written before this takes the dashes out of its copy too.
    meta = dashless_copy(meta)
    meta.setdefault("title", project.title)
    meta.setdefault("description", "")
    meta.setdefault("hashtags", [])
    # One description serves both platforms; the per-platform assembly (hashtag
    # line, length limits) happens at publish time in routers/projects.py.
    meta.pop("suggested_caption", None)
    if music_track:
        attribution = _music_attribution(music_track)
        description = str(meta.get("description", "") or "").strip()
        meta["description"] = f"{description}\n\n{attribution}".strip()
        meta["music_attribution"] = attribution
    (folder / "final" / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def refresh_music_attribution() -> list[str]:
    """Bring finished metadata up to the current music-credit wording.

    The credit is baked into final/metadata.json when a project renders, so a
    video rendered before the wording changed keeps the old text forever — the
    placeholder "Source: Local library" that named no real source, or, for the
    oldest renders, no credit at all even though the track is in the mix. This
    recomputes the credit from the project's own track and swaps it in place,
    leaving the rest of the description untouched.

    Idempotent, and safe to run on every start: a credit that already matches is
    skipped, and a description whose credit is not where we expect it is left
    alone rather than guessed at. Returns the titles it rewrote.
    """
    updated: list[str] = []
    with Session(engine) as session:
        projects = session.exec(
            select(Project).where(Project.folder_path.is_not(None))
        ).all()
        for project in projects:
            # Mirror the render's own condition: no music in the video means
            # there is nothing to credit.
            if not (project.music_enabled and project.music_track_id):
                continue
            track = session.get(MusicTrack, project.music_track_id)
            if not track:
                continue
            path = _folder(project) / "final" / "metadata.json"
            if not path.exists():
                continue
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                continue
            current = _music_attribution(track)
            stored = str(meta.get("music_attribution", "") or "")
            if stored == current:
                continue
            description = _without_attribution(
                str(meta.get("description", "") or ""), stored
            )
            if description is None:
                continue
            meta["description"] = f"{description}\n\n{current}".strip()
            meta["music_attribution"] = current
            try:
                path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            except OSError:
                continue
            updated.append(project.title)
    return updated


def _without_attribution(description: str, stored: str) -> str | None:
    """The description with its music credit stripped off the end.

    Returns None when a credit is present but not in the expected trailing
    position, which means the file was edited by hand and should be left as is.
    """
    description = description.rstrip()
    if stored and description.endswith(stored.rstrip()):
        return description[: -len(stored.rstrip())].rstrip()
    marker = "\n\nMusic credit:"
    index = description.rfind(marker)
    if index != -1:
        # A credit written by an older format, still in its own trailing block.
        return description[:index].rstrip()
    if "Music credit:" in description:
        return None
    # Rendered before credits existed: the track is in the mix and uncredited,
    # so appending one is the correct outcome rather than a no-op.
    return description


def _music_attribution(track: MusicTrack) -> str:
    if track.provider == "local":
        # Local tracks carry whatever provenance we know: the uploader and the
        # page it came from when recorded, otherwise a plain royalty-free note.
        credit = f'"{track.title}"'
        if track.artist_name:
            credit += f" by {track.artist_name}"
        lines = ["Music credit:", credit]
        if track.share_url:
            lines.append(f"Source: {track.share_url}")
        lines.append(f"License: {_license_name(track.license_url)}")
        lines.append("Changes: shortened and mixed with narration.")
        return "\n".join(lines)
    source_url = track.share_url or f"https://www.jamendo.com/track/{track.provider_track_id}"
    return "\n".join(
        [
            "Music credit:",
            f'"{track.title}" by {track.artist_name or "Jamendo artist"}',
            f"Source: {source_url}",
            f"License: {_creative_commons_license_name(track.license_url)} ({track.license_url})",
            "Changes: shortened and mixed with narration.",
        ]
    )


def _license_name(license_url: str) -> str:
    if "creativecommons.org" in (license_url or "").lower():
        return f"{_creative_commons_license_name(license_url)} ({license_url})"
    return "Royalty-free, free to use"


def _creative_commons_license_name(license_url: str) -> str:
    match = re.search(r"/licenses/([^/]+)/([^/]+)/?", (license_url or "").lower())
    if not match:
        return "Creative Commons"
    license_code, version = match.groups()
    return f"CC {license_code.upper()} {version}"


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
            tts = get_tts_generator(
                _content_voice_id(session, project),
                _content_voice_speed(session, project),
            )
            previous = (
                {
                    "path": scene.audio_path,
                    "timestamps_path": scene.timestamps_path,
                    "duration_seconds": scene.duration_seconds,
                }
                if scene.audio_path
                else None
            )
            audio_out = _candidate_path(
                folder, "audio", f"scene_{scene.order_index + 1:02d}", ".mp3"
            )
            ts_out = audio_out.with_suffix(".timestamps.json")
            ordered = session.exec(
                select(Scene).where(Scene.project_id == project.id).order_by(Scene.order_index)
            ).all()
            scene_index = next(
                (index for index, item in enumerate(ordered) if item.id == scene.id), 0
            )
            result = tts.synthesize(
                scene.narration_text,
                audio_out,
                ts_out,
                previous_text=(ordered[scene_index - 1].narration_text if scene_index else ""),
                next_text=(
                    ordered[scene_index + 1].narration_text
                    if scene_index + 1 < len(ordered) else ""
                ),
            )
            if _stop_if_canceled(session, project):
                return
            scene.audio_path = _rel(audio_out)
            scene.timestamps_path = _rel(ts_out)
            scene.duration_seconds = result.duration_seconds
            if previous:
                scene.audio_variants = _audio_options_with(
                    scene.audio_variants, previous
                )
            scene.audio_variants = _audio_options_with(
                scene.audio_variants,
                {
                    "path": scene.audio_path,
                    "timestamps_path": scene.timestamps_path,
                    "duration_seconds": scene.duration_seconds,
                },
            )
            scene.asset_version = (scene.asset_version or 0) + 1
            scene.status = "ready"
            session.add(scene)
            session.commit()
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


def rebuild_narration(project_id: str) -> None:
    """Rebuild the project-wide narration from the currently selected takes."""
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project:
            return
        ordered = session.exec(
            select(Scene).where(Scene.project_id == project.id).order_by(Scene.order_index)
        ).all()
        root = Path(settings.projects_dir.parent.parent)
        try:
            ffmpeg.concat_audio(
                [root / scene.audio_path for scene in ordered if scene.audio_path],
                _folder(project) / "audio" / "full_narration.mp3",
            )
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
            if _stop_if_canceled(session, project):
                return
            traceback.print_exc()


def regenerate_scene_clip(project_id: str, scene_id: str) -> None:
    try:
        _generate_scene_clip_job(project_id, scene_id, force=True)
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def regenerate_scene_animation(project_id: str, scene_id: str) -> None:
    """Re-render one animation scene (e.g. after its spec or audio changed)."""
    with Session(engine) as session:
        project = session.get(Project, project_id)
        scene = session.get(Scene, scene_id)
        if not project or not scene:
            return
        folder = _folder(project)
        try:
            _render_scene_animation(session, project, scene, folder)
        except Exception:  # noqa: BLE001
            if _stop_if_canceled(session, project):
                return
            traceback.print_exc()


def _rel(path: Path) -> str:
    return Path(path).resolve().relative_to(settings.projects_dir.parent.parent).as_posix()
