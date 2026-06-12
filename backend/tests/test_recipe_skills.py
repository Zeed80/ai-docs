"""Recipe lifecycle: record → retrieve → outcome stats → activate/retire + API."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import recipes
from app.db.models import RecipeSkill


@pytest_asyncio.fixture
async def recipes_db(test_engine, monkeypatch):
    """Point the recipes module at the test database; stub the vector layer."""
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    import app.db.session as session_module
    monkeypatch.setattr(session_module, "_get_session_factory", lambda: factory)

    indexed: list[dict] = []

    async def fake_index(recipe_id, example_idx, text):
        indexed.append({"recipe_id": recipe_id, "idx": example_idx, "text": text})

    search_results: list[dict] = []

    async def fake_search(text, limit=3):
        return list(search_results)

    monkeypatch.setattr(recipes, "_index_trigger", fake_index)
    monkeypatch.setattr(recipes, "_search_triggers", fake_search)
    monkeypatch.setattr(recipes, "_gate_actions_map", lambda: {"invoices": {"approve"}})
    monkeypatch.setattr(recipes, "capabilities_schema_hash", lambda: "hash-v1")

    yield {
        "factory": factory,
        "indexed": indexed,
        "search_results": search_results,
    }
    # Cleanup rows created by the recipes module (it commits outside the
    # per-test transaction).
    async with factory() as db:
        from sqlalchemy import delete
        await db.execute(delete(RecipeSkill))
        await db.commit()


_STEPS = [
    {"capability": "invoices", "action": "list",
     "args_template": {"action": "list", "filters": {"supplier_query": "Ромашка"}}},
    {"capability": "workspace", "action": "publish",
     "args_template": {"action": "publish", "canvas_id": "agent:invoices"}},
]


@pytest.mark.asyncio
async def test_record_creates_draft_with_slots(recipes_db):
    ok = await recipes.record_candidate(
        user_text='выведи счета поставщика «Ромашка» в таблицу',
        role="invoice_specialist",
        intent="invoice_list",
        steps=list(_STEPS),
    )
    assert ok is True
    async with recipes_db["factory"]() as db:
        from sqlalchemy import select
        recipe = (await db.execute(select(RecipeSkill))).scalars().one()
    assert recipe.status == "draft"
    assert recipe.capability_schema_hash == "hash-v1"
    assert recipe.param_slots and "supplier_name" in recipe.param_slots
    assert (
        recipe.steps[0]["args_template"]["filters"]["supplier_query"]
        == "{{user.supplier_name}}"
    )
    assert recipes_db["indexed"], "trigger example must be indexed for retrieval"


@pytest.mark.asyncio
async def test_record_rejects_gated_and_trivial(recipes_db):
    # Approval-gated action never enters a recipe.
    gated = [
        {"capability": "invoices", "action": "approve", "args_template": {"action": "approve"}},
        {"capability": "workspace", "action": "publish", "args_template": {}},
    ]
    assert await recipes.record_candidate(
        user_text="утверди счёт", role="accountant", intent="approve", steps=gated
    ) is False
    # Single-step turns are not worth a recipe.
    assert await recipes.record_candidate(
        user_text="покажи счета", role="accountant", intent="list", steps=_STEPS[:1]
    ) is False


@pytest.mark.asyncio
async def test_outcome_promotes_and_retires(recipes_db):
    await recipes.record_candidate(
        user_text="счета Ромашки", role="invoice_specialist",
        intent="invoice_list", steps=list(_STEPS),
    )
    async with recipes_db["factory"]() as db:
        from sqlalchemy import select
        recipe = (await db.execute(select(RecipeSkill))).scalars().one()
        rid = recipe.id

    # Two successful replays promote a clean draft to active.
    await recipes.record_outcome(rid, success=True)
    await recipes.record_outcome(rid, success=True)
    async with recipes_db["factory"]() as db:
        recipe = await db.get(RecipeSkill, rid)
        assert recipe.status == "active"
        assert recipe.success_count == 2

    # Fail-rate demotion: 3 fails over 5 uses (>50%) retires it.
    for _ in range(3):
        await recipes.record_outcome(rid, success=False)
    async with recipes_db["factory"]() as db:
        recipe = await db.get(RecipeSkill, rid)
        assert recipe.status == "retired"


@pytest.mark.asyncio
async def test_find_recipe_skips_retired(recipes_db):
    await recipes.record_candidate(
        user_text="счета Ромашки", role="invoice_specialist",
        intent="invoice_list", steps=list(_STEPS),
    )
    async with recipes_db["factory"]() as db:
        from sqlalchemy import select
        recipe = (await db.execute(select(RecipeSkill))).scalars().one()
        rid = str(recipe.id)

    recipes_db["search_results"].append({"recipe_id": rid, "score": 0.91})
    hit = await recipes.find_recipe("счета Ромашки за май")
    assert hit is not None
    found, score = hit
    assert str(found.id) == rid and score == 0.91

    await recipes.record_outcome(uuid.UUID(rid), success=False, retire=True)
    assert await recipes.find_recipe("счета Ромашки за май") is None


@pytest.mark.asyncio
async def test_dedupe_adds_trigger_example(recipes_db):
    await recipes.record_candidate(
        user_text="счета Ромашки", role="invoice_specialist",
        intent="invoice_list", steps=list(_STEPS),
    )
    async with recipes_db["factory"]() as db:
        from sqlalchemy import select
        recipe = (await db.execute(select(RecipeSkill))).scalars().one()
        rid = str(recipe.id)

    # A near-identical task enriches the existing recipe instead of duplicating.
    recipes_db["search_results"].append({"recipe_id": rid, "score": 0.96})
    ok = await recipes.record_candidate(
        user_text="покажи счета поставщика Ромашка",
        role="invoice_specialist", intent="invoice_list", steps=list(_STEPS),
    )
    assert ok is True
    async with recipes_db["factory"]() as db:
        from sqlalchemy import func, select
        count = (await db.execute(select(func.count()).select_from(RecipeSkill))).scalar()
        recipe = await db.get(RecipeSkill, uuid.UUID(rid))
        assert count == 1
        assert len(recipe.trigger_examples) == 2


@pytest.mark.asyncio
async def test_recipes_api_list_activate_retire(recipes_db, client, monkeypatch):
    await recipes.record_candidate(
        user_text="счета Ромашки", role="invoice_specialist",
        intent="invoice_list", steps=list(_STEPS),
    )
    resp = await client.get("/api/agent/recipes")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1 and items[0]["status"] == "draft"
    rid = items[0]["id"]

    resp = await client.post(f"/api/agent/recipes/{rid}/activate")
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"

    resp = await client.post(f"/api/agent/recipes/{rid}/retire")
    assert resp.status_code == 200
    assert resp.json()["status"] == "retired"
