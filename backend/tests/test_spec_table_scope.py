"""E41 audit: a spec table returns only rows its viewer may see.

The agent's spec tables compile to SQL directly; before this, every source
but e-mail returned all departments' invoices, documents and anomalies.
"""

from __future__ import annotations

import uuid

import pytest

from app.auth.models import UserInfo, UserRole
from app.db.models import AnomalyCard, Department, Document, Invoice, User
from app.domain.table_spec import ColumnSpec, TableSpec, execute_spec


def _user(sub: str, role: UserRole = UserRole.buyer) -> UserInfo:
    return UserInfo(sub=sub, email=f"{sub}@x", name=sub, preferred_username=sub, roles=[role])


def _doc(name, **kw):
    return Document(
        file_name=name,
        file_hash=uuid.uuid4().hex,
        file_size=1,
        mime_type="application/pdf",
        storage_path=f"/{name}",
        **kw,
    )


@pytest.fixture
async def data(db_session):
    dept_a, dept_b = Department(name="A", code="st-a"), Department(name="B", code="st-b")
    db_session.add_all([dept_a, dept_b])
    await db_session.flush()
    db_session.add_all(
        [
            User(sub="st:alice", email="a@x", name="A", role="buyer", department_id=dept_a.id),
            User(sub="st:bob", email="b@x", name="B", role="buyer", department_id=dept_b.id),
        ]
    )
    doc_a = _doc("st-a.pdf", owner_sub="st:alice", department_id=dept_a.id)
    doc_b = _doc("st-b.pdf", owner_sub="st:bob", department_id=dept_b.id)
    db_session.add_all([doc_a, doc_b])
    await db_session.flush()
    inv_a = Invoice(document_id=doc_a.id, invoice_number="ST-A")
    inv_b = Invoice(document_id=doc_b.id, invoice_number="ST-B")
    db_session.add_all([inv_a, inv_b])
    await db_session.flush()
    db_session.add(
        AnomalyCard(
            entity_type="invoice",
            entity_id=inv_b.id,
            anomaly_type="duplicate",
            title="ST-B anomaly",
            severity="critical",
        )
    )
    await db_session.commit()


async def _values(db, source, field, viewer):
    spec = TableSpec(source=source, columns=[ColumnSpec(field=field)])
    result = await execute_spec(db, spec, viewer=viewer)
    return {row[field] for row in result.rows}


@pytest.mark.asyncio
async def test_invoices_and_documents_follow_the_viewer(db_session, data):
    alice = _user("st:alice")
    numbers = await _values(db_session, "invoices", "invoice_number", alice)
    assert "ST-A" in numbers and "ST-B" not in numbers
    names = await _values(db_session, "documents", "file_name", alice)
    assert "st-a.pdf" in names and "st-b.pdf" not in names
    manager = _user("st:boss", UserRole.manager)
    assert {"ST-A", "ST-B"} <= await _values(db_session, "invoices", "invoice_number", manager)


@pytest.mark.asyncio
async def test_no_viewer_sees_no_owned_rows(db_session, data):
    numbers = await _values(db_session, "invoices", "invoice_number", None)
    assert not ({"ST-A", "ST-B"} & numbers)


@pytest.mark.asyncio
async def test_an_anomaly_about_bobs_invoice_is_bobs(db_session, data):
    assert "ST-B anomaly" not in await _values(db_session, "anomalies", "title", _user("st:alice"))
    assert "ST-B anomaly" in await _values(db_session, "anomalies", "title", _user("st:bob"))
