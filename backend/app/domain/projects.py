"""Project / construction-object resolution.

Free-text project/object names (from upload tagging, email subjects or
extraction) are resolved to canonical :class:`Project` / :class:`SiteObject`
rows via case-insensitive get-or-create, so documents share stable identity for
filtering and grouping. Mirrors the get-or-create pattern used for parties.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Project, SiteObject


def normalize_name(name: str) -> str:
    """Case/space-insensitive key for dedup."""
    return " ".join((name or "").strip().lower().split())


async def get_or_create_project(
    db: AsyncSession, name: str | None, *, code: str | None = None
) -> uuid.UUID | None:
    if not name or not name.strip():
        return None
    norm = normalize_name(name)
    existing = (
        await db.execute(select(Project).where(Project.normalized_name == norm))
    ).scalar_one_or_none()
    if existing:
        return existing.id
    project = Project(name=name.strip(), normalized_name=norm, code=code)
    db.add(project)
    await db.flush()
    return project.id


async def get_or_create_object(
    db: AsyncSession,
    name: str | None,
    *,
    project_id: uuid.UUID | None = None,
    code: str | None = None,
) -> uuid.UUID | None:
    if not name or not name.strip():
        return None
    norm = normalize_name(name)
    existing = (
        await db.execute(select(SiteObject).where(SiteObject.normalized_name == norm))
    ).scalar_one_or_none()
    if existing:
        # Backfill the project link if it was unknown at first sighting.
        if project_id and existing.project_id is None:
            existing.project_id = project_id
            await db.flush()
        return existing.id
    obj = SiteObject(
        name=name.strip(), normalized_name=norm, code=code, project_id=project_id
    )
    db.add(obj)
    await db.flush()
    return obj.id
