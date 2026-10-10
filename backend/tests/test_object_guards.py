"""E40 audit: /{id} routes check the row, not just the list.

Before, /documents/{id}, /invoices/{id} and /cases/{id} loaded the row by id
alone: Alice could read, download, edit or delete a document of Bob's
department as long as she had its id.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from app.auth.jwt import get_current_user
from app.auth.models import UserInfo, UserRole
from app.db.models import Department, Document, Invoice, User, WorkCase


def _user(sub: str, role: UserRole = UserRole.buyer) -> UserInfo:
    return UserInfo(sub=sub, email=f"{sub}@x", name=sub, preferred_username=sub, roles=[role])


@pytest.fixture
async def bobs(db_session):
    dept_b = Department(name="B", code="og-b")
    db_session.add(dept_b)
    await db_session.flush()
    db_session.add(User(sub="og:bob", email="b@x", name="B", role="buyer", department_id=dept_b.id))
    doc = Document(
        file_name="bob.pdf",
        file_hash=uuid.uuid4().hex,
        file_size=1,
        mime_type="application/pdf",
        storage_path="/bob.pdf",
        owner_sub="og:bob",
        department_id=dept_b.id,
    )
    db_session.add(doc)
    await db_session.flush()
    invoice = Invoice(document_id=doc.id, invoice_number="OG-1")
    case = WorkCase(title="Дело Боба", created_by="og:bob", department_id=dept_b.id)
    db_session.add_all([invoice, case])
    await db_session.commit()
    return {"doc": doc.id, "invoice": invoice.id, "case": case.id}


@pytest.fixture
def as_user(client: AsyncClient):
    from app.main import app

    def bind(user: UserInfo):
        app.dependency_overrides[get_current_user] = lambda: user

    yield bind
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_alice_gets_404_for_everything_of_bobs(client, bobs, as_user):
    as_user(_user("og:alice"))
    doc, invoice, case = bobs["doc"], bobs["invoice"], bobs["case"]
    for method, path in [
        ("GET", f"/api/documents/{doc}"),
        ("GET", f"/api/documents/{doc}/download"),
        ("GET", f"/api/documents/{doc}/invoice"),
        ("GET", f"/api/documents/{doc}/dependencies"),
        ("PATCH", f"/api/documents/{doc}"),
        ("DELETE", f"/api/documents/{doc}"),
        ("GET", f"/api/invoices/{invoice}"),
        ("PATCH", f"/api/invoices/{invoice}"),
        ("GET", f"/api/cases/{case}"),
    ]:
        resp = await client.request(method, path, json={} if method == "PATCH" else None)
        assert resp.status_code == 404, (method, path, resp.status_code, resp.text[:200])


@pytest.mark.asyncio
async def test_bob_and_the_manager_still_reach_them(client, bobs, as_user):
    for user in (_user("og:bob"), _user("og:boss", UserRole.manager)):
        as_user(user)
        assert (await client.get(f"/api/documents/{bobs['doc']}")).status_code == 200
        assert (await client.get(f"/api/invoices/{bobs['invoice']}")).status_code == 200
        assert (await client.get(f"/api/cases/{bobs['case']}")).status_code == 200


@pytest.mark.asyncio
async def test_a_supplier_catalog_is_company_reference_whoever_uploaded_it(
    client, bobs, as_user, db_session
):
    from app.db.models import DocumentType

    catalog = Document(
        file_name="catalog.pdf",
        file_hash=uuid.uuid4().hex,
        file_size=1,
        mime_type="application/pdf",
        storage_path="/catalog.pdf",
        owner_sub="og:bob",
        doc_type=DocumentType.supplier_catalog,
    )
    db_session.add(catalog)
    await db_session.commit()
    as_user(_user("og:alice"))
    assert (await client.get(f"/api/documents/{catalog.id}")).status_code == 200
    names = {d["file_name"] for d in (await client.get("/api/documents")).json()["items"]}
    assert "catalog.pdf" in names and "bob.pdf" not in names
