"""Ad-hoc editable sheets: lifecycle, formulas, isolation from production data."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_sheet_create_edit_formula_lifecycle(client):
    # Create with two numeric columns + a computed column.
    resp = await client.post("/api/workspace/sheets/create", json={
        "title": "Расчёт",
        "columns": [
            {"key": "quantity", "header": "Кол-во", "type": "number"},
            {"key": "price", "header": "Цена", "type": "number"},
            {"key": "amount", "header": "Сумма", "type": "number",
             "formula": "quantity * price"},
        ],
        "rows": [{"quantity": 10, "price": 800}],
    })
    assert resp.status_code == 200
    sheet_id = resp.json()["sheet_id"]

    # Computed column resolves on read.
    got = (await client.get(f"/api/workspace/sheets/{sheet_id}")).json()
    assert got["rows"][0]["amount"] == 8000

    # Patch a cell, add a row, verify recompute.
    await client.post(f"/api/workspace/sheets/{sheet_id}/patch-cells", json={
        "edits": [{"row": 0, "col": "quantity", "value": 5}],
    })
    await client.post(f"/api/workspace/sheets/{sheet_id}/add-row", json={
        "values": {"quantity": 3, "price": 100},
    })
    got = (await client.get(f"/api/workspace/sheets/{sheet_id}")).json()
    assert got["rows"][0]["amount"] == 4000   # 5 * 800
    assert got["rows"][1]["amount"] == 300    # 3 * 100

    # Add a per-cell formula column.
    await client.post(f"/api/workspace/sheets/{sheet_id}/add-column", json={
        "key": "vat", "header": "НДС", "type": "number", "formula": "amount * 0.2",
    })
    got = (await client.get(f"/api/workspace/sheets/{sheet_id}")).json()
    assert got["rows"][0]["vat"] == 800  # 4000 * 0.2

    # Workspace block published as type "sheet".
    from app.domain.workspace import get_workspace_block
    block = get_workspace_block(f"sheet:{sheet_id}")
    assert block and block["type"] == "sheet"

    # Delete removes the block.
    await client.delete(f"/api/workspace/sheets/{sheet_id}")
    assert get_workspace_block(f"sheet:{sheet_id}") is None


@pytest.mark.asyncio
async def test_sheet_from_spec_is_editable_copy(client):
    # Build a (read-only) spec-table block, then materialise it into a sheet.
    from app.db.models import Document, Invoice, Party

    # Minimal seed: one invoice via the workspace store path isn't needed; we
    # just need a table block — use the documents source which needs a Document.
    # Build directly through the spec-table endpoint with empty data is fine.
    spec_resp = await client.post("/api/workspace/agent/spec-table", json={
        "canvas_id": "agent:spec-table",
        "spec": {"source": "suppliers", "columns": [{"field": "name"}, {"field": "inn"}]},
    })
    assert spec_resp.status_code == 200

    resp = await client.post("/api/workspace/sheets/from-spec", json={
        "canvas_id": "agent:spec-table",
        "title": "Поставщики (правка)",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "created"
    sheet_id = data["sheet_id"]

    got = (await client.get(f"/api/workspace/sheets/{sheet_id}")).json()
    keys = {c["key"] for c in got["columns"]}
    assert keys == {"name", "inn"}
    assert all(c["editable"] for c in got["columns"])


@pytest.mark.asyncio
async def test_sheet_from_template_estimate(client):
    # Templates are discoverable.
    tpls = (await client.get("/api/workspace/sheets/templates/list")).json()
    keys = {t["key"] for t in tpls["templates"]}
    assert {"estimate", "price_comparison", "budget"} <= keys

    # Instantiate the estimate template (by Russian synonym) — formulas compute.
    resp = await client.post("/api/workspace/sheets/from-template", json={
        "template": "смета",
    })
    assert resp.status_code == 200
    sheet_id = resp.json()["sheet_id"]
    got = (await client.get(f"/api/workspace/sheets/{sheet_id}")).json()
    col_keys = [c["key"] for c in got["columns"]]
    assert "amount" in col_keys and "vat" in col_keys and "total" in col_keys

    # Fill a row → computed columns cascade (amount→vat→total).
    await client.post(f"/api/workspace/sheets/{sheet_id}/patch-cells", json={
        "edits": [
            {"row": 0, "col": "quantity", "value": 10},
            {"row": 0, "col": "price", "value": 800},
        ],
    })
    got = (await client.get(f"/api/workspace/sheets/{sheet_id}")).json()
    r0 = got["rows"][0]
    assert r0["amount"] == 8000 and r0["vat"] == 1600 and r0["total"] == 9600


@pytest.mark.asyncio
async def test_unknown_template_rejected(client):
    bad = await client.post("/api/workspace/sheets/from-template", json={
        "template": "несуществующий",
    })
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_rename_column(client):
    resp = await client.post("/api/workspace/sheets/create", json={
        "title": "X",
        "columns": [{"key": "A", "header": "A", "type": "text"}],
        "rows": [{"A": 1}],
    })
    sheet_id = resp.json()["sheet_id"]
    r = await client.post(f"/api/workspace/sheets/{sheet_id}/rename-column", json={
        "key": "A", "header": "Поставщик",
    })
    assert r.status_code == 200
    got = (await client.get(f"/api/workspace/sheets/{sheet_id}")).json()
    assert got["columns"][0]["header"] == "Поставщик"
    assert got["columns"][0]["key"] == "A"  # address unchanged


@pytest.mark.asyncio
async def test_patch_unknown_column_rejected(client):
    resp = await client.post("/api/workspace/sheets/create", json={"title": "X"})
    sheet_id = resp.json()["sheet_id"]
    bad = await client.post(f"/api/workspace/sheets/{sheet_id}/patch-cells", json={
        "edits": [{"row": 0, "col": "ZZZ", "value": 1}],
    })
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_sheet_merge_unmerge_cells(client):
    resp = await client.post("/api/workspace/sheets/create", json={
        "title": "Merge",
        "columns": [
            {"key": "A", "header": "A", "type": "text"},
            {"key": "B", "header": "B", "type": "text"},
        ],
        "rows": [{"A": "left", "B": "right"}],
    })
    sheet_id = resp.json()["sheet_id"]

    merged = await client.post(f"/api/workspace/sheets/{sheet_id}/merge-cells", json={
        "start_row": 0,
        "end_row": 0,
        "start_col": "A",
        "end_col": "B",
    })
    assert merged.status_code == 200
    got = (await client.get(f"/api/workspace/sheets/{sheet_id}")).json()
    merges = got["layout"]["merges"]
    assert len(merges) == 1
    assert merges[0]["start_col"] == "A" and merges[0]["end_col"] == "B"

    overlap = await client.post(f"/api/workspace/sheets/{sheet_id}/merge-cells", json={
        "start_row": 0,
        "end_row": 0,
        "start_col": "A",
        "end_col": "B",
    })
    assert overlap.status_code == 400

    unmerged = await client.post(f"/api/workspace/sheets/{sheet_id}/unmerge-cells", json={
        "row": 0,
        "col": "A",
    })
    assert unmerged.status_code == 200
    got = (await client.get(f"/api/workspace/sheets/{sheet_id}")).json()
    assert got["layout"]["merges"] == []
