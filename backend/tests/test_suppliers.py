"""Tests for Supplier API."""

import pytest
from httpx import AsyncClient

from app.db.models import (
    Document,
    DocumentStatus,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    Party,
    PartyRole,
    SupplierProfile,
)


@pytest.fixture
async def supplier(db_session):
    party = Party(
        name="ООО Тест",
        inn="7719826705",
        kpp="771901001",
        role=PartyRole.supplier,
        contact_email="test@supplier.ru",
        bank_account="40702810038000197568",
        bank_bik="044525225",
        address="г. Москва, ул. Тестовая, 1",
    )
    db_session.add(party)
    await db_session.flush()

    profile = SupplierProfile(
        party_id=party.id,
        total_invoices=5,
        total_amount=50000.0,
    )
    db_session.add(profile)
    await db_session.flush()

    # Create 2 invoices with lines for price history
    doc1 = Document(
        file_name="inv1.pdf",
        file_hash="h1",
        file_size=100,
        mime_type="application/pdf",
        storage_path="t/1.pdf",
        status=DocumentStatus.approved,
    )
    doc2 = Document(
        file_name="inv2.pdf",
        file_hash="h2",
        file_size=200,
        mime_type="application/pdf",
        storage_path="t/2.pdf",
        status=DocumentStatus.approved,
    )
    db_session.add_all([doc1, doc2])
    await db_session.flush()

    inv1 = Invoice(
        document_id=doc1.id,
        invoice_number="S-001",
        currency="RUB",
        total_amount=10000.0,
        status=InvoiceStatus.approved,
        supplier_id=party.id,
    )
    inv2 = Invoice(
        document_id=doc2.id,
        invoice_number="S-002",
        currency="RUB",
        total_amount=12000.0,
        status=InvoiceStatus.needs_review,
        supplier_id=party.id,
    )
    db_session.add_all([inv1, inv2])
    await db_session.flush()

    line1 = InvoiceLine(
        invoice_id=inv1.id,
        line_number=1,
        description="Болт М8",
        quantity=100,
        unit="шт",
        unit_price=50.0,
        amount=5000.0,
    )
    line2 = InvoiceLine(
        invoice_id=inv1.id,
        line_number=2,
        description="Гайка М8",
        quantity=100,
        unit="шт",
        unit_price=30.0,
        amount=3000.0,
    )
    line3 = InvoiceLine(
        invoice_id=inv2.id,
        line_number=1,
        description="Болт М8",
        quantity=100,
        unit="шт",
        unit_price=55.0,
        amount=5500.0,
    )
    db_session.add_all([line1, line2, line3])
    await db_session.commit()
    return party


@pytest.mark.asyncio
async def test_get_supplier(client: AsyncClient, supplier):
    resp = await client.get(f"/api/suppliers/{supplier.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "ООО Тест"
    assert data["inn"] == "7719826705"
    assert data["profile"] is not None
    assert data["profile"]["total_invoices"] == 5
    assert data["recent_invoices_count"] == 2


@pytest.mark.asyncio
async def test_search_suppliers(client: AsyncClient, supplier):
    resp = await client.post("/api/suppliers/search", json={"query": "Тест"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert any(r["name"] == "ООО Тест" for r in data["results"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "ООО «ИНАТЕК-М»",
        "ООО ИНАТЕК-М",
        "Общество с ограниченной ответственностью ИНАТЕК-М",
        "инатек-м",
    ],
)
async def test_search_finds_a_name_spelled_with_other_quotes_or_form(
    client: AsyncClient, db_session, query
):
    """Live 2026-10-10: the planner searched «ООО «ИНАТЕК-М»» for a supplier
    stored as 'ООО "ИНАТЕК-М"' with 17 invoices and got nothing."""
    db_session.add(Party(name='ООО "ИНАТЕК-М"', inn="7700000001", role=PartyRole.supplier))
    await db_session.commit()
    resp = await client.post("/api/suppliers/search", json={"query": query})
    assert [r["name"] for r in resp.json()["results"]] == ['ООО "ИНАТЕК-М"']


def test_company_core_name_strips_the_longest_legal_form():
    from app.domain.party_names import company_core_name

    assert company_core_name("Закрытое акционерное общество «Ромашка»") == "ромашка"
    assert company_core_name('ООО "ИНАТЕК-М"') == "инатек-м"


@pytest.mark.asyncio
async def test_search_by_inn(client: AsyncClient, supplier):
    resp = await client.post("/api/suppliers/search", json={"query": "7719826705"})
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


@pytest.mark.asyncio
async def test_price_history(client: AsyncClient, supplier):
    resp = await client.get(f"/api/suppliers/{supplier.id}/price-history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["supplier_name"] == "ООО Тест"
    assert data["total_items"] >= 1

    bolt = next((i for i in data["items"] if "Болт" in i["description"]), None)
    assert bolt is not None
    assert len(bolt["points"]) == 2
    assert bolt["points"][-1]["price"] == 55.0
    assert bolt["trend"] == "up"


@pytest.mark.asyncio
async def test_check_requisites(client: AsyncClient, supplier):
    resp = await client.post(f"/api/suppliers/{supplier.id}/check-requisites")
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_valid"] is True
    # All fields filled
    ok_fields = [c["field"] for c in data["checks"] if c["status"] == "ok"]
    assert "inn" in ok_fields
    assert "bank_account" in ok_fields


@pytest.mark.asyncio
async def test_trust_score(client: AsyncClient, supplier):
    resp = await client.get(f"/api/suppliers/{supplier.id}/trust-score")
    assert resp.status_code == 200
    data = resp.json()
    assert 0 <= data["trust_score"] <= 1.0
    assert len(data["breakdown"]) == 4
    assert data["recommendation"] is not None


@pytest.mark.asyncio
async def test_alerts(client: AsyncClient, supplier):
    resp = await client.get(f"/api/suppliers/{supplier.id}/alerts")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["alerts"], list)
    # May have price_increase or missing_docs alerts depending on fixture state
    assert data["total"] >= 0


@pytest.mark.asyncio
async def test_update_supplier(client: AsyncClient, supplier):
    resp = await client.patch(
        f"/api/suppliers/{supplier.id}",
        json={
            "contact_phone": "+7 999 123 4567",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["contact_phone"] == "+7 999 123 4567"


@pytest.mark.asyncio
async def test_list_suppliers(client: AsyncClient, supplier):
    resp = await client.get("/api/suppliers")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


@pytest.mark.asyncio
async def test_list_sorts_before_paging(client: AsyncClient, db_session):
    """sort_by=total_invoices&limit=1 used to return the alphabetically first."""
    first = Party(name="ААА Первый по алфавиту", inn="7700000101", role=PartyRole.supplier)
    leader = Party(name="ЯЯЯ Лидер по счетам", inn="7700000102", role=PartyRole.supplier)
    db_session.add_all([first, leader])
    await db_session.flush()
    db_session.add_all(
        [
            SupplierProfile(party_id=first.id, total_invoices=1, total_amount=1.0),
            SupplierProfile(party_id=leader.id, total_invoices=10_000, total_amount=1.0),
        ]
    )
    await db_session.flush()

    resp = await client.get("/api/suppliers", params={"sort_by": "total_invoices", "limit": 1})
    assert resp.status_code == 200
    assert resp.json()["items"][0]["name"] == "ЯЯЯ Лидер по счетам"

    bad = await client.get("/api/suppliers", params={"sort_by": "inn"})
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_list_counts_suppliers_without_our_buyer_entities(client: AsyncClient, db_session):
    """Without a role the total also counted buyers: 39 suppliers instead of 35."""
    db_session.add_all(
        [
            Party(name="Поставщик для подсчёта", inn="7700000201", role=PartyRole.supplier),
            Party(name="Наше юрлицо", inn="7700000202", role=PartyRole.buyer),
        ]
    )
    await db_session.flush()

    default = (await client.get("/api/suppliers", params={"limit": 200})).json()
    everyone = (await client.get("/api/suppliers", params={"role": "all", "limit": 200})).json()

    assert {item["role"] for item in default["items"]} == {"supplier"}
    assert default["total"] == everyone["total"] - sum(
        1 for item in everyone["items"] if item["role"] != "supplier"
    )
    assert "Наше юрлицо" in {item["name"] for item in everyone["items"]}
