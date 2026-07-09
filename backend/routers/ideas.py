"""Idea backlog (spec section 10.4)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from .. import pipeline
from ..adapters.llm import suggest_editorial_items
from ..database import get_session
from ..models import (
    ContentPreset,
    EditorialItem,
    EditorialPlan,
    Idea,
    PlatformPreset,
    Project,
    Stage,
)
from ..schemas import (
    EditorialItemConvert,
    EditorialItemIn,
    EditorialItemUpdate,
    EditorialPlanIn,
    EditorialPlanUpdate,
    EditorialReorderIn,
    EditorialSuggestIn,
    IdeaConvert,
    IdeaIn,
)
from ..services.music_library import apply_default as apply_default_music
from .common import default_content_preset, default_platform_preset, get_setting

router = APIRouter(prefix="/api/ideas", tags=["ideas"])


@router.get("")
def list_ideas(session: Session = Depends(get_session)):
    return [i.model_dump() for i in session.exec(select(Idea).order_by(Idea.created_at.desc()))]


@router.post("", status_code=201)
def create_idea(body: IdeaIn, session: Session = Depends(get_session)):
    duration = body.target_duration_seconds or get_setting(session, "default_duration_seconds", 75)
    if body.plan_id and not session.get(EditorialPlan, body.plan_id):
        raise HTTPException(404, "Editorial plan not found")
    idea = Idea(
        text=body.text.strip(),
        target_duration_seconds=int(duration),
        notes=body.notes,
        plan_id=body.plan_id,
    )
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
    plan = session.get(EditorialPlan, idea.plan_id) if idea.plan_id else None
    if plan:
        inherited_platform, inherited_content = _effective_presets(session, plan)
        platform = inherited_platform or default_platform_preset(session)
        content = inherited_content or default_content_preset(session)
    else:
        platform = default_platform_preset(session)
        content = default_content_preset(session)
    platform, content = _effective_presets(session, plan)
    project = Project(
        title=(body.title or idea.text).strip()[:120],
        topic_prompt=idea.text,
        target_duration_seconds=idea.target_duration_seconds,
        platform_preset_id=body.platform_preset_id or (platform.id if platform else None),
        content_preset_id=body.content_preset_id or (content.id if content else None),
        stage=Stage.IDEA,
    )
    apply_default_music(session, project)
    session.add(project)
    session.flush()
    if plan:
        session.add(EditorialItem(
            plan_id=plan.id,
            title=project.title,
            summary=idea.notes or idea.text,
            coverage_summary="Coverage not documented yet.",
            status="in_progress",
            source_type="mythforge",
            order_index=_next_order(session, plan.id),
            target_duration_seconds=idea.target_duration_seconds,
            project_id=project.id,
        ))
    session.delete(idea)
    session.commit()
    session.refresh(project)
    if body.start:
        pipeline.submit(pipeline.generate_script, project.id)
    return project.model_dump()


# --------------------------------------------------------------------------- #
# Editorial plans and ordered works
# --------------------------------------------------------------------------- #
VALID_STATUSES = {"suggested", "planned", "in_progress", "completed", "published", "skipped"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _plan(session: Session, plan_id: str) -> EditorialPlan:
    plan = session.get(EditorialPlan, plan_id)
    if not plan:
        raise HTTPException(404, "Editorial plan not found")
    return plan


def _item(session: Session, plan_id: str, item_id: str) -> EditorialItem:
    item = session.get(EditorialItem, item_id)
    if not item or item.plan_id != plan_id:
        raise HTTPException(404, "Editorial item not found")
    return item


def _next_order(session: Session, plan_id: str) -> int:
    items = session.exec(
        select(EditorialItem).where(EditorialItem.plan_id == plan_id)
    ).all()
    return max((item.order_index for item in items), default=-1) + 1


def _effective_presets(
    session: Session,
    plan: EditorialPlan,
) -> tuple[PlatformPreset | None, ContentPreset | None]:
    platform_id = plan.platform_preset_id
    content_id = plan.content_preset_id
    current = plan
    visited = {plan.id}
    while current.parent_plan_id and (not platform_id or not content_id):
        if current.parent_plan_id in visited:
            break
        visited.add(current.parent_plan_id)
        current = session.get(EditorialPlan, current.parent_plan_id)
        if not current:
            break
        platform_id = platform_id or current.platform_preset_id
        content_id = content_id or current.content_preset_id
    platform = session.get(PlatformPreset, platform_id) if platform_id else None
    content = session.get(ContentPreset, content_id) if content_id else None
    return platform, content


def _plan_payload(session: Session, plan: EditorialPlan, include_items: bool = False) -> dict:
    platform, content = _effective_presets(session, plan)
    payload = {
        **plan.model_dump(),
        "platform_preset_name": platform.name if platform else None,
        "content_preset_name": content.name if content else None,
    }
    if include_items:
        items = session.exec(
            select(EditorialItem)
            .where(EditorialItem.plan_id == plan.id)
            .order_by(EditorialItem.order_index, EditorialItem.created_at)
        ).all()
        payload["items"] = [item.model_dump() for item in items]
    return payload


@router.get("/plans")
def list_plans(session: Session = Depends(get_session)):
    plans = session.exec(select(EditorialPlan).order_by(EditorialPlan.created_at)).all()
    return [_plan_payload(session, plan) for plan in plans]


@router.post("/plans", status_code=201)
def create_plan(body: EditorialPlanIn, session: Session = Depends(get_session)):
    if body.parent_plan_id:
        _plan(session, body.parent_plan_id)
    plan = EditorialPlan(**body.model_dump(exclude={"name"}), name=body.name.strip())
    session.add(plan)
    session.commit()
    session.refresh(plan)
    return _plan_payload(session, plan, include_items=True)


@router.get("/plans/{plan_id}")
def get_plan(plan_id: str, session: Session = Depends(get_session)):
    return _plan_payload(session, _plan(session, plan_id), include_items=True)


@router.patch("/plans/{plan_id}")
def update_plan(plan_id: str, body: EditorialPlanUpdate, session: Session = Depends(get_session)):
    plan = _plan(session, plan_id)
    updates = body.model_dump(exclude_unset=True)
    if updates.get("parent_plan_id"):
        if updates["parent_plan_id"] == plan_id:
            raise HTTPException(400, "A plan cannot be its own parent")
        parent = _plan(session, updates["parent_plan_id"])
        visited = {plan_id}
        while parent:
            if parent.id in visited:
                raise HTTPException(400, "Plan hierarchy cannot contain a cycle")
            visited.add(parent.id)
            parent = session.get(EditorialPlan, parent.parent_plan_id) if parent.parent_plan_id else None
    for key, value in updates.items():
        setattr(plan, key, value.strip() if isinstance(value, str) else value)
    plan.updated_at = _now()
    session.add(plan)
    session.commit()
    return _plan_payload(session, plan, include_items=True)


@router.delete("/plans/{plan_id}", status_code=204)
def delete_plan(plan_id: str, session: Session = Depends(get_session)):
    plan = _plan(session, plan_id)
    for item in session.exec(select(EditorialItem).where(EditorialItem.plan_id == plan_id)):
        session.delete(item)
    for idea in session.exec(select(Idea).where(Idea.plan_id == plan_id)):
        idea.plan_id = None
        session.add(idea)
    for child in session.exec(select(EditorialPlan).where(EditorialPlan.parent_plan_id == plan_id)):
        child.parent_plan_id = None
        session.add(child)
    session.delete(plan)
    session.commit()


@router.post("/plans/{plan_id}/items", status_code=201)
def create_item(plan_id: str, body: EditorialItemIn, session: Session = Depends(get_session)):
    _plan(session, plan_id)
    if body.status not in VALID_STATUSES:
        raise HTTPException(400, "Invalid editorial status")
    duration = body.target_duration_seconds or get_setting(session, "default_duration_seconds", 75)
    part_group_id = body.part_group_id
    if body.part_group_title and not part_group_id:
        sibling = session.exec(
            select(EditorialItem).where(
                EditorialItem.plan_id == plan_id,
                EditorialItem.part_group_title == body.part_group_title.strip(),
            )
        ).first()
        part_group_id = sibling.part_group_id if sibling and sibling.part_group_id else str(uuid.uuid4())
    item = EditorialItem(
        **body.model_dump(exclude={"title", "target_duration_seconds", "part_group_id"}),
        plan_id=plan_id,
        title=body.title.strip(),
        target_duration_seconds=int(duration),
        order_index=_next_order(session, plan_id),
        part_group_id=part_group_id,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item.model_dump()


@router.patch("/plans/{plan_id}/items/{item_id}")
def update_item(
    plan_id: str,
    item_id: str,
    body: EditorialItemUpdate,
    session: Session = Depends(get_session),
):
    item = _item(session, plan_id, item_id)
    updates = body.model_dump(exclude_unset=True)
    if "status" in updates and updates["status"] not in VALID_STATUSES:
        raise HTTPException(400, "Invalid editorial status")
    for key, value in updates.items():
        setattr(item, key, value.strip() if isinstance(value, str) else value)
    if "part_group_title" in updates:
        if not item.part_group_title:
            item.part_group_id = None
            item.part_number = None
        elif not item.part_group_id:
            sibling = session.exec(
                select(EditorialItem).where(
                    EditorialItem.plan_id == plan_id,
                    EditorialItem.part_group_title == item.part_group_title,
                    EditorialItem.id != item.id,
                )
            ).first()
            item.part_group_id = sibling.part_group_id if sibling and sibling.part_group_id else str(uuid.uuid4())
    item.updated_at = _now()
    session.add(item)
    session.commit()
    session.refresh(item)
    return item.model_dump()


@router.delete("/plans/{plan_id}/items/{item_id}", status_code=204)
def delete_item(plan_id: str, item_id: str, session: Session = Depends(get_session)):
    session.delete(_item(session, plan_id, item_id))
    session.commit()


@router.post("/plans/{plan_id}/reorder")
def reorder_items(
    plan_id: str,
    body: EditorialReorderIn,
    session: Session = Depends(get_session),
):
    _plan(session, plan_id)
    items = session.exec(select(EditorialItem).where(EditorialItem.plan_id == plan_id)).all()
    by_id = {item.id: item for item in items}
    if set(body.item_ids) != set(by_id):
        raise HTTPException(400, "Reorder list must contain every item exactly once")
    for index, item_id in enumerate(body.item_ids):
        by_id[item_id].order_index = index
        by_id[item_id].updated_at = _now()
        session.add(by_id[item_id])
    session.commit()
    return {"ok": True}


@router.post("/plans/{plan_id}/suggest")
def suggest_items(
    plan_id: str,
    body: EditorialSuggestIn,
    session: Session = Depends(get_session),
):
    plan = _plan(session, plan_id)
    platform, content = _effective_presets(session, plan)
    existing = session.exec(
        select(EditorialItem)
        .where(EditorialItem.plan_id == plan_id)
        .order_by(EditorialItem.order_index)
    ).all()
    result = suggest_editorial_items(
        plan_name=plan.name,
        description=plan.description,
        editorial_rules=plan.editorial_rules,
        ordering_mode=plan.ordering_mode,
        platform_prompt=platform.format_prompt if platform else "",
        content_prompt=content.content_prompt if content else "",
        existing_items=[item.model_dump() for item in existing],
        instruction=body.instruction,
        count=max(1, min(body.count, 10)),
    )
    created = []
    order = _next_order(session, plan_id)
    group_ids: dict[str, str] = {}
    for offset, suggestion in enumerate(result["suggestions"]):
        group_title = suggestion.get("part_group_title", "")
        group_id = None
        if group_title:
            group_id = group_ids.setdefault(group_title.lower(), str(uuid.uuid4()))
        item = EditorialItem(
            plan_id=plan_id,
            title=suggestion["title"],
            summary=suggestion["summary"],
            coverage_summary=suggestion["coverage_scope"],
            status="suggested",
            source_type="ai",
            order_index=order + offset,
            target_duration_seconds=int(get_setting(session, "default_duration_seconds", 75)),
            part_group_id=group_id,
            part_group_title=group_title,
            part_number=suggestion.get("part_number"),
            ai_rationale=suggestion["rationale"],
        )
        session.add(item)
        created.append(item)
    session.commit()
    for item in created:
        session.refresh(item)
    return {"overview": result["overview"], "items": [item.model_dump() for item in created]}


@router.post("/plans/{plan_id}/items/{item_id}/convert")
def convert_item(
    plan_id: str,
    item_id: str,
    body: EditorialItemConvert,
    session: Session = Depends(get_session),
):
    plan = _plan(session, plan_id)
    item = _item(session, plan_id, item_id)
    if item.project_id:
        raise HTTPException(400, "This work already has a MythForge project")
    existing_titles = set(session.exec(select(Project.title)).all())
    title = item.title[:120]
    if title in existing_titles:
        base = title[:112]
        number = 2
        while f"{base} ({number})" in existing_titles:
            number += 1
        title = f"{base} ({number})"
    sibling_context = ""
    if item.part_group_id:
        siblings = session.exec(
            select(EditorialItem)
            .where(EditorialItem.part_group_id == item.part_group_id)
            .order_by(EditorialItem.part_number)
        ).all()
        sibling_context = "\nPart group:\n" + "\n".join(
            f"- Part {s.part_number}: {s.title} — {s.coverage_summary}" for s in siblings
        )
    topic_prompt = (
        f"Editorial plan: {plan.name}\n"
        f"Plan purpose: {plan.description}\n"
        f"Editorial rules: {plan.editorial_rules}\n\n"
        f"Create this planned work: {item.title}\n"
        f"Summary: {item.summary}\n"
        f"Hard coverage boundary: {item.coverage_summary}\n"
        "Respect the coverage boundary and do not duplicate material assigned to other works."
        f"{sibling_context}"
    )
    project = Project(
        title=title,
        topic_prompt=topic_prompt,
        target_duration_seconds=item.target_duration_seconds,
        platform_preset_id=platform.id if platform else None,
        content_preset_id=content.id if content else None,
        stage=Stage.IDEA,
    )
    apply_default_music(session, project)
    session.add(project)
    session.flush()
    item.project_id = project.id
    item.source_type = "mythforge"
    item.status = "in_progress"
    item.updated_at = _now()
    session.add(item)
    session.commit()
    session.refresh(project)
    if body.start:
        pipeline.submit(pipeline.generate_script, project.id)
    return project.model_dump()
