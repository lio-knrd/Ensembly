"""Platform + content preset CRUD (Layers 2 and 3, spec section 5).

Any topic preset can be paired with any platform preset — they are managed
independently.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..adapters.llm import ai_image_style_prompt
from ..database import get_session
from ..models import Character, ContentPreset, EditorialPlan, PlatformPreset, Project
from ..schemas import ContentPresetIn, PlatformPresetIn, StyleSuggestionIn

router = APIRouter(prefix="/api/presets", tags=["presets"])


# --- Platform presets ---
@router.get("/platform")
def list_platform(session: Session = Depends(get_session)):
    return [p.model_dump() for p in session.exec(select(PlatformPreset).order_by(PlatformPreset.name))]


@router.post("/platform", status_code=201)
def create_platform(body: PlatformPresetIn, session: Session = Depends(get_session)):
    preset = PlatformPreset(**body.model_dump())
    if preset.is_default:
        _clear_platform_defaults(session)
    session.add(preset)
    session.commit()
    return preset.model_dump()


@router.patch("/platform/{preset_id}")
def update_platform(preset_id: str, body: PlatformPresetIn, session: Session = Depends(get_session)):
    preset = session.get(PlatformPreset, preset_id)
    if not preset:
        raise HTTPException(404, "Preset not found")
    if body.is_default:
        _clear_platform_defaults(session)
    for field, value in body.model_dump().items():
        setattr(preset, field, value)
    session.add(preset)
    session.commit()
    return preset.model_dump()


@router.delete("/platform/{preset_id}", status_code=204)
def delete_platform(preset_id: str, session: Session = Depends(get_session)):
    preset = session.get(PlatformPreset, preset_id)
    if not preset:
        raise HTTPException(404, "Preset not found")
    for project in session.exec(select(Project).where(Project.platform_preset_id == preset_id)):
        project.platform_preset_id = None
        session.add(project)
    for plan in session.exec(select(EditorialPlan).where(EditorialPlan.platform_preset_id == preset_id)):
        plan.platform_preset_id = None
        session.add(plan)
    session.delete(preset)
    session.commit()


# --- Content presets ---
@router.get("/content")
def list_content(session: Session = Depends(get_session)):
    return [p.model_dump() for p in session.exec(select(ContentPreset).order_by(ContentPreset.name))]


@router.post("/content/suggest-style")
def suggest_content_style(body: StyleSuggestionIn):
    return {
        "image_style_prompt": ai_image_style_prompt(
            body.content_prompt,
            body.current_style_prompt,
            body.guidelines,
        )
    }


@router.post("/content", status_code=201)
def create_content(body: ContentPresetIn, session: Session = Depends(get_session)):
    preset = ContentPreset(**body.model_dump())
    if preset.is_default:
        _clear_content_defaults(session)
    session.add(preset)
    session.commit()
    return preset.model_dump()


@router.patch("/content/{preset_id}")
def update_content(preset_id: str, body: ContentPresetIn, session: Session = Depends(get_session)):
    preset = session.get(ContentPreset, preset_id)
    if not preset:
        raise HTTPException(404, "Preset not found")
    if body.is_default:
        _clear_content_defaults(session)
    for field, value in body.model_dump().items():
        setattr(preset, field, value)
    session.add(preset)
    session.commit()
    return preset.model_dump()


@router.delete("/content/{preset_id}", status_code=204)
def delete_content(preset_id: str, session: Session = Depends(get_session)):
    preset = session.get(ContentPreset, preset_id)
    if not preset:
        raise HTTPException(404, "Preset not found")
    # The group owned characters; deleting it must not orphan them behind a
    # dead id, so they drop back to ungrouped and stay in the library.
    for char in session.exec(select(Character).where(Character.content_preset_id == preset_id)):
        char.content_preset_id = None
        session.add(char)
    for project in session.exec(select(Project).where(Project.content_preset_id == preset_id)):
        project.content_preset_id = None
        session.add(project)
    for plan in session.exec(select(EditorialPlan).where(EditorialPlan.content_preset_id == preset_id)):
        plan.content_preset_id = None
        session.add(plan)
    session.delete(preset)
    session.commit()


def _clear_platform_defaults(session: Session) -> None:
    for p in session.exec(select(PlatformPreset).where(PlatformPreset.is_default == True)):  # noqa: E712
        p.is_default = False
        session.add(p)


def _clear_content_defaults(session: Session) -> None:
    for p in session.exec(select(ContentPreset).where(ContentPreset.is_default == True)):  # noqa: E712
        p.is_default = False
        session.add(p)
