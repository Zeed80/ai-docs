"""Ф8 — cached per-mailbox counters for the client sidebar.

``GET /api/email/mailboxes`` used to run three full aggregates over
``email_messages``/``email_threads`` on every client open AND on every
WebSocket event — that is a seq scan storm on a table that only changes when
mail arrives or someone reads a thread. The counters are global per mailbox
(the per-user part is which mailboxes are *visible*, applied by the caller
afterwards), so one shared cache entry serves everyone.

Invalidation is explicit on every write path; the TTL is only a backstop for a
path we missed.
"""

from __future__ import annotations

import json

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EmailMessage, EmailThread

logger = structlog.get_logger()

_KEY = "email:mailbox_counts:v1"
_TTL = 120


class MailboxCounts(dict):
    """{mailbox: {"messages": n, "threads": n, "unread": n}} with safe lookup."""

    def for_mailbox(self, name: str) -> dict[str, int]:
        row = self.get(name) or {}
        return {
            "messages": int(row.get("messages", 0)),
            "threads": int(row.get("threads", 0)),
            "unread": int(row.get("unread", 0)),
        }


async def _compute(db: AsyncSession, names: list[str]) -> MailboxCounts:
    if not names:
        return MailboxCounts()
    msg_rows = (
        await db.execute(
            select(EmailMessage.mailbox, func.count(EmailMessage.id))
            .where(EmailMessage.mailbox.in_(names))
            .group_by(EmailMessage.mailbox)
        )
    ).all()
    thread_rows = (
        await db.execute(
            select(EmailThread.mailbox, func.count(EmailThread.id))
            .where(EmailThread.mailbox.in_(names))
            .group_by(EmailThread.mailbox)
        )
    ).all()
    unread_rows = (
        await db.execute(
            select(EmailThread.mailbox, func.count(EmailThread.id))
            .where(
                EmailThread.mailbox.in_(names),
                EmailThread.is_read == False,  # noqa: E712
                EmailThread.folder == "inbox",
            )
            .group_by(EmailThread.mailbox)
        )
    ).all()

    out = MailboxCounts({n: {"messages": 0, "threads": 0, "unread": 0} for n in names})
    for mailbox, n in msg_rows:
        out.setdefault(mailbox, {})["messages"] = int(n)
    for mailbox, n in thread_rows:
        out.setdefault(mailbox, {})["threads"] = int(n)
    for mailbox, n in unread_rows:
        out.setdefault(mailbox, {})["unread"] = int(n)
    return out


async def mailbox_counts(db: AsyncSession, names: list[str]) -> MailboxCounts:
    """Counts for ``names``, from Redis when every requested mailbox is cached."""
    if not names:
        return MailboxCounts()
    cached: dict = {}
    try:
        from app.utils.redis_client import get_async_redis

        raw = await get_async_redis().get(_KEY)
        if raw:
            cached = json.loads(raw)
    except Exception as exc:  # Redis down must never break the mail client.
        logger.debug("mailbox_counts_cache_read_failed", error=str(exc))

    if cached and all(n in cached for n in names):
        return MailboxCounts({n: cached[n] for n in names})

    fresh = await _compute(db, names)
    merged = {**cached, **fresh}
    try:
        from app.utils.redis_client import get_async_redis

        await get_async_redis().set(_KEY, json.dumps(merged), ex=_TTL)
    except Exception as exc:
        logger.debug("mailbox_counts_cache_write_failed", error=str(exc))
    return fresh


async def invalidate_mailbox_counts() -> None:
    """Drop the cache after anything that changes message/thread/read state."""
    try:
        from app.utils.redis_client import get_async_redis

        await get_async_redis().delete(_KEY)
    except Exception as exc:
        logger.debug("mailbox_counts_cache_invalidate_failed", error=str(exc))


def invalidate_mailbox_counts_sync() -> None:
    """Same, from Celery tasks that have no running event loop."""
    try:
        from app.utils.redis_client import get_sync_redis

        get_sync_redis().delete(_KEY)
    except Exception as exc:
        logger.debug("mailbox_counts_cache_invalidate_failed", error=str(exc))
