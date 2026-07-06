"""Idea backlog (spec section 10.4)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from .. import pipeline
from ..database import get_session
from ..models import Idea, Project, Stage
from ..schemas import IdeaConvert, IdeaIn
from .common import default_content_preset, default_platform_preset, get_setting

router = APIRouter(prefix="/api/ideas", tags=["ideas"])


@router.get("")
def list_ideas(session: Session = Depends(get_session)):
    return [i.model_dump() for i in session.exec(select(Idea).order_by(Idea.created_at.desc()))]


@router.post("", status_code=201)
def create_idea(body: IdeaIn, session: Session = Depends(get_session)):
    duration = body.target_duration_seconds or get_setting(session, "default_duration_seconds", 75)
    idea = Idea(text=body.text.strip(), target_duration_seconds=int(duration), notes=body.notes)
    session.add(idea)
    session.commit()
    return idea.model_dump()


@router.delete("/{idea_id}", status_code=204)
def delete_idea(idea_id: str, session: Session = Depends(get_session)):
    idea = session.get(Idea, idea_id)
    if not idea:
        raise HTTPException(404, "Idea not found")
    session.delete(idea)
    session.commit()


@router.post("/{idea_id}/convert")
def convert_idea(idea_id: str, body: IdeaConvert, session: Session = Depends(get_session)):
    idea = session.get(Idea, idea_id)
    if not idea:
        raise HTTPException(404, "Idea not found")
    platform = default_platform_preset(session)
    content = default_content_preset(session)
    project = Project(
        title=(body.title or idea.text).strip()[:120],
        topic_prompt=idea.text,
        target_duration_seconds=idea.target_duration_seconds,
        platform_preset_id=body.platform_preset_id or (platform.id if platform else None),
        content_preset_id=body.content_preset_id or (content.id if content else None),
        stage=Stage.IDEA,
    )
    session.add(project)
    session.delete(idea)
    session.commit()
    session.refresh(project)
    if body.start:
        pipeline.submit(pipeline.generate_script, project.id)
    return project.model_dump()
