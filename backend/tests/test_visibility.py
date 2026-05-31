"""Tests for app/domain/access.py — row-level visibility filter."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.auth.models import UserInfo, UserRole
from app.db.models import Department, Document, User
from app.domain.access import apply_visibility


def _doc(name: str, **kw) -> Document:
    return Document(
        file_name=name, file_hash=name, file_size=1, mime_type="application/pdf",
        storage_path=f"/{name}", **kw,
    )


@pytest.fixture
async def docs(db_session):
    dept_a = Department(name="A", code="a")
    dept_b = Department(name="B", code="b")
    db_session.add_all([dept_a, dept_b])
    await db_session.flush()

    db_session.add_all([
        _doc("legacy"),                                   # unowned → visible to all
        _doc("ownA", owner_sub="u:alice", department_id=dept_a.id),
        _doc("ownB", owner_sub="u:bob", department_id=dept_b.id),
        _doc("deptA_other", owner_sub="u:carol", department_id=dept_a.id),
    ])
    # Alice belongs to department A.
    db_session.add(User(sub="u:alice", email="a@x", name="Alice", role="buyer", department_id=dept_a.id))
    await db_session.commit()
    return {"a": dept_a, "b": dept_b}


def _info(sub: str, role: UserRole) -> UserInfo:
    return UserInfo(sub=sub, email=f"{sub}@x", name=sub, preferred_username=sub, roles=[role])


async def _names(db_session, user) -> set[str]:
    q = await apply_visibility(
        db_session, user, select(Document),
        owner_col=Document.owner_sub, department_col=Document.department_id,
    )
    return {d.file_name for d in (await db_session.execute(q)).scalars().all()}


async def test_admin_sees_everything(db_session, docs):
    names = await _names(db_session, _info("u:admin", UserRole.admin))
    assert names == {"legacy", "ownA", "ownB", "deptA_other"}


async def test_manager_sees_everything(db_session, docs):
    names = await _names(db_session, _info("u:mgr", UserRole.manager))
    assert names == {"legacy", "ownA", "ownB", "deptA_other"}


async def test_user_sees_own_dept_and_legacy_not_other_dept(db_session, docs):
    # Alice (dept A, buyer): legacy + own + her department's docs, NOT dept B.
    names = await _names(db_session, _info("u:alice", UserRole.buyer))
    assert "legacy" in names
    assert "ownA" in names
    assert "deptA_other" in names  # same department
    assert "ownB" not in names     # other department


async def test_user_without_department_sees_only_own_and_legacy(db_session, docs):
    # Bob has no User row / no department here → only legacy + his own docs.
    names = await _names(db_session, _info("u:bob", UserRole.buyer))
    assert names == {"legacy", "ownB"}
