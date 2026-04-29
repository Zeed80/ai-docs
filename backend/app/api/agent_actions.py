"""Agent Actions API — audit trail for every Света step."""

import uuid

import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.db.models import AgentAction

router = APIRouter()
logger = structlog.get_logger()


class AgentActionOut(BaseModel):
    id: uuid.UUID
    session_id: str
    iteration: int
    action_type: str
    tool_name: str | None
    tool_args: dict | None
    tool_result: dict | None
    content_text: str | None
    model_name: str | None
    duration_ms: int | None
    error: str | None

    model_config = {"from_attributes": True}


class AgentActionCreate(BaseModel):
    session_id: str
    iteration: int = 0
    action_type: str
    tool_name: str | None = None
    tool_args: dict | None = None
    tool_result: dict | None = None
    content_text: str | None = None
    model_name: str | None = None
    duration_ms: int | None = None
    error: str | None = None


class AgentActionListResponse(BaseModel):
    items: list[AgentActionOut]
    total: int


@router.post("", status_code=201, response_model=AgentActionOut)
async def create_agent_action(
    payload: AgentActionCreate,
    db: AsyncSession = Depends(get_db),
):
    action = AgentAction(**payload.model_dump())
    db.add(action)
    await db.commit()
    await db.refresh(action)
    return action


@router.get("", response_model=AgentActionListResponse)
async def list_agent_actions(
    session_id: str | None = None,
    action_type: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    q = select(AgentAction)
    if session_id:
        q = q.where(AgentAction.session_id == session_id)
    if action_type:
        q = q.where(AgentAction.action_type == action_type)
    q = q.order_by(AgentAction.created_at.asc())

    from sqlalchemy import func
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    items = (await db.execute(q.offset(offset).limit(limit))).scalars().all()
    return AgentActionListResponse(items=list(items), total=total)


@router.get("/{action_id}", response_model=AgentActionOut)
async def get_agent_action(
    action_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    from fastapi import HTTPException
    result = await db.execute(select(AgentAction).where(AgentAction.id == action_id))
    action = result.scalar_one_or_none()
    if not action:
        raise HTTPException(404, "Agent action not found")
    return action
