"""Integration tests for the LoRA training API (/api/lora)."""

from __future__ import annotations

import uuid

import pytest


@pytest.mark.asyncio
async def test_create_dataset_requires_sources_or_synthetics(client):
    resp = await client.post(
        "/api/lora/datasets",
        json={"name": "пустой", "source_paths": [], "params": {"synth_count": 0}},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_dataset_queues_preparation(client, monkeypatch):
    sent: dict = {}

    from app.tasks.celery_app import celery_app

    def fake_send(name, args=None, **kw):
        sent["name"] = name
        sent["args"] = args

        class _T:
            id = "task-1"

        return _T()

    monkeypatch.setattr(celery_app, "send_task", fake_send)

    resp = await client.post(
        "/api/lora/datasets",
        json={"name": "синтетика", "source_paths": [], "params": {"synth_count": 20}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "preparing"
    assert sent["name"] == "lora.prepare_dataset"
    assert sent["args"] == [body["id"]]

    listed = await client.get("/api/lora/datasets")
    assert any(d["id"] == body["id"] for d in listed.json()["datasets"])


@pytest.mark.asyncio
async def test_run_on_unready_dataset_is_rejected(client, db_session, monkeypatch):
    from app.tasks.celery_app import celery_app

    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: type("T", (), {"id": "x"})())

    resp = await client.post(
        "/api/lora/datasets",
        json={"name": "не готов", "source_paths": [], "params": {"synth_count": 5}},
    )
    ds_id = resp.json()["id"]

    resp = await client.post(
        "/api/lora/runs",
        json={"dataset_id": ds_id, "name": "run", "config": {"steps": 500}},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_run_creation_queues_immediately(client, db_session, monkeypatch):
    """The lora.train approval gate was removed — creating a run queues the
    trainer task directly (confirmation lives in the panel dialog)."""
    from app.db.models import LoraDataset, LoraDatasetStatus
    from app.tasks.celery_app import celery_app

    sent: dict = {}

    def fake_send(name, args=None, **kw):
        sent["name"] = name
        return type("T", (), {"id": "task-run"})()

    monkeypatch.setattr(celery_app, "send_task", fake_send)

    ds = LoraDataset(
        name="готовый", status=LoraDatasetStatus.ready, preset="drawing_cleanup",
        params={}, source_paths=[], stats={"pairs": 42}, preview_paths=[],
        dataset_dir="/lora-data/datasets/test",
    )
    db_session.add(ds)
    await db_session.commit()
    await db_session.refresh(ds)

    resp = await client.post(
        "/api/lora/runs",
        json={"dataset_id": str(ds.id), "name": "проба",
              "config": {"steps": 1000, "base_model": "qwen_image_edit_2511"}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["base_family"] == "qwen"
    assert body["eta_hours"] is not None  # qwen has a measured sec_per_step
    assert sent["name"] == "lora.run_training"


@pytest.mark.asyncio
async def test_run_config_validation(client, db_session, monkeypatch):
    from app.db.models import LoraDataset, LoraDatasetStatus
    from app.tasks.celery_app import celery_app

    monkeypatch.setattr(celery_app, "send_task",
                        lambda *a, **k: type("T", (), {"id": "x"})())
    ds = LoraDataset(name="ds", status=LoraDatasetStatus.ready, preset="drawing_cleanup",
                     params={}, source_paths=[], stats={}, preview_paths=[],
                     dataset_dir="/lora-data/datasets/t")
    db_session.add(ds)
    await db_session.commit()
    await db_session.refresh(ds)

    # steps beyond the cap → 422
    resp = await client.post("/api/lora/runs", json={
        "dataset_id": str(ds.id), "name": "x", "config": {"steps": 10**7}})
    assert resp.status_code == 422
    # unknown base model → 422 (whitelist)
    resp = await client.post("/api/lora/runs", json={
        "dataset_id": str(ds.id), "name": "x",
        "config": {"steps": 500, "base_model": "stable-diffusion-1.5"}})
    assert resp.status_code == 422
    # a flux2 model is accepted and its family recorded
    resp = await client.post("/api/lora/runs", json={
        "dataset_id": str(ds.id), "name": "x",
        "config": {"steps": 500, "base_model": "flux2_klein_4b"}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["base_family"] == "flux2"


@pytest.mark.asyncio
async def test_gated_model_without_token_is_refused_early(client, db_session, monkeypatch):
    """A gated HF base model (klein-9B/dev) without HF_TOKEN must fail fast at
    creation with actionable guidance, not 10 min into a doomed download."""
    from app.config import settings
    from app.db.models import LoraDataset, LoraDatasetStatus
    from app.tasks.celery_app import celery_app

    monkeypatch.setattr(celery_app, "send_task",
                        lambda *a, **k: type("T", (), {"id": "x"})())
    monkeypatch.setattr(settings, "hf_token", None, raising=False)

    ds = LoraDataset(name="ds", status=LoraDatasetStatus.ready, preset="drawing_cleanup",
                     params={}, source_paths=[], stats={}, preview_paths=[],
                     dataset_dir="/lora-data/datasets/t")
    db_session.add(ds)
    await db_session.commit()
    await db_session.refresh(ds)

    resp = await client.post("/api/lora/runs", json={
        "dataset_id": str(ds.id), "name": "x",
        "config": {"steps": 500, "base_model": "flux2_klein_9b"}})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "gated" in detail and "HuggingFace" in detail  # actionable, GUI-oriented

    # klein-4B is open — no token required, run queues fine.
    resp = await client.post("/api/lora/runs", json={
        "dataset_id": str(ds.id), "name": "x",
        "config": {"steps": 500, "base_model": "flux2_klein_4b"}})
    assert resp.status_code == 200


def _write_lora(path, rank=32, base_version="qwen_image", step=1500):
    """Minimal valid safetensors LoRA (header only + tiny payload) for
    inspection tests."""
    import json
    import struct

    tensors = {
        "diffusion_model.blocks.0.attn.lora_A.weight":
            {"dtype": "F16", "shape": [rank, 3072], "data_offsets": [0, 2]},
        "diffusion_model.blocks.0.attn.lora_B.weight":
            {"dtype": "F16", "shape": [3072, rank], "data_offsets": [2, 4]},
    }
    meta = {"ss_base_model_version": base_version,
            "training_info": json.dumps({"step": step, "epoch": 0}),
            "software": json.dumps({"name": "ai-toolkit"}),
            "ss_output_name": "my_lora"}
    header = {**tensors, "__metadata__": meta}
    raw = json.dumps(header).encode()
    with open(path, "wb") as fh:
        fh.write(struct.pack("<Q", len(raw)))
        fh.write(raw)
        fh.write(b"\x00" * 4)


def test_inspect_lora_reads_rank_and_family(tmp_path):
    from app.ai.lora_inspect import inspect_lora

    p = tmp_path / "l.safetensors"
    _write_lora(p, rank=16, base_version="qwen_image", step=1500)
    info = inspect_lora(p)
    assert info["ok"] and info["family"] == "qwen"
    assert info["rank"] == 16 and info["step"] == 1500


def test_lora_compatibility_levels(tmp_path):
    from app.ai.lora_inspect import check_compatibility, inspect_lora

    p = tmp_path / "q.safetensors"
    _write_lora(p, rank=32, base_version="qwen_image")
    info = inspect_lora(p)
    # same family → ok, and suggested_rank surfaces the LoRA's rank
    ok = check_compatibility(info, "qwen", 32)
    assert ok["level"] == "ok" and ok["compatible"] and ok["suggested_rank"] == 32
    # wrong family → hard error (would crash the run)
    bad = check_compatibility(info, "flux2", 32)
    assert bad["level"] == "error" and not bad["compatible"]
    # no metadata → unconfirmed warning, still allowed
    p2 = tmp_path / "third.safetensors"
    _write_lora(p2, rank=8, base_version="")
    warn = check_compatibility(inspect_lora(p2), "qwen", 32)
    assert warn["level"] == "warn" and warn["compatible"]


@pytest.mark.asyncio
async def test_resume_lora_incompatible_is_refused(client, db_session, monkeypatch, tmp_path):
    """create_run must reject a family-mismatched LoRA before queuing."""
    import app.api.lora_training as api
    from app.db.models import LoraDataset, LoraDatasetStatus
    from app.tasks.celery_app import celery_app

    monkeypatch.setattr(celery_app, "send_task",
                        lambda *a, **k: type("T", (), {"id": "x"})())
    lora = tmp_path / "flux.safetensors"
    _write_lora(lora, rank=32, base_version="flux2_klein")

    async def fake_resolve(ref, db, user):
        return lora

    monkeypatch.setattr(api, "_resolve_lora_source", fake_resolve)

    ds = LoraDataset(name="ds", status=LoraDatasetStatus.ready, preset="drawing_cleanup",
                     params={}, source_paths=[], stats={}, preview_paths=[],
                     dataset_dir="/lora-data/datasets/t")
    db_session.add(ds)
    await db_session.commit()
    await db_session.refresh(ds)

    # Continuing a flux2 LoRA on a qwen base → 400.
    resp = await client.post("/api/lora/runs", json={
        "dataset_id": str(ds.id), "name": "ft",
        "config": {"steps": 500, "base_model": "qwen_image_edit_2511"},
        "resume_lora": "upload:flux.safetensors"})
    assert resp.status_code == 400
    assert "несовместима" in resp.json()["detail"].lower()


def test_hf_token_resolver_uses_shared_settings_token(monkeypatch):
    """The token is NOT LoRA-specific: it reuses the shared HuggingFace token
    from Настройки → Модели (llamacpp_manager._load_tokens), with the
    HF_TOKEN env only as a legacy fallback."""
    from app.ai import lora_base_models as m

    monkeypatch.setattr("app.config.settings.hf_token", "env-token", raising=False)
    # No shared token → env fallback.
    monkeypatch.setattr("app.ai.providers.llamacpp_manager._load_tokens", lambda: {})
    assert m.get_hf_token() == "env-token"
    assert m.hf_token_status()["source"] == "env"
    # Shared token from settings wins.
    monkeypatch.setattr("app.ai.providers.llamacpp_manager._load_tokens",
                        lambda: {"huggingface": "shared-token"})
    assert m.get_hf_token() == "shared-token"
    st = m.hf_token_status()
    assert st["configured"] and st["source"] == "settings"


@pytest.mark.asyncio
async def test_source_paths_must_be_uploads(client):
    """Arbitrary-file-read guard: sources outside uploads/<sub>/ are refused."""
    for bad in ["/etc/passwd", "../../secrets/key.png", "datasets/x/target.png"]:
        resp = await client.post("/api/lora/datasets", json={
            "name": "x", "source_paths": [bad], "params": {"synth_count": 0}})
        assert resp.status_code == 400, f"{bad} should be rejected"


@pytest.mark.asyncio
async def test_edit_preset_rejects_sources(client):
    resp = await client.post("/api/lora/datasets", json={
        "name": "edit", "preset": "drawing_edit",
        "source_paths": ["uploads/dev-user/a.png"], "params": {"synth_count": 50}})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_owner_scoping_hides_foreign_runs(client, db_session, monkeypatch):
    """A non-admin, non-owner user gets 404 on someone else's run."""
    from app.auth.jwt import get_current_user
    from app.auth.models import UserInfo, UserRole
    from app.db.models import (
        LoraDataset,
        LoraDatasetStatus,
        LoraRunStatus,
        LoraTrainingRun,
    )
    from app.main import app

    ds = LoraDataset(owner_sub="alice", name="ds", status=LoraDatasetStatus.ready,
                     preset="drawing_cleanup", params={}, source_paths=[], stats={},
                     preview_paths=[])
    db_session.add(ds)
    await db_session.flush()
    run = LoraTrainingRun(owner_sub="alice", dataset_id=ds.id, name="alice-run",
                          status=LoraRunStatus.running, config={}, progress={},
                          checkpoints=[], sample_paths=[])
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)

    app.dependency_overrides[get_current_user] = lambda: UserInfo(
        sub="bob", email="bob@x", name="Bob", preferred_username="bob",
        roles=[UserRole.viewer])
    try:
        assert (await client.get(f"/api/lora/runs/{run.id}")).status_code == 404
        assert (await client.post(f"/api/lora/runs/{run.id}/stop")).status_code == 404
        listed = await client.get("/api/lora/runs")
        assert all(r["id"] != str(run.id) for r in listed.json()["runs"])
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_stop_queued_run_cancels_and_revokes(client, db_session, monkeypatch):
    from app.db.models import (
        LoraDataset,
        LoraDatasetStatus,
        LoraRunStatus,
        LoraTrainingRun,
    )
    from app.tasks.celery_app import celery_app

    revoked: list = []
    monkeypatch.setattr(celery_app.control, "revoke",
                        lambda tid, **kw: revoked.append(tid))

    ds = LoraDataset(name="ds", status=LoraDatasetStatus.ready, preset="drawing_cleanup",
                     params={}, source_paths=[], stats={}, preview_paths=[])
    db_session.add(ds)
    await db_session.flush()
    run = LoraTrainingRun(dataset_id=ds.id, name="q", status=LoraRunStatus.queued,
                          config={}, progress={}, checkpoints=[], sample_paths=[],
                          celery_task_id="celery-abc")
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)

    resp = await client.post(f"/api/lora/runs/{run.id}/stop")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert revoked == ["celery-abc"]


@pytest.mark.asyncio
async def test_stop_requires_active_run(client, db_session):
    from app.db.models import LoraDataset, LoraDatasetStatus, LoraRunStatus, LoraTrainingRun

    ds = LoraDataset(
        name="ds", status=LoraDatasetStatus.ready, preset="drawing_cleanup",
        params={}, source_paths=[], stats={}, preview_paths=[],
    )
    db_session.add(ds)
    await db_session.flush()
    run = LoraTrainingRun(
        dataset_id=ds.id, name="done-run", status=LoraRunStatus.done,
        config={}, progress={}, checkpoints=[], sample_paths=[],
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)

    resp = await client.post(f"/api/lora/runs/{run.id}/stop")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_upload_rejects_unsupported_format(client):
    resp = await client.post(
        "/api/lora/upload",
        files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
    )
    assert resp.status_code == 400


def test_gpu_lock_blocks_local_routes(monkeypatch):
    """AI router: a held training lock must fail local routes fast with the
    human-readable message, and never break when Redis is unavailable."""
    from app.ai import gpu_lock
    from app.ai.router import AIGpuBusyError, ai_router
    from app.ai.schemas import AIRequest, AITask

    model = next(iter(ai_router.registry.models.values()))
    request = AIRequest(task=AITask.CLASSIFICATION, prompt="test")

    monkeypatch.setattr(gpu_lock, "is_locked", lambda: True)
    resolved = type("R", (), {"is_local": True})()
    with pytest.raises(AIGpuBusyError):
        ai_router._enforce_policy(request, model, resolved=resolved)

    # Redis down → check must silently pass, not break routing.
    def boom():
        raise ConnectionError("redis down")

    monkeypatch.setattr(gpu_lock, "is_locked", boom)
    ai_router._enforce_policy(request, model, resolved=resolved)


@pytest.mark.asyncio
async def test_make_workflow_clones_builtin_with_lora(client, db_session, monkeypatch):
    from app.api import lora_training as api
    from app.db.models import (
        ComfyWorkflow,
        LoraDataset,
        LoraDatasetStatus,
        LoraRunStatus,
        LoraTrainingRun,
    )
    from app.db.seeds.comfyui_workflows import seed_builtin_workflows

    await seed_builtin_workflows(db_session)

    async def fake_deploy(run, checkpoint):
        return "my_lora.safetensors"

    monkeypatch.setattr(api, "_deploy", fake_deploy)

    ds = LoraDataset(name="ds", status=LoraDatasetStatus.ready, preset="drawing_cleanup",
                     params={}, source_paths=[], stats={}, preview_paths=[])
    db_session.add(ds)
    await db_session.flush()
    run = LoraTrainingRun(dataset_id=ds.id, name="Моя LoRA", status=LoraRunStatus.done,
                          config={}, progress={}, checkpoints=["ckpt.safetensors"],
                          sample_paths=[])
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)

    resp = await client.post(
        f"/api/lora/runs/{run.id}/make-workflow",
        json={"checkpoint": "ckpt.safetensors", "strength": 0.9},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    wf = await db_session.get(ComfyWorkflow, __import__("uuid").UUID(body["workflow_id"]))
    assert wf.operation == "cleanup" and wf.enabled and not wf.is_builtin
    # Наша LoRA-нода в графе, KSampler переключён на неё, рабочая точка cfg=1/25 шагов.
    lora_nodes = [n for n in wf.graph.values()
                  if n.get("class_type") == "LoraLoaderModelOnly"
                  and n["inputs"].get("lora_name") == "my_lora.safetensors"]
    assert len(lora_nodes) == 1
    assert lora_nodes[0]["inputs"]["strength_model"] == 0.9
    sampler = next(n for n in wf.graph.values() if n.get("class_type") == "KSampler")
    assert sampler["inputs"]["steps"] == 25 and sampler["inputs"]["cfg"] == 1.0
    # Lightning выключена в клоне.
    lightning = next(n for n in wf.graph.values()
                     if n.get("class_type") == "LoraLoaderModelOnly"
                     and "Lightning" in str(n["inputs"].get("lora_name", "")))
    assert lightning["inputs"]["strength_model"] == 0.0
    assert "custom_lora_strength" in wf.params_schema


def test_build_train_config_branches_by_family():
    """qwen → uint3+ARA quant; flux2 → arch flux2 + qfloat8, with the model
    path and control-image sample coming from the catalog."""
    from app.tasks.lora_training import _build_train_config

    qwen = _build_train_config("r1", "/lora-data/datasets/x",
                               {"steps": 500, "base_model": "qwen_image_edit_2511"})
    qmodel = qwen["config"]["process"][0]["model"]
    assert qmodel["arch"] == "qwen_image_edit_plus"
    assert "uint3" in qmodel["qtype"]

    flux = _build_train_config("r2", "/lora-data/datasets/x",
                               {"steps": 500, "base_model": "flux2_klein_9b",
                                "samples": [{"prompt": "p", "ctrl_img_1": "/x.png"}]})
    fmodel = flux["config"]["process"][0]["model"]
    assert fmodel["arch"] == "flux2_klein_9b"  # klein has a size-specific arch
    assert fmodel["name_or_path"] == "black-forest-labs/FLUX.2-klein-base-9B"
    assert fmodel["qtype"] == "qfloat8"
    sample = flux["config"]["process"][0]["sample"]
    assert sample["samples"][0]["ctrl_img_1"] == "/x.png"


def test_safe_lora_filename_blocks_traversal():
    """run.name flows into the deploy filename on the ComfyUI bind mount —
    slashes and dot-dot must not escape the loras dir."""
    from app.api.lora_training import _safe_lora_filename

    out = _safe_lora_filename("../../etc/evil name_run_000001500.safetensors")
    assert "/" not in out and ".." not in out
    assert out.endswith("000001500.safetensors")


class _FakeRedis:
    """Minimal in-memory redis for gpu_lock unit tests (set/nx, get, delete,
    expire) — fakeredis isn't installed."""

    def __init__(self):
        self.store: dict = {}

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)

    def expire(self, key, ttl):
        return key in self.store


def test_gpu_lock_is_exclusive(monkeypatch):
    """acquire() is SET NX: a second run cannot take a held lock, and a
    finisher may only release/refresh its own."""
    from app.ai import gpu_lock

    fake = _FakeRedis()
    monkeypatch.setattr(gpu_lock, "_redis", lambda: fake)

    assert gpu_lock.acquire("run-A") is True
    assert gpu_lock.acquire("run-B") is False  # busy
    assert gpu_lock.holder()["run_id"] == "run-A"

    # run-B must not be able to drop run-A's lock.
    gpu_lock.release("run-B")
    assert gpu_lock.is_locked()
    assert gpu_lock.refresh("run-B") is False

    gpu_lock.release("run-A")
    assert not gpu_lock.is_locked()
    assert gpu_lock.acquire("run-B") is True  # now free


def test_seed_checkpoint_resets_training_step(tmp_path):
    """Resume-seeding must reset training_info.step: ai-toolkit reads it from
    safetensors metadata and instantly finishes runs whose seed step exceeds
    the configured steps (confirmed live with a 2500-step v2 seed)."""
    import json
    import struct

    from app.tasks.lora_training import _seed_checkpoint_with_reset_step

    src = tmp_path / "src.safetensors"
    header = {"__metadata__": {"training_info": json.dumps({"step": 2500, "epoch": 2})}}
    raw = json.dumps(header).encode()
    payload = b"\x00" * 64
    with src.open("wb") as fh:
        fh.write(struct.pack("<Q", len(raw)))
        fh.write(raw)
        fh.write(payload)

    dest = tmp_path / "dest.safetensors"
    _seed_checkpoint_with_reset_step(src, dest)

    with dest.open("rb") as fh:
        n = struct.unpack("<Q", fh.read(8))[0]
        out_header = json.loads(fh.read(n))
        out_payload = fh.read()
    assert json.loads(out_header["__metadata__"]["training_info"])["step"] == 0
    assert out_payload == payload, "tensor bytes must be untouched"
