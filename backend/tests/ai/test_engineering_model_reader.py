import pytest
from PIL import Image

from app.ai.engineering_hybrid_trace import (
    VisualMatchResponse,
    generate_trace_proposals,
    run_hybrid_trace_pass,
)
from app.ai.engineering_model_reader import (
    FocusedReadItem,
    FocusedReadResponse,
    read_focused_assertions,
)
from app.domain.engineering_model_graph import (
    Assertion,
    BuildTarget,
    EngineeringModelGraph,
    Evidence,
    ExactValue,
    GraphNode,
    GraphPatch,
    GraphSource,
    PatchMergeError,
    ReaderPassPlan,
    Requirement,
    UnknownValue,
    apply_graph_patch,
)
from app.services.engineering_model_reader import (
    EngineeringModelReader,
    ReaderPassResult,
)


def _unresolved_graph() -> EngineeringModelGraph:
    return EngineeringModelGraph(
        graph_id="emg:reader-test",
        profile="mechanical",
        nodes=[
            GraphNode(id="docs", type="DocumentSet"),
            GraphNode(id="product", type="Product"),
        ],
        assertions=[
            Assertion(
                id="a-width",
                subject_id="product",
                predicate="envelope.width",
                value=UnknownValue(kind="unknown", reason="unreadable"),
                unit="mm",
                origin="observed",
                assurance="proposed",
                impacts=["envelope"],
            )
        ],
        requirements=[
            Requirement(
                id="r-envelope",
                kind="envelope",
                target_node_ids=["product"],
                assertion_ids=["a-width"],
            )
        ],
        build_targets=[
            BuildTarget(
                id="preview",
                kind="preview_brep",
                root_node_ids=["product"],
                requirement_ids=["r-envelope"],
            )
        ],
    ).sealed()


@pytest.mark.asyncio
async def test_reader_persists_attempts_and_stops_at_fixed_point():
    graph = _unresolved_graph()
    revisions: list[EngineeringModelGraph] = []

    async def persist(patch: GraphPatch) -> EngineeringModelGraph:
        nonlocal graph
        graph = apply_graph_patch(graph, patch)
        revisions.append(graph)
        return graph

    async def no_progress(*_args) -> ReaderPassResult:
        return ReaderPassResult(add_evidence=[Evidence(
            id=f"raw-unreadable-{len(revisions)}",
            kind="model_raw_output",
            payload={"status": "unreadable"},
        )])

    outcome = await EngineeringModelReader().run(
        graph,
        target_id="preview",
        persist_patch=persist,
        read_pass=no_progress,
    )

    assert outcome.stop_reason == "fixed_point_no_progress"
    assert outcome.partial is True
    assert outcome.passes_completed == 2
    assert len(revisions) == 2
    assert graph.reader_manifest.calls_used == 2
    assert graph.reader_manifest.ordinary_attempts == {"a-width": 2}
    assert graph.reader_manifest.stop_reason == "fixed_point_no_progress"


@pytest.mark.asyncio
async def test_reader_resolves_frontier_through_atomic_patch():
    graph = _unresolved_graph()

    async def persist(patch: GraphPatch) -> EngineeringModelGraph:
        nonlocal graph
        graph = apply_graph_patch(graph, patch)
        return graph

    async def resolve(_graph, _plan) -> ReaderPassResult:
        return ReaderPassResult(
            add_assertions=[
                Assertion(
                    id="a-width-read",
                    subject_id="product",
                    predicate="envelope.width",
                    value=ExactValue(kind="exact", value=40.0),
                    unit="mm",
                    origin="observed",
                    assurance="observed",
                    confidence=0.9,
                    impacts=["envelope"],
                    supersedes_assertion_id="a-width",
                )
            ],
            supersede_assertion_ids=["a-width"],
        )

    outcome = await EngineeringModelReader().run(
        graph,
        target_id="preview",
        persist_patch=persist,
        read_pass=resolve,
    )

    assert outcome.stop_reason == "frontier_resolved"
    assert outcome.partial is False
    assert graph.revision == 1
    assert graph.reader_manifest.calls_used == 1
    assert next(item for item in graph.assertions if item.id == "a-width").state == "superseded"
    assert next(item for item in graph.assertions if item.id == "a-width-read").state == "active"


@pytest.mark.asyncio
async def test_reader_error_is_a_terminal_partial_revision():
    graph = _unresolved_graph()

    async def persist(patch: GraphPatch) -> EngineeringModelGraph:
        nonlocal graph
        graph = apply_graph_patch(graph, patch)
        return graph

    async def fail(*_args) -> ReaderPassResult:
        raise RuntimeError("model unavailable")

    outcome = await EngineeringModelReader().run(
        graph,
        target_id="preview",
        persist_patch=persist,
        read_pass=fail,
    )

    assert outcome.stop_reason == "reader_error"
    assert outcome.partial is True
    assert graph.revision == 1
    assert graph.reader_manifest.calls_used == 1
    assert graph.reader_manifest.stop_reason == "reader_error"


@pytest.mark.asyncio
async def test_reader_cannot_smuggle_validated_assertion_in_terminal_result():
    graph = _unresolved_graph()

    async def persist(patch: GraphPatch) -> EngineeringModelGraph:
        nonlocal graph
        graph = apply_graph_patch(graph, patch)
        return graph

    async def malicious_stop(*_args) -> ReaderPassResult:
        return ReaderPassResult(
            add_assertions=[Assertion(
                id="forged-approved",
                subject_id="product",
                predicate="envelope.width",
                value=ExactValue(kind="exact", value=999),
                origin="observed",
                assurance="constraint_validated",
            )],
            stop_reason="frontier_resolved",
        )

    outcome = await EngineeringModelReader().run(
        graph,
        target_id="preview",
        persist_patch=persist,
        read_pass=malicious_stop,
    )

    assert outcome.stop_reason == "reader_error"
    assert all(item.id != "forged-approved" for item in graph.assertions)


def test_reader_runtime_metadata_cannot_be_forged_by_human_patch():
    graph = _unresolved_graph()
    patch = GraphPatch(
        patch_id="forged",
        base_revision=graph.revision,
        base_sha256=graph.canonical_sha256,
        producer="human",
        pass_id="forged",
        idempotency_key="forged",
        reader_call_count=1,
        reader_attempt_assertion_ids=["a-width"],
    )

    with pytest.raises(
        PatchMergeError,
        match="reader_runtime_metadata_requires_reader_or_system",
    ):
        apply_graph_patch(graph, patch)


@pytest.mark.asyncio
async def test_focused_reader_uses_exact_roi_and_forces_no_thinking(monkeypatch):
    import io
    from types import SimpleNamespace

    image = Image.new("RGB", (100, 80), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    content = buffer.getvalue()
    graph = (
        _unresolved_graph()
        .model_copy(
            update={
                "canonical_sha256": "",
                "sources": [
                    GraphSource(
                        id="source",
                        uri="normalized.png",
                        sha256=__import__("hashlib").sha256(content).hexdigest(),
                        media_type="image/png",
                    )
                ],
                "nodes": [
                    *_unresolved_graph().nodes,
                    GraphNode(id="region", type="SourceRegion"),
                ],
                "evidence": [
                    Evidence(
                        id="raster",
                        kind="raster_region",
                        source_id="source",
                        source_region_id="region",
                        payload={"bbox_normalized": [0.1, 0.2, 0.5, 0.7]},
                    )
                ],
                "assertions": [
                    _unresolved_graph()
                    .assertions[0]
                    .model_copy(update={"evidence_ids": ["raster"]})
                ],
            }
        )
        .sealed()
    )
    captured = {}

    class Router:
        async def run(self, request):
            captured["request"] = request
            return SimpleNamespace(
                data=FocusedReadResponse(
                    readings=[
                        FocusedReadItem(
                            assertion_id="a-width",
                            status="observed",
                            value=40.0,
                            observed_text="40",
                            confidence=0.91,
                        )
                    ]
                ),
                text='{"readings": [{"assertion_id": "a-width"}]}',
                model="apex-reader",
                provider=SimpleNamespace(value="ollama"),
            )

    monkeypatch.setattr(
        "app.ai.engineering_model_reader.download_file",
        lambda _path: content,
    )
    result = await read_focused_assertions(
        graph,
        ReaderPassPlan(
            kind="read_critical",
            assertion_ids=["a-width"],
            questions=["Read width"],
            reason="test",
        ),
        router=Router(),
    )

    request = captured["request"]
    assert request.thinking is False
    assert request.confidential is True
    assert request.allow_cloud is False
    assert request.metadata["contract"] == "engineering-model-focused-reader-v1"
    assert len(request.images) == 1
    assert result.supersede_assertion_ids == ["a-width"]
    assert result.add_assertions[0].value.value == 40.0
    assert result.add_assertions[0].origin == "observed"
    assert result.add_assertions[0].assurance == "observed"
    assert result.add_evidence[0].payload["model"] == "apex-reader"


def _traceable_graph(content: bytes) -> EngineeringModelGraph:
    import hashlib

    graph = EngineeringModelGraph(
        graph_id="emg:trace-test",
        profile="mechanical",
        sources=[
            GraphSource(
                id="source",
                uri="localized.png",
                sha256=hashlib.sha256(content).hexdigest(),
                media_type="image/png",
            )
        ],
        nodes=[
            GraphNode(id="docs", type="DocumentSet"),
            GraphNode(id="product", type="Product"),
            GraphNode(id="region", type="SourceRegion"),
        ],
        assertions=[
            Assertion(
                id="a-contour",
                subject_id="product",
                predicate="decorative.contour",
                value=UnknownValue(kind="unknown", reason="local shape"),
                origin="observed",
                assurance="proposed",
                evidence_ids=["roi"],
                impacts=["visual_only"],
            ),
            Assertion(
                id="a-scale",
                subject_id="docs",
                predicate="scale.mm_per_px",
                value=ExactValue(kind="exact", value=0.5),
                unit="mm",
                origin="observed",
                assurance="observed",
                confidence=0.9,
            ),
        ],
        evidence=[
            Evidence(
                id="roi",
                kind="raster_region",
                source_id="source",
                source_region_id="region",
                payload={
                    "bbox": {"x0": 10, "y0": 10, "x1": 71, "y1": 66},
                    "anchors_px": [[10, 10], [50, 10]],
                    "excluded_bboxes": [],
                    "dimensions_mm": {"width_mm": 20, "height_mm": 17.5},
                },
            )
        ],
        build_targets=[
            BuildTarget(
                id="preview",
                kind="preview_brep",
                root_node_ids=["product"],
            )
        ],
    )
    return graph.sealed()


@pytest.mark.asyncio
async def test_hybrid_trace_stays_in_roi_and_requires_independent_match(monkeypatch):
    from types import SimpleNamespace

    import cv2
    import numpy as np

    image = np.full((80, 90, 3), 255, dtype=np.uint8)
    cv2.rectangle(image, (20, 20), (60, 55), (0, 0, 0), 2)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    content = encoded.tobytes()
    graph = _traceable_graph(content)
    monkeypatch.setattr(
        "app.ai.engineering_hybrid_trace.download_file",
        lambda _path: content,
    )
    crop, proposals = generate_trace_proposals(graph, graph.assertions[0])
    assert crop.startswith(b"\x89PNG")
    assert 1 <= len(proposals) <= 3
    assert all(item.source_bbox == (10.0, 10.0, 71.0, 66.0) for item in proposals)

    class Router:
        async def run(self, request):
            assert request.thinking is False
            assert request.confidential is True
            assert len(request.images) == 4
            return SimpleNamespace(
                data=VisualMatchResponse(
                    verdict="match",
                    element_count=1,
                    element_types=["closed contour"],
                    shape_matches=True,
                    position_matches=True,
                    orientation_matches=True,
                    connectivity_matches=True,
                    confidence=0.95,
                ),
                text="visual match",
                model="independent-verifier",
                provider=SimpleNamespace(value="ollama"),
            )

    result = await run_hybrid_trace_pass(
        graph,
        ReaderPassPlan(
            kind="hybrid_trace",
            assertion_ids=["a-contour"],
            source_region_ids=["region"],
            reason="test",
        ),
        router=Router(),
    )

    assert result.stop_reason is None
    assert result.model_calls_used >= 1
    assert result.add_assertions[0].origin == "traced"
    assert result.add_assertions[0].assurance == "corroborated"
    assert result.supersede_assertion_ids == ["a-contour"]
    assert all(item.admission.accepted for item in result.evaluations)


@pytest.mark.asyncio
async def test_hybrid_trace_rejects_verifier_that_reuses_reader_model(monkeypatch):
    from types import SimpleNamespace

    import cv2
    import numpy as np

    image = np.full((80, 90, 3), 255, dtype=np.uint8)
    cv2.rectangle(image, (20, 20), (60, 55), (0, 0, 0), 2)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    content = encoded.tobytes()
    original = _traceable_graph(content)
    graph = original.model_copy(update={
        "canonical_sha256": "",
        "evidence": [
            *original.evidence,
            Evidence(
                id="reader-raw",
                kind="model_raw_output",
                payload={"model": "shared-model"},
            ),
        ],
    }).sealed()
    monkeypatch.setattr(
        "app.ai.engineering_hybrid_trace.download_file",
        lambda _path: content,
    )

    class Router:
        async def run(self, _request):
            return SimpleNamespace(
                data=VisualMatchResponse(verdict="match", confidence=0.99),
                text="claimed match",
                model="shared-model",
                provider=SimpleNamespace(value="ollama"),
            )

    result = await run_hybrid_trace_pass(
        graph,
        ReaderPassPlan(
            kind="hybrid_trace",
            assertion_ids=["a-contour"],
            source_region_ids=["region"],
            reason="test",
        ),
        router=Router(),
    )

    assert result.stop_reason == "hybrid_trace_exhausted"
    assert result.add_assertions == []
    assert all(item.visual.verdict == "unreadable" for item in result.evaluations)
    assert all(not item.admission.accepted for item in result.evaluations)
