"""User upsert service — syncs JWT claims into the users table on login."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import UserInfo, UserRole
from app.db.models import User

_ROLE_PRIORITY = [
    UserRole.admin,
    UserRole.manager,
    UserRole.accountant,
    UserRole.buyer,
    UserRole.engineer,
    UserRole.viewer,
]


def pick_primary_role(roles: list[UserRole]) -> str:
    """Return the highest-privilege role from the list."""
    for r in _ROLE_PRIORITY:
        if r in roles:
            return r.value
    return UserRole.viewer.value


async def _any_admin_exists(db: AsyncSession) -> bool:
    """Return True if at least one active admin user exists in the DB."""
    from sqlalchemy import func as sa_func
    result = await db.execute(
        select(sa_func.count()).where(User.role == "admin", User.is_active == True)  # noqa: E712
    )
    return (result.scalar() or 0) > 0


async def upsert_user(db: AsyncSession, info: UserInfo) -> User:
    """Create or update a user record from JWT claims. Called on every login."""
    from app.config import settings

    result = await db.execute(select(User).where(User.sub == info.sub))
    user = result.scalar_one_or_none()

    canonical_role = pick_primary_role(info.roles)

    # Bootstrap first admin: if INITIAL_ADMIN_EMAIL is set and no admin exists yet,
    # the matching user automatically receives the admin role.
    if (
        settings.initial_admin_email
        and info.email.lower() == settings.initial_admin_email.lower()
        and canonical_role != "admin"
        and not await _any_admin_exists(db)
    ):
        canonical_role = "admin"

    now = datetime.now(timezone.utc)

    if user is None:
        user = User(
            sub=info.sub,
            email=info.email,
            name=info.name,
            preferred_username=info.preferred_username,
            role=canonical_role,
            is_active=True,
        )
        db.add(user)
    else:
        user.email = info.email
        user.name = info.name
        user.preferred_username = info.preferred_username
        user.role = canonical_role

    user.last_seen_at = now
    await db.flush()
    return user
