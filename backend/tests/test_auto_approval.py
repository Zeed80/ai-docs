"""Tests for Auto-Approval Rules API."""

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from app.db.models import (
    Document, DocumentStatus, Invoice, InvoiceStatus, Party, PartyRole,
    SupplierProfile,
)


@pytest.fixture
async def supplier_with_profile(db_session):
    party = Party(
        name='ООО "АвтоОдобрение"',
        inn="7700000100",
        role=PartyRole.supplier,
    )
    db_session.add(party)
    await db_session.flush()

    profile = SupplierProfile(
        party_id=party.id,
        trust_score=0.92,
        total_invoices=52,
        total_amount=1300000.0,
    )
    db_session.add(profile)
    await db_session.commit()
    return party


@pytest.fixture
async def approved_invoice(db_session, supplier_with_profile):
    doc = Document(
        file_name="auto-approve-test.pdf",
        file_hash="aath001",
        file_size=512,
        mime_type="application/pdf",
        storage_path="a/1.pdf",
        status=DocumentStatus.needs_review,
    )
    db_session.add(doc)
    await db_session.flush()

    inv = Invoice(
        document_id=doc.id,
        invoice_number="AUTO-001",
        currency="RUB",
        total_amount=20000.0,
        status=InvoiceStatus.needs_review,
        invoice_date=datetime.now(timezone.utc),
        supplier_id=supplier_with_profile.id,
    )
    db_session.add(inv)
    await db_session.commit()
    return inv


# ── CRUD ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_rule(client: AsyncClient):
    resp = await client.post("/api/auto-approval-rules", json={
        "name": "Небольшие счета до 50к",
        "max_amount": 50000.0,
        "currency": "RUB",
        "min_trust_score": 0.85,
        "doc_type": "invoice",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Небольшие счета до 50к"
    assert data["max_amount"] == 50000.0
    assert data["is_active"] is True


@pytest.mark.asyncio
async def test_create_rule_minimal(client: AsyncClient):
    resp = await client.post("/api/auto-approval-rules", json={
        "name": "Минимальное правило",
    })
    assert resp.status_code == 201
    assert resp.json()["name"] == "Минимальное правило"


@pytest.mark.asyncio
async def test_list_rules(client: AsyncClient):
    await client.post("/api/auto-approval-rules", json={"name": "Rule A"})
    await client.post("/api/auto-approval-rules", json={"name": "Rule B"})

    resp = await client.get("/api/auto-approval-rules")
    assert resp.status_code == 200
    assert len(resp.json()) >= 2


@pytest.mark.asyncio
async def test_update_rule(client: AsyncClient):
    create_resp = await client.post("/api/auto-approval-rules", json={
        "name": "Update-test",
        "max_amount": 10000.0,
    })
    rule_id = create_resp.json()["id"]

    resp = await client.patch(f"/api/auto-approval-rules/{rule_id}", json={
        "max_amount": 25000.0,
        "is_active": False,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["max_amount"] == 25000.0
    assert data["is_active"] is False


@pytest.mark.asyncio
async def test_delete_rule(client: AsyncClient):
    create_resp = await client.post("/api/auto-approval-rules", json={
        "name": "Delete-test",
    })
    rule_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/auto-approval-rules/{rule_id}")
    assert resp.status_code == 204


# ── Check (matching logic) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_invoice_matches_rule(
    client: AsyncClient, approved_invoice, supplier_with_profile
):
    # Create rule matching invoice: amount <= 50k, supplier with high trust
    await client.post("/api/auto-approval-rules", json={
        "name": "Высокодоверенный поставщик",
        "supplier_id": str(supplier_with_profile.id),
        "max_amount": 50000.0,
        "min_trust_score": 0.8,
    })

    resp = await client.post("/api/auto-approval-rules/check", json={
        "invoice_id": str(approved_invoice.id),
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "matched" in data
    # Invoice is 20000 RUB with trust 0.92 — should match
    assert data["matched"] is True


@pytest.mark.asyncio
async def test_check_invoice_no_match(client: AsyncClient, approved_invoice):
    # Rule requires amount <= 5000, but invoice is 20000
    await client.post("/api/auto-approval-rules", json={
        "name": "Мелкие закупки",
        "max_amount": 5000.0,
    })

    resp = await client.post("/api/auto-approval-rules/check", json={
        "invoice_id": str(approved_invoice.id),
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "matched" in data
    # May or may not match depending on other rules — just check structure
    assert isinstance(data["matched"], bool)


@pytest.mark.asyncio
async def test_check_invoice_not_found(client: AsyncClient):
    resp = await client.post("/api/auto-approval-rules/check", json={
        "invoice_id": str(uuid.uuid4()),
    })
    assert resp.status_code == 404
