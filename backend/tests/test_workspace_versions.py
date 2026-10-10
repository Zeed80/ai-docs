"""E42: a workspace block keeps every revision; edits do not land blind."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.domain import workspace as ws


@pytest.fixture(autouse=True)
def _clean():
    ws._FALLBACK.clear()
    ws._FALLBACK_VERSIONS.clear()
    yield
    ws._FALLBACK.clear()
    ws._FALLBACK_VERSIONS.clear()


def test_every_write_is_a_new_revision_and_old_ones_stay_readable():
    first = ws.upsert_workspace_block("b1", {"title": "v1", "rows": [1]})
    second = ws.upsert_workspace_block("b1", {"title": "v2", "rows": [1, 2]})
    assert (first["revision"], second["revision"]) == (1, 2)
    assert first["content_hash"] != second["content_hash"]
    assert ws.get_workspace_block("b1")["title"] == "v2"
    assert ws.get_workspace_block_version("b1", 1)["title"] == "v1"
    assert [v["revision"] for v in ws.list_workspace_block_versions("b1")] == [2, 1]


def test_a_writer_that_read_an_old_revision_gets_a_conflict():
    ws.upsert_workspace_block("b2", {"title": "base"})
    ws.upsert_workspace_block("b2", {"title": "tab A"}, expected_revision=1)
    with pytest.raises(HTTPException) as conflict:
        ws.upsert_workspace_block("b2", {"title": "tab B"}, expected_revision=1)
    assert conflict.value.status_code == 409
    assert conflict.value.detail["current_revision"] == 2
    assert ws.get_workspace_block("b2")["title"] == "tab A"


def test_same_content_has_the_same_hash_across_revisions():
    a = ws.upsert_workspace_block("b3", {"title": "same"})
    b = ws.upsert_workspace_block("b3", {"title": "same"})
    assert a["content_hash"] == b["content_hash"] and b["revision"] == 2


@pytest.mark.asyncio
async def test_an_approved_edit_does_not_land_on_a_row_changed_since(db_session):
    from sqlalchemy import update

    from app.db.models import Document, Invoice
    from app.domain.table_spec import apply_cell_writeback

    doc = Document(
        file_name="e42.pdf",
        file_hash="e42",
        file_size=1,
        mime_type="application/pdf",
        storage_path="/e42",
    )
    db_session.add(doc)
    await db_session.flush()
    invoice = Invoice(document_id=doc.id, invoice_number="E42", total_amount=100.0)
    db_session.add(invoice)
    await db_session.commit()
    base = invoice.updated_at.isoformat()

    # Someone changes the row after the edit was requested.
    await db_session.execute(
        update(Invoice)
        .where(Invoice.id == invoice.id)
        .values(total_amount=150.0, updated_at=datetime.now(UTC) + timedelta(seconds=5))
    )
    await db_session.commit()

    ok, message = await apply_cell_writeback(
        db_session, "invoices", invoice.id, "total_amount", "999", base_version=base
    )
    assert not ok and message.startswith("stale")
    await db_session.refresh(invoice)
    assert invoice.total_amount == 150.0


def test_the_postgres_path_keeps_versions_and_refuses_a_stale_writer(monkeypatch, test_engine):
    """Same contract on the real store (the in-memory one is for unit tests)."""
    import uuid

    from sqlalchemy import create_engine, text

    from app.ai.actor_context import set_acting_user
    from app.config import settings

    url = test_engine.url.render_as_string(hide_password=False).replace("+asyncpg", "+psycopg2")
    engine = create_engine(url)
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(ws, "_engine", lambda: engine)
    owner = f"e42-{uuid.uuid4().hex[:8]}"
    set_acting_user(owner)
    try:
        ws.upsert_workspace_block("pg", {"title": "one"})
        ws.upsert_workspace_block("pg", {"title": "two"}, expected_revision=1)
        with pytest.raises(HTTPException):
            ws.upsert_workspace_block("pg", {"title": "late"}, expected_revision=1)
        assert ws.get_workspace_block("pg")["title"] == "two"
        assert ws.get_workspace_block_version("pg", 1)["title"] == "one"
        assert [v["revision"] for v in ws.list_workspace_block_versions("pg")] == [2, 1]
        assert ws.delete_workspace_block("pg")
        assert ws.list_workspace_block_versions("pg") == []
    finally:
        set_acting_user(None)
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM owned_workspace_blocks WHERE owner_key = :o"), {"o": owner}
            )
        engine.dispose()


@pytest.mark.asyncio
async def test_a_missing_revision_is_404(client):
    ws.upsert_workspace_block("b404", {"title": "only"})
    assert (await client.get("/api/workspace/blocks/b404/versions/7")).status_code == 404
    assert (await client.get("/api/workspace/blocks/b404/versions/1")).status_code == 200
