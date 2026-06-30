"""Integration tests for the image studio API (/api/image-gen)."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_workflows_seeded_and_listed(client, db_session):
    from app.db.seeds.comfyui_workflows import seed_builtin_workflows

    await seed_builtin_workflows(db_session)

    resp = await client.get("/api/image-gen/workflows/list")
    assert resp.status_code == 200
    items = resp.json()["items"]
    keys = {w["key"] for w in items}
    assert "edit_qwen_image_edit" in keys
    assert "generate_qwen_image" in keys
    # Builtins are flagged and carry a non-empty graph + inject_map.
    edit = next(w for w in items if w["key"] == "edit_qwen_image_edit")
    assert edit["is_builtin"] is True
    assert edit["graph"] and edit["inject_map"]


@pytest.mark.asyncio
async def test_generate_requires_prompt_for_text2image(client):
    resp = await client.post(
        "/api/image-gen/generate",
        json={"operation": "generate", "prompt": ""},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_generate_requires_source_for_edit(client):
    resp = await client.post(
        "/api/image-gen/generate",
        json={"operation": "edit", "prompt": "убери фаску"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_generate_creates_queued_record(client):
    resp = await client.post(
        "/api/image-gen/generate",
        json={
            "operation": "generate",
            "prompt": "эскиз кондуктора, линейный чертёж",
            "params": {"seed": 7},
        },
    )
    assert resp.status_code == 200
    gen = resp.json()
    assert gen["status"] == "queued"
    assert gen["operation"] == "generate"

    got = await client.get(f"/api/image-gen/{gen['id']}")
    assert got.status_code == 200
    assert got.json()["id"] == gen["id"]


@pytest.mark.asyncio
async def test_duplicate_then_delete_builtin_copy(client, db_session):
    from app.db.seeds.comfyui_workflows import seed_builtin_workflows

    await seed_builtin_workflows(db_session)
    items = (await client.get("/api/image-gen/workflows/list")).json()["items"]
    builtin = next(w for w in items if w["is_builtin"])

    dup = await client.post(f"/api/image-gen/workflows/{builtin['id']}/duplicate")
    assert dup.status_code == 200
    copy = dup.json()
    assert copy["is_builtin"] is False

    # Builtins cannot be deleted; copies can.
    blocked = await client.request("DELETE", f"/api/image-gen/workflows/{builtin['id']}")
    assert blocked.status_code == 400
    ok = await client.request("DELETE", f"/api/image-gen/workflows/{copy['id']}")
    assert ok.status_code == 200
