#!/usr/bin/env python3
"""FastAPI inference server for the neural CAD vectorizer.

POST /vectorize takes a binarized ink PNG (same contract as
``cad_recognize.cv.CvRecognizer.recognize`` — uint8, 255=ink) and returns a
list of CAD IR entity dicts, greedy-decoded and rescaled back to the input's
own resolution. It does NOT decide truth: every entity comes back with
``origin="neural"``/``assurance="inferred"`` — the caller's verifier
(``cad_recognize/verify.py``) scores it against the source raster exactly
like the CV backend, and arbitration/promotion to higher assurance happens
there, never inside this service.

Runs in its own container (GPU, isolated deps) and is looked up like any
other provider node — see ``cad_recognize/neural.py`` for the client side.
"""

from __future__ import annotations

import io
import logging
import os
import pathlib
import sys

import numpy as np
import torch
from fastapi import FastAPI, File, HTTPException, Response, UploadFile
from PIL import Image
from pydantic import BaseModel

from evidence_model import EvidenceHeatmapModel
from directional_decode import decode_line_segments
from directional_model import DirectionalFieldModel
from edge_verifier import EdgeVerifier, decode_verified_edges
from model import IMG_SIZE, CadVectorizerModel
from multi_type_dataset import SUBTYPE_NAMES as MULTI_SUBTYPE_NAMES
from multi_type_dataset import TYPE_NAMES as MULTI_TYPE_NAMES
from multi_type_model import MultiTypeProposalModel
from primitive_dataset import LINE_CLASSES, TYPE_NAMES, WIDTH_CLASSES
from primitive_model import PrimitiveSetModel
from sheet_layout_dataset import VIEW_NAMES
from sheet_layout_model import SheetLayoutModel

app = FastAPI(title="cad-vectorizer")
logger = logging.getLogger("cad_vectorizer")

_MODEL: CadVectorizerModel | None = None
_PRIMITIVE_MODEL: PrimitiveSetModel | None = None
_SHEET_LAYOUT_MODEL: SheetLayoutModel | None = None
_EVIDENCE_MODEL: EvidenceHeatmapModel | None = None
_DIRECTIONAL_MODEL: DirectionalFieldModel | None = None
_EDGE_VERIFIER: EdgeVerifier | None = None
_MULTI_TYPE_MODEL: MultiTypeProposalModel | None = None
_DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
_CHECKPOINT = pathlib.Path(os.environ.get("CAD_VECTORIZER_CHECKPOINT", "/models/best.pt"))
_PRIMITIVE_CHECKPOINT = pathlib.Path(
    os.environ.get("CAD_PRIMITIVE_CHECKPOINT", "/models/primitive-best.pt")
)
_SHEET_LAYOUT_CHECKPOINT = pathlib.Path(
    os.environ.get("CAD_SHEET_LAYOUT_CHECKPOINT", "/models/sheet-layout-best.pt")
)
_EVIDENCE_CHECKPOINT = pathlib.Path(
    os.environ.get("CAD_EVIDENCE_CHECKPOINT", "/models/evidence-best.pt")
)
_DIRECTIONAL_CHECKPOINT = pathlib.Path(
    os.environ.get("CAD_DIRECTIONAL_CHECKPOINT", "/models/directional-best.pt")
)
_EDGE_VERIFIER_CHECKPOINT = pathlib.Path(
    os.environ.get("CAD_EDGE_VERIFIER_CHECKPOINT", "/models/edge-verifier-best.pt")
)
_MULTI_TYPE_CHECKPOINT = pathlib.Path(
    os.environ.get("CAD_MULTI_TYPE_CHECKPOINT", "/models/multi-type-best.pt")
)


class EntityOut(BaseModel):
    type: str
    line_class: str
    width_class: str
    confidence: float
    origin: str = "neural"
    assurance: str = "inferred"
    p1: dict | None = None
    p2: dict | None = None
    center: dict | None = None
    radius: float | None = None
    start_angle: float | None = None
    end_angle: float | None = None
    points: list[dict] | None = None
    closed: bool | None = None
    position: dict | None = None
    text: str | None = None
    height: float | None = None
    rotation: float | None = None
    kind: str | None = None
    value_mm: float | None = None
    tolerance: str | None = None
    boundary: list[dict] | None = None
    holes: list[list[dict]] | None = None
    pattern: str | None = None
    value: str | None = None
    symbol: str | None = None
    datum_refs: list[str] | None = None
    leader: dict | None = None


class VectorizeResponse(BaseModel):
    entities: list[EntityOut]
    model_step: int | None = None
    layout_step: int | None = None
    view_regions: int | None = None


def _load_model() -> CadVectorizerModel:
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    model = CadVectorizerModel().to(_DEVICE)
    if _CHECKPOINT.exists():
        state = torch.load(_CHECKPOINT, map_location=_DEVICE)
        model.load_state_dict(state["model"])
        app.state.model_step = state.get("step")
    else:
        app.state.model_step = None
    model.eval()
    _MODEL = model
    return model


def _load_primitive_model() -> PrimitiveSetModel | None:
    global _PRIMITIVE_MODEL
    if _PRIMITIVE_MODEL is not None:
        return _PRIMITIVE_MODEL
    if not _PRIMITIVE_CHECKPOINT.exists():
        return None
    state = torch.load(_PRIMITIVE_CHECKPOINT, map_location=_DEVICE)
    model = PrimitiveSetModel().to(_DEVICE)
    model.load_state_dict(state["model"])
    model.eval()
    app.state.primitive_model_step = state.get("step")
    _PRIMITIVE_MODEL = model
    return model


def _load_sheet_layout_model() -> SheetLayoutModel | None:
    global _SHEET_LAYOUT_MODEL
    if _SHEET_LAYOUT_MODEL is not None:
        return _SHEET_LAYOUT_MODEL
    if not _SHEET_LAYOUT_CHECKPOINT.exists():
        return None
    state = torch.load(_SHEET_LAYOUT_CHECKPOINT, map_location=_DEVICE)
    model = SheetLayoutModel().to(_DEVICE)
    model.load_state_dict(state["model"])
    model.eval()
    app.state.sheet_layout_model_step = state.get("step")
    _SHEET_LAYOUT_MODEL = model
    return model


def _load_evidence_model() -> EvidenceHeatmapModel | None:
    global _EVIDENCE_MODEL
    if _EVIDENCE_MODEL is not None:
        return _EVIDENCE_MODEL
    if not _EVIDENCE_CHECKPOINT.exists():
        return None
    state = torch.load(_EVIDENCE_CHECKPOINT, map_location=_DEVICE)
    model = EvidenceHeatmapModel().to(_DEVICE)
    model.load_state_dict(state["model"])
    model.eval()
    app.state.evidence_model_step = state.get("step")
    _EVIDENCE_MODEL = model
    return model


def _load_directional_model() -> DirectionalFieldModel | None:
    global _DIRECTIONAL_MODEL
    if _DIRECTIONAL_MODEL is not None:
        return _DIRECTIONAL_MODEL
    if not _DIRECTIONAL_CHECKPOINT.exists():
        return None
    state = torch.load(_DIRECTIONAL_CHECKPOINT, map_location=_DEVICE)
    model = DirectionalFieldModel().to(_DEVICE)
    model.load_state_dict(state["model"])
    model.eval()
    app.state.directional_model_step = state.get("step")
    _DIRECTIONAL_MODEL = model
    return model


def _load_edge_verifier() -> EdgeVerifier | None:
    global _EDGE_VERIFIER
    if _EDGE_VERIFIER is not None:
        return _EDGE_VERIFIER
    if not _EDGE_VERIFIER_CHECKPOINT.exists():
        return None
    state = torch.load(_EDGE_VERIFIER_CHECKPOINT, map_location=_DEVICE)
    model = EdgeVerifier().to(_DEVICE)
    model.load_state_dict(state["model"])
    model.eval()
    app.state.edge_verifier_epoch = state.get("epoch")
    app.state.edge_verifier_validation = state.get("validation_entity", {})
    _EDGE_VERIFIER = model
    return model


def _load_multi_type_model() -> MultiTypeProposalModel | None:
    global _MULTI_TYPE_MODEL
    if _MULTI_TYPE_MODEL is not None:
        return _MULTI_TYPE_MODEL
    if not _MULTI_TYPE_CHECKPOINT.exists():
        return None
    state = torch.load(_MULTI_TYPE_CHECKPOINT, map_location=_DEVICE)
    if state.get("architecture") != "multi-type-proposal-v2":
        raise RuntimeError("multi-type checkpoint architecture mismatch")
    model = MultiTypeProposalModel(**state.get("model_config", {})).to(_DEVICE)
    model.load_state_dict(state["model"])
    model.eval()
    app.state.multi_type_model_step = state.get("step")
    app.state.multi_type_validation = state.get("validation", {})
    _MULTI_TYPE_MODEL = model
    return model


@app.on_event("startup")
def _startup() -> None:
    _load_model()
    _load_primitive_model()
    _load_sheet_layout_model()
    _load_evidence_model()
    _load_directional_model()
    _load_edge_verifier()
    _load_multi_type_model()


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "device": str(_DEVICE),
        "checkpoint": str(_CHECKPOINT),
        "checkpoint_loaded": _CHECKPOINT.exists(),
        "primitive_checkpoint": str(_PRIMITIVE_CHECKPOINT),
        "primitive_checkpoint_loaded": _PRIMITIVE_CHECKPOINT.exists(),
        "sheet_layout_checkpoint": str(_SHEET_LAYOUT_CHECKPOINT),
        "sheet_layout_checkpoint_loaded": _SHEET_LAYOUT_CHECKPOINT.exists(),
        "evidence_checkpoint": str(_EVIDENCE_CHECKPOINT),
        "evidence_checkpoint_loaded": _EVIDENCE_CHECKPOINT.exists(),
        "directional_checkpoint": str(_DIRECTIONAL_CHECKPOINT),
        "directional_checkpoint_loaded": _DIRECTIONAL_CHECKPOINT.exists(),
        "edge_verifier_checkpoint": str(_EDGE_VERIFIER_CHECKPOINT),
        "edge_verifier_checkpoint_loaded": _EDGE_VERIFIER_CHECKPOINT.exists(),
        "multi_type_checkpoint": str(_MULTI_TYPE_CHECKPOINT),
        "multi_type_checkpoint_loaded": _MULTI_TYPE_CHECKPOINT.exists(),
        "multi_type_validation": getattr(app.state, "multi_type_validation", None),
    }


_LC_NAMES = ("contour", "axis", "dim", "hatch", "hidden", "thin")
_WC_NAMES = ("main", "thin")


def _rows_to_entities(rows, image_width: float, image_height: float) -> list[EntityOut]:
    from app.ai.cad_ir.sequence import COMMANDS  # lazy, see neural.py for the same trick

    out: list[EntityOut] = []
    open_points: list[dict] | None = None
    open_kind: str | None = None
    open_closed = False

    def unit(value: float) -> float:
        # The regression head is unconstrained.  Coordinates/radii/normalized
        # angles outside the training domain must never escape the image or
        # create physically impossible geometry.
        return min(max(float(value), 0.0), 1.0)

    def flush():
        nonlocal open_points, open_kind
        if open_points is None:
            return
        if open_kind == "PLN" and len(open_points) >= 2:
            out.append(EntityOut(
                type="polyline", line_class="contour", width_class="main",
                confidence=0.55, points=open_points, closed=open_closed,
            ))
        elif open_kind == "HAT" and len(open_points) >= 3:
            out.append(EntityOut(
                type="hatch", line_class="hatch", width_class="thin",
                confidence=0.5, points=open_points,
            ))
        open_points = None

    for cmd_idx, params, lc_idx, wc_idx in rows:
        if not 0 <= cmd_idx < len(COMMANDS):
            continue
        cmd = COMMANDS[cmd_idx]
        lc = _LC_NAMES[lc_idx] if 0 <= lc_idx < len(_LC_NAMES) else "contour"
        wc = _WC_NAMES[wc_idx] if 0 <= wc_idx < len(_WC_NAMES) else "main"
        if cmd == "EOS":
            break
        if cmd == "PT":
            if open_points is not None:
                open_points.append(
                    {"x": unit(params[0]) * image_width, "y": unit(params[1]) * image_height}
                )
            continue
        flush()
        if cmd == "SEG":
            out.append(EntityOut(
                type="segment", line_class=lc, width_class=wc, confidence=0.6,
                p1={"x": unit(params[0]) * image_width, "y": unit(params[1]) * image_height},
                p2={"x": unit(params[2]) * image_width, "y": unit(params[3]) * image_height},
            ))
        elif cmd == "CIR":
            out.append(EntityOut(
                type="circle", line_class=lc, width_class=wc, confidence=0.6,
                center={"x": unit(params[0]) * image_width, "y": unit(params[1]) * image_height},
                radius=max(unit(params[2]) * max(image_width, image_height), 1e-3),
            ))
        elif cmd == "ARC":
            out.append(EntityOut(
                type="arc", line_class=lc, width_class=wc, confidence=0.55,
                center={"x": unit(params[0]) * image_width, "y": unit(params[1]) * image_height},
                radius=max(unit(params[2]) * max(image_width, image_height), 1e-3),
                start_angle=unit(params[3]) * 360.0,
                end_angle=unit(params[4]) * 360.0,
            ))
        elif cmd in ("PLN", "HAT"):
            open_points, open_kind, open_closed = [], cmd, params[0] >= 0.5
    flush()
    return out


def _directional_output_to_entities(
    output: torch.Tensor,
    image_width: int,
    image_height: int,
    *,
    endpoint_threshold: float = 0.7,
    line_threshold: float = 0.4,
    min_support: float = 0.6,
) -> list[EntityOut]:
    proposals = decode_line_segments(
        output,
        endpoint_threshold=endpoint_threshold,
        line_threshold=line_threshold,
        min_support=min_support,
    )
    sx = image_width / IMG_SIZE
    sy = image_height / IMG_SIZE
    entities = []
    for proposal in proposals:
        proposal["p1"]["x"] *= sx
        proposal["p1"]["y"] *= sy
        proposal["p2"]["x"] *= sx
        proposal["p2"]["y"] *= sy
        entities.append(EntityOut.model_validate(proposal))
    return entities


def _verified_edge_output_to_entities(
    output: torch.Tensor,
    verifier: EdgeVerifier,
    image_width: int,
    image_height: int,
    *,
    node_threshold: float,
    edge_threshold: float,
) -> list[EntityOut]:
    proposals = decode_verified_edges(
        output,
        verifier,
        node_threshold=node_threshold,
        edge_threshold=edge_threshold,
    )
    entities = []
    for proposal in proposals:
        proposal["p1"]["x"] *= image_width
        proposal["p1"]["y"] *= image_height
        proposal["p2"]["x"] *= image_width
        proposal["p2"]["y"] *= image_height
        entities.append(EntityOut.model_validate(proposal))
    return entities


@app.post("/vectorize", response_model=VectorizeResponse)
async def vectorize(file: UploadFile = File(...)) -> VectorizeResponse:
    content = await file.read()
    try:
        img = Image.open(io.BytesIO(content)).convert("L")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"invalid image: {exc}") from exc

    w0, h0 = img.size
    resized = img.resize((IMG_SIZE, IMG_SIZE), Image.LANCZOS)
    arr = 1.0 - (np.asarray(resized, dtype=np.float32) / 255.0)
    tensor = torch.from_numpy(arr).unsqueeze(0).unsqueeze(0).to(_DEVICE)

    model = _load_model()
    try:
        rows = model.generate(tensor, device=_DEVICE)
        # Sequence parameters are normalized to [0, 1] by the ORIGINAL
        # source dimensions in cad_ir.sequence.encode().  Multiplying them by
        # w0/IMG_SIZE used to collapse every prediction into the top-left
        # ~1/256 of the sheet and made any checkpoint score zero.
        entities = _rows_to_entities(rows, w0, h0)
    except torch.cuda.OutOfMemoryError as exc:
        logger.exception("cad_vectorizer_cuda_oom")
        raise HTTPException(503, "GPU out of memory during inference — retry, or the caller falls back to CV") from exc
    except Exception as exc:  # noqa: BLE001 — any model failure must be a clear, loggable 500, not an opaque traceback
        logger.exception("cad_vectorizer_inference_failed")
        raise HTTPException(500, f"model inference failed: {exc}") from exc
    return VectorizeResponse(entities=entities, model_step=getattr(app.state, "model_step", None))


def _primitive_outputs_to_entities(
    outputs: dict[str, torch.Tensor],
    image_width: int,
    image_height: int,
    *,
    min_confidence: float = 0.5,
) -> list[EntityOut]:
    probabilities = outputs["type_logits"][0].softmax(-1)
    scores, kinds = probabilities.max(-1)
    params = outputs["params"][0]
    line_classes = outputs["line_logits"][0].argmax(-1)
    width_classes = outputs["width_logits"][0].argmax(-1)
    radius_scale = max(image_width, image_height)
    entities = []
    for index in range(kinds.numel()):
        kind_index = int(kinds[index])
        confidence = float(scores[index])
        if kind_index == 0 or confidence < min_confidence:
            continue
        kind = TYPE_NAMES[kind_index]
        values = params[index].tolist()
        common = {
            "type": kind,
            "line_class": LINE_CLASSES[int(line_classes[index])],
            "width_class": WIDTH_CLASSES[int(width_classes[index])],
            "confidence": confidence,
        }
        if kind == "segment":
            entities.append(
                EntityOut(
                    **common,
                    p1={"x": values[0] * image_width, "y": values[1] * image_height},
                    p2={"x": values[2] * image_width, "y": values[3] * image_height},
                )
            )
        elif kind == "circle":
            entities.append(
                EntityOut(
                    **common,
                    center={"x": values[0] * image_width, "y": values[1] * image_height},
                    radius=max(values[2] * radius_scale, 1e-3),
                )
            )
        elif kind == "arc":
            entities.append(
                EntityOut(
                    **common,
                    center={"x": values[0] * image_width, "y": values[1] * image_height},
                    radius=max(values[2] * radius_scale, 1e-3),
                    start_angle=values[3] * 360.0,
                    end_angle=values[4] * 360.0,
                )
            )
    return entities


def _multi_type_outputs_to_entities(
    outputs: dict[str, torch.Tensor],
    image_width: int,
    image_height: int,
    *,
    min_confidence: float = 0.5,
) -> list[EntityOut]:
    probabilities = outputs["type_logits"][0].softmax(-1)
    scores, kinds = probabilities.max(-1)
    params = outputs["params"][0]
    line_classes = outputs["line_logits"][0].argmax(-1)
    width_classes = outputs["width_logits"][0].argmax(-1)
    subtypes = outputs["subtype_logits"][0].argmax(-1)
    radius_scale = max(image_width, image_height)
    entities = []
    dimension_kinds = {"linear", "diameter", "radial", "angular"}
    annotation_kinds = {"roughness", "thread", "tolerance", "datum", "weld"}
    for index in range(kinds.numel()):
        kind_index = int(kinds[index])
        confidence = float(scores[index])
        if kind_index == 0 or confidence < min_confidence:
            continue
        kind = MULTI_TYPE_NAMES[kind_index]
        values = params[index].tolist()
        subtype = MULTI_SUBTYPE_NAMES[int(subtypes[index])]
        common = {
            "type": kind,
            "line_class": LINE_CLASSES[int(line_classes[index])],
            "width_class": WIDTH_CLASSES[int(width_classes[index])],
            "confidence": confidence,
        }
        p1 = {"x": values[0] * image_width, "y": values[1] * image_height}
        p2 = {"x": values[2] * image_width, "y": values[3] * image_height}
        if kind == "segment":
            entities.append(EntityOut(**common, p1=p1, p2=p2))
        elif kind == "circle":
            entities.append(EntityOut(**common, center=p1, radius=max(values[2] * radius_scale, 1e-3)))
        elif kind == "arc":
            entities.append(EntityOut(**common, center=p1, radius=max(values[2] * radius_scale, 1e-3), start_angle=values[3] * 360.0, end_angle=values[4] * 360.0))
        elif kind == "text":
            entities.append(EntityOut(**common, position=p1, text="", height=max(values[2] * radius_scale, 0.5), rotation=values[3] * 360.0))
        elif kind == "dimension":
            entities.append(EntityOut(**common, kind=subtype if subtype in dimension_kinds else "linear", p1=p1, p2=p2, text=""))
        elif kind == "annotation":
            entities.append(EntityOut(
                **common,
                kind=subtype if subtype in annotation_kinds else "roughness",
                position=p1,
                leader=p2,
                text="",
                datum_refs=[],
                height=3.5,
            ))
        elif kind == "hatch":
            x0, x1 = sorted((p1["x"], p2["x"]))
            y0, y1 = sorted((p1["y"], p2["y"]))
            if x1 - x0 >= 1 and y1 - y0 >= 1:
                entities.append(EntityOut(**common, boundary=[{"x": x0, "y": y0}, {"x": x1, "y": y0}, {"x": x1, "y": y1}, {"x": x0, "y": y1}], holes=[], pattern=subtype if subtype in {"ansi31", "solid"} else "ansi31"))
    return entities


@app.post("/detect-multi-type", response_model=VectorizeResponse)
async def detect_multi_type(
    file: UploadFile = File(...),
    min_confidence: float = 0.5,
) -> VectorizeResponse:
    """Return inferred CadIR proposals; semantic payloads remain unverified."""
    model = _load_multi_type_model()
    if model is None:
        raise HTTPException(503, "multi-type checkpoint is not configured")
    content = await file.read()
    try:
        image = Image.open(io.BytesIO(content)).convert("L")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"invalid image: {exc}") from exc
    width, height = image.size
    resized = image.resize((IMG_SIZE, IMG_SIZE), Image.Resampling.LANCZOS)
    pixels = 1.0 - np.asarray(resized, dtype=np.float32) / 255.0
    tensor = torch.from_numpy(pixels).unsqueeze(0).unsqueeze(0).to(_DEVICE)
    with torch.no_grad():
        outputs = model(tensor)
    return VectorizeResponse(
        entities=_multi_type_outputs_to_entities(outputs, width, height, min_confidence=min_confidence),
        model_step=getattr(app.state, "multi_type_model_step", None),
    )


def _layout_outputs_to_regions(
    outputs: dict[str, torch.Tensor],
    image_width: int,
    image_height: int,
    *,
    min_confidence: float = 0.8,
) -> list[tuple[int, int, int, int, float]]:
    probabilities = outputs["type_logits"][0].softmax(-1)
    scores, kinds = probabilities.max(-1)
    regions = []
    for index in range(kinds.numel()):
        if int(kinds[index]) == 0 or float(scores[index]) < min_confidence:
            continue
        cx, cy, width, height = outputs["boxes"][0, index].tolist()
        x0 = max(0, round((cx - width / 2) * image_width))
        y0 = max(0, round((cy - height / 2) * image_height))
        x1 = min(image_width, round((cx + width / 2) * image_width))
        y1 = min(image_height, round((cy + height / 2) * image_height))
        if x1 - x0 >= 16 and y1 - y0 >= 16:
            regions.append((x0, y0, x1, y1, float(scores[index])))
    # Confidence-ordered NMS prevents duplicate queries from multiplying the
    # local geometry.  It does not invent a full-sheet fallback: failure to
    # find a view stays an explicit empty candidate.
    kept = []
    for region in sorted(regions, key=lambda item: item[4], reverse=True):
        x0, y0, x1, y1, _score = region
        duplicate = False
        for kx0, ky0, kx1, ky1, _ in kept:
            intersection = max(0, min(x1, kx1) - max(x0, kx0)) * max(
                0, min(y1, ky1) - max(y0, ky0)
            )
            union = (x1 - x0) * (y1 - y0) + (kx1 - kx0) * (ky1 - ky0) - intersection
            if intersection / max(union, 1) >= 0.5:
                duplicate = True
                break
        if not duplicate:
            kept.append(region)
    return kept


@app.post("/detect-primitives", response_model=VectorizeResponse)
async def detect_primitives(
    file: UploadFile = File(...),
    min_confidence: float = 0.5,
) -> VectorizeResponse:
    model = _load_primitive_model()
    if model is None:
        raise HTTPException(503, "primitive-set checkpoint is not configured")
    content = await file.read()
    try:
        image = Image.open(io.BytesIO(content)).convert("L")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"invalid image: {exc}") from exc
    width, height = image.size
    resized = image.resize((IMG_SIZE, IMG_SIZE), Image.Resampling.LANCZOS)
    pixels = 1.0 - np.asarray(resized, dtype=np.float32) / 255.0
    tensor = torch.from_numpy(pixels).unsqueeze(0).unsqueeze(0).to(_DEVICE)
    with torch.no_grad():
        outputs = model(tensor)
    entities = _primitive_outputs_to_entities(
        outputs,
        width,
        height,
        min_confidence=min_confidence,
    )
    return VectorizeResponse(
        entities=entities,
        model_step=getattr(app.state, "primitive_model_step", None),
    )


@app.post("/detect-hierarchical", response_model=VectorizeResponse)
async def detect_hierarchical(
    file: UploadFile = File(...),
    min_layout_confidence: float = 0.8,
    min_primitive_confidence: float = 0.5,
) -> VectorizeResponse:
    layout_model = _load_sheet_layout_model()
    primitive_model = _load_primitive_model()
    if layout_model is None or primitive_model is None:
        raise HTTPException(503, "hierarchical sheet checkpoints are not configured")
    content = await file.read()
    try:
        image = Image.open(io.BytesIO(content)).convert("L")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"invalid image: {exc}") from exc
    width, height = image.size
    layout_image = image.resize((IMG_SIZE, IMG_SIZE), Image.Resampling.LANCZOS)
    layout_pixels = 1.0 - np.asarray(layout_image, dtype=np.float32) / 255.0
    layout_tensor = torch.from_numpy(layout_pixels).unsqueeze(0).unsqueeze(0).to(_DEVICE)
    with torch.no_grad():
        layout_outputs = layout_model(layout_tensor)
    regions = _layout_outputs_to_regions(
        layout_outputs,
        width,
        height,
        min_confidence=min_layout_confidence,
    )
    entities = []
    for x0, y0, x1, y1, _score in regions:
        crop = image.crop((x0, y0, x1, y1)).resize(
            (IMG_SIZE, IMG_SIZE), Image.Resampling.LANCZOS
        )
        pixels = 1.0 - np.asarray(crop, dtype=np.float32) / 255.0
        tensor = torch.from_numpy(pixels).unsqueeze(0).unsqueeze(0).to(_DEVICE)
        with torch.no_grad():
            outputs = primitive_model(tensor)
        local = _primitive_outputs_to_entities(
            outputs,
            x1 - x0,
            y1 - y0,
            min_confidence=min_primitive_confidence,
        )
        for entity in local:
            if entity.p1:
                entity.p1["x"] += x0
                entity.p1["y"] += y0
            if entity.p2:
                entity.p2["x"] += x0
                entity.p2["y"] += y0
            if entity.center:
                entity.center["x"] += x0
                entity.center["y"] += y0
            entities.append(entity)
    return VectorizeResponse(
        entities=entities,
        model_step=getattr(app.state, "primitive_model_step", None),
        layout_step=getattr(app.state, "sheet_layout_model_step", None),
        view_regions=len(regions),
    )


@app.post("/predict-evidence")
async def predict_evidence(
    file: UploadFile = File(...),
    threshold: float = 0.5,
) -> Response:
    """Return a binary evidence mask (255=predicted geometry).

    Vector fitting intentionally remains outside the network.  This endpoint
    cannot claim entities or exactness; the backend deterministic fitter and
    independent source verifier retain those responsibilities.
    """

    model = _load_evidence_model()
    if model is None:
        raise HTTPException(503, "evidence heatmap checkpoint is not configured")
    content = await file.read()
    try:
        image = Image.open(io.BytesIO(content)).convert("L")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"invalid image: {exc}") from exc
    width, height = image.size
    resized = image.resize((IMG_SIZE, IMG_SIZE), Image.Resampling.LANCZOS)
    pixels = 1.0 - np.asarray(resized, dtype=np.float32) / 255.0
    tensor = torch.from_numpy(pixels).unsqueeze(0).unsqueeze(0).to(_DEVICE)
    with torch.no_grad():
        probabilities = model(tensor).sigmoid()[0]
    combined = probabilities.max(dim=0).values
    mask = (combined >= threshold).to(torch.uint8).cpu().numpy() * 255
    output = Image.fromarray(mask, mode="L").resize(
        (width, height), Image.Resampling.NEAREST
    )
    stream = io.BytesIO()
    output.save(stream, format="PNG")
    return Response(
        content=stream.getvalue(),
        media_type="image/png",
        headers={
            "X-CAD-Evidence-Step": str(getattr(app.state, "evidence_model_step", "")),
            "X-CAD-Evidence-Channels": "line,circle,arc",
        },
    )


@app.post("/detect-directional", response_model=VectorizeResponse)
async def detect_directional(
    file: UploadFile = File(...),
    endpoint_threshold: float = 0.7,
    line_threshold: float = 0.4,
    min_support: float = 0.6,
) -> VectorizeResponse:
    """Return direct line proposals from learned directional fields.

    The endpoint/direction/support decoder is fail-closed and emits only
    inferred segments.  Circle and arc fields remain evidence-only in v1.
    """

    model = _load_directional_model()
    if model is None:
        raise HTTPException(503, "directional field checkpoint is not configured")
    content = await file.read()
    try:
        image = Image.open(io.BytesIO(content)).convert("L")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"invalid image: {exc}") from exc
    width, height = image.size
    resized = image.resize((IMG_SIZE, IMG_SIZE), Image.Resampling.LANCZOS)
    pixels = 1.0 - np.asarray(resized, dtype=np.float32) / 255.0
    tensor = torch.from_numpy(pixels).unsqueeze(0).unsqueeze(0).to(_DEVICE)
    with torch.no_grad():
        output = model(tensor)[0].cpu()
    entities = _directional_output_to_entities(
        output,
        width,
        height,
        endpoint_threshold=endpoint_threshold,
        line_threshold=line_threshold,
        min_support=min_support,
    )
    return VectorizeResponse(
        entities=entities,
        model_step=getattr(app.state, "directional_model_step", None),
    )


@app.post("/detect-edge-graph", response_model=VectorizeResponse)
async def detect_edge_graph(file: UploadFile = File(...)) -> VectorizeResponse:
    field_model = _load_directional_model()
    verifier = _load_edge_verifier()
    if field_model is None or verifier is None:
        raise HTTPException(503, "directional and edge verifier checkpoints are required")
    content = await file.read()
    try:
        image = Image.open(io.BytesIO(content)).convert("L")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"invalid image: {exc}") from exc
    width, height = image.size
    resized = image.resize((IMG_SIZE, IMG_SIZE), Image.Resampling.LANCZOS)
    pixels = 1.0 - np.asarray(resized, dtype=np.float32) / 255.0
    tensor = torch.from_numpy(pixels).unsqueeze(0).unsqueeze(0).to(_DEVICE)
    with torch.no_grad():
        output = field_model(tensor)[0]
        selected = getattr(app.state, "edge_verifier_validation", {})
        entities = _verified_edge_output_to_entities(
            output,
            verifier,
            width,
            height,
            node_threshold=float(selected.get("node_threshold", 0.7)),
            edge_threshold=float(selected.get("edge_threshold", 0.5)),
        )
    return VectorizeResponse(
        entities=entities,
        model_step=getattr(app.state, "directional_model_step", None),
    )


def _bootstrap_backend_path() -> None:
    """Same fake-parent-package trick as train.py — needed only for the
    ``COMMANDS`` tuple import above, nothing heavier."""
    import types

    repo_backend = pathlib.Path(os.environ.get("REPO_BACKEND", "/repo/backend"))
    sys.path.insert(0, str(repo_backend))
    if "app" not in sys.modules:
        pkg = types.ModuleType("app")
        pkg.__path__ = [str(repo_backend / "app")]
        sys.modules["app"] = pkg
    if "app.ai" not in sys.modules:
        pkg = types.ModuleType("app.ai")
        pkg.__path__ = [str(repo_backend / "app" / "ai")]
        sys.modules["app.ai"] = pkg


_bootstrap_backend_path()
