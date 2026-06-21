"""Push notification service — self-hosted ntfy (no Google services).

Privacy: only a per-device random topic is stored; pushes carry a short
title/body plus type/action_url/notification_id. Document content is never sent.
"""
from __future__ import annotations

import secrets

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import DeviceRegistration

logger = structlog.get_logger()

_TIMEOUT = 5.0


def new_topic() -> str:
    """Generate a hard-to-guess ntfy topic (acts as the per-device secret)."""
    return f"sveta-{secrets.token_urlsafe(18)}"


def _absolute_url(action_url: str | None) -> str | None:
    if not action_url:
        return None
    if action_url.startswith("http://") or action_url.startswith("https://"):
        return action_url
    base = (settings.frontend_url or "").rstrip("/")
    return f"{base}{action_url}" if base else action_url


def _headers(title: str, click_url: str | None, ntype: str | None) -> dict[str, str]:
    headers: dict[str, str] = {
        # ntfy reads these UTF-8 fields as message metadata.
        "Title": title.encode("utf-8", "replace").decode("latin-1", "replace")
        if title.isascii()
        else title,
        "Markdown": "no",
    }
    # ntfy requires header values to be latin-1 safe; send non-ASCII titles in the body instead.
    if not title.isascii():
        headers.pop("Title", None)
    if click_url:
        headers["Click"] = click_url
    if ntype:
        headers["Tags"] = ntype
    if settings.ntfy_token:
        headers["Authorization"] = f"Bearer {settings.ntfy_token}"
    return headers


def _publish_sync(topic: str, title: str, body: str, click_url: str | None, ntype: str | None) -> None:
    url = f"{settings.ntfy_url.rstrip('/')}/{topic}"
    # When the title can't go in a header (non-ASCII), prepend it to the body.
    message = body if title.isascii() else f"{title}\n{body}"
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            client.post(url, content=message.encode("utf-8"), headers=_headers(title, click_url, ntype))
    except Exception as e:  # never let push failures break the caller
        logger.warning("ntfy_publish_failed", topic=topic, error=str(e))


async def _publish_async(topic: str, title: str, body: str, click_url: str | None, ntype: str | None) -> None:
    url = f"{settings.ntfy_url.rstrip('/')}/{topic}"
    message = body if title.isascii() else f"{title}\n{body}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            await client.post(url, content=message.encode("utf-8"), headers=_headers(title, click_url, ntype))
    except Exception as e:
        logger.warning("ntfy_publish_failed", topic=topic, error=str(e))


async def push_to_user(
    db: AsyncSession,
    user_sub: str,
    title: str,
    body: str,
    *,
    action_url: str | None = None,
    notification_type: str | None = None,
) -> None:
    """Publish a push to all enabled devices of a user (async context)."""
    if not settings.ntfy_enabled:
        return
    rows = (
        await db.execute(
            select(DeviceRegistration.ntfy_topic).where(
                DeviceRegistration.user_sub == user_sub,
                DeviceRegistration.enabled == True,  # noqa: E712
            )
        )
    ).scalars().all()
    click = _absolute_url(action_url)
    for topic in rows:
        await _publish_async(topic, title, body, click, notification_type)


def push_to_user_sync(
    db: Session,
    user_sub: str,
    title: str,
    body: str,
    *,
    action_url: str | None = None,
    notification_type: str | None = None,
) -> None:
    """Publish a push to all enabled devices of a user (sync/Celery context)."""
    if not settings.ntfy_enabled:
        return
    rows = (
        db.execute(
            select(DeviceRegistration.ntfy_topic).where(
                DeviceRegistration.user_sub == user_sub,
                DeviceRegistration.enabled == True,  # noqa: E712
            )
        )
        .scalars()
        .all()
    )
    click = _absolute_url(action_url)
    for topic in rows:
        _publish_sync(topic, title, body, click, notification_type)
