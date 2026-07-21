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

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from app.ai.cad_ir.schema import (
    CadIR,
    DimensionEntity,
    Point,
    Segment,
    SourceInfo,
    TextEntity,
)


class SpecEvidence(BaseModel):
    """Auditable source observation backing one structured spec value."""

    image_index: int = Field(ge=0)
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    raw_text: str | None = None


class SpecSection(BaseModel):
    diameter_mm: float = Field(gt=0)
    length_mm: float | None = Field(default=None, gt=0)
    note: str | None = None
    evidence: list[SpecEvidence] = Field(default_factory=list)


class SpecBody(BaseModel):
    name: str | None = None
    type: str = "unknown"
    outer: list[SpecSection] = Field(default_factory=list)
    bore: list[SpecSection] = Field(default_factory=list)
    # Accepted only for compatibility with already stored prototype responses.
    # The deterministic drafter still requires explicit, complete outer[] data.
    features: list[dict[str, Any]] = Field(default_factory=list)


class SpecDimension(BaseModel):
    value: str = Field(min_length=1)
    applies_to: str = ""
    evidence: list[SpecEvidence] = Field(default_factory=list)


class SpecAnnotation(BaseModel):
    kind: Literal["roughness", "hardness", "tolerance", "thread", "material", "other"]
    text: str = Field(min_length=1)
    evidence: list[SpecEvidence] = Field(default_factory=list)


class EngineeringDrawingSpec(BaseModel):
    """Fail-closed contract between drawing recognition and CAD drafting."""

    schema_version: Literal[1] = 1
    part: str = ""
    main_view: SpecBody
    parts: list[SpecBody] = Field(default_factory=list)
    dimensions: list[SpecDimension] = Field(default_factory=list)
    annotations: list[SpecAnnotation] = Field(default_factory=list)
    title_block: dict[str, Any] = Field(default_factory=dict)
    unresolved: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _record_incomplete_rotation_sections(self) -> "EngineeringDrawingSpec":
        bodies = [self.main_view, *self.parts]
        for body_index, body in enumerate(bodies):
            rotation = any(word in body.type.lower() for word in ("вращ", "вал", "shaft"))
            if not rotation:
                continue
            if len(body.outer) < 2:
                self.unresolved.append(f"body:{body_index}:outer-profile-incomplete")
            for section_index, section in enumerate(body.outer):
                if section.length_mm is None:
                    self.unresolved.append(
                        f"body:{body_index}:outer:{section_index}:length-missing"
                    )
            for section_index, section in enumerate(body.bore):
                if section.length_mm is None:
                    self.unresolved.append(
                        f"body:{body_index}:bore:{section_index}:length-missing"
                    )
        self.unresolved = sorted(set(self.unresolved))
        return self


_SPEC_PROMPT = (
    "Ты — инженер-конструктор. Изучи чертёж и опиши деталь СТРУКТУРНО для "
    "повторного черчения. Изображение 0 — общий вид листа, остальные изображения "
    "— полноразмерные перекрывающиеся фрагменты; их границы перечислены ниже. "
    "Верни СТРОГО JSON:\n"
    '{"schema_version":1,"part":"название",'
    '"main_view":{"type":"тело вращения (вал)|призматическая",'
    '"outer":[{"diameter_mm":0,"length_mm":0,"note":"резьба/конус/...",'
    '"evidence":[{"image_index":1,"bbox":[0,0,100,30],"raw_text":"Ø40"}]}],'
    '"bore":[{"diameter_mm":0,"length_mm":0,"note":"..."}]},'
    '"parts":[{"name":"..","type":"..","outer":[...],"bore":[...]}],'
    '"dimensions":[{"value":"Ø80js6","applies_to":".."}],'
    '"annotations":[{"kind":"roughness|hardness|tolerance|thread","text":".."}],'
    '"title_block":{"material":"..","scale":".."},'
    '"unresolved":["что именно не удалось доказать"]}\n'
    "ПРАВИЛА для тела вращения:\n"
    "1) outer[] — ВСЕ ступени наружного контура ПО ПОРЯДКУ слева направо, БЕЗ "
    "пропусков (включая резьбовые участки — бери наружный диаметр резьбы, и "
    "конусы — средний диаметр).\n"
    "2) length_mm — ОСЕВАЯ ДЛИНА ИМЕННО ЭТОЙ ступени, а НЕ размер с цепочки. "
    "Если на чертеже цепочка накопительных размеров от торца — вычисли длину "
    "ступени как РАЗНОСТЬ соседних размеров.\n"
    "3) Сумма length_mm ≈ полная длина детали (сверься с габаритным размером).\n"
    "4) Если деталь ПОЛАЯ (в разрезе видно осевое отверстие/расточку) — опиши "
    "внутренний контур в bore[] так же по порядку.\n"
    "5) Фаски/канавки/шпонпазы/поперечные отверстия НЕ включай в outer/bore.\n"
    "Если деталей несколько — каждую в parts[], главную продублируй в main_view.\n"
    "Читай только реально видимые значения. ЗАПРЕЩЕНО угадывать, усреднять или "
    "достраивать отсутствующие размеры. Неизвестное оставь null и добавь причину "
    "в unresolved. Для каждого прочитанного размера приложи evidence. Только JSON."
)


def _spec_images(image, *, tile_size: int = 1400, overlap: int = 160) -> tuple[list[bytes], list[str]]:
    """Build a context image plus source-resolution tiles without data loss."""
    import io

    context = image.copy()
    context.thumbnail((1400, 1400))
    buffer = io.BytesIO()
    context.save(buffer, format="PNG")
    encoded = [buffer.getvalue()]
    descriptions = [f"image 0: overview 0,0,{image.width},{image.height}"]
    if image.width <= tile_size and image.height <= tile_size:
        return encoded, descriptions
    step = tile_size - overlap
    xs = list(range(0, max(image.width - tile_size, 0) + 1, step))
    ys = list(range(0, max(image.height - tile_size, 0) + 1, step))
    if not xs or xs[-1] != max(image.width - tile_size, 0):
        xs.append(max(image.width - tile_size, 0))
    if not ys or ys[-1] != max(image.height - tile_size, 0):
        ys.append(max(image.height - tile_size, 0))
    # Bound latency for unusually large sheets while covering both edges and centre.
    boxes = [(x, y, min(x + tile_size, image.width), min(y + tile_size, image.height)) for y in ys for x in xs]
    if len(boxes) > 8:
        indexes = sorted({0, len(boxes) - 1, *(round(i * (len(boxes) - 1) / 7) for i in range(8))})
        boxes = [boxes[index] for index in indexes]
    for index, box in enumerate(boxes, start=1):
        tile = image.crop(box)
        tile_buffer = io.BytesIO()
        tile.save(tile_buffer, format="PNG")
        encoded.append(tile_buffer.getvalue())
        descriptions.append(f"image {index}: source bbox {box[0]},{box[1]},{box[2]},{box[3]}")
    return encoded, descriptions


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
    images, tile_descriptions = _spec_images(image)
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
        messages=[ChatMessage(
            role="user",
            content=_SPEC_PROMPT + "\nКАРТА ИЗОБРАЖЕНИЙ:\n" + "\n".join(tile_descriptions),
        )],
        images=[base64.b64encode(value).decode() for value in images],
        confidential=confidential,
        allow_cloud=False,
    )
    try:
        response = await router.run(request)
    except Exception:  # noqa: BLE001 — never sink the pipeline on a VLM error
        return {}
    parsed = _parse_spec_json(response.text or "")
    if not parsed:
        return {}
    try:
        return EngineeringDrawingSpec.model_validate(parsed).model_dump(mode="json")
    except ValidationError:
        return {}


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


# A section is any coaxial body-of-revolution segment. The VLM labels these
# inconsistently (cylinder/cone/step/neck/journal/shaft/…), so accept any
# feature that carries a diameter UNLESS it is clearly a sub-feature cut into
# the body (a hole/keyway/thread/chamfer/groove).
_SUB_FEATURES = {"hole", "keyway", "thread", "chamfer", "groove", "slot", "bore", "fillet"}


def _sections_from_list(items: Any) -> list[dict]:
    """Ordered (diameter, length) sections from a plain outer/bore list."""
    sections: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        diameter = _num(it.get("diameter_mm")) or _num(it.get("diameter"))
        if diameter is not None and diameter > 0:
            sections.append({
                "d": diameter,
                "l": _num(it.get("length_mm")) or _num(it.get("length")),
                "note": it.get("note"),
            })
    return sections


def _sections_from_features(features: Any) -> list[dict]:
    """Ordered sections from a legacy ``features`` list (kind-filtered)."""
    sections: list[dict] = []
    for feature in features or []:
        if not isinstance(feature, dict):
            continue
        kind = str(feature.get("kind", "")).lower()
        if any(sub in kind for sub in _SUB_FEATURES):
            continue
        diameter = _num(feature.get("diameter_mm")) or _num(feature.get("diameter"))
        if diameter is not None and diameter > 0:
            sections.append({
                "d": diameter,
                "l": _num(feature.get("length_mm")) or _num(feature.get("length")),
                "note": feature.get("note"),
            })
    return sections


def _outer_sections(node: dict) -> list[dict]:
    """Outer profile sections of a body node: prefer ``outer``, else ``features``."""
    if node.get("outer"):
        return _sections_from_list(node.get("outer"))
    return _sections_from_features(node.get("features", []))


def _bore_sections(node: dict) -> list[dict]:
    """Inner bore sections of a body node (empty when solid)."""
    return _sections_from_list(node.get("bore"))


def _rotation_sections(spec: dict) -> list[dict]:
    """Outer sections of the single main-view rotation body (back-compat helper)."""
    return _outer_sections(spec.get("main_view") or {})


def _rotation_parts(spec: dict) -> list[dict]:
    """One body descriptor PER rotation body: {"outer":[...], "bore":[...]}.

    Uses ``parts[]`` when the reader found several bodies, else ``main_view``.
    Only bodies with ≥2 outer sections qualify (a real stepped profile), so
    prismatic parts fall through to the generative model.
    """
    result: list[dict] = []
    for part in spec.get("parts") or []:
        if not isinstance(part, dict):
            continue
        outer = _outer_sections(part)
        if len(outer) >= 2:
            result.append({"outer": outer, "bore": _bore_sections(part)})
    if not result:
        main = spec.get("main_view") or {}
        outer = _outer_sections(main)
        if len(outer) >= 2:
            result.append({"outer": outer, "bore": _bore_sections(main)})
    return result


def _sections_are_complete(sections: list[dict]) -> bool:
    """A missing dimension is an unresolved fact, never a drafting hint."""
    return bool(sections) and all(section.get("d") and section.get("l") for section in sections)


def _emit_profile(
    sections: list[dict], px_per_mm: float, x_left: float, axis_y: float, seg,
    bore: list[dict] | None = None,
) -> float:
    """Emit one stepped rotation profile (both generatrices + its OWN axis).

    The axis is CONSTRUCTED here, never guessed — the profile is exactly
    symmetric about it. When ``bore`` is given the part is hollow: its inner
    stepped contour is drawn symmetric about the same axis. Returns the right
    edge x (for canvas sizing).
    """
    x = x_left
    prev_r = None
    for s in sections:
        length_px = s["l"] * px_per_mm
        r = s["d"] * px_per_mm / 2.0
        if prev_r is None:
            seg(x, axis_y - r, x, axis_y + r)  # left end cap
        elif abs(r - prev_r) > 0.5:
            seg(x, axis_y - prev_r, x, axis_y - r)
            seg(x, axis_y + prev_r, x, axis_y + r)
        seg(x, axis_y - r, x + length_px, axis_y - r)  # top generatrix
        seg(x, axis_y + r, x + length_px, axis_y + r)  # bottom generatrix
        x += length_px
        prev_r = r
    right = x
    seg(x, axis_y - prev_r, x, axis_y + prev_r)  # right end cap

    if bore:
        # Inner bore contour (hollow part), symmetric about the same axis.
        bx = x_left
        prev_br = None
        for s in bore:
            length_px = s["l"] * px_per_mm
            br = s["d"] * px_per_mm / 2.0
            if prev_br is None:
                seg(bx, axis_y - br, bx, axis_y + br)  # bore mouth
            elif abs(br - prev_br) > 0.5:
                seg(bx, axis_y - prev_br, bx, axis_y - br)
                seg(bx, axis_y + prev_br, bx, axis_y + br)
            seg(bx, axis_y - br, bx + length_px, axis_y - br)
            seg(bx, axis_y + br, bx + length_px, axis_y + br)
            bx += length_px
            prev_br = br
        if prev_br is not None and bx < right:
            seg(bx, axis_y - prev_br, bx, axis_y + prev_br)  # bore bottom

    seg(x_left - 20, axis_y, right + 20, axis_y, cls="axis", width="thin")  # centreline
    return right


# ГОСТ 2.301 sheet sizes (short, long) mm; ГОСТ 2.302 standard scale series.
_GOST_SHEETS: dict[str, tuple[float, float]] = {
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "A2": (420.0, 594.0),
    "A1": (594.0, 841.0),
    "A0": (841.0, 1189.0),
}
# ratio = drawn / real, descending (enlargements → 1:1 → reductions).
_STD_SCALES: list[tuple[float, str]] = [
    (100.0, "100:1"), (50.0, "50:1"), (40.0, "40:1"), (20.0, "20:1"),
    (10.0, "10:1"), (5.0, "5:1"), (4.0, "4:1"), (2.5, "2.5:1"), (2.0, "2:1"),
    (1.0, "1:1"),
    (1 / 2, "1:2"), (1 / 2.5, "1:2.5"), (1 / 4, "1:4"), (1 / 5, "1:5"),
    (1 / 10, "1:10"), (1 / 15, "1:15"), (1 / 20, "1:20"), (1 / 25, "1:25"),
    (1 / 40, "1:40"), (1 / 50, "1:50"), (1 / 75, "1:75"), (1 / 100, "1:100"),
    (1 / 200, "1:200"), (1 / 400, "1:400"), (1 / 500, "1:500"), (1 / 1000, "1:1000"),
]
_FRAME_LEFT_MM, _FRAME_OTHER_MM = 20.0, 5.0


def choose_standard_scale(
    obj_w_mm: float, obj_h_mm: float, sheet_format: str, *, landscape: bool = True,
    fill: float = 0.8,
) -> tuple[float, str]:
    """Pick the LARGEST ГОСТ 2.302 scale at which the object fits the sheet.

    Fits within ``fill`` of the inner ГОСТ drawing frame (leaving room for
    dimensions/title block). Returns ``(ratio, label)`` e.g. ``(0.5, "1:2")``.
    """
    short, long = _GOST_SHEETS.get(sheet_format.upper(), _GOST_SHEETS["A4"])
    pw, ph = (long, short) if landscape else (short, long)
    avail_w = (pw - _FRAME_LEFT_MM - _FRAME_OTHER_MM) * fill
    avail_h = (ph - 2 * _FRAME_OTHER_MM) * fill
    if obj_w_mm <= 0 or obj_h_mm <= 0:
        return 1.0, "1:1"
    for ratio, label in _STD_SCALES:
        if obj_w_mm * ratio <= avail_w and obj_h_mm * ratio <= avail_h:
            return ratio, label
    return _STD_SCALES[-1]


def draft_rotation_body(
    spec: dict,
    *,
    px_per_mm: float | None = None,
    sheet_format: str | None = None,
    landscape: bool = True,
) -> CadIR | None:
    """Construct clean stepped-shaft main view(s) from a rotation-body spec.

    Handles MULTIPLE bodies (``parts[]``): each is drafted as an exact symmetric
    stepped profile about its OWN constructed axis, and the bodies are stacked
    vertically so they never overlap. The axis is never "found" — it is built,
    so the contour is always correct.

    When ``sheet_format`` is given, all bodies share one auto-chosen ГОСТ 2.302
    scale and are centred on that sheet. Otherwise they free-fit.

    Returns None when the spec has no usable rotation body (so the caller can
    fall back to the generative model for prismatic/complex geometry).
    """
    parts = _rotation_parts(spec)
    if not parts:
        return None
    for body in parts:
        if not _sections_are_complete(body["outer"]):
            return None
        if body.get("bore") and not _sections_are_complete(body["bore"]):
            return None

    part_dims = [
        (sum(s["l"] for s in body["outer"]), max(s["d"] for s in body["outer"]))
        for body in parts
    ]
    layout_w = max(w for w, _ in part_dims)
    gap_mm = 0.2 * max(h for _, h in part_dims)  # vertical gap between bodies
    layout_h = sum(h for _, h in part_dims) + gap_mm * (len(parts) - 1)

    scale_label: str | None = None
    scale_source: str | None = None
    sheet_info = None
    if sheet_format:
        ratio, scale_label = choose_standard_scale(
            layout_w, layout_h, sheet_format, landscape=landscape
        )
        ppp = 4.0  # paper resolution for the sheet canvas
        px_per_mm = ratio * ppp
        short, long = _GOST_SHEETS.get(sheet_format.upper(), _GOST_SHEETS["A4"])
        pw_mm, ph_mm = (long, short) if landscape else (short, long)
        width_px = pw_mm * ppp
        height_px = ph_mm * ppp
        frame_x0 = _FRAME_LEFT_MM * ppp
        frame_y0 = _FRAME_OTHER_MM * ppp
        frame_w = (pw_mm - _FRAME_LEFT_MM - _FRAME_OTHER_MM) * ppp
        frame_h = (ph_mm - 2 * _FRAME_OTHER_MM) * ppp
        x_left = frame_x0 + max((frame_w - layout_w * px_per_mm) / 2.0, 0.0)
        y_top = frame_y0 + max((frame_h - layout_h * px_per_mm) / 2.0, 0.0)
        scale_source = "sheet_format"
    else:
        if px_per_mm is None:
            px_per_mm = 900.0 / max(layout_w, 1.0)
        x_left = 60.0
        y_top = 60.0
        width_px = height_px = 0.0  # computed from content below

    entities: list[Any] = []

    def seg(x1, y1, x2, y2, cls="contour", width="main"):
        entities.append(
            Segment(
                p1=Point(x=x1, y=y1), p2=Point(x=x2, y=y2),
                line_class=cls, width_class=width, origin="spec", assurance="inferred",
            )
        )

    cursor_y = y_top
    right_edge = x_left
    for body, (_w, h) in zip(parts, part_dims):
        axis_y = cursor_y + h * px_per_mm / 2.0
        right_edge = max(right_edge, _emit_profile(
            body["outer"], px_per_mm, x_left, axis_y, seg, bore=body.get("bore"),
        ))
        cursor_y += (h + gap_mm) * px_per_mm

    if sheet_format:
        from app.ai.cad_ir.schema import SheetInfo

        sheet_info = SheetInfo(
            format=sheet_format.upper(),
            frame=False,
            title_block={"scale": scale_label} if scale_label else {},
        )
    else:
        width_px = right_edge + 60.0
        height_px = cursor_y + 60.0

    extra = {"sheet": sheet_info} if sheet_info is not None else {}
    ir = CadIR(
        source=SourceInfo(image_width=int(width_px), image_height=int(height_px), kind="scan"),
        scale=1.0 / px_per_mm,
        scale_source=scale_source,
        entities=entities,
        recognizer_used="spec-drafter-rotation",
        digitization_status="review_required",
        **extra,
    )
    return ir


def draft_from_spec(
    spec: dict,
    *,
    px_per_mm: float | None = None,
    draft_model: str | None = None,
    router: Any | None = None,
    sheet_format: str | None = None,
    landscape: bool = True,
) -> CadIR | None:
    """Dispatch a structured spec to a drafter (Model 2).

    When ``draft_model`` is set (Settings → Models → Оцифровка → «Чертёжник»),
    a generative model — e.g. a LoRA fine-tuned drafter — turns the spec into
    geometry. On any failure, or when no model is assigned, fall back to the
    deterministic parametric drafter (clean by construction, rotation bodies).

    ``sheet_format`` (+ ``landscape``) lays the part out on that ГОСТ sheet at an
    automatically chosen standard scale (ГОСТ 2.302).
    """
    # Deterministic-first: the parametric drafter is exact by construction for
    # what it handles (rotation bodies) — no model beats it there. A generative
    # model is used ONLY for parts it declines (returns None): prismatic/complex.
    deterministic = draft_rotation_body(
        spec, px_per_mm=px_per_mm, sheet_format=sheet_format, landscape=landscape
    )
    if deterministic is not None:
        return deterministic
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
    return None


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
    sheet_format: str | None = None,
    landscape: bool = True,
) -> CadIR | None:
    """Async variant: usable from inside a running event loop (the digitize task).

    Deterministic-first for rotation bodies: their axis+symmetry are CONSTRUCTED
    (never guessed), so the contour is always correct — and the parametric
    drafter now handles MULTIPLE bodies too. A generative model is used only for
    parts it declines (prismatic/complex), where free drawing is the only option.
    """
    deterministic = draft_rotation_body(
        spec, px_per_mm=px_per_mm, sheet_format=sheet_format, landscape=landscape
    )
    if deterministic is not None:
        return deterministic
    if draft_model:
        try:
            generated = await _draft_generative(
                spec, draft_model, router=router,
                sheet_format=sheet_format, landscape=landscape,
            )
            if generated is not None and generated.entities:
                return generated
        except Exception:  # noqa: BLE001
            pass
    return None


_DRAFT_PROMPT = (
    "Ты — генеративный чертёжник САПР. По СПЕЦИФИКАЦИИ построй ЧИСТУЮ геометрию "
    "главного вида (для тел вращения — продольный контур с осью; для "
    "призматических — очертание и отверстия). ВАЖНО:\n"
    "1) Если деталей/тел НЕСКОЛЬКО — начерти КАЖДОЕ, разнеся их по горизонтали, "
    "не накладывая друг на друга.\n"
    "2) Строго соблюдай ПРОПОРЦИИ по указанным размерам (диаметры/длины из "
    "features и dimensions). Ступень большего диаметра — шире по вертикали.\n"
    "3) Тело вращения симметрично относительно оси; вычерти обе образующие "
    "(верх и низ) и осевую линию.\n"
    "Верни СТРОГО JSON примитивов в изотропном пространстве 0..1000 (обе оси в "
    "одном масштабе, 0,0 — верхний левый угол):\n"
    '{"lines":[[x1,y1,x2,y2],...],"circles":[[cx,cy,r],...],'
    '"arcs":[[cx,cy,r,start_deg,end_deg],...],'
    '"polylines":[{"pts":[[x,y],...],"closed":0}],'
    '"axes":[[x1,y1,x2,y2],...]}\n'
    "Только JSON, без пояснений.\nСПЕЦИФИКАЦИЯ:\n"
)


async def _draft_generative(
    spec: dict,
    draft_model: str,
    *,
    router: Any | None = None,
    sheet_format: str | None = None,
    landscape: bool = True,
) -> CadIR | None:
    """Model 2 (generative): a model turns the spec text into a geometry DSL.

    Handles multiple bodies. When ``sheet_format`` is set, the generated geometry
    is laid out on that ГОСТ sheet at an auto-chosen standard scale.
    """
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
    if not dsl:
        return None
    ir = _dsl_to_ir(dsl)
    if ir is not None and sheet_format:
        _layout_on_sheet(ir, spec, sheet_format, landscape)
    return ir


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
    for ax in dsl.get("axes", []) or []:
        if isinstance(ax, (list, tuple)) and len(ax) >= 4:
            entities.append(Segment(
                p1=_pt(ax[0], ax[1]), p2=_pt(ax[2], ax[3]),
                line_class="axis", width_class="thin",
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


def _entity_points(e: Any) -> list[tuple[float, float]]:
    """All defining points of an entity, for bbox computation."""
    if e.type == "segment":
        return [(e.p1.x, e.p1.y), (e.p2.x, e.p2.y)]
    if e.type == "circle":
        return [(e.center.x - e.radius, e.center.y - e.radius),
                (e.center.x + e.radius, e.center.y + e.radius)]
    if e.type == "arc":
        return [(e.center.x - e.radius, e.center.y - e.radius),
                (e.center.x + e.radius, e.center.y + e.radius)]
    if e.type == "polyline":
        return [(p.x, p.y) for p in e.points]
    return []


def _translate_scale(e: Any, k: float, ox: float, oy: float, bx0: float, by0: float) -> None:
    """In-place map an entity from generated space to sheet px: (p-b0)*k+o."""
    def m(px, py):
        return (px - bx0) * k + ox, (py - by0) * k + oy

    if e.type == "segment":
        e.p1.x, e.p1.y = m(e.p1.x, e.p1.y)
        e.p2.x, e.p2.y = m(e.p2.x, e.p2.y)
    elif e.type in ("circle", "arc"):
        e.center.x, e.center.y = m(e.center.x, e.center.y)
        e.radius *= k
    elif e.type == "polyline":
        for p in e.points:
            p.x, p.y = m(p.x, p.y)


def _layout_on_sheet(ir: CadIR, spec: dict, sheet_format: str, landscape: bool) -> None:
    """Fit generated (relative 0..1000) geometry onto a ГОСТ sheet, in place.

    Chooses a standard ГОСТ 2.302 scale when the spec states a real overall size
    (the largest generated span maps to the largest stated dimension); otherwise
    fits the drawing into the frame without claiming a named scale.
    """
    from app.ai.cad_ir.schema import SheetInfo

    pts = [p for e in ir.entities for p in _entity_points(e)]
    if not pts:
        return
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    bx0, bx1, by0, by1 = min(xs), max(xs), min(ys), max(ys)
    gen_w = max(bx1 - bx0, 1e-6); gen_h = max(by1 - by0, 1e-6)

    short, long = _GOST_SHEETS.get(sheet_format.upper(), _GOST_SHEETS["A4"])
    pw_mm, ph_mm = (long, short) if landscape else (short, long)
    ppp = 4.0  # px per paper mm
    frame_x0 = _FRAME_LEFT_MM * ppp
    frame_y0 = _FRAME_OTHER_MM * ppp
    frame_w = (pw_mm - _FRAME_LEFT_MM - _FRAME_OTHER_MM) * ppp
    frame_h = (ph_mm - 2 * _FRAME_OTHER_MM) * ppp

    # Real overall dimensions from the spec (largest numeric on each axis).
    dims = []
    for d in spec.get("dimensions", []) or []:
        v = _num(d.get("value"))
        if v and v > 0:
            dims.append(v)
    real_max = max(dims) if dims else None

    scale_label = None
    if real_max:
        # Largest generated span == largest real dimension → mm per gen-unit.
        mm_per_unit = real_max / max(gen_w, gen_h)
        real_w = gen_w * mm_per_unit
        real_h = gen_h * mm_per_unit
        ratio, scale_label = choose_standard_scale(real_w, real_h, sheet_format, landscape=landscape)
        k = mm_per_unit * ratio * ppp  # gen-unit → paper px at the standard scale
        ir.scale = 1.0 / (ratio * ppp)  # real mm per px
        ir.scale_source = "sheet_format"
    else:
        k = min(frame_w / gen_w, frame_h / gen_h) * 0.8  # fit-to-frame, 80%

    draw_w = gen_w * k; draw_h = gen_h * k
    ox = frame_x0 + max((frame_w - draw_w) / 2.0, 0.0)
    oy = frame_y0 + max((frame_h - draw_h) / 2.0, 0.0)
    for e in ir.entities:
        _translate_scale(e, k, ox, oy, bx0, by0)

    ir.source.image_width = int(pw_mm * ppp)
    ir.source.image_height = int(ph_mm * ppp)
    ir.sheet = SheetInfo(
        format=sheet_format.upper(), frame=False,
        title_block={"scale": scale_label} if scale_label else {},
    )
