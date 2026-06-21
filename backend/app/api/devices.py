"""Device registry API — mobile push subscriptions (self-hosted ntfy).

The mobile shell obtains/keeps a per-device random ntfy topic and registers it
here (authenticated via the WebView cookie). Pushes carry no document content.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import get_current_user
from app.auth.models import UserInfo
from app.config import settings
from app.db.models import DeviceRegistration
from app.db.session import get_db
from app.services import push

router = APIRouter()
logger = structlog.get_logger()


class DeviceRegisterRequest(BaseModel):
    # If omitted, the server allocates a fresh topic and returns it.
    ntfy_topic: str | None = Field(default=None, max_length=255)
    ntfy_endpoint: str | None = Field(default=None, max_length=1000)
    platform: str = Field(default="android", max_length=20)
    app_version: str | None = Field(default=None, max_length=50)


class DeviceOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    platform: str
    app_version: str | None
    ntfy_topic: str
    enabled: bool
    last_seen_at: datetime | None
    created_at: datetime


class DeviceRegisterResponse(DeviceOut):
    # Where the device should subscribe (external ntfy URL + topic).
    ntfy_url: str | None
    ntfy_enabled: bool


@router.post("/register", response_model=DeviceRegisterResponse)
async def register_device(
    payload: DeviceRegisterRequest,
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DeviceRegisterResponse:
    """Register (or refresh) a device for this user. Idempotent per topic."""
    topic = payload.ntfy_topic or push.new_topic()

    existing = (
        await db.execute(select(DeviceRegistration).where(DeviceRegistration.ntfy_topic == topic))
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if existing:
        if existing.user_sub != user.sub:
            raise HTTPException(status_code=409, detail="Topic already registered to another user")
        existing.ntfy_endpoint = payload.ntfy_endpoint or existing.ntfy_endpoint
        existing.platform = payload.platform
        existing.app_version = payload.app_version or existing.app_version
        existing.enabled = True
        existing.last_seen_at = now
        device = existing
    else:
        device = DeviceRegistration(
            user_sub=user.sub,
            ntfy_topic=topic,
            ntfy_endpoint=payload.ntfy_endpoint,
            platform=payload.platform,
            app_version=payload.app_version,
            enabled=True,
            last_seen_at=now,
        )
        db.add(device)

    await db.flush()
    await db.commit()
    await db.refresh(device)

    return DeviceRegisterResponse(
        **DeviceOut.model_validate(device).model_dump(),
        ntfy_url=settings.ntfy_external_url or None,
        ntfy_enabled=settings.ntfy_enabled,
    )


@router.get("", response_model=list[DeviceOut])
async def list_devices(
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DeviceOut]:
    rows = (
        await db.execute(
            select(DeviceRegistration)
            .where(DeviceRegistration.user_sub == user.sub)
            .order_by(DeviceRegistration.created_at.desc())
        )
    ).scalars().all()
    return [DeviceOut.model_validate(r) for r in rows]


@router.delete("/{device_id}", status_code=204)
async def delete_device(
    device_id: uuid.UUID,
    user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    device = (
        await db.execute(
            select(DeviceRegistration).where(
                DeviceRegistration.id == device_id,
                DeviceRegistration.user_sub == user.sub,
            )
        )
    ).scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    await db.delete(device)
    await db.commit()
