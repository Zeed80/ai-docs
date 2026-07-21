"""Understanding -> drafting vectorizer (the "two-model" path).

Model 1 (a VLM) reads a drawing into a structured feature/dimension SPEC;
Model 2 (here, a deterministic parametric drafter) constructs a CLEAN, editable
CAD IR from that spec. Nothing is traced pixel-by-pixel, so the result is clean
by construction and dimensionally driven by what the VLM read — and the same
drafter serves "draft from a description" (the spec can come from an engineer,
not only from an image).

This module holds the drafter (spec -> CadIR). The spec extractor (image ->
spec) lives alongside the existing VLM text reader.
"""

from __future__ import annotations

from typing import Any

from app.ai.cad_ir.schema import (
    CadIR,
    DimensionEntity,
    Point,
    Segment,
    SourceInfo,
    TextEntity,
)


_SPEC_PROMPT = (
    "Ты — инженер-конструктор. Изучи чертёж и опиши деталь СТРУКТУРНО как "
    "спецификацию для повторного черчения. Верни СТРОГО JSON:\n"
    '{"part":"название",'
    '"views":[{"name":"главный вид/разрез А-А/...","role":"main|section|detail"}],'
    '"main_view":{"type":"тело вращения (вал)|призматическая|...",'
    '"features":[{"kind":"cylinder|cone|step|keyway|hole|chamfer|thread|groove",'
    '"diameter_mm":0,"length_mm":0,"pos":"положение","note":"..."}]},'
    '"dimensions":[{"value":"Ø80js6(±0.0095)","applies_to":"..."}],'
    '"annotations":[{"kind":"roughness|hardness|tolerance|thread","text":"Ra 0.8"}],'
    '"title_block":{"material":"..","scale":".."}}\n'
    "Читай реальные значения с чертежа. Только JSON."
)


async def read_drawing_spec(
    image_bytes: bytes, *, router: Any | None = None, confidential: bool = True
) -> dict:
    """Model 1: a VLM reads the drawing into a structured feature/dimension spec.

    Robust to real scans (understanding, not pixel localisation). Returns {} on
    failure so the caller can fall back to the tracing method.
    """
    import base64
    import io

    from PIL import Image

    from app.ai.schemas import AIRequest, AITask, ChatMessage
    from app.ai.vlm_dimensions import _parse_json_array  # tolerant fence stripping

    if router is None:
        from app.ai.router import ai_router

        router = ai_router
    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:  # noqa: BLE001
        return {}
    image.thumbnail((1400, 1400))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    request = AIRequest(
        task=AITask.DRAWING_ANALYSIS_VLM,
        messages=[ChatMessage(role="user", content=_SPEC_PROMPT)],
        images=[base64.b64encode(buffer.getvalue()).decode()],
        confidential=confidential,
        allow_cloud=False,
    )
    try:
        response = await router.run(request)
    except Exception:  # noqa: BLE001 — never sink the pipeline on a VLM error
        return {}
    return _parse_spec_json(response.text or "")


def _parse_spec_json(raw: str) -> dict:
    import json
    import re

    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if not (0 <= start < end):
        return {}
    try:
        value = json.loads(text[start : end + 1])
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _num(value: Any) -> float | None:
    """Best-effort numeric from a spec field (handles '30', 30, 'Ø30h6')."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    import re

    match = re.search(r"-?\d+(?:[.,]\d+)?", str(value))
    return float(match.group().replace(",", ".")) if match else None


def _rotation_sections(spec: dict) -> list[dict]:
    """Pull ordered cylindrical sections (diameter, length) from the spec.

    A rotation body's main view is a run of coaxial cylinders/cones; steps are
    just transitions between them and carry no length of their own.
    """
    main = spec.get("main_view") or {}
    sections: list[dict] = []
    for feature in main.get("features", []) or []:
        if not isinstance(feature, dict):
            continue
        kind = str(feature.get("kind", "")).lower()
        if kind in ("cylinder", "cone", "step"):
            diameter = _num(feature.get("diameter_mm"))
            length = _num(feature.get("length_mm"))
            if diameter is not None:
                sections.append({"d": diameter, "l": length, "note": feature.get("note")})
    return sections


def draft_rotation_body(spec: dict, *, px_per_mm: float | None = None) -> CadIR | None:
    """Construct a clean stepped-shaft main view from a rotation-body spec.

    Returns None when the spec isn't a usable rotation body (too few sections
    with a diameter), so the caller can fall back to another method.
    """
    sections = _rotation_sections(spec)
    diameters = [s["d"] for s in sections if s["d"]]
    if len(sections) < 2 or not diameters:
        return None

    # Fill missing lengths: distribute the overall length (or a default) across
    # the sections that lack one, so the profile is still proportionate.
    overall = None
    for dim in spec.get("dimensions", []) or []:
        text = str(dim.get("applies_to", "")) + " " + str(dim.get("value", ""))
        if any(word in text.lower() for word in ("общая", "overall", "длина детали")):
            overall = _num(dim.get("value"))
            break
    known = sum(s["l"] for s in sections if s["l"])
    missing = [s for s in sections if not s["l"]]
    if missing:
        remaining = (overall - known) if (overall and overall > known) else max(known, len(missing) * max(diameters))
        each = max(1.0, remaining / len(missing))
        for s in missing:
            s["l"] = each

    total_len = sum(s["l"] for s in sections)
    max_d = max(diameters)
    if px_per_mm is None:
        px_per_mm = 900.0 / max(total_len, 1.0)  # fit the length into ~900px

    margin = 60.0
    axis_y = margin + max_d * px_per_mm / 2.0
    entities: list[Any] = []

    def seg(x1, y1, x2, y2, cls="contour", width="main"):
        entities.append(
            Segment(
                p1=Point(x=x1, y=y1), p2=Point(x=x2, y=y2),
                line_class=cls, width_class=width, origin="spec", assurance="constraint_validated",
            )
        )

    x = margin
    prev_r = None
    for s in sections:
        length_px = s["l"] * px_per_mm
        r = s["d"] * px_per_mm / 2.0
        # vertical step at the boundary between the previous and this diameter
        if prev_r is None:
            seg(x, axis_y - r, x, axis_y + r)  # left end cap
        elif abs(r - prev_r) > 0.5:
            seg(x, axis_y - prev_r, x, axis_y - r)
            seg(x, axis_y + prev_r, x, axis_y + r)
        # top and bottom edges of this cylinder
        seg(x, axis_y - r, x + length_px, axis_y - r)
        seg(x, axis_y + r, x + length_px, axis_y + r)
        x += length_px
        prev_r = r
    seg(x, axis_y - prev_r, x, axis_y + prev_r)  # right end cap

    # Axis centreline (dash-dot), a touch past both ends.
    seg(margin - 20, axis_y, x + 20, axis_y, cls="axis", width="thin")

    width_px = x + margin
    height_px = axis_y + max_d * px_per_mm / 2.0 + margin
    ir = CadIR(
        source=SourceInfo(image_width=int(width_px), image_height=int(height_px), kind="scan"),
        scale=1.0 / px_per_mm,
        entities=entities,
        recognizer_used="spec-drafter-rotation",
        digitization_status="review_required",
    )
    return ir


def draft_from_spec(spec: dict, *, px_per_mm: float | None = None) -> CadIR | None:
    """Dispatch a structured spec to the right parametric drafter."""
    main = (spec.get("main_view") or {})
    part_type = str(main.get("type", "")).lower()
    if "враще" in part_type or "вал" in part_type or "shaft" in part_type or "rotation" in part_type:
        return draft_rotation_body(spec, px_per_mm=px_per_mm)
    # Rotation body is a strong default when the features read as coaxial cylinders.
    return draft_rotation_body(spec, px_per_mm=px_per_mm)
