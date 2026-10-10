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
        if "agent.cron.run" in self.actions:
            if self.actions != ["agent.cron.run"]:
                raise ValueError("Cron authority cannot be combined with tool authority")
            if set(self.constraints) != {"schedule", "prompt_sha256"}:
                raise ValueError(
                    "Cron authority requires exact schedule and prompt_sha256 constraints"
                )
            schedule = self.constraints["schedule"]
            prompt_sha256 = self.constraints["prompt_sha256"]
            if (
                not isinstance(schedule, str)
                or not schedule.strip()
                or not isinstance(prompt_sha256, str)
                or len(prompt_sha256) != 64
                or any(char not in "0123456789abcdef" for char in prompt_sha256)
            ):
                raise ValueError("Cron authority requires a schedule and SHA-256 prompt digest")
            return self
        for action in self.actions:
            definition = TOOLS.get(action)
            if (
                definition is None
                or definition.admin_only
                or definition.effect not in {"write", "delete", "external"}
            ):
                raise ValueError(f"Action cannot be delegated: {action}")
        # E46: every constraint must be a field the action really carries,
        # of its type, an exact value — the server does not trust the form.
        from app.domain.delegations import check_constraints

        check_constraints(self.actions, self.constraints)
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
    from app.domain.delegations import delegation_fields

    items = []
    for tool in TOOLS.values():
        if tool.admin_only or tool.effect not in {"write", "delete", "external"}:
            continue
        fields = delegation_fields(tool.name)
        # An action with nothing exact to pin cannot be scoped: not offered.
        if fields:
            items.append({"name": tool.name, "effect": tool.effect, "fields": fields})
    items.append(
        {
            "name": "agent.cron.run",
            "effect": "execute",
            "fields": [
                {"name": "schedule", "type": "string", "format": None, "required": True},
                {"name": "prompt_sha256", "type": "string", "format": "sha256", "required": True},
            ],
        }
    )
    return {"items": items}


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
