"""Pipeline state machine + background orchestration (spec sections 8-9).

Long-running generation runs off the request thread (a small thread pool) so the
UI never blocks; progress is pushed over the WebSocket event bus. Any stage can
be re-run for a single scene or the whole project without restarting from IDEA.
"""
from __future__ import annotations

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
    CharacterForm,
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


def _content_voice_id(session: Session, project: Project) -> str | None:
    """The TTS voice from the project's content preset, falling back to config."""
    if not project.content_preset_id:
        return None
    preset = session.get(ContentPreset, project.content_preset_id)
    return (preset.voice_id if preset else "") or None


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
    return (
        f"{prompt.strip()}\n\n"
        f"Visual style directive (mandatory, overrides any generic render look): {style}"
        if style
        else prompt.strip()
    )


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

        characters = session.exec(select(Character)).all()
        by_name = {c.name.lower(): c for c in characters}

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
                    continuity_context=s.continuity_context,
                    scene_type=SceneType(s.scene_type),
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
                "reference_prompt": form.reference_prompt if form else "",
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
            form.reference_prompt = ref_prompt
            form.reference_style_prompt = style
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
        _reconcile_scene_assignments(session, project.id, char, form, state)
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
            tts = get_tts_generator(_content_voice_id(session, project))
            for scene in scenes:
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

                existing_duration = _timestamp_duration(ts_out)
                if audio_out.exists() and existing_duration is not None:
                    scene.audio_path = _rel(audio_out)
                    scene.timestamps_path = _rel(ts_out)
                    scene.duration_seconds = existing_duration
                    _save_scene_ready(session, project, scene)
                    continue
                scene.status = "generating"
                session.add(scene)
                session.commit()
                bus.publish("scene.status", project_id=project.id, scene_id=scene.id, status="generating", stage="audio")
                result = tts.synthesize(scene.narration_text, audio_out, ts_out)
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
                out = folder / "images" / f"scene_{scene.order_index + 1:02d}.png"
                if _asset_exists(scene.image_path):
                    _save_scene_ready(session, project, scene)
                    continue
                if out.exists():
                    scene.image_path = _rel(out)
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
    refs = [ref["path"] for ref in character_refs] + [ref["path"] for ref in continuity_refs]

    out = _candidate_path(
        folder, "images", f"scene_{scene.order_index + 1:02d}", ".png"
    )
    ref_label = (
        "style reference image"
        if getattr(img, "name", "") in {"krea", "krea-direct"}
        else "reference image panel"
    )
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
        duration = scene.duration_seconds or 3.0
        character_refs = _scene_character_refs(session, scene)
        element_limit = getattr(vid, "_MAX_ELEMENTS", len(character_refs))
        element_refs = character_refs[:element_limit]
        prompt = _build_clip_prompt(scene.image_prompt, element_refs)
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
    meta.setdefault("title", project.title)
    meta.setdefault("description", "")
    meta.setdefault("hashtags", [])
    meta.setdefault(
        "suggested_caption",
        f"{meta.get('title', project.title)} {' '.join(meta.get('hashtags', []))}".strip(),
    )
    if music_track:
        attribution = _music_attribution(music_track)
        description = str(meta.get("description", "") or "").strip()
        meta["description"] = f"{description}\n\n{attribution}".strip()
        meta["music_attribution"] = attribution
    (folder / "final" / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _music_attribution(track: MusicTrack) -> str:
    if track.provider == "local":
        return "\n".join(
            [
                "Music credit:",
                f'"{track.title}"',
                f"Source: {track.artist_name or 'Local music library'}",
                "License: Royalty-free user-provided track",
            ]
        )
    source_url = track.share_url or f"https://www.jamendo.com/track/{track.provider_track_id}"
    license_name = _creative_commons_license_name(track.license_url)
    return "\n".join(
        [
            "Music credit:",
            f'"{track.title}" by {track.artist_name or "Jamendo artist"}',
            f"Source: {source_url}",
            f"License: {license_name} ({track.license_url})",
            "Changes: shortened and mixed with narration.",
        ]
    )


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
            tts = get_tts_generator(_content_voice_id(session, project))
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
            result = tts.synthesize(scene.narration_text, audio_out, ts_out)
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


def _rel(path: Path) -> str:
    return Path(path).resolve().relative_to(settings.projects_dir.parent.parent).as_posix()
