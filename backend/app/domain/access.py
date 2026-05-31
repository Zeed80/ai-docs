"""Row-level visibility layer — decides WHICH records a user may see.

Separation of concerns:
  * RBAC (app/auth/models.py ROLE_PERMISSIONS) answers "may this action be performed?"
  * This module answers "over which rows?" — applied to listing/detail queries.

Backward compatibility is intentional: a record with neither an owner nor a
department (legacy data) stays visible to every reader. Visibility only tightens
once records carry ownership metadata.

Visibility rules (for a user with read permission):
  * admin / manager       → everything (managers oversee cross-department work).
  * everyone else         → records that are unowned (legacy), owned by them,
                            in their department subtree, or where they are the
                            owner via `created_by`/`owner_sub`.
"""
from __future__ import annotations

import uuid

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement
from sqlalchemy.sql.selectable import Select

from app.auth.models import UserInfo, UserRole
from app.domain.org import get_department_descendants, get_user


def _is_unrestricted(user: UserInfo) -> bool:
    return UserRole.admin in user.roles or UserRole.manager in user.roles


async def _visible_department_ids(db: AsyncSession, user: UserInfo) -> set[uuid.UUID]:
    """Department subtree the user can see (their department and everything below)."""
    db_user = await get_user(db, user.sub)
    if db_user is None or db_user.department_id is None:
        return set()
    return await get_department_descendants(db, db_user.department_id)


async def visibility_filter(
    db: AsyncSession,
    user: UserInfo,
    *,
    owner_col: ColumnElement,
    department_col: ColumnElement | None = None,
) -> ColumnElement | None:
    """Build a WHERE clause restricting rows to what `user` may see.

    Returns None when no restriction applies (admin/manager) — callers should then
    add no extra filter. `owner_col` is the column holding the owner identity
    (e.g. Document.owner_sub or WorkCase.created_by). `department_col` is optional.
    """
    if _is_unrestricted(user):
        return None

    # Legacy/unowned rows remain visible to everyone.
    clauses: list[ColumnElement] = [owner_col.is_(None)]
    if department_col is not None:
        clauses.append(department_col.is_(None))

    # Rows the user owns.
    clauses.append(owner_col == user.sub)

    # Rows in the user's department subtree.
    if department_col is not None:
        dept_ids = await _visible_department_ids(db, user)
        if dept_ids:
            clauses.append(department_col.in_(dept_ids))

    return or_(*clauses)


async def apply_visibility(
    db: AsyncSession,
    user: UserInfo,
    query: Select,
    *,
    owner_col: ColumnElement,
    department_col: ColumnElement | None = None,
) -> Select:
    """Convenience wrapper: append visibility_filter() to a SELECT if one applies."""
    clause = await visibility_filter(
        db, user, owner_col=owner_col, department_col=department_col
    )
    return query if clause is None else query.where(clause)
