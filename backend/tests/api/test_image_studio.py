"""Integration tests for the image studio API (/api/image-gen)."""

from __future__ import annotations

import hashlib
import io
from types import SimpleNamespace

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
async def test_design_history_restore_queues_revision_safe_rebuild(
    client, db_session, monkeypatch,
):
    from app.ai.cad_ir.feature_tree import Feature3D, FeatureTreeCandidate
    from app.db.models import ImageGeneration, ImageGenStatus
    from app.services.engineering_model_graph import persist_feature_tree_revision

    objects: dict[str, bytes] = {}
    monkeypatch.setattr(
        "app.services.engineering_model_graph.upload_file",
        lambda content, path, _content_type: objects.setdefault(path, content),
    )
    monkeypatch.setattr(
        "app.services.engineering_model_graph.download_file",
        lambda path: objects[path],
    )
    queued: list[tuple[list, str]] = []
    monkeypatch.setattr(
        "app.tasks.cad_trace.restore_design_revision.apply_async",
        lambda args, queue: (
            queued.append((args, queue)) or SimpleNamespace(id="restore-task")
        ),
    )

    generation = ImageGeneration(
        owner_sub="dev-user",
        operation="vectorize",
        status=ImageGenStatus.done,
        params={"spec": {"part": "plate"}},
        source_image_paths=[],
    )
    db_session.add(generation)
    await db_session.flush()
    graph_id = f"image-generation:{generation.id}:design"
    base = FeatureTreeCandidate(
        features=[Feature3D(kind="extrude", params={"depth_mm": 10.0})],
        score=1.0,
        label="base plate",
    )
    row0 = await persist_feature_tree_revision(
        db_session,
        graph_id=graph_id,
        spec=generation.params["spec"],
        candidate=base,
        producer="system",
        pass_id="design-fork:test",
        idempotency_key="design-history-base",
    )
    revised = base.model_copy(deep=True)
    revised.features.append(
        Feature3D(kind="boss", params={
            "profile": "circle", "diameter_mm": 4.0, "depth_mm": 2.0,
            "center_x_mm": 0.0, "center_y_mm": 0.0,
        })
    )
    row1 = await persist_feature_tree_revision(
        db_session,
        graph_id=graph_id,
        spec=generation.params["spec"],
        candidate=revised,
        producer="human",
        pass_id="human-add-feature:boss",
        idempotency_key="design-history-edit",
        expected_base_revision=row0.revision,
        expected_base_sha256=row0.canonical_sha256,
        decision_note="Добавить бобышку",
        actor_sub="dev-user",
    )
    generation.params = {
        **generation.params,
        "engineering_model_graph": {
            "revision_id": str(row1.id),
            "graph_id": graph_id,
            "revision": row1.revision,
            "canonical_sha256": row1.canonical_sha256,
        },
    }
    await db_session.commit()

    history = await client.get(
        f"/api/image-gen/{generation.id}/model-graph/design-history"
    )
    assert history.status_code == 200, history.text
    assert history.json()["current_revision"] == 1
    assert [item["revision"] for item in history.json()["revisions"]] == [0, 1]

    restore = await client.post(
        f"/api/image-gen/{generation.id}/model-graph/design-history/restore",
        json={
            "target_revision": 0,
            "note": "Откатить ошибочно добавленную бобышку",
            "idempotency_key": "restore-design-history-r0",
        },
    )
    assert restore.status_code == 200, restore.text
    assert restore.json()["rebuild_task_id"] == "restore-task"
    assert queued == [([
        str(generation.id), 0, "Откатить ошибочно добавленную бобышку",
        "restore-design-history-r0", "dev-user", 1, row1.canonical_sha256,
    ], "celery")]

    current = await client.post(
        f"/api/image-gen/{generation.id}/model-graph/design-history/restore",
        json={
            "target_revision": 1,
            "note": "Нельзя восстанавливать текущую",
            "idempotency_key": "restore-current-design-r1",
        },
    )
    assert current.status_code == 409


@pytest.mark.asyncio
async def test_failed_digitization_exposes_owned_model_graph_as_review_required(
    client, db_session, monkeypatch,
):
    from app.db.models import ImageGeneration, ImageGenStatus
    from app.domain.engineering_model_graph import (
        Assertion,
        BuildTarget,
        DeterministicTraceChecks,
        EngineeringModelGraph,
        Evidence,
        GraphSource,
        GraphNode,
        TracePrimitive,
        TraceProposal,
        UnknownValue,
    )
    from app.db.models import TraceProposalRecord
    from app.services.engineering_model_graph import persist_pipeline_graph
    from PIL import Image

    objects: dict[str, bytes] = {}
    source_buffer = io.BytesIO()
    Image.new("RGB", (100, 80), "white").save(source_buffer, format="PNG")
    source_bytes = source_buffer.getvalue()
    source_path = f"image-gen/dev-user/{id(source_bytes)}.png"
    monkeypatch.setattr(
        "app.services.engineering_model_graph.upload_file",
        lambda content, path, _content_type: objects.setdefault(path, content),
    )
    monkeypatch.setattr(
        "app.services.engineering_model_graph.download_file",
        lambda path: objects[path],
    )
    monkeypatch.setattr(
        "app.api.image_generation.download_file",
        lambda path: source_bytes if path == source_path else objects[path],
    )
    rebuild_calls: list[tuple[list[str], str]] = []
    monkeypatch.setattr(
        "app.tasks.cad_trace.rebuild_from_spec.apply_async",
        lambda args, queue: (
            rebuild_calls.append((args, queue))
            or SimpleNamespace(id="rebuild-task")
        ),
    )
    generation = ImageGeneration(
        owner_sub="dev-user",
        operation="vectorize",
        status=ImageGenStatus.failed,
        params={
            "spec": {
                "part": "Blocked shaft",
                "hole": {"diameter_mm": None},
                "length_mm": 100,
                "outer_diameter_mm": 30,
            },
        },
        source_image_paths=[source_path],
        error="critical dimensions unresolved",
    )
    db_session.add(generation)
    await db_session.flush()
    graph = EngineeringModelGraph(
        graph_id=f"image-generation:{generation.id}",
        profile="mechanical",
        sources=[GraphSource(
            id="source:sheet",
            uri=source_path,
            sha256=hashlib.sha256(source_bytes).hexdigest(),
            media_type="image/png",
        )],
        nodes=[
            GraphNode(id="docs", type="DocumentSet"),
            GraphNode(id="product:legacy-spec", type="Product", name="Blocked shaft"),
            GraphNode(id="region:whole", type="SourceRegion"),
        ],
        assertions=[Assertion(
            id="assertion:hole-diameter",
            subject_id="product:legacy-spec",
            predicate="hole.diameter_mm",
            value=UnknownValue(kind="unknown", reason="unreadable"),
            unit="mm",
            origin="observed",
            assurance="proposed",
            evidence_ids=["evidence:whole"],
            confidence=0.2,
            impacts=["connection_opening"],
        ), Assertion(
            id="assertion:length",
            subject_id="product:legacy-spec",
            predicate="length_mm",
            value=UnknownValue(kind="unknown", reason="needs review"),
            unit="mm", origin="observed", assurance="proposed",
            evidence_ids=["evidence:whole"], confidence=0.5,
            impacts=["envelope"],
        ), Assertion(
            id="assertion:outer-diameter",
            subject_id="product:legacy-spec",
            predicate="outer_diameter_mm",
            value=UnknownValue(kind="unknown", reason="needs review"),
            unit="mm", origin="observed", assurance="proposed",
            evidence_ids=["evidence:whole"], confidence=0.5,
            impacts=["envelope"],
        )],
        evidence=[Evidence(
            id="evidence:whole",
            kind="raster_region",
            source_id="source:sheet",
            source_region_id="region:whole",
            payload={"bbox_normalized": [0, 0, 1, 1], "fallback": True},
        )],
        build_targets=[BuildTarget(
            id="preview", kind="preview_brep", root_node_ids=["product:legacy-spec"]
        )],
    ).sealed()
    row = await persist_pipeline_graph(db_session, graph)
    proposal = TraceProposal(
        id="trace-test",
        source_region_id="region:whole",
        hypothesis_id="hypothesis:trace-test",
        primitives=[TracePrimitive(
            kind="polyline",
            parameters={"points": [10, 10, 40, 10, 40, 30, 10, 30]},
        )],
        trace_parameters={"scale_mm_per_px": 1.0},
        source_bbox=(0, 0, 100, 80),
        uncertainty=0.1,
        checks=DeterministicTraceChecks(
            connected=True,
            closed=True,
            no_self_intersections=True,
            no_dangling_ends=True,
            anchors_satisfied=True,
            dimensions_satisfied=True,
            forbidden_geometry_clear=True,
            pixel_precision=0.9,
            pixel_recall=0.9,
        ),
    )
    db_session.add(TraceProposalRecord(
        graph_revision_id=row.id,
        proposal_id=proposal.id,
        source_region_id=proposal.source_region_id,
        assertion_id="assertion:hole-diameter",
        rank=1,
        status="accepted",
        score=0.9,
        payload=proposal.model_dump(mode="json"),
    ))
    generation.params = {
        **generation.params,
        "engineering_model_graph": {"revision_id": str(row.id)},
    }
    await db_session.commit()

    response = await client.get(f"/api/image-gen/{generation.id}/model-graph")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(row.id)
    assert body["source_generation_status"] == "failed"
    assert body["workflow_status"] == "review_required"
    assert any(item.get("name") == "Blocked shaft" for item in body["graph"]["nodes"])

    downloaded = await client.get(
        f"/api/image-gen/{generation.id}/model-graph/download"
    )
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith(
        "application/vnd.ptsai.emg+json"
    )
    assert downloaded.headers["x-engineering-graph-sha256"] == row.canonical_sha256
    assert downloaded.headers["x-engineering-graph-revision"] == "0"
    assert downloaded.headers["content-disposition"].endswith('.emg.json"')
    assert downloaded.json()["canonical_sha256"] == row.canonical_sha256

    trace_overlay = await client.get(
        f"/api/image-gen/{generation.id}/model-graph/assertions/"
        "assertion:hole-diameter/source-overlay?mode=overlay&proposal_id=trace-test"
    )
    assert trace_overlay.status_code == 200, trace_overlay.text
    assert trace_overlay.headers["x-trace-proposal-id"] == "trace-test"
    with Image.open(io.BytesIO(trace_overlay.content)) as overlay_image:
        assert overlay_image.size == (100, 80)

    verification = await client.post(
        f"/api/image-gen/{generation.id}/model-graph/verify"
    )
    assert verification.status_code == 200
    assert verification.json()["workflow_status"] == "review_required"

    correction_body = {
        "value": {"kind": "exact", "value": 15.7},
        "note": "Размер подтверждён по выноске Ø15.7",
        "idempotency_key": "human-test-hole-15-7",
        "source_bbox_normalized": [0.1, 0.25, 0.4, 0.5],
        "rebuild": True,
    }
    correction = await client.post(
        f"/api/image-gen/{generation.id}/model-graph/assertions/"
        "assertion:hole-diameter/corrections",
        json=correction_body,
    )
    assert correction.status_code == 200, correction.text
    revised = correction.json()
    assert revised["revision"] == 1
    assert revised["compatibility_spec_updated"] is True
    assert revised["rebuild_task_id"] == "rebuild-task"
    dependency = revised["dependency_validation"]
    assert dependency["status"] == "passed"
    assert dependency["scope"] == "dependency_graph"
    assert dependency["geometry_validated"] is False
    assert dependency["changed_assertion_ids"] == [replacement_id := next(
        item["id"] for item in revised["graph"]["assertions"]
        if item.get("supersedes_assertion_id") == "assertion:hole-diameter"
    )]
    assert dependency["requires_kernel_rebuild"] is True
    assert dependency["validation_errors"] == []
    assert len(rebuild_calls) == 1
    assert rebuild_calls[0][0][0] == str(generation.id)
    assert rebuild_calls[0][0][1].startswith("human-graph:")
    assert rebuild_calls[0][1] == "celery"
    await db_session.refresh(generation)
    assert generation.params["spec_corrected"]["hole"]["diameter_mm"] == 15.7
    old = next(
        item for item in revised["graph"]["assertions"]
        if item["id"] == "assertion:hole-diameter"
    )
    replacement = next(
        item for item in revised["graph"]["assertions"]
        if item.get("supersedes_assertion_id") == "assertion:hole-diameter"
    )
    assert old["state"] == "superseded"
    assert replacement["origin"] == "human"
    assert replacement["assurance"] == "human_approved"
    assert replacement["value"] == {"kind": "exact", "value": 15.7}
    assert replacement["id"] == replacement_id
    raster = next(
        item for item in revised["graph"]["evidence"]
        if item["id"] in replacement["evidence_ids"]
        and item["kind"] == "raster_region"
    )
    assert raster["payload"]["bbox_normalized"] == [0.1, 0.25, 0.4, 0.5]
    assert raster["payload"]["fallback"] is False

    overlay = await client.get(
        f"/api/image-gen/{generation.id}/model-graph/assertions/"
        f"{replacement['id']}/source-overlay?mode=source"
    )
    assert overlay.status_code == 200
    assert overlay.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(overlay.content)) as crop:
        assert crop.size == (30, 20)

    sheet = await client.get(
        f"/api/image-gen/{generation.id}/model-graph/assertions/"
        f"{replacement['id']}/source-overlay?mode=sheet"
    )
    assert sheet.status_code == 200
    with Image.open(io.BytesIO(sheet.content)) as full_sheet:
        assert full_sheet.size == (100, 80)

    batch = await client.post(
        f"/api/image-gen/{generation.id}/model-graph/corrections",
        json={
            "corrections": [
                {
                    "assertion_id": "assertion:length",
                    "value": {"kind": "exact", "value": 120},
                    "source_bbox_normalized": [0.1, 0.1, 0.5, 0.2],
                },
                {
                    "assertion_id": "assertion:outer-diameter",
                    "value": {"kind": "exact", "value": 32},
                    "source_bbox_normalized": [0.5, 0.1, 0.8, 0.2],
                },
            ],
            "note": "Связанная размерная цепь подтверждена инженером",
            "idempotency_key": "human-batch-length-diameter",
        },
    )
    assert batch.status_code == 200, batch.text
    batch_body = batch.json()
    assert batch_body["revision"] == 2
    assert batch_body["corrected_assertion_ids"] == [
        "assertion:length", "assertion:outer-diameter",
    ]
    assert batch_body["dependency_validation"]["status"] == "passed"
    assert len(batch_body["dependency_validation"]["changed_assertion_ids"]) == 2
    assert batch_body["dependency_validation"]["geometry_validated"] is False
    assert batch_body["compatibility_spec_updated"] is True
    assert len([
        item for item in batch_body["graph"]["assertions"]
        if item.get("origin") == "human" and item.get("state") == "active"
    ]) == 3
    duplicate_batch = await client.post(
        f"/api/image-gen/{generation.id}/model-graph/corrections",
        json={
            "corrections": [{
                "assertion_id": "assertion:length",
                "value": {"kind": "exact", "value": 120},
            }],
            "note": "Повтор",
            "idempotency_key": "human-batch-length-diameter",
        },
    )
    assert duplicate_batch.status_code in {404, 409}

    patches = await client.get(f"/api/image-gen/{generation.id}/model-graph/patches")
    assert patches.status_code == 200
    assert patches.json()[0]["producer"] == "human"
    assert patches.json()[0]["accepted"] is True

    duplicate = await client.post(
        f"/api/image-gen/{generation.id}/model-graph/assertions/"
        f"{replacement['id']}/corrections",
        json=correction_body,
    )
    assert duplicate.status_code == 409

    other = ImageGeneration(
        owner_sub="another-user",
        operation="vectorize",
        status=ImageGenStatus.failed,
    )
    db_session.add(other)
    await db_session.commit()
    denied = await client.get(f"/api/image-gen/{other.id}/model-graph")
    assert denied.status_code == 404
    denied_download = await client.get(
        f"/api/image-gen/{other.id}/model-graph/download"
    )
    assert denied_download.status_code == 404


async def _seed_feature_correction_graph(db_session, monkeypatch):
    """One Feature node (Ф1.2, descriptive) whose feature.param./feature.kind
    assertions correspond to a real spec leaf — the fixture the two tests
    below use to prove a Feature-graph correction reaches the compatibility
    spec (and, from there, the existing rebuild) while feature.kind does not.
    """
    from app.db.models import ImageGeneration, ImageGenStatus
    from app.domain.engineering_model_graph import (
        Assertion,
        BuildTarget,
        EngineeringModelGraph,
        ExactValue,
        GraphNode,
        GraphSource,
    )
    from app.services.engineering_model_graph import persist_pipeline_graph

    objects: dict[str, bytes] = {}
    monkeypatch.setattr(
        "app.services.engineering_model_graph.upload_file",
        lambda content, path, _content_type: objects.setdefault(path, content),
    )
    monkeypatch.setattr(
        "app.services.engineering_model_graph.download_file",
        lambda path: objects[path],
    )
    monkeypatch.setattr(
        "app.api.image_generation.download_file",
        lambda path: objects[path],
    )
    generation = ImageGeneration(
        owner_sub="dev-user",
        operation="vectorize",
        status=ImageGenStatus.done,
        params={"spec": {"main_view": {"chamfers": [
            {"id": "0:chamfers:0", "size_mm": 1.0, "location": "left_end"},
        ]}}},
    )
    db_session.add(generation)
    await db_session.flush()
    graph = EngineeringModelGraph(
        graph_id=f"image-generation:{generation.id}",
        profile="mechanical",
        sources=[GraphSource(id="source:sheet", sha256="a" * 64, media_type="image/png")],
        nodes=[
            GraphNode(id="docs", type="DocumentSet"),
            GraphNode(id="product:legacy-spec", type="Product", name="Bracket"),
            GraphNode(id="feature:0:chamfers:0", type="Feature", name="chamfer 0:chamfers:0"),
        ],
        assertions=[Assertion(
            id="assertion:chamfer-size",
            subject_id="feature:0:chamfers:0",
            predicate="feature.param.size_mm",
            value=ExactValue(kind="exact", value=1.0),
            unit="mm", origin="observed", assurance="proposed", confidence=0.6,
        ), Assertion(
            id="assertion:chamfer-kind",
            subject_id="feature:0:chamfers:0",
            predicate="feature.kind",
            value=ExactValue(kind="exact", value="chamfer"),
            origin="observed", assurance="proposed", confidence=0.6,
        )],
        build_targets=[BuildTarget(
            id="preview", kind="preview_brep", root_node_ids=["product:legacy-spec"]
        )],
    ).sealed()
    row = await persist_pipeline_graph(db_session, graph)
    generation.params = {
        **generation.params,
        "engineering_model_graph": {"revision_id": str(row.id)},
    }
    await db_session.commit()
    return generation


@pytest.mark.asyncio
async def test_feature_param_correction_mirrors_into_compat_spec_and_rebuilds(
    client, db_session, monkeypatch,
):
    """Ф2.6b: correcting a Ф1.2 Feature's own feature.param.<name> assertion
    — not just product:legacy-spec — now reaches the compatibility spec
    (via feature_spec_path) and can trigger the existing rebuild, closing
    the gap where the descriptive graph never affected compiled geometry."""
    from types import SimpleNamespace

    generation = await _seed_feature_correction_graph(db_session, monkeypatch)
    rebuild_calls: list[tuple[list[str], str]] = []
    monkeypatch.setattr(
        "app.tasks.cad_trace.rebuild_from_spec.apply_async",
        lambda args, queue: (
            rebuild_calls.append((args, queue)) or SimpleNamespace(id="rebuild-task")
        ),
    )

    correction = await client.post(
        f"/api/image-gen/{generation.id}/model-graph/assertions/"
        "assertion:chamfer-size/corrections",
        json={
            "value": {"kind": "exact", "value": 1.6},
            "note": "Уточнено по выноске 1.6×45°",
            "idempotency_key": "human-test-chamfer-size",
            "rebuild": True,
        },
    )
    assert correction.status_code == 200, correction.text
    body = correction.json()
    assert body["compatibility_spec_updated"] is True
    assert body["rebuild_task_id"] == "rebuild-task"
    assert len(rebuild_calls) == 1
    await db_session.refresh(generation)
    assert (
        generation.params["spec_corrected"]["main_view"]["chamfers"][0]["size_mm"] == 1.6
    )


@pytest.mark.asyncio
async def test_feature_kind_correction_cannot_rebuild(client, db_session, monkeypatch):
    """feature.kind names which LIST an item lives in, not a leaf value — it
    has no compatibility-spec mirror, so rebuild must fail loudly (422),
    never silently no-op past the human's stated intent."""
    generation = await _seed_feature_correction_graph(db_session, monkeypatch)

    correction = await client.post(
        f"/api/image-gen/{generation.id}/model-graph/assertions/"
        "assertion:chamfer-kind/corrections",
        json={
            "value": {"kind": "exact", "value": "fillet"},
            "note": "Похоже на скругление, не фаску",
            "idempotency_key": "human-test-chamfer-kind",
            "rebuild": True,
        },
    )
    assert correction.status_code == 422
    assert "feature.kind" in correction.text


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
async def test_vectorize_from_description_does_not_require_source_image(client):
    resp = await client.post(
        "/api/image-gen/generate",
        json={
            "operation": "vectorize",
            "prompt": "Пластина 120×80×10 мм с четырьмя отверстиями Ø10",
            "params": {"vectorize_method": "text_spec", "sheet_format": "A3"},
            "source_image_paths": [],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["operation"] == "vectorize"
    assert body["source_image_paths"] == []
    assert body["prompt"].startswith("Пластина")


@pytest.mark.asyncio
async def test_graph_description_method_requires_source_sheet(client):
    resp = await client.post(
        "/api/image-gen/generate",
        json={
            "operation": "vectorize",
            "prompt": "",
            "params": {"vectorize_method": "spec", "sheet_format": "A3"},
            "source_image_paths": [],
        },
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
async def test_cad_model_outputs_are_loaded_on_demand_not_in_generation_poll(
    client, db_session
):
    from app.db.models import ImageGeneration, ImageGenStatus

    gen = ImageGeneration(
        owner_sub="dev-user",
        operation="vectorize",
        status=ImageGenStatus.failed,
        params={
            "cad_process": {"events": [{"sequence": 1}]},
            "cad_partial_spec": {"main_view": {"outer": []}},
            "cad_model_outputs": [{
                "id": "model-output-1",
                "sequence": 1,
                "at": "2026-08-08T00:00:00Z",
                "stage": "reader.fragment.question",
                "answer": '{"outer":[]}',
            }],
        },
        source_image_paths=[],
    )
    db_session.add(gen)
    await db_session.commit()
    await db_session.refresh(gen)

    polled = await client.get(f"/api/image-gen/{gen.id}")
    assert polled.status_code == 200
    assert "cad_model_outputs" not in polled.json()["params"]
    assert "cad_partial_spec" not in polled.json()["params"]
    assert polled.json()["params"]["cad_process"]["events"]

    audit = await client.get(f"/api/image-gen/{gen.id}/cad-model-outputs")
    assert audit.status_code == 200
    assert audit.json()["count"] == 1
    assert audit.json()["outputs"][0]["answer"] == '{"outer":[]}'


@pytest.mark.asyncio
async def test_iterate_edit_does_not_inherit_cleanup_workflow(client, db_session, monkeypatch):
    from app.db.models import ComfyWorkflow, ImageGeneration, ImageGenStatus

    monkeypatch.setattr("app.api.image_generation._enqueue", lambda generation_id: None)

    cleanup_wf = ComfyWorkflow(
        key="cleanup_test",
        title="Cleanup test",
        category="cleanup",
        operation="cleanup",
        graph={"1": {"class_type": "Text", "inputs": {"text": "cleanup"}}},
        inject_map={"prompt": {"node": "1", "input": "text"}},
        params_schema={},
        enabled=True,
        is_builtin=True,
    )
    edit_wf = ComfyWorkflow(
        key="edit_test",
        title="Edit test",
        category="edit",
        operation="edit",
        graph={"1": {"class_type": "Text", "inputs": {"text": ""}}},
        inject_map={"prompt": {"node": "1", "input": "text"}},
        params_schema={},
        enabled=True,
        is_builtin=True,
    )
    db_session.add_all([cleanup_wf, edit_wf])
    await db_session.flush()
    parent = ImageGeneration(
        owner_sub="dev-user",
        operation="cleanup",
        workflow_id=cleanup_wf.id,
        status=ImageGenStatus.done,
        prompt="cleanup parent",
        params={},
        source_image_paths=[],
        result_path="image-gen/dev-user/parent.png",
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    resp = await client.post(
        f"/api/image-gen/{parent.id}/iterate",
        json={"operation": "edit", "prompt": "убери дерево и забор"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["operation"] == "edit"
    assert body["prompt"] == "убери дерево и забор"
    assert body["workflow_id"] is None


@pytest.mark.asyncio
async def test_iterate_edit_requires_prompt(client, db_session, monkeypatch):
    from app.db.models import ImageGeneration, ImageGenStatus

    monkeypatch.setattr("app.api.image_generation._enqueue", lambda generation_id: None)

    parent = ImageGeneration(
        owner_sub="dev-user",
        operation="cleanup",
        status=ImageGenStatus.done,
        prompt="cleanup parent",
        params={},
        source_image_paths=[],
        result_path="image-gen/dev-user/parent.png",
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    resp = await client.post(
        f"/api/image-gen/{parent.id}/iterate",
        json={"operation": "edit", "prompt": "   "},
    )

    assert resp.status_code == 400
    assert "prompt" in resp.json()["detail"]


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
async def test_spec_corrections_are_distinct_audited_rebuild_events(
    monkeypatch,
):
    from types import SimpleNamespace
    from uuid import UUID

    from app.api.image_generation import SpecCorrectionRequest, correct_vectorize_spec
    from app.tasks.cad_trace import rebuild_from_spec

    queued: list[tuple[list[object], str]] = []

    def queue_rebuild(*, args, queue):
        queued.append((args, queue))
        return SimpleNamespace(id=f"task-{len(queued)}")

    monkeypatch.setattr(rebuild_from_spec, "apply_async", queue_rebuild)
    read_spec = {
        "schema_version": 1,
        "part": "Фланец",
        "main_view": {
            "type": "фланец",
            "profile": {
                "shape": "circle",
                "diameter_mm": 100,
                "thickness_mm": 12,
                "holes": [],
            },
        },
        "title_block": {"material": "Сталь 20", "scale": "1:1"},
    }
    gen = SimpleNamespace(
        owner_sub="dev-user",
        params={"spec": read_spec},
    )

    class Session:
        async def get(self, *_args):
            return gen

        async def commit(self):
            return None

    generation_id = UUID("00000000-0000-0000-0000-000000000123")
    first = await correct_vectorize_spec(
        generation_id,
        SpecCorrectionRequest(material="Сталь 45", rebuild=True),
        Session(),
        _user("dev-user"),
    )
    second = await correct_vectorize_spec(
        generation_id,
        SpecCorrectionRequest(material="Сталь 20", rebuild=True),
        Session(),
        _user("dev-user"),
    )

    first_event = first["correction_event_id"]
    second_event = second["correction_event_id"]
    assert first_event != second_event
    assert queued == [
        ([str(generation_id), first_event], "celery"),
        ([str(generation_id), second_event], "celery"),
    ]
    assert gen.params["spec_correction_event_id"] == second_event
    assert [
        item["correction_event_id"]
        for item in gen.params["spec_correction_history"]
    ] == [first_event, second_event]
    assert gen.params["spec_corrected"]["title_block"]["material"] == "Сталь 20"


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
