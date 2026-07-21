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
    # Dedicated slot for the spec reader (Settings → Models → Оцифровка). When it
    # has no assignment, fall back to the shared drawing-analysis VLM so behaviour
    # is unchanged out of the box.
    from app.ai.task_routing import get_routing_for

    read_task = (
        AITask.CAD_SPEC_READ
        if get_routing_for(AITask.CAD_SPEC_READ).primary
        else AITask.DRAWING_ANALYSIS_VLM
    )
    request = AIRequest(
        task=read_task,
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
    # A section is any coaxial body-of-revolution segment. The VLM labels these
    # inconsistently (cylinder/cone/step/neck/journal/shaft/…), so accept any
    # feature that carries a diameter UNLESS it is clearly a sub-feature cut
    # into the body (a hole/keyway/thread/chamfer/groove).
    sub_features = {"hole", "keyway", "thread", "chamfer", "groove", "slot", "bore", "fillet"}
    sections: list[dict] = []
    for feature in main.get("features", []) or []:
        if not isinstance(feature, dict):
            continue
        kind = str(feature.get("kind", "")).lower()
        if any(sub in kind for sub in sub_features):
            continue
        diameter = _num(feature.get("diameter_mm")) or _num(feature.get("diameter"))
        if diameter is not None and diameter > 0:
            sections.append({
                "d": diameter,
                "l": _num(feature.get("length_mm")) or _num(feature.get("length")),
                "note": feature.get("note"),
            })
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


def draft_from_spec(
    spec: dict,
    *,
    px_per_mm: float | None = None,
    draft_model: str | None = None,
    router: Any | None = None,
) -> CadIR | None:
    """Dispatch a structured spec to a drafter (Model 2).

    When ``draft_model`` is set (Settings → Models → Оцифровка → «Чертёжник»),
    a generative model — e.g. a LoRA fine-tuned drafter — turns the spec into
    geometry. On any failure, or when no model is assigned, fall back to the
    deterministic parametric drafter (clean by construction, rotation bodies).
    """
    if draft_model:
        try:
            import asyncio

            generated = asyncio.get_event_loop().run_until_complete(
                _draft_generative(spec, draft_model, router=router)
            ) if not _in_running_loop() else None
            if generated is not None and generated.entities:
                return generated
        except Exception:  # noqa: BLE001 — never sink the pipeline on a model error
            pass

    main = (spec.get("main_view") or {})
    part_type = str(main.get("type", "")).lower()
    if "враще" in part_type or "вал" in part_type or "shaft" in part_type or "rotation" in part_type:
        return draft_rotation_body(spec, px_per_mm=px_per_mm)
    # Rotation body is a strong default when the features read as coaxial cylinders.
    return draft_rotation_body(spec, px_per_mm=px_per_mm)


def _in_running_loop() -> bool:
    import asyncio

    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


async def draft_from_spec_async(
    spec: dict,
    *,
    px_per_mm: float | None = None,
    draft_model: str | None = None,
    router: Any | None = None,
) -> CadIR | None:
    """Async variant: usable from inside a running event loop (the digitize task)."""
    if draft_model:
        try:
            generated = await _draft_generative(spec, draft_model, router=router)
            if generated is not None and generated.entities:
                return generated
        except Exception:  # noqa: BLE001
            pass
    return draft_from_spec(spec, px_per_mm=px_per_mm)


_DRAFT_PROMPT = (
    "Ты — генеративный чертёжник САПР. По СПЕЦИФИКАЦИИ детали построй геометрию "
    "главного вида и верни СТРОГО JSON примитивов в изотропном пространстве 0..1000 "
    "(обе оси в одном масштабе, 0,0 — верхний левый угол):\n"
    '{"lines":[[x1,y1,x2,y2],...],"circles":[[cx,cy,r],...],'
    '"arcs":[[cx,cy,r,start_deg,end_deg],...],'
    '"polylines":[{"pts":[[x,y],...],"closed":0}]}\n'
    "Только JSON, без пояснений.\nСПЕЦИФИКАЦИЯ:\n"
)


async def _draft_generative(
    spec: dict, draft_model: str, *, router: Any | None = None
) -> CadIR | None:
    """Model 2 (generative): a model turns the spec text into a geometry DSL."""
    import json

    from app.ai.schemas import AIRequest, AITask, ChatMessage

    if router is None:
        from app.ai.router import ai_router

        router = ai_router
    request = AIRequest(
        task=AITask.CAD_SPEC_DRAFT,
        messages=[ChatMessage(
            role="user",
            content=_DRAFT_PROMPT + json.dumps(spec, ensure_ascii=False),
        )],
        preferred_model=draft_model,
        confidential=True,
        allow_cloud=False,
    )
    response = await router.run(request)
    dsl = _parse_spec_json(response.text or "")
    return _dsl_to_ir(dsl) if dsl else None


def _dsl_to_ir(dsl: dict, *, canvas: int = 1000) -> CadIR | None:
    """Decode a 0..1000 isotropic geometry DSL into a clean CadIR.

    Inverse of ``tools/cad-dataset/build_vlm_sft.ir_to_dsl`` — the format the
    generative drafter is trained to emit.
    """
    from app.ai.cad_ir.schema import Arc, Circle, Polyline

    entities: list[Any] = []

    def _pt(x, y):
        return Point(x=float(x), y=float(y))

    for ln in dsl.get("lines", []) or []:
        if isinstance(ln, (list, tuple)) and len(ln) >= 4:
            entities.append(Segment(
                p1=_pt(ln[0], ln[1]), p2=_pt(ln[2], ln[3]),
                line_class="contour", width_class="main",
                origin="spec", assurance="inferred",
            ))
    for c in dsl.get("circles", []) or []:
        if isinstance(c, (list, tuple)) and len(c) >= 3:
            entities.append(Circle(
                center=_pt(c[0], c[1]), radius=float(c[2]),
                line_class="contour", width_class="main",
                origin="spec", assurance="inferred",
            ))
    for a in dsl.get("arcs", []) or []:
        if isinstance(a, (list, tuple)) and len(a) >= 5:
            entities.append(Arc(
                center=_pt(a[0], a[1]), radius=float(a[2]),
                start_angle=float(a[3]), end_angle=float(a[4]),
                line_class="contour", width_class="main",
                origin="spec", assurance="inferred",
            ))
    for pl in dsl.get("polylines", []) or []:
        if not isinstance(pl, dict):
            continue
        pts = [_pt(p[0], p[1]) for p in (pl.get("pts") or []) if len(p) >= 2]
        if len(pts) >= 2:
            entities.append(Polyline(
                points=pts, closed=bool(pl.get("closed")),
                line_class="contour", width_class="main",
                origin="spec", assurance="inferred",
            ))
    if not entities:
        return None
    return CadIR(
        source=SourceInfo(image_width=canvas, image_height=canvas, kind="scan"),
        scale=1.0,
        entities=entities,
        recognizer_used="spec-drafter-generative",
        digitization_status="review_required",
    )
