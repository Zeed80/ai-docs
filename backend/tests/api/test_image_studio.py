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
async def test_generate_can_use_previous_generation_result_as_source(client, db_session, monkeypatch):
    from app.db.models import ImageGeneration, ImageGenStatus

    copied: dict[str, object] = {}

    monkeypatch.setattr(
        "app.api.image_generation.download_file",
        lambda path: b"previous-result-png",
    )

    def _upload(content: bytes, path: str, content_type: str) -> str:
        copied.update(content=content, path=path, content_type=content_type)
        return path

    monkeypatch.setattr("app.api.image_generation.upload_file", _upload)

    source = ImageGeneration(
        owner_sub="dev-user",
        operation="generate",
        status=ImageGenStatus.done,
        prompt="исходный эскиз",
        params={},
        source_image_paths=[],
        result_path="image-gen/dev-user/source-result.png",
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)

    resp = await client.post(
        "/api/image-gen/generate",
        json={
            "operation": "edit",
            "prompt": "сделай линии чётче",
            "source_image_paths": [f"generation:{source.id}"],
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["operation"] == "edit"
    assert body["source_image_paths"][0].startswith("image-gen-src/dev-user/")
    assert body["source_image_paths"][0].endswith(".png")
    assert body["source_image_paths"][0] == copied["path"]
    assert copied["content"] == b"previous-result-png"
    assert copied["content_type"] == "image/png"


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
    assert gen["job_id"]

    got = await client.get(f"/api/image-gen/{gen['id']}")
    assert got.status_code == 200
    assert got.json()["id"] == gen["id"]

    queue = await client.get("/api/studio/queue")
    assert queue.status_code == 200
    assert any(j["generation_id"] == gen["id"] for j in queue.json()["items"])


@pytest.mark.asyncio
async def test_generate_truncates_queue_title_for_long_prompt(client):
    prompt = "Убери со здания текстуру кирпича и удали дерево и забор. " * 20

    resp = await client.post(
        "/api/image-gen/generate",
        json={
            "operation": "generate",
            "prompt": prompt,
        },
    )

    assert resp.status_code == 200
    gen = resp.json()
    assert gen["prompt"] == prompt

    queue = await client.get("/api/studio/queue")
    assert queue.status_code == 200
    job = next(j for j in queue.json()["items"] if j["generation_id"] == gen["id"])
    assert len(job["title"]) <= 300
    assert job["title"].endswith("…")


@pytest.mark.asyncio
async def test_generate_eskd_requires_prompt(client):
    # "eskd" is a text→image ЕСКД-styled diffusion op — same prompt contract as
    # "generate", so an empty prompt is a 400.
    resp = await client.post(
        "/api/image-gen/generate",
        json={"operation": "eskd", "prompt": ""},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_generate_eskd_creates_queued_record_without_source(client):
    # Unlike edit/inpaint/cleanup, an eskd job needs no source image.
    resp = await client.post(
        "/api/image-gen/generate",
        json={
            "operation": "eskd",
            "prompt": "чертёж кронштейна, вид спереди",
            "params": {"seed": 3},
        },
    )
    assert resp.status_code == 200
    gen = resp.json()
    assert gen["status"] == "queued"
    assert gen["operation"] == "eskd"


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
async def test_push_workflow_to_comfyui_saves_graph_to_userdata(client, db_session, monkeypatch):
    import httpx as httpx_mod

    from app.db.seeds.comfyui_workflows import seed_builtin_workflows

    await seed_builtin_workflows(db_session)
    items = (await client.get("/api/image-gen/workflows/list")).json()["items"]
    wf = next(w for w in items if w["key"] == "edit_qwen_image_edit")

    captured = {}

    def handler(request: httpx_mod.Request) -> httpx_mod.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return httpx_mod.Response(200, text="workflows/edit_qwen_image_edit.json")

    class _FakeAsyncClient(httpx_mod.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx_mod.MockTransport(handler)
            super().__init__(*args, **kwargs)

    # `image_generation.py` does `import httpx` at module level, so this is
    # the same module object it uses — no need to patch the import site too.
    monkeypatch.setattr(httpx_mod, "AsyncClient", _FakeAsyncClient)

    resp = await client.post(f"/api/image-gen/workflows/{wf['id']}/push-to-comfyui")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["filename"] == "workflows/edit_qwen_image_edit.json"
    assert "userdata/" in captured["url"]
    assert b"class_type" in captured["body"] or captured["body"]  # real graph JSON, not empty
    # The placeholder LoadImage filename must not be pushed verbatim — see
    # _strip_placeholder_image_inputs (any server that lacks a real file
    # named exactly "input.png" would show a broken thumbnail for it).
    assert b'"input.png"' not in captured["body"]


def test_strip_placeholder_image_inputs_removes_only_loadimage_placeholder():
    from app.api.image_generation import _strip_placeholder_image_inputs

    graph = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "input.png"}},
        "2": {"class_type": "KSampler", "inputs": {"seed": 42}},
    }
    cleaned = _strip_placeholder_image_inputs(graph)
    assert "image" not in cleaned["1"]["inputs"]
    assert cleaned["2"]["inputs"] == {"seed": 42}
    # original untouched (deep-copied, not mutated in place)
    assert graph["1"]["inputs"]["image"] == "input.png"


@pytest.mark.asyncio
async def test_push_workflow_to_comfyui_returns_404_for_missing_workflow(client):
    resp = await client.post(
        "/api/image-gen/workflows/00000000-0000-0000-0000-000000000099/push-to-comfyui"
    )
    assert resp.status_code == 404


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


def test_fit_image_for_comfy_caps_large_sources():
    import io

    from PIL import Image

    from app.tasks.image_generation import _fit_image_for_comfy

    img = Image.new("RGB", (4096, 2048), "white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    content, size, resized = _fit_image_for_comfy(buf.getvalue())

    assert resized is True
    assert max(size) <= 1280
    assert size[0] * size[1] <= 1_250_000
    assert len(content) < len(buf.getvalue())


@pytest.mark.asyncio
async def test_studio_queue_cancel_marks_generation_cancelled(client, db_session, monkeypatch):
    from app.db.models import ImageGeneration, ImageGenStatus
    from app.services import studio_queue
    from app.tasks.celery_app import celery_app

    revoked: list[str] = []
    monkeypatch.setattr(celery_app.control, "revoke", lambda tid, **kw: revoked.append(tid))

    gen = ImageGeneration(
        owner_sub="dev-user",
        operation="generate",
        status=ImageGenStatus.queued,
        prompt="эскиз",
        params={},
        source_image_paths=[],
    )
    db_session.add(gen)
    await db_session.flush()
    job = await studio_queue.create_image_job(db_session, gen, title="эскиз")
    job.celery_task_id = "celery-123"
    await db_session.commit()
    await db_session.refresh(gen)
    await db_session.refresh(job)

    resp = await client.post(f"/api/studio/queue/{job.id}/cancel")

    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert revoked == ["celery-123"]
    await db_session.refresh(gen)
    assert gen.status == ImageGenStatus.cancelled


@pytest.mark.asyncio
async def test_delete_generation_detaches_studio_job(client, db_session):
    from app.db.models import ImageGeneration, ImageGenStatus, StudioJobStatus
    from app.services import studio_queue

    gen = ImageGeneration(
        owner_sub="dev-user",
        operation="generate",
        status=ImageGenStatus.done,
        prompt="удалить",
        params={},
        source_image_paths=[],
        result_path="image-gen/result.png",
    )
    db_session.add(gen)
    await db_session.flush()
    job = await studio_queue.create_image_job(db_session, gen, title="удалить")
    job.status = StudioJobStatus.done
    await db_session.commit()

    resp = await client.delete(f"/api/image-gen/{gen.id}")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert await db_session.get(ImageGeneration, gen.id) is None
    assert await db_session.get(type(job), job.id) is None


@pytest.mark.asyncio
async def test_studio_queue_list_cleans_done_and_cancelled_jobs(client, db_session):
    from app.db.models import ImageGeneration, ImageGenStatus
    from app.services import studio_queue

    jobs = []
    for idx, status in enumerate(
        [
            studio_queue.StudioJobStatus.done,
            studio_queue.StudioJobStatus.cancelled,
            studio_queue.StudioJobStatus.failed,
        ]
    ):
        gen = ImageGeneration(
            owner_sub="dev-user",
            operation="generate",
            status=ImageGenStatus.done if status != studio_queue.StudioJobStatus.failed else ImageGenStatus.failed,
            prompt=f"job-{idx}",
            params={},
            source_image_paths=[],
        )
        db_session.add(gen)
        await db_session.flush()
        job = await studio_queue.create_image_job(db_session, gen, title=f"job-{idx}")
        job.status = status
        jobs.append(job)
    await db_session.commit()

    resp = await client.get("/api/studio/queue")

    assert resp.status_code == 200
    returned_ids = {item["id"] for item in resp.json()["items"]}
    assert str(jobs[0].id) not in returned_ids
    assert str(jobs[1].id) not in returned_ids
    assert str(jobs[2].id) in returned_ids
    assert await db_session.get(type(jobs[0]), jobs[0].id) is None
    assert await db_session.get(type(jobs[1]), jobs[1].id) is None
    assert await db_session.get(type(jobs[2]), jobs[2].id) is not None


@pytest.mark.asyncio
async def test_studio_queue_stats_exposes_limits_and_counts(client):
    resp = await client.post(
        "/api/image-gen/generate",
        json={"operation": "generate", "prompt": "очередь для метрик"},
    )
    assert resp.status_code == 200

    stats = await client.get("/api/studio/queue/stats")
    assert stats.status_code == 200
    body = stats.json()
    assert body["limits"]["global_active"] >= 1
    assert body["active"] >= 1
    assert body["by_kind"]["image_generation"]["queued"] >= 1


@pytest.mark.asyncio
async def test_studio_queue_pause_rejects_new_generation(client):
    paused = await client.patch(
        "/api/studio/queue/control",
        json={"paused": True, "reason": "maintenance"},
    )
    assert paused.status_code == 200
    try:
        resp = await client.post(
            "/api/image-gen/generate",
            json={"operation": "generate", "prompt": "не ставить"},
        )
        assert resp.status_code == 503
        assert "maintenance" in resp.text
    finally:
        await client.patch(
            "/api/studio/queue/control",
            json={"paused": False, "drain": False, "reason": None},
        )


@pytest.mark.asyncio
async def test_studio_queue_retry_failed_generation(client, db_session, monkeypatch):
    from app.db.models import ImageGeneration, ImageGenStatus
    from app.services import studio_queue
    from app.tasks.celery_app import celery_app

    class _Task:
        id = "retry-task-1"

    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda *args, **kwargs: _Task(),
    )

    gen = ImageGeneration(
        owner_sub="dev-user",
        operation="generate",
        status=ImageGenStatus.failed,
        prompt="повтор",
        params={},
        source_image_paths=[],
        error="boom",
    )
    db_session.add(gen)
    await db_session.flush()
    job = await studio_queue.create_image_job(db_session, gen, title="повтор")
    job.status = studio_queue.StudioJobStatus.failed
    job.error = "boom"
    await db_session.commit()

    resp = await client.post(f"/api/studio/queue/{job.id}/retry")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["can_retry"] is False
    await db_session.refresh(gen)
    assert gen.status == ImageGenStatus.queued
    assert gen.celery_task_id == "retry-task-1"


def test_pick_upscale_model_parses_combo_shapes():
    """object_info COMBO for the upscale model varies by ComfyUI version;
    _pick_upscale_model must read both shapes and prefer a sharp model."""
    from app.tasks.image_generation import _pick_upscale_model

    # newer: ["COMBO", {"options": [...]}]
    oi_new = {"UpscaleModelLoader": {"input": {"required": {
        "model_name": ["COMBO", {"options": ["4x-UltraSharp.pth", "x2.pth"]}]}}}}
    assert _pick_upscale_model(oi_new) == "4x-UltraSharp.pth"

    # older: [[opt, ...], {...}]
    oi_old = {"UpscaleModelLoader": {"input": {"required": {
        "model_name": [["RealESRGAN_x4.pth", "other.pth"], {}]}}}}
    assert _pick_upscale_model(oi_old) == "RealESRGAN_x4.pth"  # 4x preferred

    # no upscaler node → None (skip gracefully)
    assert _pick_upscale_model({}) is None
