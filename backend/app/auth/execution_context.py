"""Short-lived, audience-bound identity for internal agent requests."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict


class ExecutionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str
    audience: str = "backend"
    expires_at: int
    version: int = 1


def sign_execution_context(actor: str) -> str:
    from app.config import settings

    context = ExecutionContext(actor=actor, expires_at=int(time.time()) + 60)
    payload = base64.urlsafe_b64encode(context.model_dump_json().encode()).decode()
    signature = hmac.new(
        settings.app_secret_key.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{signature}"


def verify_execution_context(token: str) -> ExecutionContext:
    from app.config import settings

    try:
        payload, signature = token.split(".", 1)
        expected = hmac.new(
            settings.app_secret_key.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid signature")
        context = ExecutionContext.model_validate(json.loads(base64.urlsafe_b64decode(payload)))
        now = int(time.time())
        if (
            context.audience != "backend"
            or context.version != 1
            or context.expires_at <= now
            or context.expires_at > now + 65
            or context.actor in {"agent-service", "anonymous", ""}
        ):
            raise ValueError("invalid context")
        return context
    except Exception as exc:
        raise HTTPException(401, "Invalid or expired execution context") from exc


async def resolve_execution_actor(actor: str):
    from sqlalchemy import select

    from app.auth.models import UserInfo, UserRole
    from app.db.models import User
    from app.db.session import _get_session_factory
    from app.domain.sections import visible_section_keys

    async with _get_session_factory()() as db:
        row = await db.scalar(select(User).where(User.sub == actor, User.is_active.is_(True)))
    if row is None:
        raise HTTPException(403, "Execution actor is inactive or unknown")
    try:
        role = UserRole(row.role)
    except ValueError:
        role = UserRole.viewer
    return UserInfo(
        sub=row.sub,
        email=row.email,
        name=row.name,
        preferred_username=row.preferred_username,
        roles=[role],
        department_id=str(row.department_id) if row.department_id else None,
        section_access=row.section_access,
        sections=visible_section_keys([role], row.section_access),
        timezone=row.timezone,
        via_agent=True,
    )
