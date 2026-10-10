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

from fastapi import Depends, HTTPException, Request
from sqlalchemy import and_, or_
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

    # Legacy rows — neither an owner nor a department — remain visible to
    # everyone. Both must be missing: a row owned by someone but with no
    # department (a personal mailbox's attachment, an upload) is theirs, not
    # the company's. This was an OR, which made every such row public.
    legacy = owner_col.is_(None)
    if department_col is not None:
        legacy = and_(legacy, department_col.is_(None))
    clauses: list[ColumnElement] = [legacy]

    # Rows the user owns.
    clauses.append(owner_col == user.sub)

    # Rows in the user's department subtree.
    if department_col is not None:
        dept_ids = await _visible_department_ids(db, user)
        if dept_ids:
            clauses.append(department_col.in_(dept_ids))

    return or_(*clauses)


# Document types that are company reference material whoever brought them in:
# a supplier's catalog is read by every buyer and engineer. The owner still
# records who uploaded it.
SHARED_DOCUMENT_TYPES = ("supplier_catalog",)


async def document_visibility_filter(db: AsyncSession, user: UserInfo) -> ColumnElement | None:
    """visibility_filter for documents, with the company-wide document types."""
    from app.db.models import Document, DocumentType

    clause = await visibility_filter(
        db, user, owner_col=Document.owner_sub, department_col=Document.department_id
    )
    if clause is None:
        return None
    shared = [DocumentType(value) for value in SHARED_DOCUMENT_TYPES]
    return or_(clause, Document.doc_type.in_(shared))


async def apply_visibility(
    db: AsyncSession,
    user: UserInfo,
    query: Select,
    *,
    owner_col: ColumnElement,
    department_col: ColumnElement | None = None,
) -> Select:
    """Convenience wrapper: append visibility_filter() to a SELECT if one applies."""
    clause = await visibility_filter(db, user, owner_col=owner_col, department_col=department_col)
    return query if clause is None else query.where(clause)


# ── Per-object guards for /{id} routes ──────────────────────────────────────
# Lists applied visibility_filter, but /documents/{id}, /invoices/{id} and
# /cases/{id} loaded the row by id alone: anyone holding an id could read,
# download, edit or delete another department's document (E40 audit). A
# router-level dependency closes every such route at once, including ones
# added later; an object the user may not see answers exactly like a
# missing one.


async def document_visible_to(db: AsyncSession, user: UserInfo, document_id: uuid.UUID) -> bool:
    from sqlalchemy import select

    from app.db.models import Document

    row = (
        await db.execute(
            select(Document.owner_sub, Document.department_id, Document.doc_type).where(
                Document.id == document_id
            )
        )
    ).first()
    if row is None:
        return True  # the route answers 404 itself
    if row[2] is not None and getattr(row[2], "value", row[2]) in SHARED_DOCUMENT_TYPES:
        return True
    return await _row_visible(db, user, row[0], row[1])


async def invoice_visible_to(db: AsyncSession, user: UserInfo, invoice_id: uuid.UUID) -> bool:
    from sqlalchemy import select

    from app.db.models import Invoice

    row = (await db.execute(select(Invoice.document_id).where(Invoice.id == invoice_id))).first()
    if row is None or row[0] is None:
        return True
    return await document_visible_to(db, user, row[0])


async def case_visible_to(db: AsyncSession, user: UserInfo, case_id: uuid.UUID) -> bool:
    from sqlalchemy import select

    from app.db.models import CaseMember, WorkCase

    row = (
        await db.execute(
            select(WorkCase.created_by, WorkCase.department_id).where(WorkCase.id == case_id)
        )
    ).first()
    if row is None:
        return True
    if await _row_visible(db, user, row[0], row[1]):
        return True
    member = await db.scalar(
        select(CaseMember.id).where(CaseMember.case_id == case_id, CaseMember.user_sub == user.sub)
    )
    return member is not None


async def _row_visible(
    db: AsyncSession, user: UserInfo, owner: str | None, department: uuid.UUID | None
) -> bool:
    if _is_unrestricted(user):
        return True
    if owner is None and department is None:
        return True
    if owner is not None and owner == user.sub:
        return True
    if department is not None:
        return department in await _visible_department_ids(db, user)
    return False


def path_object_guard(param: str, visible, label: str):
    """Router dependency: 404 for a ``{param}`` the current user may not see."""
    from app.auth.jwt import get_current_user
    from app.db.session import get_db

    async def guard(
        request: Request,
        db: AsyncSession = Depends(get_db),
        user: UserInfo = Depends(get_current_user),
    ) -> None:
        raw = request.path_params.get(param)
        if raw is None:
            return
        try:
            object_id = uuid.UUID(str(raw))
        except ValueError:
            return  # the route's own validation answers 422
        if not await visible(db, user, object_id):
            raise HTTPException(status_code=404, detail=f"{label} not found")

    return guard
