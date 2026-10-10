"""E34: passwords a person stores for the browser broker.

Only a person creates, lists or revokes them; an agent call (``via_agent``)
is refused. Listing never returns a value. The value is used only by
``computer_use.desktop_fill_secret``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.secret_box import encrypt
from app.audit.service import log_action
from app.auth.acting import get_effective_user
from app.auth.models import UserInfo
from app.db.models import BrowserSecret
from app.db.session import get_db

router = APIRouter()


def normalize_origin(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("origin must be an http(s) URL")
    default = {"http": 80, "https": 443}[parts.scheme]
    port = parts.port or default
    host = parts.hostname.lower()
    return f"{parts.scheme}://{host}" + ("" if port == default else f":{port}")


class BrowserSecretIn(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    origin: str = Field(min_length=8, max_length=500)
    value: str = Field(min_length=1, max_length=4000)


class BrowserSecretOut(BaseModel):
    id: uuid.UUID
    label: str
    origin: str
    created_at: datetime
    revoked_at: datetime | None = None


def _human(user: UserInfo) -> None:
    if user.via_agent:
        raise HTTPException(403, "Browser secrets are managed by a person, not the agent")


@router.post("", response_model=BrowserSecretOut, status_code=201)
async def create_browser_secret(
    payload: BrowserSecretIn,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_effective_user),
) -> BrowserSecret:
    _human(user)
    try:
        origin = normalize_origin(payload.origin)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    secret = BrowserSecret(
        owner_sub=user.sub,
        label=payload.label,
        origin=origin,
        value_encrypted=encrypt(payload.value),
    )
    db.add(secret)
    await db.flush()
    await log_action(
        db,
        action="browser_secret.create",
        entity_type="browser_secret",
        entity_id=secret.id,
        details={"label": payload.label, "origin": origin},
    )
    await db.commit()
    await db.refresh(secret)
    return secret


@router.get("", response_model=list[BrowserSecretOut])
async def list_browser_secrets(
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_effective_user),
) -> list[BrowserSecret]:
    return list(
        await db.scalars(
            select(BrowserSecret)
            .where(BrowserSecret.owner_sub == user.sub)
            .order_by(BrowserSecret.created_at.desc())
        )
    )


@router.post("/{secret_id}/revoke", response_model=BrowserSecretOut)
async def revoke_browser_secret(
    secret_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_effective_user),
) -> BrowserSecret:
    _human(user)
    secret = await db.get(BrowserSecret, secret_id, with_for_update=True)
    if secret is None or secret.owner_sub != user.sub:
        raise HTTPException(404, "Browser secret not found")
    if secret.revoked_at is None:
        secret.revoked_at = datetime.now(UTC)
        await log_action(
            db,
            action="browser_secret.revoke",
            entity_type="browser_secret",
            entity_id=secret.id,
            details={"label": secret.label},
        )
        await db.commit()
        await db.refresh(secret)
    return secret
