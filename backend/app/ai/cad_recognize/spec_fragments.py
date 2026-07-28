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
import re
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_KIND_PROMPT = (
    "Ты — инженер-конструктор. Посмотри на чертёж и ответь ОДНОЙ строкой JSON "
    "без пояснений:\n"
    '{"part":"название детали из штампа","kind":"rotation|plate|flange|other",'
    '"bodies":1,"views":["front","side","top","section","detail","removed_section"]}\n'
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

# Asked as its own question, and asked LAST. These features are small, they are
# scattered, and mixing them into the "describe the whole contour" question is
# what made the previous contract give up on them entirely: the reader was told
# to leave them out precisely because including them derailed the profile.
_FEATURES_PROMPT = (
    "Перед тобой главный вид тела вращения. Контур уже прочитан — теперь нужны "
    "ТОЛЬКО мелкие элементы, вырезанные в детали. Отвечай ОДНОЙ строкой JSON:\n"
    '{"chamfers":[{"size_mm":1,"angle_deg":45,"location":"left_end|right_end|shoulder",'
    '"at_diameter_mm":null}],'
    '"grooves":[{"axial_position_mm":0,"width_mm":3,"depth_mm":1.5}],'
    '"keyways":[{"axial_start_mm":0,"length_mm":0,"width_mm":0,"depth_mm":0}],'
    '"cross_holes":[{"diameter_mm":0,"axial_position_mm":0,"count":1,"through":true}]}\n'
    "ПРАВИЛА:\n"
    "1) Осевые координаты — от ЛЕВОГО торца детали, в миллиметрах.\n"
    "2) Фаска: «1×45°» на чертеже означает size_mm=1, angle_deg=45. location — "
    "где она: left_end/right_end (торец) или shoulder (уступ между ступенями).\n"
    "3) Канавка (проточка) — узкий кольцевой вырез; width_mm вдоль оси, "
    "depth_mm вглубь от поверхности.\n"
    "4) Шпоночный паз: depth_mm — глубина t1 от поверхности вала.\n"
    "5) Поперечное отверстие — сверление ПОПЕРЁК оси; count, если их несколько "
    "по окружности.\n"
    "6) Чего на чертеже нет — оставь пустым массивом. НЕ придумывай элементы и "
    "НЕ повторяй здесь ступени контура.\n"
    "Только JSON."
)
_FEATURES_SCHEMA = {
    "type": "object",
    "properties": {
        "chamfers": {"type": "array", "maxItems": 32, "items": {"type": "object"}},
        "grooves": {"type": "array", "maxItems": 32, "items": {"type": "object"}},
        "keyways": {"type": "array", "maxItems": 16, "items": {"type": "object"}},
        "cross_holes": {"type": "array", "maxItems": 32, "items": {"type": "object"}},
    },
}

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

# A shaft sheet does not state step lengths — it states a CHAIN of axial
# positions from one face, and the step lengths are their differences. Asking
# for "the length of this step" is asking the model to do that subtraction in
# its head while also reading, and it reliably gets it wrong: live, 150+78+240+
# 470 came back as four step lengths on a part whose overall length is 470.
# So the two things the sheet actually shows are asked for directly.
_CHAIN_PROMPT = (
    "Перед тобой главный вид тела вращения. С чертежа уже прочитаны числа.\n"
    "ДИАМЕТРЫ (на чертеже помечены знаком Ø): {diameters}\n"
    "ОСЕВЫЕ РАЗМЕРЫ (без Ø): {lengths}\n\n"
    "Ответь ОДНОЙ строкой JSON:\n"
    '{{"diameters_mm":[],"bore_diameters_mm":[],"chain_mm":[],"overall_mm":null}}\n'
    "diameters_mm — диаметры ступеней НАРУЖНОГО контура слева направо, по "
    "одному на ступень.\n"
    "ВАЖНО: главный вид дан В РАЗРЕЗЕ, поэтому внутри детали видны размеры "
    "РАСТОЧКИ. Наружный контур — это самая верхняя и самая нижняя линии "
    "силуэта детали; его диаметры измеряются между ними. Размеры, выносимые "
    "изнутри детали (отверстие, расточка, конус), в diameters_mm НЕ включай — "
    "для них есть bore_diameters_mm.\n"
    "bore_diameters_mm — диаметры внутреннего контура слева направо.\n"
    "chain_mm — осевые размеры от ЛЕВОГО торца по возрастанию: положение конца "
    "каждой ступени. Последнее значение равно габаритной длине.\n"
    "overall_mm — габаритная длина детали.\n"
    "diameters_mm и bore_diameters_mm бери ТОЛЬКО из списка ДИАМЕТРЫ. "
    "chain_mm и overall_mm бери ТОЛЬКО из списка ОСЕВЫЕ РАЗМЕРЫ. Не переноси "
    "число из одного списка в другой. Длина chain_mm должна равняться длине "
    "diameters_mm. Только JSON."
)
_CHAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "diameters_mm": {"type": "array", "maxItems": 40, "items": {"type": "number"}},
        "chain_mm": {"type": "array", "maxItems": 40, "items": {"type": "number"}},
        "bore_diameters_mm": {"type": "array", "maxItems": 20, "items": {"type": "number"}},
        "overall_mm": {"type": ["number", "null"]},
    },
    "required": ["diameters_mm", "chain_mm"],
}


_SHAPE_PROMPT = (
    "Посмотри на контур детали на чертеже. Он круглый или прямоугольный? "
    'Ответь ОДНОЙ строкой JSON: {"shape":"circle|rectangle"}. Только JSON.'
)
_SHAPE_SCHEMA = {
    "type": "object",
    "properties": {"shape": {"type": "string", "enum": ["rectangle", "circle"]}},
    "required": ["shape"],
}

# Assignment, not reading: the values are already known from the callouts, and
# the model only says which role each one plays. A read that cannot invent a
# number cannot invent a part — the live failure this replaces had the reader
# call a flange "rectangle" and hand back the BORE diameter as the outline.
_ASSIGN_PROMPT = (
    "С чертежа уже прочитаны размерные надписи:\n{callouts}\n\n"
    "Определи, какую роль играет каждое значение на этой детали. Числа бери "
    "ТОЛЬКО из списка выше, своих не придумывай; если роли на чертеже нет — "
    "поставь null. Ответь ОДНОЙ строкой JSON:\n"
    '{{"outer_diameter_mm":null,"width_mm":null,"height_mm":null,'
    '"thickness_mm":null,"bore_diameter_mm":null,'
    '"bolt_circle_diameter_mm":null,"bolt_hole_diameter_mm":null,'
    '"bolt_hole_count":null}}\n'
    "outer_diameter_mm — наружный габарит круглой детали (самый большой Ø). "
    "bore_diameter_mm — центральное отверстие. thickness_mm — толщина с вида "
    "сбоку или разреза. Только JSON."
)
_ASSIGN_SCHEMA = {
    "type": "object",
    "properties": {
        key: {"type": ["number", "null"]}
        for key in (
            "outer_diameter_mm", "width_mm", "height_mm", "thickness_mm",
            "bore_diameter_mm", "bolt_circle_diameter_mm", "bolt_hole_diameter_mm",
        )
    } | {"bolt_hole_count": {"type": ["integer", "null"]}},
}


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
    """The ГОСТ 2.104 stamp, measured where possible.

    Reading five short fields off a corner crop is a different task from
    finding them on a whole A3 sheet, and it is the one the model is good at.
    Which corner, though, was a guess: a fixed 45%x28% of the sheet is too
    small for a stamp on a wide sheet and drags a view into the crop on a tall
    one. The stamp is a drawn object with measurable edges, so it is measured
    (``sheet_layout``), and the fractions remain only for a sheet whose stamp
    cannot be found at all.
    """
    width, height = image.size
    try:
        from app.ai.cad_recognize.sheet_layout import detect_sheet_layout

        title_block = detect_sheet_layout(image).title_block
    except Exception as exc:  # noqa: BLE001 — a crop must never lose the read
        logger.warning("cad_stamp_layout_failed", error=str(exc)[:160])
        title_block = None
    if title_block is not None and title_block.width > 40 and title_block.height > 20:
        pad_x, pad_y = int(width * 0.01), int(height * 0.01)
        return image.crop((
            max(0, title_block.x0 - pad_x), max(0, title_block.y0 - pad_y),
            min(width, title_block.x1 + pad_x), min(height, title_block.y1 + pad_y),
        ))
    return image.crop((int(width * 0.55), int(height * 0.72), width, height))


# JSON schemas for the bounded questions. Ollama enforces these during
# decoding, so the answer cannot arrive fenced, truncated or mis-nested — the
# three ways free-form answers were losing whole sheets.
_KIND_SCHEMA = {
    "type": "object",
    "properties": {
        "part": {"type": ["string", "null"]},
        "kind": {"type": "string", "enum": ["rotation", "plate", "flange", "other"]},
        "bodies": {"type": ["integer", "null"]},
        # Bounded on purpose. An unbounded array under constrained decoding
        # invites repetition: on a dense spindle sheet the model emitted
        # "section" a dozen times, ran past the token budget and produced
        # nothing parseable at all — the schema turned a good answer into no
        # answer. Every array below carries a ceiling for the same reason.
        "views": {"type": "array", "maxItems": 6, "items": {
            "type": "string", "enum": ["front", "side", "top", "section", "detail", "removed_section"],
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
        "outer": {"type": "array", "maxItems": 40, "items": _SECTION_SCHEMA},
        "bore": {"type": "array", "maxItems": 20, "items": _SECTION_SCHEMA},
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
        "holes": {"type": "array", "maxItems": 64, "items": {"type": "object", "properties": {
            "center_x_mm": {"type": ["number", "null"]},
            "center_y_mm": {"type": ["number", "null"]},
            "diameter_mm": {"type": ["number", "null"]},
        }}},
        "hole_patterns": {"type": "array", "maxItems": 12, "items": {"type": "object", "properties": {
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
        "dimensions": {"type": "array", "maxItems": 80, "items": {"type": "object", "properties": {
            "value": {"type": "string"},
            "applies_to": {"type": ["string", "null"]},
        }, "required": ["value"]}},
        "annotations": {"type": "array", "maxItems": 40, "items": {"type": "object", "properties": {
            "kind": {"type": "string"},
            "text": {"type": "string"},
        }, "required": ["text"]}},
    },
}


def _dominant_view_crop(image):
    """The single densest drawing on the sheet, by connected ink.

    A crop of "everything but the stamp" still hands the model three section
    views, a detail view and a requirements column at once. The main view is the
    largest connected cluster of ink that is not the frame — measured on the
    spindle sheet it is 1796x901 of 2484x1758, i.e. the longitudinal view alone.
    Falls back to the whole drawing area when nothing dominates.
    """
    import cv2
    import numpy as np

    width, height = image.size
    grayscale = np.asarray(image.convert("L"))
    ink = (grayscale < 200).astype(np.uint8)
    try:
        count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(ink, 8)
    except Exception:  # noqa: BLE001
        return _main_view_crop(image)
    best = None
    for index in range(1, count):
        x, y, box_w, box_h, area = stats[index]
        # The sheet frame is a huge, nearly empty box — it is not a view.
        if box_w > width * 0.8 and box_h > height * 0.8:
            continue
        if box_w < width * 0.15 or box_h < height * 0.1:
            continue
        if best is None or area > best[4]:
            best = (x, y, box_w, box_h, area)
    if best is None:
        return _main_view_crop(image)
    x, y, box_w, box_h, _area = best
    pad_x, pad_y = int(width * 0.02), int(height * 0.02)
    return image.crop((
        max(x - pad_x, 0), max(y - pad_y, 0),
        min(x + box_w + pad_x, width), min(y + box_h + pad_y, height),
    ))


def _main_view_crop(image):
    """The drawing itself, without the stamp, the notes column or the margins.

    The single biggest measured win of fragment reading was aiming the stamp
    question at the corner the stamp lives in. The same argument applies to
    geometry: asking "what are the steps of this shaft" while the model is also
    looking at a title block, a technical-requirements column and a frame is a
    harder question than it needs to be.

    The crop is found from ink, not from assumed proportions: the drawing area
    is the bounding box of the ink left after the stamp band and the margins
    are removed, so it follows whatever the sheet actually contains.
    """
    import numpy as np

    width, height = image.size
    grayscale = np.asarray(image.convert("L"))
    ink = grayscale < 200

    # Blank out the ГОСТ frame margins and the bottom-right stamp band before
    # looking for the drawing, or the frame's own rectangle becomes the answer.
    margin_x = max(int(width * 0.03), 4)
    margin_y = max(int(height * 0.03), 4)
    mask = np.zeros_like(ink)
    mask[margin_y : height - margin_y, margin_x : width - margin_x] = True
    mask[int(height * 0.70) :, int(width * 0.50) :] = False
    ink = ink & mask

    rows = np.flatnonzero(ink.any(axis=1))
    cols = np.flatnonzero(ink.any(axis=0))
    if rows.size == 0 or cols.size == 0:
        return image
    pad_x, pad_y = int(width * 0.02), int(height * 0.02)
    left = max(int(cols[0]) - pad_x, 0)
    right = min(int(cols[-1]) + pad_x, width)
    top = max(int(rows[0]) - pad_y, 0)
    bottom = min(int(rows[-1]) + pad_y, height)
    if right - left < width * 0.15 or bottom - top < height * 0.15:
        # A crop that small is a detection failure, not a drawing.
        return image
    return image.crop((left, top, right, bottom))


async def _ask(
    prompt: str, image, *, router: Any, confidential: bool, num_predict: int,
    schema: dict | None = None, audit: list[dict[str, Any]] | None = None,
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
    if audit is not None:
        audit.append({
            "question": prompt.splitlines()[0][:200],
            "model": seeing_model,
            "raw_response": response.text or "",
        })
    parsed = _parse_spec_json(response.text or "")
    return _coerce_spec_containers(parsed) if parsed else {}


# A purpose-built document model rather than a bigger general one. Measured on
# the labelled sheets: at 1.1B parameters it reads the callouts a 30B generalist
# misses — the flange's Ø80H7 bore and its 20±0.1 thickness, the shaft's HRC
# 42...48 and "Остальные Ra 6.3" out of the notes column, GD&T frames and the
# full stamp. This matches what the literature reports for drawings (a 0.23B
# fine-tuned extractor beating frontier models on GD&T): the win on a technical
# sheet is specialisation, not scale.
_OCR_MODEL = "glm-ocr:latest"  # fallback only; the assignment decides
# It repeats its answer when given room, so the budget is small and repeated
# blocks are collapsed.
_OCR_NUM_PREDICT = 700


def _ocr_model_and_url() -> tuple[str, str]:
    """The assigned text-layer model, or the built-in default.

    This used to be a constant and a direct Ollama call, so the one component
    measured to fix fit and roughness recall could not be swapped, compared or
    even seen from the settings UI. It is a routed task now (cad_text_ocr), and
    the constant survives only as the fallback for a database that has not been
    seeded yet.
    """
    from app.config import settings

    url = str(settings.ollama_url).rstrip("/")
    try:
        from app.ai.schemas import AITask
        from app.ai.task_routing import resolve_model

        model, _provider = resolve_model(AITask.CAD_TEXT_OCR)
    except Exception as exc:  # noqa: BLE001 — routing must never lose the layer
        logger.warning("cad_ocr_routing_failed", error=str(exc)[:160])
        model = None
    return (model or _OCR_MODEL), url


async def read_callouts_with_ocr(image, *, router: Any = None) -> dict:
    """Dimensions and annotations transcribed by the document model.

    Returns the same shape as the callout question so the two can be merged.
    Lines are deduplicated because the model loops, and nothing is invented:
    a line becomes a dimension only if it carries a number, an annotation only
    if it names a known kind.
    """
    import base64
    import io as _io
    import re

    import httpx

    model, ollama_url = _ocr_model_and_url()
    buffer = _io.BytesIO()
    image.save(buffer, format="PNG")
    payload = {
        "model": model,
        "prompt": "Прочитай все надписи и размеры с этого чертежа.",
        "images": [base64.b64encode(buffer.getvalue()).decode()],
        "stream": False,
        "think": False,
        "options": {"num_predict": _OCR_NUM_PREDICT, "temperature": 0},
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=5.0)) as client:
            response = await client.post(f"{ollama_url}/api/generate", json=payload)
            response.raise_for_status()
            text = (response.json().get("response") or "")
    except Exception as exc:  # noqa: BLE001 — one lost layer, not the sheet
        logger.warning("cad_ocr_layer_failed", error=str(exc)[:200])
        return {}

    seen: set[str] = set()
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().strip("`").strip()
        if not line or line.lower() in seen:
            continue
        seen.add(line.lower())
        lines.append(line)

    kinds = (
        ("roughness", re.compile(r"\bR[az]\s*\d", re.IGNORECASE)),
        ("hardness", re.compile(r"\bHRC|\bHB\b|твёрд|тверд", re.IGNORECASE)),
        ("thread", re.compile(r"\bM\d+\s*[x×]", re.IGNORECASE)),
        ("material", re.compile(r"сталь|чугун|бронз|латун|алюмин", re.IGNORECASE)),
    )
    dimensions: list[dict] = []
    annotations: list[dict] = []
    for line in lines:
        matched = None
        for kind, pattern in kinds:
            if pattern.search(line):
                matched = kind
                break
        if matched:
            annotations.append({"kind": matched, "text": line[:200]})
            continue
        if re.search(r"\d", line) and len(line) <= 60:
            dimensions.append({"value": line[:60], "applies_to": None})
    return {"dimensions": dimensions, "annotations": annotations}


# A standard's number is not a dimension. "Сталь 55 ГОСТ 1050-2013" and
# "AT6 по ГОСТ 19860-73" put 1050, 2013, 19860 and 73 into the candidate list
# the reader is told to choose its diameters and axial positions from — on the
# spindle sheet the three largest "dimensions" offered were 19860, 2013 and
# 1050, all of them citations.
_STANDARD_REFERENCE = re.compile(
    r"(?:ГОСТ|ОСТ|СТП|ТУ|ISO|DIN|EN|ANSI|ASME)\s*[Рр]?\s*[\d]+(?:[.\-–—]\d+)*",
    re.IGNORECASE,
)


def _matches_callout(value: float, candidates: list[float], tol: float = 0.02) -> bool:
    """Is this number one the sheet actually carries? (2% or 0.5 mm.)"""
    window = max(0.5, abs(value) * tol)
    return any(abs(value - candidate) <= window for candidate in candidates)


async def _read_cut_features(
    image, outer: list[dict], *, router: Any, confidential: bool,
    audit: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict]]:
    """Chamfers, grooves, keyways and cross-drillings, as their own question.

    Every one of them is checked against the contour that was already read: a
    groove 900 mm along a 470 mm shaft, or a keyway deeper than the shaft's
    radius, is a misread rather than a feature — and one bad entry must not cost
    the rest, so entries are dropped individually.
    """
    answer = await _ask(
        _FEATURES_PROMPT, image, num_predict=1500, schema=_FEATURES_SCHEMA,
        router=router, confidential=confidential, audit=audit,
    )
    if not answer:
        return {}

    total_length = sum(_num(s.get("length_mm")) or 0.0 for s in outer)
    max_radius = max(
        ((_num(s.get("diameter_mm")) or 0.0) / 2.0 for s in outer), default=0.0
    )

    def _within(position: float | None) -> bool:
        return position is not None and -1e-6 <= position <= total_length + 1e-6

    result: dict[str, list[dict]] = {}
    chamfers = [
        item for item in (answer.get("chamfers") or [])
        if isinstance(item, dict)
        and (_num(item.get("size_mm")) or 0) > 0
        and item.get("location") in ("left_end", "right_end", "shoulder", "bore_mouth")
    ]
    if chamfers:
        result["chamfers"] = chamfers

    grooves = [
        item for item in (answer.get("grooves") or [])
        if isinstance(item, dict)
        and _within(_num(item.get("axial_position_mm")))
        and (_num(item.get("width_mm")) or 0) > 0
        and 0 < (_num(item.get("depth_mm")) or 0) < max(max_radius, 1e-9)
    ]
    if grooves:
        result["grooves"] = grooves

    keyways = [
        item for item in (answer.get("keyways") or [])
        if isinstance(item, dict)
        and _within(_num(item.get("axial_start_mm")))
        and _within(
            (_num(item.get("axial_start_mm")) or 0.0) + (_num(item.get("length_mm")) or 0.0)
        )
        and (_num(item.get("width_mm")) or 0) > 0
        and 0 < (_num(item.get("depth_mm")) or 0) < max(max_radius, 1e-9)
        and (_num(item.get("length_mm")) or 0) >= (_num(item.get("width_mm")) or 0)
    ]
    if keyways:
        result["keyways"] = keyways

    holes = [
        item for item in (answer.get("cross_holes") or [])
        if isinstance(item, dict)
        and _within(_num(item.get("axial_position_mm")))
        and 0 < (_num(item.get("diameter_mm")) or 0) < max(2.0 * max_radius, 1e-9)
    ]
    if holes:
        result["cross_holes"] = holes
    return result


def _checked_bore(
    bore: list[dict], outer: list[dict], callouts: dict
) -> tuple[list[dict], str | None]:
    """The bore, but only if the sheet supports it.

    The outer contour earns four guards on the way in — the chain must rise, it
    must not be evenly spaced, its numbers must appear among the callouts, and
    it must agree with the overall size. The bore had none: it came from its own
    answer to a separate question and went straight into the part, so a bore the
    reader invented, or read off the wrong view, became a hole through a real
    shaft. Ø18 where the sheet says Ø80H7 is exactly that failure.

    Rejecting the bore does NOT reject the part: the outer profile stays and the
    reason travels with it, so the reviewer sees a solid shaft plus "the bore was
    not confirmed" instead of a hollow one nobody checked.
    """
    sections = [item for item in bore if isinstance(item, dict)]
    if not sections:
        return [], None

    diameters_seen = _callout_numbers(callouts, "diameter")
    off_sheet = [
        _num(item.get("diameter_mm")) for item in sections
        if _num(item.get("diameter_mm")) is not None
        and not _matches_callout(_num(item.get("diameter_mm")), diameters_seen)
    ]
    if diameters_seen and len(off_sheet) > len(sections) / 2:
        return [], (
            "диаметров расточки нет среди прочитанных выносок: "
            + ", ".join(f"{value:g}" for value in off_sheet[:4])
        )

    outer_diameters = [
        _num(item.get("diameter_mm")) for item in outer if isinstance(item, dict)
    ]
    largest_outer = max((value for value in outer_diameters if value), default=None)
    largest_bore = max(
        (value for value in (_num(item.get("diameter_mm")) for item in sections) if value),
        default=None,
    )
    if largest_outer and largest_bore and largest_bore >= largest_outer:
        return [], (
            f"расточка Ø{largest_bore:g} не меньше наружного Ø{largest_outer:g} — "
            "прочитан не тот контур"
        )

    bore_length = sum(
        value for value in (_num(item.get("length_mm")) for item in sections) if value
    )
    outer_length = sum(
        value for value in
        (_num(item.get("length_mm")) for item in outer if isinstance(item, dict))
        if value
    )
    if bore_length and outer_length and bore_length > outer_length * 1.02:
        return [], (
            f"расточка длиной {bore_length:g} мм длиннее детали ({outer_length:g} мм)"
        )
    return sections, None


def _num(value: Any) -> float | None:
    """Best-effort float from a fragment answer (None for anything else)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# A drawing already says which of its numbers are diameters: it marks them Ø.
# Everything else on a shaft sheet — 150, 78, 240, 470 — is a length. Pooling
# the two into one list, which is what this did, hands the reader a bag of
# numbers and lets it answer "Ø102" when asked for an axial position and "470"
# when asked for a diameter. That is precisely the confusion behind every
# refusal on the spindle sheet: 8 diameters against 6 axial values, an outer
# profile summing to 364 on a part 470 long.
_DIAMETER_MARK = re.compile(r"[ØøΦφ⌀]|\bØ")

# The grade digit of a fit is not a size. "Ø80js6" carries one diameter, 80 —
# but a bare digit scan also yields 6, and "Ø44H7" yields 7, so the candidate
# list the reader chooses its diameters from was salted with 6s and 7s that
# are nowhere on the part. Matched only when the code ENDS the token, so a
# thread pitch ("M75x1,5") and a decimal are left alone.
_FIT_CODE = re.compile(r"(?<=\d)\s*[A-Za-z]{1,2}\d{1,2}(?![\d,.x×])")


def _callout_numbers(callouts: dict, kind: str = "all") -> list[float]:
    """Numbers from the sheet's callouts, largest first.

    ``kind`` selects by the sheet's own marking: ``diameter`` keeps only
    callouts carrying Ø, ``linear`` keeps only those without it, ``all`` keeps
    everything (used where the distinction does not apply, e.g. plausibility
    checks that just need to know a number was on the sheet).
    """
    import re as _re

    values: list[float] = []
    for item in (callouts.get("dimensions") or []) + (callouts.get("annotations") or []):
        text = str((item or {}).get("value") or (item or {}).get("text") or "")
        text = _STANDARD_REFERENCE.sub(" ", text)
        marked = bool(_DIAMETER_MARK.search(text))
        text = _FIT_CODE.sub(" ", text)
        if kind == "diameter" and not marked:
            continue
        if kind == "linear" and marked:
            continue
        for match in _re.finditer(r"\d+(?:[.,]\d+)?", text):
            try:
                value = float(match.group().replace(",", "."))
            except ValueError:
                continue
            if 0 < value <= 100_000:
                values.append(value)
    return sorted({round(v, 3) for v in values}, reverse=True)


async def _sections_from_chain(
    image, callouts: dict, *, router: Any, confidential: bool,
    audit: list[dict[str, Any]] | None = None,
) -> tuple[list[dict], str | None]:
    """Step lengths as DIFFERENCES of the axial chain the sheet draws.

    Returns the sections and, when the reading is self-inconsistent, the reason
    — a chain that does not increase, or one whose last value disagrees with the
    stated overall, is a misread and says so instead of producing a part.
    """
    diameters_seen = _callout_numbers(callouts, "diameter")
    lengths_seen = _callout_numbers(callouts, "linear")
    candidates = _callout_numbers(callouts)
    if not candidates:
        return [], "выноски не прочитаны"
    answer = await _ask(
        _CHAIN_PROMPT.format(
            diameters=", ".join(f"{value:g}" for value in diameters_seen[:24]) or "—",
            lengths=", ".join(f"{value:g}" for value in lengths_seen[:24]) or "—",
        ),
        image, num_predict=900,
        schema=_CHAIN_SCHEMA, router=router, confidential=confidential,
        audit=audit,
    )
    if not answer:
        return [], None

    def numbers(key: str) -> list[float]:
        return [
            float(v) for v in (answer.get(key) or [])
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
        ]

    diameters, chain = numbers("diameters_mm"), numbers("chain_mm")
    if len(diameters) < 2 or len(chain) != len(diameters):
        return [], (
            f"цепочка не сходится с числом ступеней ({len(diameters)} диаметров, "
            f"{len(chain)} осевых размеров)"
        )
    if any(b <= a for a, b in zip(chain, chain[1:], strict=False)):
        return [], "осевые размеры не возрастают — это не размерная цепочка"

    # An evenly spaced chain is a fabrication, not a reading. Asked for the
    # axial positions of a ten-step spindle, the reader answered 0, 45, 90,
    # 135 ... 405 — a part whose every step is the same length is not what the
    # sheet shows, and none of those numbers appears among its callouts. The
    # length check above passes such an answer happily, so it needs its own.
    steps = [b - a for a, b in zip(chain, chain[1:], strict=False)]
    if len(steps) >= 3 and max(steps) - min(steps) <= max(0.5, max(steps) * 0.02):
        return [], (
            f"осевые размеры идут ровным шагом {steps[0]:g} мм — "
            "это выдуманная цепочка, а не прочитанная с листа"
        )
    # Checked against the AXIAL list specifically: a chain value that only
    # matches a diameter is the reader crossing the two lists, which is the
    # failure this split exists to catch.
    off_sheet = [
        value for value in chain
        if not _matches_callout(value, lengths_seen or candidates)
    ]
    if len(off_sheet) > len(chain) / 2:
        return [], (
            "больше половины осевых размеров нет среди прочитанных выносок "
            f"({', '.join(f'{v:g}' for v in off_sheet[:6])})"
        )

    overall = answer.get("overall_mm")
    if isinstance(overall, (int, float)) and overall > 0:
        if abs(chain[-1] - float(overall)) > max(0.5, float(overall) * 0.02):
            return [], (
                f"конец цепочки {chain[-1]:g} мм не совпадает с габаритом "
                f"{float(overall):g} мм"
            )

    bores = numbers("bore_diameters_mm")
    if bores and diameters and max(bores) >= max(diameters):
        # A bore cannot be the widest thing on a turned part; when it is, the
        # reader has handed back internal dimensions as the outer contour —
        # the exact confusion a sectioned main view invites.
        return [], (
            f"наибольший внутренний Ø{max(bores):g} не меньше наружного "
            f"Ø{max(diameters):g} — прочитаны размеры расточки вместо контура"
        )

    sections: list[dict] = []
    previous = 0.0
    for diameter, position in zip(diameters, chain, strict=True):
        sections.append({
            "diameter_mm": diameter,
            "length_mm": round(position - previous, 3),
        })
        previous = position
    return sections, None


async def _profile_by_assignment(
    image, callouts: dict, *, router: Any, confidential: bool,
    audit: list[dict[str, Any]] | None = None,
) -> dict | None:
    """Build the outline by ASSIGNING already-read numbers to roles.

    Two narrow questions instead of one broad one: what shape is the contour,
    and which of the numbers the sheet states plays which part. Every returned
    value is checked against the callout list and dropped if it is not there,
    so this path structurally cannot introduce a dimension the sheet never had.
    """
    candidates = _callout_numbers(callouts)
    if not candidates:
        return None
    ask = {"router": router, "confidential": confidential, "audit": audit}
    shape_answer = await _ask(_SHAPE_PROMPT, image, num_predict=120, schema=_SHAPE_SCHEMA, **ask)
    shape = str(shape_answer.get("shape") or "").strip().lower()
    if shape not in ("circle", "rectangle"):
        return None

    listed = ", ".join(f"{value:g}" for value in candidates[:24])
    answer = await _ask(
        _ASSIGN_PROMPT.format(callouts=listed), image,
        num_predict=400, schema=_ASSIGN_SCHEMA, **ask,
    )
    if not answer:
        return None

    allowed = set(candidates)

    def taken(key: str) -> float | None:
        value = answer.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        value = float(value)
        # The number must be one the sheet actually states.
        return value if any(abs(value - c) <= max(0.05, c * 0.005) for c in allowed) else None

    profile: dict[str, Any] = {"shape": shape}
    if shape == "circle":
        outer = taken("outer_diameter_mm")
        if not outer:
            return None
        profile["diameter_mm"] = outer
    else:
        width, height = taken("width_mm"), taken("height_mm")
        if not width or not height:
            return None
        profile["width_mm"], profile["height_mm"] = width, height
    profile["thickness_mm"] = taken("thickness_mm")

    holes: list[dict] = []
    bore = taken("bore_diameter_mm")
    if bore:
        holes.append({"center_x_mm": 0.0, "center_y_mm": 0.0, "diameter_mm": bore})
    profile["holes"] = holes

    patterns: list[dict] = []
    pcd = taken("bolt_circle_diameter_mm")
    bolt = taken("bolt_hole_diameter_mm")
    count = answer.get("bolt_hole_count")
    if pcd and bolt and isinstance(count, int) and 2 <= count <= 128:
        patterns.append({
            "kind": "bolt_circle", "count": count,
            "bolt_circle_diameter_mm": pcd, "hole_diameter_mm": bolt,
            "start_angle_deg": 0.0,
        })
    profile["hole_patterns"] = patterns
    return profile


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
    # Geometry questions get the drawing without the stamp and the notes; the
    # classification and callout questions keep the whole sheet, because they
    # are ABOUT the sheet.
    geometry_view = _overview(_main_view_crop(image))
    # The chain question gets the densest single view rather than the whole
    # drawing area: on a busy sheet the difference is one longitudinal view
    # against that view plus three sections, a detail and the notes column.
    chain_view = _overview(_dominant_view_crop(image))
    fragment_answers: list[dict[str, Any]] = []
    ask = {
        "router": router,
        "confidential": confidential,
        "audit": fragment_answers,
    }

    kind_answer = await _ask(_KIND_PROMPT, overview, num_predict=400, schema=_KIND_SCHEMA, **ask)
    kind = str(kind_answer.get("kind") or "").strip().lower()
    part = str(kind_answer.get("part") or "").strip()

    stamp = await _ask(_STAMP_PROMPT, _stamp_crop(image), num_predict=600, schema=_STAMP_SCHEMA, **ask)

    # Callouts are read BEFORE geometry: the outline of a flat part is then
    # assembled by assigning those numbers to roles instead of reading them a
    # second time, which is what let a flange come back as a "rectangle" whose
    # diameter was actually its bore.
    callouts = await _ask(_CALLOUT_PROMPT, overview, num_predict=3000, schema=_CALLOUT_SCHEMA, **ask)
    # The document model reads the sheet's text better than the general reader;
    # its lines are ADDED to what the general reader found rather than replacing
    # it, since the two miss different things.
    ocr = await read_callouts_with_ocr(overview, router=router)
    if ocr:
        known = {str((d or {}).get("value") or "").strip().lower()
                 for d in (callouts.get("dimensions") or [])}
        callouts.setdefault("dimensions", []).extend(
            d for d in ocr.get("dimensions") or []
            if str(d.get("value") or "").strip().lower() not in known
        )
        known_notes = {str((a or {}).get("text") or "").strip().lower()
                       for a in (callouts.get("annotations") or [])}
        callouts.setdefault("annotations", []).extend(
            a for a in ocr.get("annotations") or []
            if str(a.get("text") or "").strip().lower() not in known_notes
        )

    body: dict[str, Any] = {"type": _type_label(kind)}
    unresolved: list[str] = []
    if kind == "rotation":
        # Chain first: the sheet states positions, not lengths.
        outer, chain_problem = await _sections_from_chain(
            chain_view, callouts, router=router, confidential=confidential,
            audit=fragment_answers,
        )
        geometry: dict = {}
        if not outer:
            if chain_problem:
                unresolved.append(f"размерная цепочка: {chain_problem}")
            geometry = await _ask(_ROTATION_PROMPT, geometry_view, num_predict=4000, schema=_ROTATION_SCHEMA, **ask)
            outer = [s for s in (geometry.get("outer") or []) if isinstance(s, dict)]
        else:
            geometry = await _ask(_ROTATION_PROMPT, geometry_view, num_predict=4000, schema=_ROTATION_SCHEMA, **ask)
        # The bore answers a separate question, so it is checked against the
        # sheet the same way the outer chain is — an unchecked cavity is a hole
        # through a real part.
        bore, bore_problem = _checked_bore(
            geometry.get("bore") or [], outer, callouts
        )
        if outer:
            body["outer"] = outer
        else:
            unresolved.append("ступенчатый контур не прочитан")
        if bore:
            body["bore"] = bore
        elif bore_problem:
            unresolved.append(f"расточка: {bore_problem}")
        if outer:
            # Only worth asking once there is a contour to hang them on: these
            # are positions along a profile, and without the profile they have
            # nothing to be positioned against.
            body.update(
                await _read_cut_features(
                    geometry_view, outer, router=router, confidential=confidential,
                    audit=fragment_answers,
                )
            )
    elif kind in ("plate", "flange"):
        profile = await _profile_by_assignment(
            geometry_view, callouts, router=router, confidential=confidential,
            audit=fragment_answers,
        )
        if profile is None:
            # Fall back to reading the outline directly when the sheet's
            # callouts were not enough to assign roles from.
            profile = await _ask(
                _PROFILE_PROMPT, geometry_view, num_predict=2000,
                schema=_PROFILE_SCHEMA, **ask,
            )
        if profile and profile.get("shape"):
            body["profile"] = profile
        else:
            unresolved.append("контур плоской детали не прочитан")
    else:
        unresolved.append(
            f"класс детали не определён (ответ модели: {kind or 'пусто'})"
        )

    assembled: dict[str, Any] = {
        "schema_version": 1,
        "part": part or str(stamp.get("name") or ""),
        "main_view": body,
        "parts": [],
        "views": [
            {"kind": view, "body_index": 0}
            for view in (kind_answer.get("views") or [])
            if view in ("front", "top", "side", "section", "detail", "removed_section")
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
        "fragment_answers": fragment_answers,
    }
    fragments = assembled.pop("fragments")
    fragment_answers = assembled.pop("fragment_answers")
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
        validated["fragment_answers"] = fragment_answers
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


async def read_fragments_consensus(
    image_bytes: bytes, *, passes: int, router: Any | None = None,
    confidential: bool = True,
) -> dict:
    """Read the sheet in fragments several times and keep what agrees.

    A single fragment read is still a single bet on a stochastic model, and the
    reader is not merely inaccurate — it is INCONSISTENT: two passes over the
    same shaft disagreed about how many steps it has (4 versus 12). Reading once
    and shipping the answer turns that coin flip into a dimension nobody can
    tell from a measured one.

    So the same intersection the whole-sheet path has always used is applied
    here. Consensus can only REMOVE values, never add them, so this cannot
    invent a part; at worst it declines to build one a single lucky pass would
    have built, and says which value the passes disagreed on.
    """
    from app.ai.cad_recognize.spec_consensus import consensus_spec

    reads: list[dict] = []
    for _attempt in range(max(1, passes)):
        spec = await read_spec_by_fragments(
            image_bytes, router=router, confidential=confidential
        )
        if spec:
            reads.append(spec)
    if not reads:
        return {}
    merged = consensus_spec(reads)
    if not merged:
        return {}
    # Telemetry the contract drops: which narrow question failed, and how the
    # passes compared. Taken from the richest read so a pass that simply
    # answered less does not erase it.
    richest = max(reads, key=lambda spec: len(str(spec.get("fragments") or "")))
    if richest.get("fragments"):
        merged["fragments"] = richest["fragments"]
    merged["reader_attempts"] = [
        {"pass": index + 1, "mode": "fragments", "spec": spec}
        for index, spec in enumerate(reads)
    ]
    return merged


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

    BOTH paths go through consensus. Until now the fragment path returned its
    single read directly whenever it produced geometry — which is the common
    case — so in a normal run the multi-pass agreement this pipeline advertises
    never happened at all, and ``passes`` only ever reached the fallback.

    The stamp is taken from the fragment read even when geometry came from the
    fallback: on the flange it read all four fields correctly where whole-sheet
    reading returned an empty title block.
    """
    from app.ai.cad_recognize.spec_vectorize import read_drawing_spec_consensus

    fragments = await read_fragments_consensus(
        image_bytes, passes=passes, router=router, confidential=confidential
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
        whole["fragment_reader_attempts"] = fragments.get("reader_attempts") or []
    return whole
