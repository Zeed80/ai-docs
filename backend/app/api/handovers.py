"""Handovers API — document routing between users."""
from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import get_current_user
from app.auth.models import UserInfo
from app.db.models import Handover, NotificationType
from app.db.session import get_db

router = APIRouter()
logger = structlog.get_logger()


class HandoverCreate(BaseModel):
    entity_type: str
    entity_id: uuid.UUID
    to_user: str
    comment: str | None = None


class HandoverOut(BaseModel):
    id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    from_user: str
    to_user: str
    comment: str | None
    status: str
    created_at: str

    class Config:
        from_attributes = True


@router.post("", response_model=HandoverOut, status_code=201)
async def create_handover(
    payload: HandoverCreate,
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HandoverOut:
    handover = Handover(
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        from_user=user.sub,
        to_user=payload.to_user,
        comment=payload.comment,
        status="pending",
    )
    db.add(handover)
    await db.flush()

    from app.services.notifications import create_notification
    await create_notification(
        db=db,
        user_sub=payload.to_user,
        type=NotificationType.handover,
        title="Документ передан вам",
        body=payload.comment or f"Получен {payload.entity_type} для обработки",
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        action_url=f"/{payload.entity_type}s/{payload.entity_id}",
    )

    await db.commit()
    await db.refresh(handover)
    logger.info("handover_created", entity=payload.entity_type, to=payload.to_user)
    return HandoverOut(
        id=handover.id,
        entity_type=handover.entity_type,
        entity_id=handover.entity_id,
        from_user=handover.from_user,
        to_user=handover.to_user,
        comment=handover.comment,
        status=handover.status,
        created_at=handover.created_at.isoformat(),
    )


@router.get("/inbox", response_model=list[HandoverOut])
async def inbox(
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[HandoverOut]:
    result = await db.execute(
        select(Handover)
        .where(Handover.to_user == user.sub, Handover.status == "pending")
        .order_by(Handover.created_at.desc())
    )
    items = result.scalars().all()
    return [
        HandoverOut(
            id=h.id, entity_type=h.entity_type, entity_id=h.entity_id,
            from_user=h.from_user, to_user=h.to_user, comment=h.comment,
            status=h.status, created_at=h.created_at.isoformat(),
        )
        for h in items
    ]


@router.get("/outbox", response_model=list[HandoverOut])
async def outbox(
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[HandoverOut]:
    result = await db.execute(
        select(Handover)
        .where(Handover.from_user == user.sub)
        .order_by(Handover.created_at.desc())
        .limit(50)
    )
    items = result.scalars().all()
    return [
        HandoverOut(
            id=h.id, entity_type=h.entity_type, entity_id=h.entity_id,
            from_user=h.from_user, to_user=h.to_user, comment=h.comment,
            status=h.status, created_at=h.created_at.isoformat(),
        )
        for h in items
    ]


async def _get_my_handover(db: AsyncSession, handover_id: uuid.UUID, user_sub: str) -> Handover:
    result = await db.execute(select(Handover).where(Handover.id == handover_id))
    h = result.scalar_one_or_none()
    if not h:
        raise HTTPException(status_code=404, detail="Handover not found")
    if h.to_user != user_sub:
        raise HTTPException(status_code=403, detail="Not your handover")
    if h.status != "pending":
        raise HTTPException(status_code=400, detail=f"Handover already {h.status}")
    return h


@router.post("/{handover_id}/accept")
async def accept_handover(
    handover_id: uuid.UUID,
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    h = await _get_my_handover(db, handover_id, user.sub)
    h.status = "accepted"
    await db.commit()
    return {"status": "accepted"}


@router.post("/{handover_id}/forward")
async def forward_handover(
    handover_id: uuid.UUID,
    payload: HandoverCreate,
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HandoverOut:
    h = await _get_my_handover(db, handover_id, user.sub)
    h.status = "forwarded"
    new_h = Handover(
        entity_type=h.entity_type,
        entity_id=h.entity_id,
        from_user=user.sub,
        to_user=payload.to_user,
        comment=payload.comment,
        status="pending",
    )
    db.add(new_h)
    await db.flush()

    from app.services.notifications import create_notification
    await create_notification(
        db=db,
        user_sub=payload.to_user,
        type=NotificationType.handover,
        title="Документ передан вам",
        body=payload.comment or "Переброс от коллеги",
        entity_type=new_h.entity_type,
        entity_id=new_h.entity_id,
        action_url=f"/{new_h.entity_type}s/{new_h.entity_id}",
    )

    await db.commit()
    await db.refresh(new_h)
    return HandoverOut(
        id=new_h.id, entity_type=new_h.entity_type, entity_id=new_h.entity_id,
        from_user=new_h.from_user, to_user=new_h.to_user, comment=new_h.comment,
        status=new_h.status, created_at=new_h.created_at.isoformat(),
    )


@router.post("/{handover_id}/return")
async def return_handover(
    handover_id: uuid.UUID,
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    h = await _get_my_handover(db, handover_id, user.sub)
    h.status = "returned"

    from app.services.notifications import create_notification
    await create_notification(
        db=db,
        user_sub=h.from_user,
        type=NotificationType.handover,
        title="Документ возвращён",
        body=f"{h.entity_type} возвращён от {user.sub}",
        entity_type=h.entity_type,
        entity_id=h.entity_id,
        action_url=f"/{h.entity_type}s/{h.entity_id}",
    )

    await db.commit()
    return {"status": "returned"}
