"""Integration tests for the image studio API (/api/image-gen)."""

from __future__ import annotations

import pytest

# NOTE: the "agent-service" ownership-bypass tests below exercise pure helper
# functions directly rather than going through the `client` fixture. The
# fixture's AUTH_ENABLED=false test mode makes `get_current_user` always
# return `_DEV_USER` regardless of headers (see auth/jwt.py), so an
# `X-API-Key` header is silently ignored in tests — the agent-service identity
# path is only reachable with real auth enabled. This is exactly why the bug
# these tests guard against (agent-mediated capability calls 404 on every
# owner-scoped image_studio endpoint, discovered via live-stack testing with
# AUTH_ENABLED=true) was invisible to the pre-existing test suite.

VALID_SHAFT = {
    "type": "shaft",
    "segments": [{"diameter": 45, "length": 60, "tolerance": "h6", "roughness": 0.8}],
    "title": {"name": "Вал"},
}
INVALID_SHAFT = {
    "type": "shaft",
    "segments": [{"diameter": 45, "length": 60, "roughness": 0.9}],
    "title": {"name": "Вал"},
}


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


@pytest.mark.asyncio
async def test_techdraw_direct_spec_valid_renders(client):
    resp = await client.post("/api/image-gen/techdraw", json={"spec": VALID_SHAFT})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "done"
    assert body["operation"] == "techdraw"
    assert body["has_result"] is True


@pytest.mark.asyncio
async def test_techdraw_direct_spec_invalid_returns_422_with_reason(client):
    resp = await client.post("/api/image-gen/techdraw", json={"spec": INVALID_SHAFT})
    assert resp.status_code == 422
    assert "RA_INVALID" in resp.text or "0.9" in resp.text


@pytest.mark.asyncio
async def test_techdraw_description_repairs_after_one_invalid_attempt(client, monkeypatch):
    from app.ai.schemas import AIResponse, AITask, ProviderKind

    calls = {"n": 0}

    class FakeAIRouter:
        async def run(self, request):
            calls["n"] += 1
            import json

            spec = INVALID_SHAFT if calls["n"] == 1 else VALID_SHAFT
            return AIResponse(
                task=AITask.ENGINEERING_REASONING, provider=ProviderKind.OLLAMA,
                model="fake", text=json.dumps(spec),
            )

    monkeypatch.setattr("app.ai.router.AIRouter", FakeAIRouter)
    resp = await client.post("/api/image-gen/techdraw", json={"description": "вал 45 h6"})
    assert resp.status_code == 200
    assert calls["n"] == 2  # first attempt invalid, one repair retry


@pytest.mark.asyncio
async def test_techdraw_description_gives_up_after_repair_fails(client, monkeypatch):
    from app.ai.schemas import AIResponse, AITask, ProviderKind

    class FakeAIRouter:
        async def run(self, request):
            import json

            return AIResponse(
                task=AITask.ENGINEERING_REASONING, provider=ProviderKind.OLLAMA,
                model="fake", text=json.dumps(INVALID_SHAFT),
            )

    monkeypatch.setattr("app.ai.router.AIRouter", FakeAIRouter)
    resp = await client.post("/api/image-gen/techdraw", json={"description": "вал 45 без Ra"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_accept_techdraw_endpoint_accepts_techdraw_result(client):
    gen_resp = await client.post("/api/image-gen/techdraw", json={"spec": VALID_SHAFT})
    gen_id = gen_resp.json()["id"]
    resp = await client.post(f"/api/image-gen/{gen_id}/accept-techdraw")
    assert resp.status_code == 200
    assert resp.json()["accepted"] is True


@pytest.mark.asyncio
async def test_accept_techdraw_endpoint_rejects_diffusion_result(client):
    gen_resp = await client.post(
        "/api/image-gen/generate",
        json={"operation": "generate", "prompt": "эскиз"},
    )
    gen_id = gen_resp.json()["id"]
    resp = await client.post(f"/api/image-gen/{gen_id}/accept-techdraw")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_plain_accept_blocks_agent_service_call_for_techdraw(client, monkeypatch):
    """Closes the loophole: an agent can't dodge the accept_techdraw gate by
    just calling action=accept for the same techdraw generation_id."""
    from app.config import settings

    monkeypatch.setattr(settings, "agent_service_key", "test-secret-key", raising=False)

    gen_resp = await client.post("/api/image-gen/techdraw", json={"spec": VALID_SHAFT})
    gen_id = gen_resp.json()["id"]

    resp = await client.post(
        f"/api/image-gen/{gen_id}/accept",
        headers={"X-API-Key": "test-secret-key"},
    )
    assert resp.status_code == 423
    assert resp.json()["detail"]["error_code"] == "approval_required"


@pytest.mark.asyncio
async def test_plain_accept_still_works_for_human_browser_session(client, monkeypatch):
    """A human clicking "Принять" in the Studio UI (no service-key header) is unaffected."""
    from app.config import settings

    monkeypatch.setattr(settings, "agent_service_key", "test-secret-key", raising=False)

    gen_resp = await client.post("/api/image-gen/techdraw", json={"spec": VALID_SHAFT})
    gen_id = gen_resp.json()["id"]

    resp = await client.post(f"/api/image-gen/{gen_id}/accept")
    assert resp.status_code == 200
    assert resp.json()["accepted"] is True


@pytest.mark.asyncio
async def test_techdraw_links_to_document_and_case(client, db_session):
    from app.db.models import Document, DocumentStatus, DocumentType, WorkCase

    doc = Document(
        file_name="drawing.pdf", file_hash="techdraw-link-hash", file_size=10,
        mime_type="application/pdf", storage_path="documents/drawing.pdf",
        doc_type=DocumentType.other, status=DocumentStatus.approved,
    )
    case = WorkCase(title="Изготовление вала", created_by="tester")
    db_session.add_all([doc, case])
    await db_session.flush()

    resp = await client.post("/api/image-gen/techdraw", json={
        "spec": VALID_SHAFT,
        "source_document_id": str(doc.id),
        "case_id": str(case.id),
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_document_id"] == str(doc.id)
    assert body["case_id"] == str(case.id)


def _user(sub: str):
    from app.auth.models import UserInfo

    return UserInfo(sub=sub, email="x@y.z", name="t", preferred_username="t", roles=[])


def test_is_agent_service_identifies_the_internal_service_sub():
    from app.api.image_generation import _is_agent_service

    assert _is_agent_service(_user("agent-service")) is True
    assert _is_agent_service(_user("some-real-user-sub")) is False


def test_owns_lets_agent_service_bypass_ownership_for_any_record():
    """Regression test for a live-stack finding: the capability dispatcher

    (`/api/agent/cap/*`) never forwards the real chatting user's identity to
    the proxied REST call — every agent-mediated request resolves to
    ``sub="agent-service"`` (see auth.jwt._verify_api_key). Before this fix,
    every owner-scoped image_studio endpoint (list/get/accept/iterate/delete)
    404'd for ANY agent-mediated call, making the whole capability
    non-functional end-to-end whenever AUTH_ENABLED=true.
    """
    from app.api.image_generation import _owns
    from app.db.models import ImageGenStatus, ImageGeneration

    gen = ImageGeneration(
        owner_sub="real-human-user", operation="techdraw",
        status=ImageGenStatus.done, params={}, source_image_paths=[],
    )
    assert _owns(gen, _user("agent-service")) is True
    assert _owns(gen, _user("real-human-user")) is True
    assert _owns(gen, _user("a-different-human")) is False
    assert _owns(None, _user("agent-service")) is False
