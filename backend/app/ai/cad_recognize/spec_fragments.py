"""Read the sheet as several narrow questions instead of one huge one.

Asking a model to emit a whole EngineeringDrawingSpec in a single answer is
where most of the observed damage came from: the answer ran past the output
limit mid-object, mis-nested a top-level field, or simply came back empty, and
in every case the ENTIRE sheet was lost. Live, two passes over the same shaft
even disagreed about how many steps it has (4 versus 12).

Here the sheet is read as a short sequence of bounded questions — what kind of
part is this, what does the stamp say, what is the profile, what are the
callouts — each answering with a small JSON object. Three things improve at
once: a small answer cannot truncate, a failed question costs one fragment
instead of the drawing, and a question aimed at a crop (the stamp lives in the
bottom-right corner) is a far easier question than the same one aimed at an A3
sheet.

The assembled result is the same ``EngineeringDrawingSpec`` the rest of the
pipeline consumes, and it goes through the same coercion and validation — this
changes how the sheet is READ, not what the contract means.
"""

from __future__ import annotations

import base64
import io
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_KIND_PROMPT = (
    "Ты — инженер-конструктор. Посмотри на чертёж и ответь ОДНОЙ строкой JSON "
    "без пояснений:\n"
    '{"part":"название детали из штампа","kind":"rotation|plate|flange|other",'
    '"bodies":1,"views":["front","side","top","section"]}\n'
    "kind: rotation — тело вращения (вал, шпиндель, втулка); plate — плоская "
    "деталь с толщиной; flange — круглая пластина/фланец; other — остальное.\n"
    "views — только те проекции, что реально есть на листе.\n"
    "Кириллицу пиши буквами, не экранируй. Только JSON."
)

_STAMP_PROMPT = (
    "Перед тобой основная надпись (штамп) чертежа по ГОСТ 2.104. Прочитай "
    "поля и верни ОДНОЙ строкой JSON без пояснений:\n"
    '{"designation":"обозначение","name":"наименование","material":"материал",'
    '"scale":"масштаб","mass":"масса"}\n'
    "Пиши ТОЛЬКО то, что действительно написано. Непрочитанное поле — null. "
    "Кириллицу пиши буквами, не экранируй. Только JSON."
)

_ROTATION_PROMPT = (
    "Перед тобой главный вид тела вращения. Опиши ТОЛЬКО наружный ступенчатый "
    "контур слева направо и, если деталь полая, внутренний. ОДНОЙ строкой JSON:\n"
    '{"outer":[{"diameter_mm":0,"length_mm":0,"note":null}],'
    '"bore":[{"diameter_mm":0,"length_mm":0}]}\n'
    "ПРАВИЛА:\n"
    "1) length_mm — ОСЕВАЯ ДЛИНА ИМЕННО ЭТОЙ ступени, а НЕ размер из цепочки "
    "от торца и НЕ габарит детали. Если размеры даны цепочкой — вычисли "
    "разности соседних.\n"
    "2) Сумма всех length_mm обязана равняться габаритной длине детали. "
    "Проверь это перед ответом.\n"
    "3) Фаски, канавки, шпонпазы и поперечные отверстия в outer НЕ включай.\n"
    "4) Не уверен в длине ступени — поставь null, не выдумывай.\n"
    "Только JSON."
)

_PROFILE_PROMPT = (
    "Перед тобой плоская деталь (пластина или фланец). Опиши её контур ОДНОЙ "
    "строкой JSON:\n"
    '{"shape":"rectangle|circle","width_mm":null,"height_mm":null,'
    '"diameter_mm":null,"thickness_mm":null,'
    '"holes":[{"center_x_mm":0,"center_y_mm":0,"diameter_mm":0}],'
    '"hole_patterns":[{"kind":"bolt_circle","count":4,'
    '"bolt_circle_diameter_mm":0,"hole_diameter_mm":0,"start_angle_deg":0}]}\n'
    "ПРАВИЛА: координаты отверстий — от ЦЕНТРА контура (+x вправо, +y вверх). "
    "thickness_mm бери с вида сбоку или разреза; не видно — null. Равномерный "
    "массив отверстий описывай ОДНИМ hole_patterns, а не перечислением. "
    "Только JSON."
)

_CALLOUT_PROMPT = (
    "Выпиши с чертежа размерные надписи и технические обозначения. ОДНОЙ "
    "строкой JSON:\n"
    '{"dimensions":[{"value":"Ø80js6","applies_to":null}],'
    '"annotations":[{"kind":"roughness|hardness|tolerance|thread|material|other",'
    '"text":"Ra 1,6"}]}\n'
    "value — ровно как написано на чертеже, вместе с допуском и посадкой. "
    "Ничего не добавляй от себя. Кириллицу не экранируй. Только JSON."
)


def _encode(image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def _overview(image, side: int = 1400):
    context = image.copy()
    context.thumbnail((side, side))
    return context


def _stamp_crop(image):
    """The ГОСТ 2.104 stamp: bottom-right corner, kept at source resolution.

    Reading five short fields off a corner crop is a different task from
    finding them on a whole A3 sheet, and it is the one the model is good at.
    """
    width, height = image.size
    left = int(width * 0.55)
    top = int(height * 0.72)
    return image.crop((left, top, width, height))


# JSON schemas for the bounded questions. Ollama enforces these during
# decoding, so the answer cannot arrive fenced, truncated or mis-nested — the
# three ways free-form answers were losing whole sheets.
_KIND_SCHEMA = {
    "type": "object",
    "properties": {
        "part": {"type": ["string", "null"]},
        "kind": {"type": "string", "enum": ["rotation", "plate", "flange", "other"]},
        "bodies": {"type": ["integer", "null"]},
        "views": {"type": "array", "items": {
            "type": "string", "enum": ["front", "side", "top", "section"],
        }},
    },
    "required": ["kind"],
}
_STAMP_SCHEMA = {
    "type": "object",
    "properties": {
        field: {"type": ["string", "null"]}
        for field in ("designation", "name", "material", "scale", "mass")
    },
}
_SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "diameter_mm": {"type": ["number", "null"]},
        "length_mm": {"type": ["number", "null"]},
        "note": {"type": ["string", "null"]},
    },
}
_ROTATION_SCHEMA = {
    "type": "object",
    "properties": {
        "outer": {"type": "array", "items": _SECTION_SCHEMA},
        "bore": {"type": "array", "items": _SECTION_SCHEMA},
    },
    "required": ["outer"],
}
_PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "shape": {"type": "string", "enum": ["rectangle", "circle"]},
        "width_mm": {"type": ["number", "null"]},
        "height_mm": {"type": ["number", "null"]},
        "diameter_mm": {"type": ["number", "null"]},
        "thickness_mm": {"type": ["number", "null"]},
        "holes": {"type": "array", "items": {"type": "object", "properties": {
            "center_x_mm": {"type": ["number", "null"]},
            "center_y_mm": {"type": ["number", "null"]},
            "diameter_mm": {"type": ["number", "null"]},
        }}},
        "hole_patterns": {"type": "array", "items": {"type": "object", "properties": {
            "kind": {"type": "string"},
            "count": {"type": ["integer", "null"]},
            "bolt_circle_diameter_mm": {"type": ["number", "null"]},
            "hole_diameter_mm": {"type": ["number", "null"]},
            "start_angle_deg": {"type": ["number", "null"]},
        }}},
    },
    "required": ["shape"],
}
_CALLOUT_SCHEMA = {
    "type": "object",
    "properties": {
        "dimensions": {"type": "array", "items": {"type": "object", "properties": {
            "value": {"type": "string"},
            "applies_to": {"type": ["string", "null"]},
        }, "required": ["value"]}},
        "annotations": {"type": "array", "items": {"type": "object", "properties": {
            "kind": {"type": "string"},
            "text": {"type": "string"},
        }, "required": ["text"]}},
    },
}


async def _ask(
    prompt: str, image, *, router: Any, confidential: bool, num_predict: int,
    schema: dict | None = None,
) -> dict:
    """One bounded question. A failure returns {} and never raises."""
    from app.ai.cad_recognize.spec_vectorize import (
        _coerce_spec_containers,
        _first_vision_model,
        _parse_spec_json,
    )
    from app.ai.schemas import AIRequest, AITask, ChatMessage
    from app.ai.task_routing import get_routing_for

    read_task = (
        AITask.CAD_SPEC_READ
        if get_routing_for(AITask.CAD_SPEC_READ).primary
        else AITask.DRAWING_ANALYSIS_VLM
    )
    seeing_model, _chain_can_see = _first_vision_model(read_task)
    request = AIRequest(
        task=read_task,
        messages=[ChatMessage(role="user", content=prompt)],
        images=[_encode(image)],
        confidential=confidential,
        allow_cloud=False,
        preferred_model=seeing_model,
        metadata={"num_predict": num_predict, "json_schema": schema},
    )
    try:
        response = await router.run(request)
    except Exception as exc:  # noqa: BLE001 — one lost fragment, not the sheet
        logger.warning("cad_fragment_failed", error=str(exc)[:200])
        return {}
    parsed = _parse_spec_json(response.text or "")
    return _coerce_spec_containers(parsed) if parsed else {}


async def read_spec_by_fragments(
    image_bytes: bytes, *, router: Any | None = None, confidential: bool = True
) -> dict:
    """Assemble a spec from several narrow reads of the same sheet."""
    from PIL import Image

    from app.ai.cad_recognize.spec_vectorize import (
        EngineeringDrawingSpec,
        SpecReaderNotVisionError,
        _coerce_spec_containers,
        _first_vision_model,
    )
    from app.ai.schemas import AITask
    from app.ai.task_routing import get_routing_for
    from pydantic import ValidationError

    if router is None:
        from app.ai.router import ai_router

        router = ai_router

    read_task = (
        AITask.CAD_SPEC_READ
        if get_routing_for(AITask.CAD_SPEC_READ).primary
        else AITask.DRAWING_ANALYSIS_VLM
    )
    _model, chain_can_see = _first_vision_model(read_task)
    if not chain_can_see:
        raise SpecReaderNotVisionError(
            "слот «Чтение чертежа (VLM)» назначен на модель без зрения. "
            "Назначьте vision-модель в Настройки → Модели → Оцифровка."
        )

    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:  # noqa: BLE001
        return {}

    overview = _overview(image)
    ask = {"router": router, "confidential": confidential}

    kind_answer = await _ask(_KIND_PROMPT, overview, num_predict=400, schema=_KIND_SCHEMA, **ask)
    kind = str(kind_answer.get("kind") or "").strip().lower()
    part = str(kind_answer.get("part") or "").strip()

    stamp = await _ask(_STAMP_PROMPT, _stamp_crop(image), num_predict=600, schema=_STAMP_SCHEMA, **ask)

    body: dict[str, Any] = {"type": _type_label(kind)}
    unresolved: list[str] = []
    if kind == "rotation":
        geometry = await _ask(_ROTATION_PROMPT, overview, num_predict=4000, schema=_ROTATION_SCHEMA, **ask)
        outer = [s for s in (geometry.get("outer") or []) if isinstance(s, dict)]
        bore = [s for s in (geometry.get("bore") or []) if isinstance(s, dict)]
        if outer:
            body["outer"] = outer
        else:
            unresolved.append("ступенчатый контур не прочитан")
        if bore:
            body["bore"] = bore
    elif kind in ("plate", "flange"):
        profile = await _ask(_PROFILE_PROMPT, overview, num_predict=2000, schema=_PROFILE_SCHEMA, **ask)
        if profile.get("shape"):
            body["profile"] = profile
        else:
            unresolved.append("контур плоской детали не прочитан")
    else:
        unresolved.append(
            f"класс детали не определён (ответ модели: {kind or 'пусто'})"
        )

    callouts = await _ask(_CALLOUT_PROMPT, overview, num_predict=3000, schema=_CALLOUT_SCHEMA, **ask)

    assembled: dict[str, Any] = {
        "schema_version": 1,
        "part": part or str(stamp.get("name") or ""),
        "main_view": body,
        "parts": [],
        "views": [
            {"kind": view, "body_index": 0}
            for view in (kind_answer.get("views") or [])
            if view in ("front", "top", "side", "section")
        ],
        "dimensions": [
            d for d in (callouts.get("dimensions") or []) if isinstance(d, dict)
        ],
        "annotations": [
            a for a in (callouts.get("annotations") or []) if isinstance(a, dict)
        ],
        "title_block": {
            key: value for key, value in (stamp or {}).items()
            if key in ("designation", "name", "material", "scale", "mass") and value
        },
        "unresolved": unresolved,
        "optional_unresolved": [],
        # Which questions actually answered — a fragment read that lost the
        # stamp is a different result from one that lost the profile.
        "fragments": {
            "kind": bool(kind_answer), "stamp": bool(stamp),
            "geometry": bool(body.get("outer") or body.get("profile")),
            "callouts": bool(callouts),
        },
    }
    fragments = assembled.pop("fragments")
    # The per-question coercion runs on FRAGMENT answers, where a profile is the
    # top-level object; the assembled spec nests it under main_view, so the
    # cleanup has to run once more here or unusable entries reach validation and
    # take the sheet with them.
    assembled = _coerce_spec_containers(assembled)
    try:
        validated = EngineeringDrawingSpec.model_validate(assembled).model_dump(
            mode="json"
        )
        # The schema drops unknown keys, so this telemetry is re-attached after
        # validation: knowing WHICH question failed is the point of reading in
        # fragments at all.
        validated["fragments"] = fragments
        return validated
    except ValidationError as exc:
        logger.warning(
            "cad_fragment_spec_invalid",
            fields=[".".join(str(p) for p in e["loc"]) for e in exc.errors()[:6]],
        )
        return {}


def _type_label(kind: str) -> str:
    return {
        "rotation": "тело вращения (вал)",
        "plate": "призматическая (пластина)",
        "flange": "фланец",
    }.get(kind, kind or "")


def _has_geometry(spec: dict) -> bool:
    body = spec.get("main_view") or {}
    return bool(body.get("outer") or (body.get("profile") or {}).get("shape"))


async def read_spec_best_effort(
    image_bytes: bytes, *, passes: int = 3, router: Any | None = None,
    confidential: bool = True,
) -> dict:
    """Fragments first, whole-sheet consensus as the fallback for geometry.

    Measured on real sheets: fragment reading is several times faster and gets
    the stamp right (a corner crop is an easy question), but on a dense shaft
    it can fail to extract the stepped profile — which whole-sheet reading with
    consensus sometimes manages. So the fragments run first and the expensive
    read happens only when geometry is what is missing.

    The stamp is taken from the fragment read even when geometry came from the
    fallback: on the flange it read all four fields correctly where whole-sheet
    reading returned an empty title block.
    """
    from app.ai.cad_recognize.spec_vectorize import read_drawing_spec_consensus

    fragments = await read_spec_by_fragments(
        image_bytes, router=router, confidential=confidential
    )
    if fragments and _has_geometry(fragments):
        return fragments

    whole = await read_drawing_spec_consensus(
        image_bytes, passes=passes, router=router, confidential=confidential
    )
    if not whole:
        return fragments
    if fragments:
        if not (whole.get("title_block") or {}):
            whole["title_block"] = fragments.get("title_block") or {}
        if not (whole.get("dimensions") or []):
            whole["dimensions"] = fragments.get("dimensions") or []
        if not (whole.get("views") or []):
            whole["views"] = fragments.get("views") or []
        whole["fragments"] = fragments.get("fragments")
    return whole
