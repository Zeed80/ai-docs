"""Administrator-managed verified channel bindings."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import log_action
from app.auth.jwt import require_human_role
from app.auth.models import UserInfo, UserRole
from app.db.agent_runtime_models import AgentChannelIdentity
from app.db.models import User
from app.db.session import get_db

router = APIRouter(prefix="/api/agent/channels", tags=["agent-channels"])
admin = require_human_role(UserRole.admin)


class TelegramBinding(BaseModel):
    owner_key: str = Field(min_length=1, max_length=200)
    telegram_user_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")


@router.post("/telegram")
async def bind_telegram(
    body: TelegramBinding, db: AsyncSession = Depends(get_db), user: UserInfo = Depends(admin)
):
    owner = await db.scalar(
        select(User).where(User.sub == body.owner_key, User.is_active.is_(True))
    )
    if owner is None:
        raise HTTPException(404, "Active owner not found")
    # A row lock cannot protect a missing row. Serialize this external identity
    # so concurrent administrators cannot both pass the select-then-insert gap.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"telegram:{body.telegram_user_id}"},
    )
    identity = await db.scalar(
        select(AgentChannelIdentity)
        .where(
            AgentChannelIdentity.channel == "telegram",
            AgentChannelIdentity.external_id == body.telegram_user_id,
            AgentChannelIdentity.is_active.is_(True),
        )
        .with_for_update()
    )
    if identity is not None:
        raise HTTPException(409, "Channel already bound; revoke it before rebinding")
    # Revoked bindings are historical security principals: never recycle their
    # id, since durable runs retain it as their reply destination.
    identity = AgentChannelIdentity(
        owner_key=body.owner_key,
        channel="telegram",
        external_id=body.telegram_user_id,
    )
    db.add(identity)
    await db.flush()
    await log_action(
        db,
        action="agent.channel.bound",
        entity_type="agent_channel",
        entity_id=identity.id,
        user_id=user.sub,
        details=body.model_dump(),
    )
    await db.commit()
    return {"id": str(identity.id), **body.model_dump()}


@router.delete("/telegram/{telegram_user_id}")
async def unbind_telegram(
    telegram_user_id: str, db: AsyncSession = Depends(get_db), user: UserInfo = Depends(admin)
):
    identity = await db.scalar(
        select(AgentChannelIdentity)
        .where(
            AgentChannelIdentity.channel == "telegram",
            AgentChannelIdentity.external_id == telegram_user_id,
            AgentChannelIdentity.is_active.is_(True),
        )
        .with_for_update()
    )
    if identity is None:
        raise HTTPException(404, "Binding not found")
    await log_action(
        db,
        action="agent.channel.revoked",
        entity_type="agent_channel",
        entity_id=identity.id,
        user_id=user.sub,
    )
    identity.is_active = False
    await db.commit()
    return {"revoked": True}
