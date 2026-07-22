"""Contract tests for full-sheet graph recognition and one-to-one drafting."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import ezdxf
import pytest
from pydantic import ValidationError

from app.ai.cad_drawing_graph import (
    DrawingGraphDraftError,
    EngineeringDrawingGraph,
    draft_drawing_graph,
    read_drawing_graph,
    read_drawing_graph_attempt,
    verify_drawing_graph,
    verify_graph_evidence_with_vlm,
)
from app.ai.cad_ir.dxf_render import render_ir_to_dxf


def _graph_payload(*, status: str = "reader_output", assurance: str = "observed") -> dict:
    entity_ids = [
        "contour-1",
        "hole-1",
        "arc-1",
        "polyline-1",
        "text-1",
        "dimension-1",
        "hatch-1",
        "roughness-1",
    ]
    common = {
        "origin": "cv",
        "assurance": assurance,
        "evidence": ["evidence-sheet"],
    }
    relation_assurance = assurance
    return {
        "schema_version": 1,
        "graph_status": status,
        "source": {
            "image_width": 1000,
            "image_height": 800,
            "kind": "scan",
            "sha256": "a" * 64,
        },
        "scale_mm_per_px": 0.25,
        "scale_source": "calibration",
        "evidence": [{
            "id": "evidence-sheet",
            "kind": "pixel_support",
            "region": {"x0": 1, "y0": 1, "x1": 999, "y1": 799},
            "confidence": 0.99,
        }],
        "views": [{
            "id": "view-front",
            "kind": "front",
            "region": {"x0": 50, "y0": 50, "x1": 900, "y1": 700},
            "entity_ids": entity_ids,
            "confidence": 0.98,
            "evidence": ["evidence-sheet"],
        }],
        "entities": [
            {
                "id": "contour-1",
                "type": "segment",
                "p1": {"x": 100, "y": 100},
                "p2": {"x": 500, "y": 100},
                **common,
            },
            {
                "id": "hole-1",
                "type": "circle",
                "center": {"x": 300, "y": 300},
                "radius": 40,
                **common,
            },
            {
                "id": "arc-1",
                "type": "arc",
                "center": {"x": 500, "y": 300},
                "radius": 60,
                "start_angle": 0,
                "end_angle": 90,
                **common,
            },
            {
                "id": "polyline-1",
                "type": "polyline",
                "points": [{"x": 100, "y": 500}, {"x": 200, "y": 550}],
                **common,
            },
            {
                "id": "text-1",
                "type": "text",
                "position": {"x": 150, "y": 650},
                "text": "ДЕТАЛЬ 1",
                "height": 16,
                **common,
            },
            {
                "id": "dimension-1",
                "type": "dimension",
                "kind": "linear",
                "p1": {"x": 100, "y": 150},
                "p2": {"x": 500, "y": 150},
                "text": "100±0,1",
                "value_mm": 100,
                "tolerance": "±0,1",
                **common,
            },
            {
                "id": "hatch-1",
                "type": "hatch",
                "boundary": [
                    {"x": 600, "y": 400},
                    {"x": 800, "y": 400},
                    {"x": 800, "y": 500},
                    {"x": 600, "y": 500},
                ],
                **common,
            },
            {
                "id": "roughness-1",
                "type": "annotation",
                "kind": "roughness",
                "position": {"x": 550, "y": 150},
                "leader": {"x": 500, "y": 100},
                "value": "3.2",
                "text": "Ra 3.2",
                **common,
            },
        ],
        "relations": [
            {
                "id": "rel-dimension-contour",
                "kind": "dimension_applies_to",
                "source_entity_id": "dimension-1",
                "target_entity_ids": ["contour-1"],
                "confidence": 0.97,
                "assurance": relation_assurance,
                "evidence": ["evidence-sheet"],
            },
            {
                "id": "rel-roughness-contour",
                "kind": "annotation_applies_to",
                "source_entity_id": "roughness-1",
                "target_entity_ids": ["contour-1"],
                "confidence": 0.95,
                "assurance": relation_assurance,
                "evidence": ["evidence-sheet"],
            },
        ],
    }


def test_drawing_graph_drafter_preserves_entities_relations_and_ids_in_dxf():
    graph = EngineeringDrawingGraph.model_validate(_graph_payload())
    ir = draft_drawing_graph(graph)

    assert [entity.id for entity in ir.entities] == [
        entity.id for entity in graph.entities
    ]
    assert [relation.id for relation in ir.relations] == [
        relation.id for relation in graph.relations
    ]
    assert ir.counts() == {
        "segment": 1,
        "circle": 1,
        "arc": 1,
        "polyline": 1,
        "text": 1,
        "dimension": 1,
        "hatch": 1,
        "annotation": 1,
    }
    assert ir.recognizer_used == "drawing-graph-drafter-v1"
    assert ir.digitization_status == "review_required"
    assert all(entity.source_region is not None for entity in ir.entities)

    doc = ezdxf.read(io.StringIO(render_ir_to_dxf(ir).decode()))
    modelspace_types = {entity.dxftype() for entity in doc.modelspace()}
    assert {"LINE", "CIRCLE", "ARC", "LWPOLYLINE", "DIMENSION", "HATCH"} <= (
        modelspace_types
    )


def test_verified_graph_can_become_exact_candidate_without_reader_self_certifying():
    graph = EngineeringDrawingGraph.model_validate(
        _graph_payload(status="verified", assurance="constraint_validated")
    )
    verification = verify_drawing_graph(
        graph, pixel_recall=1.0, pixel_precision=1.0
    )
    assert verification.exact_ready is True
    assert draft_drawing_graph(
        graph, verification=verification
    ).digitization_status == "exact_candidate"
    assert draft_drawing_graph(graph).digitization_status == "review_required"

    payload = _graph_payload(assurance="constraint_validated")
    with pytest.raises(ValidationError, match="cannot self-assign"):
        EngineeringDrawingGraph.model_validate(payload)


def test_graph_rejects_dimension_without_geometry_relation():
    payload = _graph_payload()
    payload["relations"] = [payload["relations"][1]]
    with pytest.raises(ValidationError, match="dimensions have no geometry relations"):
        EngineeringDrawingGraph.model_validate(payload)


def test_graph_rejects_missing_evidence_and_out_of_sheet_geometry():
    payload = _graph_payload()
    payload["entities"][0]["evidence"] = []
    with pytest.raises(ValidationError, match="has no source evidence"):
        EngineeringDrawingGraph.model_validate(payload)

    payload = _graph_payload()
    payload["entities"][1]["center"]["x"] = 990
    with pytest.raises(ValidationError, match="outside the source sheet"):
        EngineeringDrawingGraph.model_validate(payload)


def test_graph_rejects_duplicate_view_ownership_and_broken_relation_refs():
    payload = _graph_payload()
    payload["views"].append({
        "id": "view-detail",
        "kind": "detail",
        "region": {"x0": 250, "y0": 250, "x1": 350, "y1": 350},
        "entity_ids": ["hole-1"],
        "confidence": 0.8,
        "evidence": ["evidence-sheet"],
    })
    with pytest.raises(ValidationError, match="exactly one view"):
        EngineeringDrawingGraph.model_validate(payload)

    payload = _graph_payload()
    payload["relations"][0]["target_entity_ids"] = ["missing"]
    with pytest.raises(ValidationError, match="references missing entities"):
        EngineeringDrawingGraph.model_validate(payload)


def test_unresolved_region_blocks_drafting_even_when_graph_is_structurally_valid():
    payload = _graph_payload()
    payload["unresolved_regions"] = [{
        "id": "unresolved-ink-1",
        "region": {"x0": 850, "y0": 100, "x1": 900, "y1": 150},
        "reason": "unvectorized_ink",
    }]
    graph = EngineeringDrawingGraph.model_validate(payload)
    with pytest.raises(DrawingGraphDraftError, match="unresolved-ink-1"):
        draft_drawing_graph(graph)


def test_graph_verifier_rejects_missing_pixel_check_and_dimension_mismatch():
    graph = EngineeringDrawingGraph.model_validate(_graph_payload())
    missing_pixel = verify_drawing_graph(graph)
    assert {issue.code for issue in missing_pixel.blocking} == {
        "GRAPH_PIXEL_CHECK_MISSING"
    }

    payload = _graph_payload()
    payload["entities"][5]["value_mm"] = 12
    mismatch_graph = EngineeringDrawingGraph.model_validate(payload)
    mismatch = verify_drawing_graph(
        mismatch_graph, pixel_recall=1.0, pixel_precision=1.0
    )
    assert "GRAPH_DIMENSION_MISMATCH" in {
        issue.code for issue in mismatch.blocking
    }


class _GraphRouter:
    def __init__(self, payload: dict):
        self.payload = payload
        self.request = None

    async def run(self, request):
        self.request = request
        return SimpleNamespace(
            text=json.dumps(self.payload, ensure_ascii=False),
            provider=SimpleNamespace(value="ollama"),
            model="coordinate-reader-test",
        )


@pytest.mark.asyncio
async def test_graph_reader_uses_dedicated_local_task_and_authoritative_source_metadata():
    from PIL import Image

    image = Image.new("RGB", (1000, 800), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    payload = _graph_payload()
    payload["source"] = {
        "image_width": 1,
        "image_height": 1,
        "kind": "photo",
        "sha256": "0" * 64,
    }
    router = _GraphRouter(payload)

    graph = await read_drawing_graph(buffer.getvalue(), router=router)

    assert graph is not None
    assert graph.source.image_width == 1000 and graph.source.image_height == 800
    assert graph.source.sha256 != "0" * 64
    assert graph.reader_manifest == {
        "task": "cad_drawing_graph_read",
        "provider": "ollama",
        "model": "coordinate-reader-test",
        "contract": "engineering-drawing-graph-v1",
    }
    assert router.request.task.value == "cad_drawing_graph_read"
    assert router.request.confidential is True
    assert router.request.allow_cloud is False
    assert router.request.thinking is False


@pytest.mark.asyncio
async def test_graph_reader_rejects_partial_model_output():
    from PIL import Image

    image = Image.new("RGB", (100, 80), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    router = _GraphRouter({"views": [], "entities": []})
    assert await read_drawing_graph(buffer.getvalue(), router=router) is None

    attempt = await read_drawing_graph_attempt(buffer.getvalue(), router=router)
    assert attempt.valid is False
    assert attempt.raw_text
    assert attempt.raw_sha256
    assert attempt.parsed_payload is not None
    assert attempt.validation_errors


class _EvidenceRouter:
    def __init__(self, *, model: str = "qwen3-vl-crop-verifier"):
        self.model = model
        self.requests = []

    async def run(self, request):
        self.requests.append(request)
        entity_id = request.metadata["entity_id"]
        observed = {
            "text-1": {
                "visible": True,
                "entity_type": "text",
                "text": "ДЕТАЛЬ 1",
                "confidence": 0.99,
            },
            "dimension-1": {
                "visible": True,
                "entity_type": "dimension",
                "text": "100±0,1",
                "value_mm": 100,
                "tolerance": "±0,1",
                "confidence": 0.99,
            },
            "roughness-1": {
                "visible": True,
                "entity_type": "annotation",
                "text": "Ra 3.2",
                "value": "3.2",
                "symbol": None,
                "confidence": 0.99,
            },
        }[entity_id]
        return SimpleNamespace(
            text=json.dumps(observed, ensure_ascii=False),
            provider=SimpleNamespace(value="ollama"),
            model=self.model,
        )


@pytest.mark.asyncio
async def test_vlm_crop_evidence_verifier_is_exact_independent_and_ocr_free():
    from PIL import Image

    image = Image.new("RGB", (1000, 800), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    payload = _graph_payload()
    payload["reader_manifest"] = {"model": "coordinate-reader"}
    graph = EngineeringDrawingGraph.model_validate(payload)
    router = _EvidenceRouter()

    report = await verify_graph_evidence_with_vlm(
        buffer.getvalue(), graph, router=router
    )

    assert report.expected_checks == 3
    assert report.exact_checks == 3
    assert report.complete is True
    assert report.independent is True
    assert report.classic_ocr_used is False
    assert all(check.raw_sha256 for check in report.checks)
    assert all(
        request.task.value == "cad_drawing_graph_evidence_verify"
        and request.thinking is False
        and request.allow_cloud is False
        and len(request.images) == 1
        for request in router.requests
    )
    verification = verify_drawing_graph(
        graph,
        pixel_recall=1.0,
        pixel_precision=1.0,
        vlm_evidence=report,
        require_vlm_evidence=True,
    )
    assert not verification.blocking


@pytest.mark.asyncio
async def test_vlm_crop_evidence_must_use_a_different_model_from_reader():
    from PIL import Image

    image = Image.new("RGB", (1000, 800), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    payload = _graph_payload()
    payload["reader_manifest"] = {"model": "same-vlm"}
    graph = EngineeringDrawingGraph.model_validate(payload)
    report = await verify_graph_evidence_with_vlm(
        buffer.getvalue(), graph, router=_EvidenceRouter(model="same-vlm")
    )

    assert report.complete is True
    assert report.independent is False
    verification = verify_drawing_graph(
        graph,
        pixel_recall=1.0,
        pixel_precision=1.0,
        vlm_evidence=report,
        require_vlm_evidence=True,
    )
    assert {issue.code for issue in verification.blocking} == {
        "GRAPH_VLM_VERIFIER_NOT_INDEPENDENT"
    }
