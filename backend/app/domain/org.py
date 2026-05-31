"""Organization hierarchy helpers — departments tree and manager→reports links.

Reused by the visibility layer (app/domain/access.py) and approval auto-routing
(app/api/approvals.py). All functions are read-only and tolerant of missing data
(unset department/manager) so they are safe to call for any user.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Department, User


async def get_user(db: AsyncSession, sub: str) -> User | None:
    result = await db.execute(select(User).where(User.sub == sub))
    return result.scalar_one_or_none()


async def get_department_descendants(
    db: AsyncSession, department_id: uuid.UUID
) -> set[uuid.UUID]:
    """Return department_id and all departments below it in the tree (inclusive).

    Walks the parent_id edges in memory — department trees are small. Cycle-safe.
    """
    rows = (await db.execute(select(Department.id, Department.parent_id))).all()
    children: dict[uuid.UUID, list[uuid.UUID]] = {}
    for dep_id, parent_id in rows:
        if parent_id is not None:
            children.setdefault(parent_id, []).append(dep_id)

    out: set[uuid.UUID] = set()
    stack = [department_id]
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        out.add(cur)
        stack.extend(children.get(cur, []))
    return out


async def get_subordinate_subs(
    db: AsyncSession, manager_sub: str, *, recursive: bool = True
) -> set[str]:
    """Return the `sub`s of everyone reporting to manager_sub.

    recursive=True follows the chain transitively (reports of reports). Cycle-safe.
    """
    rows = (await db.execute(select(User.sub, User.manager_sub))).all()
    reports: dict[str, list[str]] = {}
    for sub, mgr in rows:
        if mgr:
            reports.setdefault(mgr, []).append(sub)

    out: set[str] = set()
    stack = list(reports.get(manager_sub, []))
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        out.add(cur)
        if recursive:
            stack.extend(reports.get(cur, []))
    return out


async def is_manager_of(
    db: AsyncSession, manager_sub: str, report_sub: str, *, recursive: bool = True
) -> bool:
    """True if report_sub reports (directly or transitively) to manager_sub."""
    if manager_sub == report_sub:
        return False
    return report_sub in await get_subordinate_subs(db, manager_sub, recursive=recursive)


async def get_department_manager_sub(
    db: AsyncSession, department_id: uuid.UUID
) -> str | None:
    """Best-effort: the `sub` of a manager-role user in the given department.

    Used to auto-route approvals. Returns the first active manager/admin found.
    """
    result = await db.execute(
        select(User.sub)
        .where(
            User.department_id == department_id,
            User.is_active == True,  # noqa: E712
            User.role.in_(["manager", "admin"]),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()
