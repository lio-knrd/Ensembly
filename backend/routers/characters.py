"""Global character library (spec section 10.3)."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlmodel import Session, func, select

from ..adapters import get_image_generator
from ..config import settings
from ..database import get_session
from ..models import Character, ProjectCharacter, Scene
from ..schemas import CharacterCreate, CharacterUpdate
from ..storage import character_folder

router = APIRouter(prefix="/api/characters", tags=["characters"])


def _rel(path: Path) -> str:
    return Path(path).resolve().relative_to(settings.projects_dir.parent.parent).as_posix()


def _usage_count(session: Session, character_id: str) -> int:
    return session.exec(
        select(func.count()).select_from(ProjectCharacter).where(
            ProjectCharacter.character_id == character_id
        )
    ).one()


def _serialize(session: Session, char: Character) -> dict:
    return {**char.model_dump(), "used_in_projects": _usage_count(session, char.id)}


@router.get("")
def list_characters(session: Session = Depends(get_session)):
    chars = session.exec(select(Character).order_by(Character.name)).all()
    return [_serialize(session, c) for c in chars]


@router.post("", status_code=201)
def create_character(body: CharacterCreate, session: Session = Depends(get_session)):
    char = Character(name=body.name.strip(), description=body.description.strip())
    session.add(char)
    session.commit()
    session.refresh(char)

    folder = character_folder(char.name)
    if body.generate_reference and char.description:
        prompt = (
            f"Character reference portrait of {char.name}. {char.description}. "
            f"Neutral background, consistent identity, full-body, high detail."
        )
        try:
            out = get_image_generator().generate(prompt, folder / "reference.png", None)
            char.reference_image_path = _rel(out)
            char.reference_prompt = prompt
            char.reference_style_prompt = ""
            session.add(char)
            session.commit()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"Reference image generation failed: {exc}")

    _write_metadata(folder, char)
    return _serialize(session, char)


@router.patch("/{character_id}")
def update_character(character_id: str, body: CharacterUpdate, session: Session = Depends(get_session)):
    char = session.get(Character, character_id)
    if not char:
        raise HTTPException(404, "Character not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(char, field, value)
    session.add(char)
    session.commit()
    _write_metadata(character_folder(char.name), char)
    return _serialize(session, char)


@router.delete("/{character_id}", status_code=204)
def delete_character(character_id: str, session: Session = Depends(get_session)):
    char = session.get(Character, character_id)
    if not char:
        raise HTTPException(404, "Character not found")
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
    session.delete(char)
    session.commit()


@router.post("/{character_id}/reference")
async def upload_reference(
    character_id: str, file: UploadFile = File(...), session: Session = Depends(get_session)
):
    char = session.get(Character, character_id)
    if not char:
        raise HTTPException(404, "Character not found")
    folder = character_folder(char.name)
    suffix = Path(file.filename or "ref.png").suffix or ".png"
    dest = folder / f"reference{suffix}"
    dest.write_bytes(await file.read())
    char.reference_image_path = _rel(dest)
    char.reference_prompt = ""
    char.reference_style_prompt = ""
    session.add(char)
    session.commit()
    _write_metadata(folder, char)
    return _serialize(session, char)


@router.post("/{character_id}/regenerate-reference")
def regenerate_reference(character_id: str, session: Session = Depends(get_session)):
    char = session.get(Character, character_id)
    if not char:
        raise HTTPException(404, "Character not found")
    folder = character_folder(char.name)
    prompt = (
        f"Character reference portrait of {char.name}. {char.description}. "
        f"Neutral background, consistent identity, full-body, high detail."
    )
    try:
        out = get_image_generator().generate(prompt, folder / "reference.png", None)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Reference image generation failed: {exc}")
    char.reference_image_path = _rel(out)
    char.reference_prompt = prompt
    char.reference_style_prompt = ""
    session.add(char)
    session.commit()
    _write_metadata(folder, char)
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
