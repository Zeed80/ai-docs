"""Tests for app/domain/org.py — department tree and manager→reports hierarchy."""

from __future__ import annotations

import pytest

from app.db.models import Department, User
from app.domain.org import (
    get_department_descendants,
    get_subordinate_subs,
    is_manager_of,
    get_department_manager_sub,
)


@pytest.fixture
async def org_tree(db_session):
    """root ─ proc ─ proc-sub ; standalone hr.

    Users: boss (manager of proc), buyer1 (reports to boss), buyer2 (reports to buyer1).
    """
    root = Department(name="HQ", code="hq")
    proc = Department(name="Procurement", code="proc")
    proc_sub = Department(name="Procurement Sub", code="proc-sub")
    db_session.add_all([root, proc, proc_sub])
    await db_session.flush()
    proc.parent_id = root.id
    proc_sub.parent_id = proc.id

    boss = User(sub="u:boss", email="b@x", name="Boss", role="manager", department_id=proc.id)
    buyer1 = User(sub="u:b1", email="b1@x", name="B1", role="buyer", manager_sub="u:boss", department_id=proc.id)
    buyer2 = User(sub="u:b2", email="b2@x", name="B2", role="buyer", manager_sub="u:b1")
    db_session.add_all([boss, buyer1, buyer2])
    await db_session.commit()
    return {"root": root, "proc": proc, "proc_sub": proc_sub}


async def test_department_descendants_includes_self_and_children(db_session, org_tree):
    ids = await get_department_descendants(db_session, org_tree["root"].id)
    assert org_tree["root"].id in ids
    assert org_tree["proc"].id in ids
    assert org_tree["proc_sub"].id in ids


async def test_department_descendants_leaf_only_self(db_session, org_tree):
    ids = await get_department_descendants(db_session, org_tree["proc_sub"].id)
    assert ids == {org_tree["proc_sub"].id}


async def test_subordinates_recursive(db_session, org_tree):
    subs = await get_subordinate_subs(db_session, "u:boss", recursive=True)
    assert subs == {"u:b1", "u:b2"}


async def test_subordinates_direct_only(db_session, org_tree):
    subs = await get_subordinate_subs(db_session, "u:boss", recursive=False)
    assert subs == {"u:b1"}


async def test_is_manager_of_transitive(db_session, org_tree):
    assert await is_manager_of(db_session, "u:boss", "u:b2") is True
    assert await is_manager_of(db_session, "u:b1", "u:boss") is False
    assert await is_manager_of(db_session, "u:boss", "u:boss") is False


async def test_department_manager_lookup(db_session, org_tree):
    assert await get_department_manager_sub(db_session, org_tree["proc"].id) == "u:boss"
