"""Group-scoped character library (spec section 10.3).

Every character belongs to one group (content preset); the library is browsed
and filtered per group, and a project only ever casts from its own group.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlmodel import Session, func, select

from ..adapters import get_image_generator
from ..config import settings
from ..database import get_session
from ..events import bus
from ..models import Character, CharacterForm, ContentPreset, ProjectCharacter, Scene
from ..schemas import CharacterCreate, CharacterReferenceSelect, CharacterUpdate
from ..storage import character_folder
from .common import default_content_preset

router = APIRouter(prefix="/api/characters", tags=["characters"])


def _rel(path: Path) -> str:
    return Path(path).resolve().relative_to(settings.projects_dir.parent.parent).as_posix()


def _candidate_path(folder: Path, stem: str, suffix: str) -> Path:
    return folder / "variants" / f"{stem}_{uuid.uuid4().hex[:10]}{suffix}"


def _with_candidates(paths: list[str] | None, *candidates: str | None) -> list[str]:
    result = list(paths or [])
    for candidate in candidates:
        if candidate and candidate not in result:
            result.append(candidate)
    return result


def _usage_count(session: Session, character_id: str) -> int:
    return session.exec(
        select(func.count()).select_from(ProjectCharacter).where(
            ProjectCharacter.character_id == character_id
        )
    ).one()


def _serialize(session: Session, char: Character) -> dict:
    forms = session.exec(
        select(CharacterForm).where(CharacterForm.character_id == char.id).order_by(CharacterForm.is_default.desc(), CharacterForm.name)
    ).all()
    group = (
        session.get(ContentPreset, char.content_preset_id)
        if char.content_preset_id
        else None
    )
    return {
        **char.model_dump(),
        "content_preset_name": group.name if group else "",
        "forms": [f.model_dump() for f in forms],
        "used_in_projects": _usage_count(session, char.id),
    }


def _resolve_group(session: Session, content_preset_id: str | None) -> str | None:
    """Validate an explicit group, or fall back to the default one."""
    if content_preset_id:
        if not session.get(ContentPreset, content_preset_id):
            raise HTTPException(404, "Group not found")
        return content_preset_id
    fallback = default_content_preset(session)
    return fallback.id if fallback else None


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
        reference_variants=char.reference_variants,
        variant_paths=char.variant_paths,
        is_default=True,
    )
    session.add(form)
    session.commit()
    session.refresh(form)
    return form


def _sync_default_form(session: Session, char: Character) -> None:
    form = _default_form(session, char)
    form.description = char.description
    form.reference_image_path = char.reference_image_path
    form.reference_prompt = char.reference_prompt
    form.reference_style_prompt = char.reference_style_prompt
    form.reference_version = char.reference_version
    form.reference_variants = char.reference_variants
    form.variant_paths = char.variant_paths
    session.add(form)


def _project_ids(session: Session, character_id: str) -> list[str]:
    return session.exec(
        select(ProjectCharacter.project_id).where(ProjectCharacter.character_id == character_id)
    ).all()


def _publish_character(session: Session, char: Character) -> None:
    bus.publish("character.updated", character_id=char.id, project_ids=_project_ids(session, char.id))


@router.get("")
def list_characters(
    content_preset_id: str | None = Query(
        default=None,
        description="Only characters in this group; 'none' lists ungrouped ones.",
    ),
    session: Session = Depends(get_session),
):
    query = select(Character)
    if content_preset_id == "none":
        query = query.where(Character.content_preset_id == None)  # noqa: E711
    elif content_preset_id:
        query = query.where(Character.content_preset_id == content_preset_id)
    chars = session.exec(query.order_by(Character.name)).all()
    return [_serialize(session, c) for c in chars]


@router.post("", status_code=201)
def create_character(body: CharacterCreate, session: Session = Depends(get_session)):
    # An explicitly-null group means ungrouped; an omitted one means "wherever
    # the default group is", so an API caller never has to know group ids.
    if "content_preset_id" in body.model_fields_set:
        group_id = (
            _resolve_group(session, body.content_preset_id)
            if body.content_preset_id
            else None
        )
    else:
        group_id = _resolve_group(session, None)
    char = Character(
        name=body.name.strip(),
        description=body.description.strip(),
        content_preset_id=group_id,
    )
    session.add(char)
    session.commit()
    session.refresh(char)
    _default_form(session, char)

    folder = character_folder(char.name)
    if body.generate_reference and char.description:
        prompt = (
            f"Character reference portrait of {char.name}. {char.description}. "
            f"Neutral background, consistent identity, full-body, high detail."
        )
        try:
            out = get_image_generator().generate(
                prompt, _candidate_path(folder, "reference", ".png"), None
            )
            char.reference_image_path = _rel(out)
            char.reference_variants = _with_candidates(
                char.reference_variants, char.reference_image_path
            )
            char.reference_prompt = prompt
            char.reference_style_prompt = ""
            char.reference_version = (char.reference_version or 0) + 1
            session.add(char)
            _sync_default_form(session, char)
            session.commit()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"Reference image generation failed: {exc}")

    _write_metadata(folder, char)
    _publish_character(session, char)
    return _serialize(session, char)


@router.patch("/{character_id}")
def update_character(character_id: str, body: CharacterUpdate, session: Session = Depends(get_session)):
    char = session.get(Character, character_id)
    if not char:
        raise HTTPException(404, "Character not found")
    updates = body.model_dump(exclude_unset=True)
    if "content_preset_id" in updates:
        # An explicit null moves the character out of every group rather than
        # silently re-homing it in the default one.
        updates["content_preset_id"] = (
            _resolve_group(session, updates["content_preset_id"])
            if updates["content_preset_id"]
            else None
        )
    for field, value in updates.items():
        setattr(char, field, value)
    session.add(char)
    _sync_default_form(session, char)
    session.commit()
    _write_metadata(character_folder(char.name), char)
    _publish_character(session, char)
    return _serialize(session, char)


@router.delete("/{character_id}", status_code=204)
def delete_character(character_id: str, session: Session = Depends(get_session)):
    char = session.get(Character, character_id)
    if not char:
        raise HTTPException(404, "Character not found")
    project_ids = _project_ids(session, character_id)
    for scene in session.exec(select(Scene)):
        if character_id not in scene.character_ids:
            continue
        scene.character_ids = [cid for cid in scene.character_ids if cid != character_id]
        if not any(name.lower() == char.name.lower() for name in scene.suggested_characters):
            scene.suggested_characters = [*scene.suggested_characters, char.name]
        session.add(scene)
    for link in session.exec(
        select(ProjectCharacter).where(ProjectCharacter.character_id == character_id)
    ):
        session.delete(link)
    for form in session.exec(select(CharacterForm).where(CharacterForm.character_id == character_id)):
        session.delete(form)
    session.delete(char)
    session.commit()
    bus.publish("character.deleted", character_id=character_id, project_ids=project_ids)


@router.post("/{character_id}/reference")
async def upload_reference(
    character_id: str, file: UploadFile = File(...), session: Session = Depends(get_session)
):
    char = session.get(Character, character_id)
    if not char:
        raise HTTPException(404, "Character not found")
    form = _default_form(session, char)
    return await _store_reference_upload(session, char, form, file)


@router.post("/{character_id}/forms/{form_id}/reference")
async def upload_form_reference(
    character_id: str,
    form_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    char = session.get(Character, character_id)
    form = session.get(CharacterForm, form_id)
    if not char:
        raise HTTPException(404, "Character not found")
    if not form or form.character_id != char.id:
        raise HTTPException(404, "Character form not found")
    return await _store_reference_upload(session, char, form, file)


async def _store_reference_upload(
    session: Session,
    char: Character,
    form: CharacterForm,
    file: UploadFile,
) -> dict:
    folder = character_folder(char.name)
    suffix = Path(file.filename or "ref.png").suffix or ".png"
    stem = f"upload-{form.id[:8]}"
    dest = _candidate_path(folder, stem, suffix)
    dest.write_bytes(await file.read())
    previous = form.reference_image_path
    form.reference_image_path = _rel(dest)
    form.reference_variants = _with_candidates(
        form.reference_variants, previous, form.reference_image_path
    )
    form.reference_prompt = ""
    form.reference_style_prompt = ""
    form.reference_version = (form.reference_version or 0) + 1
    if form.is_default:
        char.reference_image_path = form.reference_image_path
        char.reference_variants = form.reference_variants
        char.reference_prompt = ""
        char.reference_style_prompt = ""
        char.reference_version = form.reference_version
    session.add(char)
    session.add(form)
    session.commit()
    session.refresh(char)
    _write_metadata(folder, char)
    _publish_character(session, char)
    return _serialize(session, char)


@router.post("/{character_id}/regenerate-reference")
def regenerate_reference(character_id: str, session: Session = Depends(get_session)):
    char = session.get(Character, character_id)
    if not char:
        raise HTTPException(404, "Character not found")
    return _regenerate_form_reference(session, char, _default_form(session, char))


@router.post("/{character_id}/forms/{form_id}/regenerate-reference")
def regenerate_form_reference(
    character_id: str,
    form_id: str,
    session: Session = Depends(get_session),
):
    char = session.get(Character, character_id)
    form = session.get(CharacterForm, form_id)
    if not char:
        raise HTTPException(404, "Character not found")
    if not form or form.character_id != char.id:
        raise HTTPException(404, "Character form not found")
    return _regenerate_form_reference(session, char, form)


def _regenerate_form_reference(
    session: Session,
    char: Character,
    form: CharacterForm,
) -> dict:
    folder = character_folder(char.name)
    label = char.name if form.is_default else f"{char.name} ({form.name or form.state})"
    description = form.description or char.description
    prompt = form.reference_prompt or (
        f"Character reference portrait of {label}. {description}. "
        f"Neutral background, consistent identity, full-body, high detail."
    )
    if form.reference_style_prompt and form.reference_style_prompt not in prompt:
        prompt = f"{prompt.rstrip()} Visual style: {form.reference_style_prompt.strip()}"
    try:
        out = get_image_generator().generate(
            prompt,
            _candidate_path(folder, f"reference-{form.id[:8]}", ".png"),
            None,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Reference image generation failed: {exc}")
    previous = form.reference_image_path
    form.reference_image_path = _rel(out)
    form.reference_variants = _with_candidates(
        form.reference_variants, previous, form.reference_image_path
    )
    form.reference_prompt = prompt
    form.reference_version = (form.reference_version or 0) + 1
    if form.is_default:
        char.reference_image_path = form.reference_image_path
        char.reference_variants = form.reference_variants
        char.reference_prompt = form.reference_prompt
        char.reference_style_prompt = form.reference_style_prompt
        char.reference_version = form.reference_version
    session.add(char)
    session.add(form)
    session.commit()
    session.refresh(char)
    _write_metadata(folder, char)
    _publish_character(session, char)
    return _serialize(session, char)


@router.post("/{character_id}/select-reference")
def select_reference(
    character_id: str,
    body: CharacterReferenceSelect,
    session: Session = Depends(get_session),
):
    char = session.get(Character, character_id)
    if not char:
        raise HTTPException(404, "Character not found")
    form = session.get(CharacterForm, body.form_id) if body.form_id else _default_form(session, char)
    if not form or form.character_id != char.id:
        raise HTTPException(404, "Character form not found")
    candidates = _with_candidates(form.reference_variants, form.reference_image_path)
    if body.path not in candidates:
        raise HTTPException(400, "Unknown character-sheet candidate")

    form.reference_image_path = body.path
    form.reference_variants = candidates
    form.reference_version = (form.reference_version or 0) + 1
    if form.is_default:
        char.reference_image_path = body.path
        char.reference_variants = candidates
        char.reference_version = form.reference_version
    session.add(form)
    session.add(char)
    session.commit()
    session.refresh(form)
    session.refresh(char)
    _write_metadata(character_folder(char.name), char)
    _publish_character(session, char)
    return _serialize(session, char)


def _write_metadata(folder: Path, char: Character) -> None:
    (folder / "metadata.json").write_text(
        json.dumps(
            {
                "id": char.id,
                "name": char.name,
                "description": char.description,
                "reference_prompt": char.reference_prompt,
                "reference_style_prompt": char.reference_style_prompt,
                "reference_image_path": char.reference_image_path,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
