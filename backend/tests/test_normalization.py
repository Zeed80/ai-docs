"""Normalization API tests — norm.list_rules, norm.create_rule, norm.activate_rule,
norm.apply_rules, norm.suggest_rule"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Document,
    DocumentExtraction,
    DocumentStatus,
    ExtractionField,
    NormalizationRule,
    NormRuleStatus,
)


async def _create_rule(db: AsyncSession, **kwargs) -> uuid.UUID:
    defaults = {
        "field_name": "supplier_name",
        "pattern": "OOO AKME",
        "replacement": 'ООО "АКМЕ"',
        "is_regex": False,
        "status": NormRuleStatus.proposed,
        "suggested_by": "system",
        "source_corrections": 3,
    }
    defaults.update(kwargs)
    rule = NormalizationRule(**defaults)
    db.add(rule)
    await db.commit()
    return rule.id


async def _create_doc_with_extraction(
    db: AsyncSession,
    field_name: str = "supplier_name",
    field_value: str = "OOO AKME",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create Document + Extraction + Field, return (doc_id, extraction_id, field_id)."""
    doc = Document(
        file_name="norm_test.pdf",
        file_hash=f"normhash{uuid.uuid4().hex[:8]}",
        file_size=512,
        mime_type="application/pdf",
        storage_path="documents/no/rm/normtest",
        status=DocumentStatus.needs_review,
    )
    db.add(doc)
    await db.flush()

    extraction = DocumentExtraction(
        document_id=doc.id,
        model_name="gemma4:e4b",
        overall_confidence=0.8,
    )
    db.add(extraction)
    await db.flush()

    field = ExtractionField(
        extraction_id=extraction.id,
        field_name=field_name,
        field_value=field_value,
        confidence=0.7,
    )
    db.add(field)
    await db.commit()
    return doc.id, extraction.id, field.id


@pytest.mark.asyncio
async def test_create_rule(client: AsyncClient):
    """norm.create_rule — create a new rule."""
    resp = await client.post(
        "/api/normalization/rules",
        json={
            "field_name": "supplier_name",
            "pattern": "OOO TEST",
            "replacement": 'ООО "Тест"',
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["field_name"] == "supplier_name"
    assert data["status"] == "proposed"
    assert data["pattern"] == "OOO TEST"


@pytest.mark.asyncio
async def test_list_rules(client: AsyncClient, db_session: AsyncSession):
    """norm.list_rules — returns rules."""
    await _create_rule(db_session)

    resp = await client.get("/api/normalization/rules")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert data["total"] >= 1


@pytest.mark.asyncio
async def test_list_rules_filter_status(client: AsyncClient, db_session: AsyncSession):
    """norm.list_rules — filter by status."""
    await _create_rule(db_session, status=NormRuleStatus.active)

    resp = await client.get("/api/normalization/rules?status=active")
    assert resp.status_code == 200
    data = resp.json()
    assert all(r["status"] == "active" for r in data["items"])


@pytest.mark.asyncio
async def test_activate_rule(client: AsyncClient, db_session: AsyncSession):
    """norm.activate_rule — activate proposed rule."""
    rule_id = await _create_rule(db_session)

    resp = await client.post(
        f"/api/normalization/rules/{rule_id}/activate",
        json={"activated_by": "test_user"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "active"
    assert data["activated_by"] == "test_user"
    assert data["activated_at"] is not None


@pytest.mark.asyncio
async def test_activate_already_active(client: AsyncClient, db_session: AsyncSession):
    """norm.activate_rule — cannot activate already active."""
    rule_id = await _create_rule(db_session, status=NormRuleStatus.active)

    resp = await client.post(
        f"/api/normalization/rules/{rule_id}/activate",
        json={"activated_by": "user"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_disable_rule(client: AsyncClient, db_session: AsyncSession):
    """norm.disable_rule — disable active rule."""
    rule_id = await _create_rule(db_session, status=NormRuleStatus.active)

    resp = await client.post(f"/api/normalization/rules/{rule_id}/disable")
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"


@pytest.mark.asyncio
async def test_apply_rules(client: AsyncClient, db_session: AsyncSession):
    """norm.apply_rules — apply active rules to document extraction."""
    doc_id, _, _ = await _create_doc_with_extraction(
        db_session, field_name="supplier_name", field_value="OOO AKME"
    )
    await _create_rule(db_session, status=NormRuleStatus.active)

    resp = await client.post(
        "/api/normalization/apply",
        json={"document_id": str(doc_id)},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["rules_applied"] >= 1
    assert len(data["fields_modified"]) >= 1
    assert data["fields_modified"][0]["new_value"] == 'ООО "АКМЕ"'


@pytest.mark.asyncio
async def test_apply_rules_no_match(client: AsyncClient, db_session: AsyncSession):
    """norm.apply_rules — no modifications when patterns don't match."""
    doc_id, _, _ = await _create_doc_with_extraction(
        db_session, field_name="supplier_name", field_value="No Match Here"
    )
    await _create_rule(db_session, status=NormRuleStatus.active)

    resp = await client.post(
        "/api/normalization/apply",
        json={"document_id": str(doc_id)},
    )
    assert resp.status_code == 200
    assert resp.json()["rules_applied"] == 0


@pytest.mark.asyncio
async def test_suggest_rules(client: AsyncClient, db_session: AsyncSession):
    """norm.suggest_rule — detect repeated corrections and propose rules."""
    # Create 3 corrected fields with same pattern
    for _ in range(3):
        doc_id, ext_id, _ = await _create_doc_with_extraction(
            db_session, field_name="unit", field_value="sht"
        )
        # Mark field as corrected
        from sqlalchemy import select
        result = await db_session.execute(
            select(ExtractionField).where(ExtractionField.extraction_id == ext_id)
        )
        field = result.scalar_one()
        field.human_corrected = True
        field.corrected_value = "шт"
        await db_session.commit()

    resp = await client.post(
        "/api/normalization/suggest",
        json={"min_corrections": 3},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["suggested_rules"]) >= 1
    assert data["suggested_rules"][0]["pattern"] == "sht"
    assert data["suggested_rules"][0]["replacement"] == "шт"
    assert data["suggested_rules"][0]["status"] == "proposed"


@pytest.mark.asyncio
async def test_create_regex_rule_invalid(client: AsyncClient):
    """norm.create_rule — reject invalid regex."""
    resp = await client.post(
        "/api/normalization/rules",
        json={
            "field_name": "test",
            "pattern": "[invalid",
            "replacement": "x",
            "is_regex": True,
        },
    )
    assert resp.status_code == 400
    assert "regex" in resp.json()["detail"].lower()
