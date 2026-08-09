"""Fail-closed local ROI tracing with independent visual verification."""

from __future__ import annotations

import base64
import hashlib
import io
import math
import uuid
from typing import Any, Literal

import cv2
import numpy as np
from PIL import Image
from pydantic import Field

from app.ai.schemas import AIRequest, AITask, ChatMessage
from app.domain.engineering_model_graph import (
    Assertion,
    DeterministicTraceChecks,
    EngineeringModelGraph,
    Evidence,
    ExactValue,
    HypothesisOption,
    HypothesisSet,
    ReaderPassPlan,
    StrictModel,
    TraceAdmission,
    TracePrimitive,
    TraceProposal,
    VisualDifference,
    VisualVerification,
    critical_assertion_ids,
    evaluate_trace_admission,
    is_hybrid_trace_candidate,
)
from app.services.engineering_model_reader import ReaderPassResult
from app.storage import download_file

_VISUAL_PROMPT = """Compare the source crop with the candidate contour render.
Images are: source, candidate, overlay, difference mask. Judge only visible
shape, position, orientation, element count and connectivity. Engineering
dimensions and proposed parameter values are intentionally absent. Return the
strict structured verdict. Use unreadable when the source cannot decide.
"""


class VisualMatchDifference(StrictModel):
    kind: Literal["shape", "position", "orientation", "connectivity", "count", "other"]
    bbox: tuple[float, float, float, float]
    detail: str


class VisualMatchResponse(StrictModel):
    verdict: Literal["match", "mismatch", "unreadable"]
    element_count: int | None = None
    element_types: list[str] = Field(default_factory=list)
    shape_matches: bool | None = None
    position_matches: bool | None = None
    orientation_matches: bool | None = None
    connectivity_matches: bool | None = None
    differences: list[VisualMatchDifference] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class TraceEvaluation(StrictModel):
    proposal: TraceProposal
    visual: VisualVerification
    admission: TraceAdmission


class HybridTracePassResult(ReaderPassResult):
    evaluations: list[TraceEvaluation] = Field(default_factory=list, exclude=True)
    selected_proposal_id: str | None = Field(default=None, exclude=True)


def _active_scale(graph: EngineeringModelGraph) -> float:
    candidates = [
        item
        for item in graph.assertions
        if item.state == "active"
        and item.predicate == "scale.mm_per_px"
        and item.value.kind == "exact"
        and item.assurance in {"observed", "corroborated", "constraint_validated", "human_approved"}
        and isinstance(item.value.value, (int, float))
        and not isinstance(item.value.value, bool)
        and float(item.value.value) > 0
    ]
    if len(candidates) != 1:
        raise ValueError("hybrid trace requires one confirmed mm/px scale")
    return float(candidates[0].value.value)


def _localized_region(
    graph: EngineeringModelGraph,
    assertion: Assertion,
) -> tuple[Evidence, str, bytes, tuple[int, int, int, int]]:
    evidence_by_id = {item.id: item for item in graph.evidence}
    source_by_id = {item.id: item for item in graph.sources}
    regions = [
        evidence_by_id[item_id]
        for item_id in assertion.evidence_ids
        if item_id in evidence_by_id
        and evidence_by_id[item_id].kind == "raster_region"
        and not evidence_by_id[item_id].payload.get("fallback")
    ]
    if len(regions) != 1:
        raise ValueError("hybrid trace requires exactly one localized SourceRegion")
    region = regions[0]
    source = source_by_id.get(region.source_id)
    if source is None or not source.uri:
        raise ValueError("hybrid trace source URI is unavailable")
    content = download_file(source.uri)
    if hashlib.sha256(content).hexdigest() != source.sha256:
        raise ValueError("hybrid trace source hash mismatch")
    image = Image.open(io.BytesIO(content))
    raw_bbox = region.payload.get("bbox")
    if not isinstance(raw_bbox, dict) or not all(
        key in raw_bbox for key in ("x0", "y0", "x1", "y1")
    ):
        raise ValueError("hybrid trace requires a pixel bbox")
    bbox = (
        max(0, int(raw_bbox["x0"])),
        max(0, int(raw_bbox["y0"])),
        min(image.width, int(raw_bbox["x1"])),
        min(image.height, int(raw_bbox["y1"])),
    )
    if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        raise ValueError("hybrid trace SourceRegion bbox is empty")
    return region, source.uri, content, bbox


def _segment_intersects(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> bool:
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    return orient(a, b, c) * orient(a, b, d) < 0 and orient(c, d, a) * orient(c, d, b) < 0


def _self_intersects(points: list[tuple[float, float]]) -> bool:
    segments = list(zip(points, points[1:] + points[:1], strict=True))
    for first, (a, b) in enumerate(segments):
        for second, (c, d) in enumerate(segments):
            if abs(first - second) <= 1 or {first, second} == {0, len(segments) - 1}:
                continue
            if _segment_intersects(a, b, c, d):
                return True
    return False


def _mask_exclusions(
    ink: np.ndarray,
    region: Evidence,
) -> tuple[np.ndarray, list[tuple[int, int, int, int]]]:
    cleaned = ink.copy()
    forbidden = []
    for raw in region.payload.get("excluded_bboxes") or []:
        if not isinstance(raw, (list, tuple)) or len(raw) != 4:
            continue
        x0, y0, x1, y1 = (int(value) for value in raw)
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(cleaned.shape[1], x1), min(cleaned.shape[0], y1)
        if x0 < x1 and y0 < y1:
            cleaned[y0:y1, x0:x1] = 0
            forbidden.append((x0, y0, x1, y1))
    # The sheet frame is never geometry inside a local ROI.
    cleaned[:2, :] = 0
    cleaned[-2:, :] = 0
    cleaned[:, :2] = 0
    cleaned[:, -2:] = 0
    return cleaned, forbidden


def _proposal_checks(
    points: list[tuple[float, float]],
    ink: np.ndarray,
    *,
    anchors: list[tuple[float, float]],
    forbidden: list[tuple[int, int, int, int]],
    scale: float,
    dimensions: dict[str, float],
) -> DeterministicTraceChecks:
    rendered = np.zeros_like(ink)
    cv_points = np.array(points, dtype=np.int32).reshape((-1, 1, 2))
    cv2.polylines(rendered, [cv_points], True, 255, 2, cv2.LINE_AA)
    rendered_mask = rendered > 0
    ink_mask = ink > 0
    tolerance_kernel = np.ones((3, 3), dtype=np.uint8)
    ink_support = cv2.dilate(ink_mask.astype(np.uint8), tolerance_kernel) > 0
    rendered_support = cv2.dilate(rendered_mask.astype(np.uint8), tolerance_kernel) > 0
    precision = float((rendered_mask & ink_support).sum() / max(1, rendered_mask.sum()))
    recall = float((ink_mask & rendered_support).sum() / max(1, ink_mask.sum()))
    anchors_ok = all(min(math.dist(anchor, point) for point in points) <= 5.0 for anchor in anchors)
    forbidden_clear = not any(
        x0 <= point[0] <= x1 and y0 <= point[1] <= y1
        for point in points
        for x0, y0, x1, y1 in forbidden
    )
    width_mm = (max(point[0] for point in points) - min(point[0] for point in points)) * scale
    height_mm = (max(point[1] for point in points) - min(point[1] for point in points)) * scale
    dimensions_ok = all(
        abs(actual - expected) <= max(2.0 * scale, abs(expected) * 0.01)
        for name, actual in (("width_mm", width_mm), ("height_mm", height_mm))
        if (expected := dimensions.get(name)) is not None
    )
    return DeterministicTraceChecks(
        connected=True,
        closed=len(points) >= 3,
        no_self_intersections=not _self_intersects(points),
        no_dangling_ends=True,
        anchors_satisfied=anchors_ok,
        dimensions_satisfied=dimensions_ok,
        forbidden_geometry_clear=forbidden_clear,
        pixel_precision=max(0.0, min(1.0, precision)),
        pixel_recall=max(0.0, min(1.0, recall)),
    )


def generate_trace_proposals(
    graph: EngineeringModelGraph,
    assertion: Assertion,
) -> tuple[bytes, list[TraceProposal]]:
    """Generate at most three deterministic contours inside one safe ROI."""
    scale = _active_scale(graph)
    region, _uri, content, bbox = _localized_region(graph, assertion)
    anchors = [
        (float(item[0]), float(item[1]))
        for item in region.payload.get("anchors_px") or []
        if isinstance(item, (list, tuple)) and len(item) == 2
    ]
    if not anchors:
        raise ValueError("hybrid trace requires confirmed anchors")
    dimensions = {
        str(key): float(value)
        for key, value in (region.payload.get("dimensions_mm") or {}).items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    image = Image.open(io.BytesIO(content)).convert("L").crop(bbox)
    gray = np.array(image)
    _threshold, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    ink, forbidden = _mask_exclusions(ink, region)
    contours, _hierarchy = cv2.findContours(ink, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    eligible = [
        contour
        for contour in contours
        if cv2.contourArea(contour) >= 25
        and all(cv2.pointPolygonTest(contour, anchor, True) >= -5 for anchor in anchors)
    ]
    if not eligible:
        empty_buffer = io.BytesIO()
        image.save(empty_buffer, format="PNG")
        return empty_buffer.getvalue(), []
    contour = max(eligible, key=cv2.contourArea)
    perimeter = cv2.arcLength(contour, True)
    proposals = []
    seen = set()
    for rank, epsilon_ratio in enumerate((0.004, 0.008, 0.015), start=1):
        approximated = cv2.approxPolyDP(contour, perimeter * epsilon_ratio, True)
        points = [(float(item[0][0]), float(item[0][1])) for item in approximated]
        signature = tuple((round(x, 3), round(y, 3)) for x, y in points)
        if len(points) < 3 or signature in seen:
            continue
        seen.add(signature)
        checks = _proposal_checks(
            points,
            ink,
            anchors=anchors,
            forbidden=forbidden,
            scale=scale,
            dimensions=dimensions,
        )
        proposal_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{graph.canonical_sha256}:{assertion.id}:{rank}:{signature}",
            )
        )
        proposals.append(
            TraceProposal(
                id=proposal_id,
                source_region_id=str(region.source_region_id),
                hypothesis_id=f"hypothesis:trace:{proposal_id}",
                primitives=[
                    TracePrimitive(
                        kind="polyline",
                        parameters={
                            "points": [
                                coordinate * scale for point in points for coordinate in point
                            ],
                        },
                    )
                ],
                trace_parameters={
                    "epsilon_ratio": epsilon_ratio,
                    "scale_mm_per_px": scale,
                    "anchors_px": anchors,
                },
                source_bbox=tuple(float(value) for value in bbox),
                uncertainty=max(
                    0.0, min(1.0, 1.0 - min(checks.pixel_precision, checks.pixel_recall))
                ),
                checks=checks,
            )
        )
    crop_buffer = io.BytesIO()
    image.save(crop_buffer, format="PNG")
    return crop_buffer.getvalue(), proposals[:3]


def _comparison_images(crop_png: bytes, proposal: TraceProposal) -> list[bytes]:
    source = Image.open(io.BytesIO(crop_png)).convert("RGB")
    points_mm = proposal.primitives[0].parameters["points"]
    scale = float(proposal.trace_parameters["scale_mm_per_px"])
    points = np.array(
        [
            [points_mm[index] / scale, points_mm[index + 1] / scale]
            for index in range(0, len(points_mm), 2)
        ],
        dtype=np.int32,
    ).reshape((-1, 1, 2))
    candidate = np.zeros((source.height, source.width, 3), dtype=np.uint8)
    cv2.polylines(candidate, [points], True, (255, 255, 255), 2, cv2.LINE_AA)
    source_np = np.array(source)
    overlay = source_np.copy()
    cv2.polylines(overlay, [points], True, (255, 0, 0), 2, cv2.LINE_AA)
    gray = cv2.cvtColor(source_np, cv2.COLOR_RGB2GRAY)
    _threshold, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    difference = cv2.absdiff(ink, cv2.cvtColor(candidate, cv2.COLOR_RGB2GRAY))
    images = [source_np, candidate, overlay, cv2.cvtColor(difference, cv2.COLOR_GRAY2RGB)]
    encoded = []
    for item in images:
        buffer = io.BytesIO()
        Image.fromarray(item).save(buffer, format="PNG")
        encoded.append(buffer.getvalue())
    return encoded


async def _verify_proposal(
    graph: EngineeringModelGraph,
    crop_png: bytes,
    proposal: TraceProposal,
    *,
    router: Any,
) -> VisualVerification:
    images = _comparison_images(crop_png, proposal)
    response = await router.run(
        AIRequest(
            task=AITask.CAD_DRAWING_GRAPH_EVIDENCE_VERIFY,
            messages=[ChatMessage(role="user", content=_VISUAL_PROMPT)],
            images=[base64.b64encode(item).decode() for item in images],
            response_schema=VisualMatchResponse,
            confidential=True,
            allow_cloud=False,
            thinking=False,
            metadata={
                "contract": "engineering-hybrid-visual-verifier-v1",
                "num_predict": 1024,
                "proposal_id": proposal.id,
            },
        )
    )
    parsed = VisualMatchResponse.model_validate(response.data)
    reader_models = {
        str(item.payload.get("model"))
        for item in graph.evidence
        if item.kind == "model_raw_output" and item.payload.get("model")
    }
    verdict = "unreadable" if response.model in reader_models else parsed.verdict
    raw_output = response.text or parsed.model_dump_json()
    return VisualVerification(
        proposal_id=proposal.id,
        verdict=verdict,
        element_count=parsed.element_count,
        element_types=parsed.element_types,
        shape_matches=parsed.shape_matches,
        position_matches=parsed.position_matches,
        orientation_matches=parsed.orientation_matches,
        connectivity_matches=parsed.connectivity_matches,
        differences=[VisualDifference(**item.model_dump()) for item in parsed.differences],
        confidence=parsed.confidence,
        raw_output=raw_output,
        verifier_model=response.model,
    )


async def run_hybrid_trace_pass(
    graph: EngineeringModelGraph,
    plan: ReaderPassPlan,
    *,
    router: Any | None = None,
) -> HybridTracePassResult:
    if plan.kind != "hybrid_trace" or len(plan.assertion_ids) != 1:
        raise ValueError("hybrid trace requires one planned assertion")
    assertions = {item.id: item for item in graph.assertions}
    assertion = assertions[plan.assertion_ids[0]]
    if not is_hybrid_trace_candidate(graph, assertion):
        return HybridTracePassResult(model_calls_used=0, stop_reason="hybrid_trace_not_allowed")
    critical = (
        set().union(*(critical_assertion_ids(graph, target.id) for target in graph.build_targets))
        if graph.build_targets
        else set()
    )
    if assertion.id in critical:
        return HybridTracePassResult(model_calls_used=0, stop_reason="critical_unresolved")
    crop_png, proposals = generate_trace_proposals(graph, assertion)
    if not proposals:
        return HybridTracePassResult(model_calls_used=0, stop_reason="hybrid_trace_no_proposal")
    if router is None:
        from app.ai.router import ai_router

        router = ai_router
    remaining_calls = max(
        0,
        graph.reader_manifest.max_model_calls - graph.reader_manifest.calls_used,
    )
    evaluations = []
    calls_used = 0
    for proposal in proposals[: min(3, remaining_calls)]:
        if proposal.checks.passed:
            visual = await _verify_proposal(graph, crop_png, proposal, router=router)
            calls_used += 1
        else:
            visual = VisualVerification(
                proposal_id=proposal.id,
                verdict="unreadable",
                confidence=0.0,
                raw_output="deterministic checks failed before visual verification",
                verifier_model="deterministic-gate",
            )
        admission = evaluate_trace_admission(
            proposal,
            visual,
            assertion_is_non_critical=True,
            conflicts_with_validated=assertion.assurance
            in {"constraint_validated", "human_approved"},
        )
        evaluations.append(
            TraceEvaluation(
                proposal=proposal,
                visual=visual,
                admission=admission,
            )
        )
    accepted = sorted(
        (item for item in evaluations if item.admission.accepted),
        key=lambda item: (-item.admission.score, item.proposal.id),
    )
    if not accepted:
        return HybridTracePassResult(
            evaluations=evaluations,
            model_calls_used=calls_used,
            stop_reason="hybrid_trace_exhausted",
        )
    winner = accepted[0]
    trace_evidence_id = f"evidence:trace:{winner.proposal.id}"
    visual_evidence_id = f"evidence:visual:{winner.proposal.id}"
    replacement_id = f"assertion:trace:{winner.proposal.id}"
    replacement = Assertion(
        id=replacement_id,
        subject_id=assertion.subject_id,
        predicate=assertion.predicate,
        value=ExactValue(
            kind="exact",
            value={
                "primitives": [item.model_dump(mode="json") for item in winner.proposal.primitives]
            },
        ),
        unit=assertion.unit,
        coordinate_system=assertion.coordinate_system,
        origin="traced",
        assurance="corroborated",
        evidence_ids=[*assertion.evidence_ids, trace_evidence_id, visual_evidence_id],
        confidence=winner.admission.score,
        impacts=assertion.impacts,
        hypothesis_id=winner.proposal.hypothesis_id,
        supersedes_assertion_id=assertion.id,
    )
    return HybridTracePassResult(
        add_assertions=[replacement],
        add_evidence=[
            Evidence(
                id=trace_evidence_id,
                kind="trace_run",
                source_region_id=winner.proposal.source_region_id,
                payload=winner.proposal.model_dump(mode="json"),
            ),
            Evidence(
                id=visual_evidence_id,
                kind="visual_verification",
                source_region_id=winner.proposal.source_region_id,
                payload=winner.visual.model_dump(mode="json"),
            ),
        ],
        add_hypothesis_options=[
            HypothesisOption(
                id=winner.proposal.hypothesis_id,
                assertion_ids=[replacement_id],
                hard_constraints_satisfied=True,
                evidence_coverage=winner.admission.score,
                cross_view_consistency=winner.visual.confidence,
                hybrid_trace_score=winner.admission.score,
            )
        ],
        add_hypothesis_sets=[
            HypothesisSet(
                id=f"hypothesis-set:trace:{winner.proposal.id}",
                option_ids=[winner.proposal.hypothesis_id],
                selected_option_id=winner.proposal.hypothesis_id,
            )
        ],
        supersede_assertion_ids=[assertion.id],
        evaluations=evaluations,
        model_calls_used=calls_used,
        selected_proposal_id=winner.proposal.id,
    )
