"""Shared @mention parsing and resolution.

Single source of truth for turning `@username` tokens in free text into user `sub`s,
reused by comments and room messages so the matching rules stay consistent.
"""
from __future__ import annotations

import re

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User

MENTION_RE = re.compile(r"@([\w\-\.]+)", re.UNICODE)


def extract_mentions(text: str) -> set[str]:
    """Return the distinct @-tokens (without the @) found in text."""
    return set(MENTION_RE.findall(text or ""))


async def resolve_mentioned_subs(
    db: AsyncSession, text: str, *, exclude_sub: str | None = None
) -> set[str]:
    """Resolve @tokens in text to existing user `sub`s.

    A token matches a user by preferred_username or by sub. The author
    (exclude_sub) is dropped so people are never notified about their own mention.
    """
    tokens = extract_mentions(text)
    if not tokens:
        return set()

    rows = (
        await db.execute(
            select(User.sub).where(
                or_(User.preferred_username.in_(tokens), User.sub.in_(tokens))
            )
        )
    ).scalars().all()

    subs = set(rows)
    subs.discard(exclude_sub)
    return subs
