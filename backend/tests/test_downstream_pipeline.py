"""Downstream pipeline tests: post-approval DB filling + knowledge graph.

Covers what happens AFTER an invoice is approved — the
``process_approved_document`` chain that turns a verified extraction into:

* **SQL records** — Party (supplier/buyer, deduplicated by ИНН), Invoice,
  InvoiceLine, a pending WarehouseReceipt + lines, and a SupplierProfile.
* **Knowledge graph** — document/entity nodes, mentions and edges
  (``build_document_memory_sync``).
* **Embedding** — verifies the vector-indexing task is queued (the real Qdrant
  upsert is exercised live; see ``scripts``/docker-exec check in the PR notes).

Self-contained: uses an in-memory SQLite DB shared via StaticPool and stubs the
external `.delay` calls, so it runs anywhere with no Postgres/Qdrant/Ollama.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

# Import the REAL extraction module at collection time. test_extraction_api.py
# installs a MagicMock in its place when it is imported first and the real module
# is not yet loaded; importing it here (this file sorts before that one) keeps the
# genuine functions available to both.
import app.tasks.extraction  # noqa: F401,E402
from app.db import models  # noqa: F401 — register mappers
from app.db.base import Base
from app.db.models import (
    Document,
    DocumentExtraction,
    DocumentStatus,
    EntityMention,
    Invoice,
    InvoiceLine,
    KnowledgeEdge,
    KnowledgeNode,
    Party,
    SupplierProfile,
    WarehouseReceipt,
    WarehouseReceiptLine,
)

# ── Realistic invoice extraction (valid INN checksums, consistent arithmetic) ─

INVOICE = {
    "invoice_number": "УТ-2834",
    "invoice_date": "2024-08-09",
    "currency": "RUB",
    "supplier": {
        "name": 'ООО "НВС Компани"',
        "inn": "7707083893",  # valid 10-digit checksum
        "kpp": "770701001",
        "bank_bik": "044525225",
        "bank_account": "40702810400000000225",
        "corr_account": "30101810400000000225",
    },
    "buyer": {
        "name": 'АО "ПТС"',
        "inn": "5036167355",  # valid 10-digit checksum
        "kpp": "503601001",
    },
    "lines": [
        {"line_number": 1, "sku": "A-1", "description": "Фреза концевая Ø10",
         "quantity": 10, "unit": "шт", "unit_price": 1500.0, "amount": 15000.0,
         "tax_rate": 0.2, "tax_amount": 3000.0},
        {"line_number": 2, "sku": "B-2", "description": "Сверло спиральное Ø5",
         "quantity": 5, "unit": "шт", "unit_price": 800.0, "amount": 4000.0,
         "tax_rate": 0.2, "tax_amount": 800.0},
    ],
    "subtotal": 19000.0,
    "tax_amount": 3800.0,
    "total_amount": 22800.0,
}

INVOICE_TEXT = (
    'Счёт № УТ-2834 от 09.08.2024\n'
    'Поставщик: ООО "НВС Компани", ИНН 7707083893, КПП 770701001\n'
    'Покупатель: АО "ПТС", ИНН 5036167355\n'
    'Фреза концевая Ø10 — 10 шт — 1500,00\n'
    'Сверло спиральное Ø5 — 5 шт — 800,00\n'
    'Итого без НДС: 19000,00  НДС 20%: 3800,00  Всего к оплате: 22800,00\n'
)


def _make_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def _seed_document(engine, structured: dict, *, file_name="invoice.pdf", file_hash=None) -> uuid.UUID:
    with Session(engine) as s:
        doc = Document(
            file_name=file_name,
            file_hash=file_hash or uuid.uuid4().hex,
            file_size=1024,
            mime_type="application/pdf",
            storage_path=f"documents/{file_name}",
            status=DocumentStatus.approved,
        )
        s.add(doc)
        s.flush()
        s.add(DocumentExtraction(
            document_id=doc.id,
            model_name="qwen3.5:9b/ollama",
            raw_output=structured,
            structured_data=structured,
            overall_confidence=0.99,
        ))
        s.commit()
        return doc.id


def _real_extraction_module():
    """Return the genuine app.tasks.extraction, even if another test installed a
    MagicMock in its place (test_extraction_api does this at import time)."""
    import importlib
    import sys

    mod = sys.modules.get("app.tasks.extraction")
    if mod is None or not hasattr(mod, "_get_sync_session"):
        sys.modules.pop("app.tasks.extraction", None)
        mod = importlib.import_module("app.tasks.extraction")
    return mod


@pytest.fixture
def pipeline_env(monkeypatch):
    """Wire process_approved_document to an in-memory DB; stub external tasks."""
    engine = _make_engine()
    ex = _real_extraction_module()
    from app.tasks.embedding import embed_document

    embed_calls: list[str] = []
    anomaly_calls: list[str] = []

    monkeypatch.setattr(ex, "_get_sync_session", lambda: Session(engine))
    monkeypatch.setattr(ex, "_get_document_text", lambda doc: INVOICE_TEXT)
    monkeypatch.setattr(embed_document, "delay", lambda doc_id, *a, **k: embed_calls.append(doc_id))
    monkeypatch.setattr(ex.check_invoice_anomalies, "delay", lambda inv_id, *a, **k: anomaly_calls.append(inv_id))

    return engine, embed_calls, anomaly_calls


def _run(doc_id: uuid.UUID) -> dict:
    from app.tasks.extraction import process_approved_document

    return process_approved_document.apply(args=[str(doc_id)]).get()


# ── Tests ────────────────────────────────────────────────────────────────────

def test_post_approval_fills_sql_records(pipeline_env):
    engine, embed_calls, anomaly_calls = pipeline_env
    doc_id = _seed_document(engine, INVOICE)

    result = _run(doc_id)
    assert result.get("status") != "error", result

    with Session(engine) as s:
        # Parties: supplier + buyer, with the extracted INNs
        parties = {p.role.value: p for p in s.execute(select(Party)).scalars().all()}
        assert "supplier" in parties and "buyer" in parties
        assert parties["supplier"].inn == "7707083893"
        assert parties["buyer"].inn == "5036167355"

        # Invoice with correct money fields, linked to both parties
        inv = s.execute(select(Invoice)).scalar_one()
        assert inv.invoice_number == "УТ-2834"
        assert inv.subtotal == 19000.0
        assert inv.tax_amount == 3800.0
        assert inv.total_amount == 22800.0
        assert inv.supplier_id == parties["supplier"].id
        assert inv.buyer_id == parties["buyer"].id

        # Line items copied verbatim
        lines = s.execute(
            select(InvoiceLine).order_by(InvoiceLine.line_number)
        ).scalars().all()
        assert len(lines) == 2
        assert lines[0].description == "Фреза концевая Ø10"
        assert lines[0].quantity == 10 and lines[0].unit_price == 1500.0
        assert lines[1].amount == 4000.0

        # Pending warehouse receipt auto-created with matching lines
        receipts = s.execute(select(WarehouseReceipt)).scalars().all()
        assert len(receipts) == 1
        assert receipts[0].status == "pending"
        wlines = s.execute(select(WarehouseReceiptLine)).scalars().all()
        assert len(wlines) == 2

        # Supplier profile stats updated
        profile = s.execute(select(SupplierProfile)).scalar_one()
        assert profile.total_invoices >= 1
        assert profile.total_amount == 22800.0

    # Embedding + anomaly detection were queued
    assert embed_calls == [str(doc_id)]
    assert len(anomaly_calls) == 1


def test_post_approval_builds_knowledge_graph(pipeline_env):
    engine, _, _ = pipeline_env
    doc_id = _seed_document(engine, INVOICE, file_name="invoice-graph.pdf")

    _run(doc_id)

    with Session(engine) as s:
        nodes = s.execute(select(KnowledgeNode)).scalars().all()
        titles = {n.title for n in nodes}
        # The document itself becomes a node…
        assert "invoice-graph.pdf" in titles
        # …and the build produced mentions + edges from the invoice text.
        mentions = s.execute(select(func.count()).select_from(EntityMention)).scalar()
        edges = s.execute(select(func.count()).select_from(KnowledgeEdge)).scalar()
        assert mentions >= 1
        assert edges >= 1


def test_party_deduplicated_by_inn_across_invoices(pipeline_env):
    engine, _, _ = pipeline_env
    # Two different invoices from the SAME supplier (same ИНН)
    inv2 = dict(INVOICE)
    inv2 = {**INVOICE, "invoice_number": "УТ-2900"}
    doc1 = _seed_document(engine, INVOICE, file_name="a.pdf")
    doc2 = _seed_document(engine, inv2, file_name="b.pdf")

    _run(doc1)
    _run(doc2)

    with Session(engine) as s:
        suppliers = s.execute(
            select(Party).where(Party.inn == "7707083893")
        ).scalars().all()
        assert len(suppliers) == 1, "supplier must be deduplicated by ИНН"
        # Both invoices persisted, sharing the one supplier party
        invoices = s.execute(select(Invoice)).scalars().all()
        assert len(invoices) == 2
        assert {i.supplier_id for i in invoices} == {suppliers[0].id}
        # Supplier profile accumulates both invoices
        profile = s.execute(select(SupplierProfile)).scalar_one()
        assert profile.total_invoices == 2
        assert profile.total_amount == 45600.0  # 22800 × 2
