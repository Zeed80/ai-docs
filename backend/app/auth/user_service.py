"""User upsert service — syncs JWT claims into the users table on login."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import UserInfo, UserRole
from app.db.models import User

logger = structlog.get_logger()

# `/api/admin/users` pre-provisions a row before the person's first real SSO
# login, under a synthetic sub it can't know in advance (see admin.create_user).
# These prefixes mark such placeholders so upsert_user can recognize and adopt
# them below instead of leaving them orphaned.
_PLACEHOLDER_SUB_PREFIXES = ("authentik:", "local:")

_ROLE_PRIORITY = [
    UserRole.admin,
    UserRole.manager,
    UserRole.accountant,
    UserRole.buyer,
    UserRole.engineer,
    UserRole.technologist,
    UserRole.viewer,
]


def _department_code_from_groups(groups: list[str]) -> str | None:
    """Extract a department code from a `dept:<code>` Authentik group, if present."""
    for g in groups:
        low = g.lower()
        if low.startswith("dept:") and len(low) > 5:
            return g.split(":", 1)[1].strip()
    return None


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

    if user is None:
        # No row for this real `sub` yet. If an admin pre-provisioned this
        # person via /api/admin/users before their first SSO login, a
        # placeholder row exists under a synthetic sub that will never match a
        # real JWT `sub` claim — left as-is, upsert would create a second row
        # for the same email (silently orphaning the admin's role/permission
        # setup on the placeholder). Adopt the placeholder instead.
        placeholder = (
            await db.execute(
                select(User).where(
                    User.email == info.email,
                    or_(*(User.sub.startswith(p) for p in _PLACEHOLDER_SUB_PREFIXES)),
                )
            )
        ).scalar_one_or_none()
        if placeholder is not None:
            old_sub = placeholder.sub
            user = placeholder
            user.sub = info.sub
            logger.info(
                "user_placeholder_adopted",
                email=info.email,
                placeholder_sub=old_sub,
                real_sub=info.sub,
            )
        else:
            # A *different* real (non-placeholder) sub already using this email
            # is a genuine identity duplicate (e.g. two separate Authentik
            # accounts) — not something upsert can safely resolve on its own.
            # Create the new row as usual but flag it so an admin can merge or
            # deactivate the stale one instead of it going unnoticed.
            stray = (
                await db.execute(select(User).where(User.email == info.email))
            ).scalar_one_or_none()
            if stray is not None:
                logger.warning(
                    "duplicate_email_different_identity",
                    email=info.email,
                    existing_sub=stray.sub,
                    new_sub=info.sub,
                )

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

    now = datetime.now(UTC)

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

    # Sync department from a `dept:<code>` group, if one is present and matches an
    # existing Department. Absent group → leave any admin-assigned department intact.
    dept_code = _department_code_from_groups(info.groups)
    if dept_code:
        from app.db.models import Department

        dept = (
            await db.execute(select(Department).where(Department.code == dept_code))
        ).scalar_one_or_none()
        if dept is not None:
            user.department_id = dept.id

    user.last_seen_at = now
    await db.flush()
    return user
