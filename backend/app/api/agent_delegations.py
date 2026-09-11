"""Human-controlled, bounded standing permissions for the agent."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tool_catalog import TOOLS
from app.audit.service import log_action
from app.auth.jwt import require_human_role
from app.auth.models import UserInfo, UserRole
from app.db.agent_runtime_models import DelegationGrant
from app.db.session import get_db

router = APIRouter(prefix="/api/agent/delegations", tags=["agent-delegations"])
human_owner = require_human_role(
    UserRole.admin,
    UserRole.manager,
    UserRole.engineer,
    UserRole.accountant,
    UserRole.buyer,
    UserRole.technologist,
    UserRole.normcontroller,
    UserRole.calculator,
)


class DelegationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    actions: list[str] = Field(min_length=1, max_length=20)
    constraints: dict[str, Any]
    max_actions: int = Field(default=20, ge=1, le=200)
    duration_hours: int = Field(default=2, ge=1, le=168)

    @model_validator(mode="after")
    def validate_authority(self):
        if not self.constraints or any(
            k in {"reason", "action", "body", "filters"} for k in self.constraints
        ):
            raise ValueError("Non-empty exact argument constraints are required")
        for action in self.actions:
            definition = TOOLS.get(action)
            if (
                definition is None
                or definition.admin_only
                or definition.effect not in {"write", "delete", "external"}
            ):
                raise ValueError(f"Action cannot be delegated: {action}")
        return self


def describe(grant: DelegationGrant) -> dict:
    return {
        key: getattr(grant, key)
        for key in (
            "id",
            "title",
            "actions",
            "constraints",
            "max_actions",
            "used_actions",
            "expires_at",
            "revoked_at",
        )
    }


@router.get("/actions")
async def delegation_actions(user: UserInfo = Depends(human_owner)):
    return {
        "items": [
            {"name": tool.name, "effect": tool.effect}
            for tool in TOOLS.values()
            if not tool.admin_only and tool.effect in {"write", "delete", "external"}
        ]
    }


@router.get("")
async def list_delegations(
    db: AsyncSession = Depends(get_db), user: UserInfo = Depends(human_owner)
):
    grants = await db.scalars(
        select(DelegationGrant)
        .where(
            DelegationGrant.owner_key == user.sub,
        )
        .order_by(DelegationGrant.created_at.desc())
        .limit(200)
    )
    return {"items": [describe(grant) for grant in grants]}


@router.post("", status_code=201)
async def create_delegation(
    body: DelegationCreate,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(human_owner),
):
    grant = DelegationGrant(
        owner_key=user.sub,
        title=body.title,
        actions=body.actions,
        constraints=body.constraints,
        max_actions=body.max_actions,
        used_actions=0,
        expires_at=datetime.now(UTC) + timedelta(hours=body.duration_hours),
    )
    db.add(grant)
    await db.flush()
    await log_action(
        db,
        action="agent.delegation.created",
        entity_type="delegation",
        entity_id=grant.id,
        user_id=user.sub,
        details=body.model_dump(mode="json"),
    )
    await db.commit()
    return describe(grant)


@router.delete("/{grant_id}")
async def revoke_delegation(
    grant_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: UserInfo = Depends(human_owner)
):
    grant = await db.scalar(
        select(DelegationGrant)
        .where(
            DelegationGrant.id == grant_id,
            DelegationGrant.owner_key == user.sub,
        )
        .with_for_update()
    )
    if grant is None:
        raise HTTPException(404, "Delegation not found")
    if grant.revoked_at is None:
        grant.revoked_at = datetime.now(UTC)
        await log_action(
            db,
            action="agent.delegation.revoked",
            entity_type="delegation",
            entity_id=grant.id,
            user_id=user.sub,
        )
        await db.commit()
    return describe(grant)
