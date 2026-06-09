"""Visibility on invoice listing (inherited from document) and pending approvals."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from app.auth.jwt import get_current_user
from app.auth.models import UserInfo, UserRole
from app.db.models import (
    Approval,
    ApprovalActionType,
    ApprovalStatus,
    Department,
    Document,
    Invoice,
    User,
)
from app.main import app


def _doc(name: str, **kw) -> Document:
    return Document(
        file_name=name, file_hash=name, file_size=1, mime_type="application/pdf",
        storage_path=f"/{name}", **kw,
    )


def _info(sub: str, role: UserRole) -> UserInfo:
    return UserInfo(sub=sub, email=f"{sub}@x", name=sub, preferred_username=sub, roles=[role])


def _as_user(user: UserInfo):
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_current_user, None)


# ── Invoices ────────────────────────────────────────────────────────────────

@pytest.fixture
async def invoices_setup(db_session):
    dept_a = Department(name="A", code="a")
    dept_b = Department(name="B", code="b")
    db_session.add_all([dept_a, dept_b])
    await db_session.flush()

    d_leg = _doc("legacy")
    d_a = _doc("ownA", owner_sub="u:alice", department_id=dept_a.id)
    d_b = _doc("ownB", owner_sub="u:bob", department_id=dept_b.id)
    db_session.add_all([d_leg, d_a, d_b])
    await db_session.flush()

    db_session.add_all([
        Invoice(document_id=d_leg.id, invoice_number="LEG"),
        Invoice(document_id=d_a.id, invoice_number="A1"),
        Invoice(document_id=d_b.id, invoice_number="B1"),
    ])
    db_session.add(User(sub="u:alice", email="a@x", name="Alice", role="buyer", department_id=dept_a.id))
    await db_session.commit()


async def test_invoice_visibility_scopes_by_document(client: AsyncClient, invoices_setup):
    _as_user(_info("u:alice", UserRole.buyer))
    r = await client.get("/api/invoices")
    assert r.status_code == 200
    nums = {i["invoice_number"] for i in r.json()["items"]}
    assert "LEG" in nums   # legacy/unowned visible to all
    assert "A1" in nums    # alice's department
    assert "B1" not in nums  # other department hidden


async def test_invoice_admin_sees_all(client: AsyncClient, invoices_setup):
    _as_user(_info("u:admin", UserRole.admin))
    r = await client.get("/api/invoices")
    nums = {i["invoice_number"] for i in r.json()["items"]}
    assert nums == {"LEG", "A1", "B1"}


# ── Approvals ─────────────────────────────────────────────────────────────────

def _approval(**kw) -> Approval:
    # Use entity_type that is not subject to the orphan-exists filter
    # (filter only applies to 'document' and 'invoice' entity types)
    return Approval(
        action_type=ApprovalActionType("invoice.approve"),
        entity_type="email_draft",
        entity_id=uuid.uuid4(),
        status=ApprovalStatus.pending,
        **kw,
    )


@pytest.fixture
async def approvals_setup(db_session):
    db_session.add_all([
        _approval(assigned_to="u:alice"),                       # mine
        _approval(assigned_to="u:bob"),                         # someone else's
        _approval(assigned_to=None, chain_root_id=None),        # unassigned pickup queue
        _approval(assigned_to="u:carol", requested_by="u:alice"),  # I requested it
    ])
    await db_session.commit()


async def test_pending_approvals_scoped_for_non_manager(client: AsyncClient, approvals_setup):
    _as_user(_info("u:alice", UserRole.buyer))
    r = await client.get("/api/approvals/pending")
    assert r.status_code == 200
    assigned = [a["assigned_to"] for a in r.json()["items"]]
    assert "u:alice" in assigned       # assigned to me
    assert None in assigned            # unassigned visible
    assert "u:carol" in assigned       # requested by me
    assert "u:bob" not in assigned     # other's — hidden


async def test_pending_approvals_manager_sees_all(client: AsyncClient, approvals_setup):
    _as_user(_info("u:mgr", UserRole.manager))
    r = await client.get("/api/approvals/pending")
    assigned = {a["assigned_to"] for a in r.json()["items"]}
    assert {"u:alice", "u:bob", "u:carol", None} <= assigned
