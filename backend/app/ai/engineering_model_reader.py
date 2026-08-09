"""Focused local VLM pass for unresolved EngineeringModelGraph assertions."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import uuid
from typing import Any, Literal

from PIL import Image
from pydantic import Field, model_validator

from app.ai.schemas import AIRequest, AITask, ChatMessage
from app.domain.engineering_model_graph import (
    Assertion,
    EngineeringModelGraph,
    Evidence,
    ExactValue,
    ReaderPassPlan,
    StrictModel,
)
from app.services.engineering_model_reader import ReaderPassResult
from app.storage import download_file

_FOCUSED_READER_PROMPT = """You read a bounded region of an engineering drawing.
Return only observations that are directly legible in the supplied raster.
Do not infer from standards, typical parts, candidate geometry, or priors.
For every requested assertion return exactly one item. If it is not legible,
use status=unreadable and value=null. Preserve decimal signs and symbols.
The assertion IDs and predicates are identifiers, not proposed values.
"""


class FocusedReadItem(StrictModel):
    assertion_id: str
    status: Literal["observed", "unreadable"]
    value: str | int | float | bool | None = None
    observed_text: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_observation(self) -> FocusedReadItem:
        if self.status == "observed" and self.value is None:
            raise ValueError("observed reading requires value")
        if self.status == "unreadable" and self.value is not None:
            raise ValueError("unreadable reading cannot provide value")
        return self


class FocusedReadResponse(StrictModel):
    readings: list[FocusedReadItem] = Field(min_length=1, max_length=8)


def _source_and_bbox(
    graph: EngineeringModelGraph,
    plan: ReaderPassPlan,
) -> tuple[str, str, tuple[float, float, float, float]]:
    assertions = {item.id: item for item in graph.assertions}
    evidence = {item.id: item for item in graph.evidence}
    sources = {item.id: item for item in graph.sources}
    candidates = [
        evidence[evidence_id]
        for assertion_id in plan.assertion_ids
        if assertion_id in assertions
        for evidence_id in assertions[assertion_id].evidence_ids
        if evidence_id in evidence and evidence[evidence_id].kind == "raster_region"
    ]
    if plan.source_region_ids:
        allowed = set(plan.source_region_ids)
        candidates = [item for item in candidates if item.source_region_id in allowed]
    if not candidates:
        raise ValueError("reader pass has no raster SourceRegion evidence")
    source_ids = {item.source_id for item in candidates}
    if len(source_ids) != 1:
        raise ValueError("reader pass spans more than one source")
    source = sources.get(next(iter(source_ids)))
    if source is None or not source.uri:
        raise ValueError("reader source URI is unavailable")
    boxes = []
    for item in candidates:
        bbox = item.payload.get("bbox_normalized")
        if isinstance(bbox, list) and len(bbox) == 4:
            boxes.append(tuple(float(value) for value in bbox))
            continue
        raw = item.payload.get("bbox")
        if isinstance(raw, dict) and all(key in raw for key in ("x0", "y0", "x1", "y1")):
            # Pixel bboxes are normalized after the image dimensions are known.
            boxes.append((float(raw["x0"]), float(raw["y0"]), float(raw["x1"]), float(raw["y1"])))
    if not boxes:
        raise ValueError("reader SourceRegion has no bbox")
    return (
        source.uri,
        source.sha256,
        (
            min(item[0] for item in boxes),
            min(item[1] for item in boxes),
            max(item[2] for item in boxes),
            max(item[3] for item in boxes),
        ),
    )


def _crop_source(
    content: bytes,
    bbox: tuple[float, float, float, float],
) -> bytes:
    image = Image.open(io.BytesIO(content)).convert("RGB")
    x0, y0, x1, y1 = bbox
    if max(bbox) <= 1.0:
        x0, x1 = x0 * image.width, x1 * image.width
        y0, y1 = y0 * image.height, y1 * image.height
    pad = max(4, int(max(x1 - x0, y1 - y0) * 0.04))
    crop_box = (
        max(0, int(x0) - pad),
        max(0, int(y0) - pad),
        min(image.width, int(x1) + pad),
        min(image.height, int(y1) + pad),
    )
    if crop_box[0] >= crop_box[2] or crop_box[1] >= crop_box[3]:
        raise ValueError("reader SourceRegion bbox is empty")
    crop = image.crop(crop_box)
    crop.thumbnail((1800, 1800))
    output = io.BytesIO()
    crop.save(output, format="PNG")
    return output.getvalue()


async def read_focused_assertions(
    graph: EngineeringModelGraph,
    plan: ReaderPassPlan,
    *,
    router: Any | None = None,
) -> ReaderPassResult:
    """Execute one local no-thinking pass and return an unsealed patch payload."""
    if plan.kind not in {"read_critical", "read_focused"}:
        raise ValueError("focused reader received incompatible pass kind")
    if router is None:
        from app.ai.router import ai_router

        router = ai_router
    source_uri, expected_sha256, bbox = _source_and_bbox(graph, plan)
    content = download_file(source_uri)
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError("reader source hash mismatch")
    crop = _crop_source(content, bbox)
    assertions = {item.id: item for item in graph.assertions}
    requested = [assertions[item_id] for item_id in plan.assertion_ids]
    questions = [
        {
            "assertion_id": item.id,
            "predicate": item.predicate,
            "unit": item.unit,
            "question": plan.questions[index] if index < len(plan.questions) else None,
        }
        for index, item in enumerate(requested)
    ]
    response = await router.run(
        AIRequest(
            task=AITask.CAD_DRAWING_GRAPH_FRAGMENT_READ,
            messages=[
                ChatMessage(role="system", content=_FOCUSED_READER_PROMPT),
                ChatMessage(
                    role="user",
                    content="REQUESTED ASSERTIONS:\n"
                    + json.dumps(
                        questions,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            ],
            images=[base64.b64encode(crop).decode()],
            response_schema=FocusedReadResponse,
            confidential=True,
            allow_cloud=False,
            thinking=False,
            metadata={
                "contract": "engineering-model-focused-reader-v1",
                "num_predict": 2048,
                "graph_id": graph.graph_id,
                "graph_revision": graph.revision,
                "pass_kind": plan.kind,
            },
        )
    )
    parsed = FocusedReadResponse.model_validate(response.data)
    requested_ids = set(plan.assertion_ids)
    returned_ids = [item.assertion_id for item in parsed.readings]
    if len(returned_ids) != len(set(returned_ids)) or set(returned_ids) != requested_ids:
        raise ValueError("focused reader did not return exactly the requested assertions")
    raw_output = response.text or parsed.model_dump_json()
    pass_seed = f"{graph.canonical_sha256}:{','.join(plan.assertion_ids)}"
    pass_id = str(uuid.uuid5(uuid.NAMESPACE_URL, pass_seed))
    raw_evidence_id = f"evidence:reader:{pass_id}"
    raw_evidence = Evidence(
        id=raw_evidence_id,
        kind="model_raw_output",
        payload={
            "model": response.model,
            "provider": response.provider.value,
            "pass_kind": plan.kind,
            "assertion_ids": plan.assertion_ids,
            "raw_output": raw_output,
        },
        sha256=hashlib.sha256(raw_output.encode()).hexdigest(),
    )
    replacements = []
    superseded = []
    for reading in parsed.readings:
        if reading.status != "observed":
            continue
        previous = assertions[reading.assertion_id]
        assertion_seed = f"{pass_id}:{reading.assertion_id}:{reading.value!r}"
        replacements.append(
            Assertion(
                id="assertion:reader:" + str(uuid.uuid5(uuid.NAMESPACE_URL, assertion_seed)),
                subject_id=previous.subject_id,
                predicate=previous.predicate,
                value=ExactValue(kind="exact", value=reading.value),
                unit=previous.unit,
                coordinate_system=previous.coordinate_system,
                origin="observed",
                assurance="observed",
                evidence_ids=[*previous.evidence_ids, raw_evidence_id],
                confidence=reading.confidence,
                impacts=previous.impacts,
                impact_magnitude_percent=previous.impact_magnitude_percent,
                hypothesis_id=previous.hypothesis_id,
                supersedes_assertion_id=previous.id,
            )
        )
        superseded.append(previous.id)
    return ReaderPassResult(
        add_assertions=replacements,
        add_evidence=[raw_evidence],
        supersede_assertion_ids=superseded,
    )
