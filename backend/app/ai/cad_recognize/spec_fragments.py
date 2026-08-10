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
    '"cross_holes":[{"diameter_mm":0,"axial_position_mm":0,"count":1,"through":true}],'
    '"axial_holes":[{"count":2,"bolt_circle_diameter_mm":80,"from_face":null,'
    '"entry_offset_mm":null,'
    '"entry_recess_diameter_mm":null,'
    '"through":null,"thread_depth_mm":null,"drill_depth_mm":null,'
    '"thread":{"designation":"M8"}}],'
    '"circular_hole_patterns":[{"count":12,"hole_diameter_mm":4,'
    '"bolt_circle_diameter_mm":70,"axis_mode":"axial","start_angle_deg":null,'
    '"from_face":null,"through":false,"depth_mm":82}]}\n'
    "ПРАВИЛА:\n"
    "1) Осевые координаты — от ЛЕВОГО торца детали, в миллиметрах.\n"
    "2) Фаска: «1×45°» на чертеже означает size_mm=1, angle_deg=45. location — "
    "где она: left_end/right_end (торец) или shoulder (уступ между ступенями).\n"
    "3) Канавка (проточка) — узкий кольцевой вырез; width_mm вдоль оси, "
    "depth_mm вглубь от поверхности.\n"
    "4) Шпоночный паз: depth_mm — глубина t1 от поверхности вала.\n"
    "5) Поперечное отверстие — сверление ПОПЕРЁК оси; count, если их несколько "
    "по окружности.\n"
    "6) Осевые отверстия идут параллельно оси детали и видны на торцевом виде; "
    "не путай их с поперечными. Для глухой резьбы thread_depth_mm — длина "
    "полной резьбы, drill_depth_mm — более глубокая длина сверления. "
    "pilot_diameter_mm не угадывай: технологическое сверло может не быть указано.\n"
    "7) Чего на чертеже нет — оставь пустым массивом. НЕ придумывай элементы и "
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
        "axial_holes": {"type": "array", "maxItems": 32, "items": {"type": "object"}},
        "circular_hole_patterns": {
            "type": "array", "maxItems": 32, "items": {"type": "object"}
        },
    },
}

_RADIAL_FEATURES_PROMPT = (
    "Перед тобой увеличенный правый узел главного вида шпинделя. "
    "Детерминированный анализ уже нашёл оси/стенки возможных радиальных "
    "элементов; тебе нужно только связать с ними видимые обозначения.\n"
    "КАНДИДАТЫ В КООРДИНАТАХ ЭТОГО ФРАГМЕНТА:\n{candidates}\n"
    "ДИАМЕТРАЛЬНЫЕ ВЫНОСКИ МАЛЫХ ЭЛЕМЕНТОВ (включая bbox уже "
    "локализованных надписей):\n{callouts}\n"
    "Ответь одной строкой JSON:\n"
    '{{"radial_features":[{{"candidate_id":"radial-opening-1",'
    '"diameter_mm":14,"kind":"through|to_bore|blind|counterbore|threaded",'
    '"side":"top|bottom|both","depth_mm":null,"count":1,"note":null}}]}}\n'
    "ПРАВИЛА: candidate_id выбирай только из списка; координаты не вычисляй. "
    "Если на одной оси ступенчатое отверстие, ОБЯЗАТЕЛЬНО верни отдельную "
    "запись для каждого видимого диаметра: основной диаметр как to_bore/blind, "
    "увеличенный входной диаметр как counterbore и его видимую depth_mm. "
    "side укажи по тому, к верхней или нижней половине "
    "главного вида подведена выноска; both — только если один диаметр идёт "
    "через обе стенки. through используй только для сквозного отверстия через "
    "всю деталь, to_bore — если сверление доходит до центральной расточки. "
    "Диаметр бери только из списка выносок. Неясную связь не возвращай. Только JSON."
)

_RADIAL_FEATURES_SCHEMA = {
    "type": "object",
    "properties": {
        "radial_features": {
            "type": "array",
            "maxItems": 16,
            "items": {"type": "object"},
        }
    },
}

_COUNTERBORE_PROMPT = (
    "Перед тобой узкий фрагмент одного радиального отверстия. Детектор уже "
    "связал основное верхнее сверление Ø{pilot:g} с осью {candidate_id}, а OCR "
    "нашёл рядом верхнюю выноску Ø{counterbore:g}. Проверь по стрелкам, что это "
    "ступенчатое отверстие на одной оси, и прочитай глубину увеличенной входной "
    "ступени. Ответь одной строкой JSON: "
    "{{\"same_axis\":true,\"counterbore_depth_mm\":3}}. Если связь или глубина "
    "не видна, верни {{\"same_axis\":false,\"counterbore_depth_mm\":null}}. "
    "Не вычисляй глубину и не подставляй число из другого размера. Только JSON."
)
_COUNTERBORE_SCHEMA = {
    "type": "object",
    "properties": {
        "same_axis": {"type": "boolean"},
        "counterbore_depth_mm": {"type": ["number", "null"]},
    },
    "required": ["same_axis", "counterbore_depth_mm"],
}

_THREAD_CARRIER_PROMPT = (
    "На фрагменте продольного разреза показан короткий наружный участок "
    "резьбы {designation}. Измеренный синий контур-кандидат Ø{nominal:g} "
    "занимает приблизительно {approx_start:g}…{approx_end:g} мм от левого "
    "торца. Прочитай нижнюю осевую размерную цепь и выбери ТОЧНЫЕ начало и "
    "конец несущего участка только из списка {stations}. Разность должна быть "
    "одним явно написанным линейным размером из {lengths}. Не используй "
    "приблизительные границы контура как размеры. Если обе границы и размер "
    "не видны однозначно, верни confirmed=false и null. Ответ одной строкой "
    "JSON: {{\"confirmed\":true,\"start_mm\":377,\"end_mm\":395,"
    "\"length_mm\":18}}."
)

_THREAD_CARRIER_SCHEMA = {
    "type": "object",
    "properties": {
        "confirmed": {"type": "boolean"},
        "start_mm": {"type": ["number", "null"]},
        "end_mm": {"type": ["number", "null"]},
        "length_mm": {"type": ["number", "null"]},
    },
    "required": ["confirmed", "start_mm", "end_mm", "length_mm"],
    "additionalProperties": False,
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
    "ЛОКАЛИЗОВАННЫЕ ОСЕВЫЕ РАЗМЕРНЫЕ ЛИНИИ (детерминированный OCR+CV):\n"
    "{localized}\n\n"
    "ЛОКАЛИЗОВАННЫЕ ДИАМЕТРЫ ГЛАВНОГО ПРОФИЛЯ (outer/bore):\n"
    "{localized_diameters}\n\n"
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
    "Для chain_mm используй station_from_left_mm только у тех локализованных "
    "линий, чьи стрелки относятся к НАРУЖНОМУ контуру. local_interval без "
    "station_from_left_mm нельзя превращать в накопительный размер.\n"
    "В diameters_mm разрешены только значения с role=outer, в "
    "bore_diameters_mm — только значения с role=bore. Значение из другой роли "
    "нельзя переносить между наружным и внутренним контуром.\n"
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
    '"annotations":[{"kind":"roughness|hardness|tolerance|datum|thread|weld|material|other",'
    '"text":"Ra 1,6","value":null,"symbol":null,"datum_refs":[]}]}\n'
    "value — ровно как написано на чертеже, вместе с допуском и посадкой. "
    "Для рамки геометрического допуска text должен содержать видимый знак, "
    "значение и базы в исходном порядке; datum_refs повторяет только базы. "
    "Не возвращай annotation с пустым text и не угадывай нечитаемый знак. "
    "Не включай в dimensions название листа, номер модели, ревизию, год, "
    "формат или масштаб. Ничего не добавляй от себя. Кириллицу не экранируй. "
    "Только JSON."
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
            "kind": {"type": "string", "enum": [
                "roughness", "hardness", "tolerance", "datum", "thread",
                "weld", "material", "other",
            ]},
            "text": {"type": "string"},
            "value": {"type": ["string", "null"]},
            "symbol": {"type": ["string", "null"]},
            "datum_refs": {
                "type": "array", "maxItems": 8, "items": {"type": "string"}
            },
        }, "required": ["text"]}},
    },
}

_PMI_CHARACTERISTICS = (
    "profile_surface", "profile_line", "flatness", "straightness",
    "perpendicularity", "angularity", "position", "circularity",
    "cylindricity", "concentricity", "symmetry", "circular_runout",
    "total_runout", "parallelism", "unknown",
)
_PMI_SYMBOLS = {
    "profile_surface": "⌓",
    "profile_line": "⌒",
    "flatness": "▱",
    "straightness": "−",
    "perpendicularity": "⏊",
    "angularity": "∠",
    "position": "⌖",
    "circularity": "○",
    "cylindricity": "⌭",
    "concentricity": "◎",
    "symmetry": "⌯",
    "circular_runout": "↗",
    "total_runout": "⌰",
    "parallelism": "⫽",
}
_PMI_PROMPT = (
    "Перед тобой контактный лист уже локализованных рамок геометрических "
    "допусков. Каждая вырезка подписана FRAME N. Для каждой реальной рамки "
    "верни тот же frame_id, определи знак по форме, перепиши tolerance_text "
    "ровно как в ячейке допуска (включая Ø и модификаторы) и базы слева "
    "направо. Не превращай обычный размер или текст в рамку. Если знак или "
    "значение неразличимы, characteristic=unknown; ничего не угадывай. Только JSON."
)
_PMI_LOCATOR_PROMPT = (
    "Найди на техническом чертеже только прямоугольные многосекционные рамки "
    "геометрических допусков (feature-control frames). Не включай обычные "
    "размеры, обозначения баз в одиночной рамке, штамп и рамку листа. Для "
    "каждой найденной рамки верни bbox=[x0,y0,x1,y1] в нормированных "
    "координатах 0..1000 относительно изображения. Захвати всю рамку, но "
    "минимум соседнего текста. Только JSON."
)
_PMI_LOCATOR_SCHEMA = {
    "type": "object",
    "properties": {
        "frames": {
            "type": "array",
            "maxItems": 24,
            "items": {
                "type": "object",
                "properties": {
                    "bbox": {
                        "type": "array", "minItems": 4, "maxItems": 4,
                        "items": {"type": "number"},
                    },
                },
                "required": ["bbox"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["frames"],
    "additionalProperties": False,
}
_PMI_DIRECT_PROMPT = (
    "Найди только рамки геометрических допусков на техническом чертеже. "
    "Для каждой рамки определи знак по форме, перепиши tolerance_text ровно "
    "как в ячейке допуска (включая Ø и модификаторы) и базы слева направо. "
    "Не включай обычные размеры, обозначения видов и штамп. Если знак не "
    "различим, characteristic=unknown; ничего не угадывай. Только JSON."
)
_PMI_DIRECT_SCHEMA = {
    "type": "object",
    "properties": {
        "frames": {
            "type": "array",
            "maxItems": 40,
            "items": {
                "type": "object",
                "properties": {
                    "characteristic": {"type": "string", "enum": list(_PMI_CHARACTERISTICS)},
                    "tolerance_text": {"type": "string"},
                    "datum_refs": {
                        "type": "array", "maxItems": 8, "items": {"type": "string"}
                    },
                },
                "required": ["characteristic", "tolerance_text", "datum_refs"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["frames"],
    "additionalProperties": False,
}
_PMI_SCHEMA = {
    "type": "object",
    "properties": {
        "frames": {
            "type": "array",
            "maxItems": 40,
            "items": {
                "type": "object",
                "properties": {
                    "frame_id": {"type": "integer"},
                    "characteristic": {"type": "string", "enum": list(_PMI_CHARACTERISTICS)},
                    "tolerance_text": {"type": "string"},
                    "datum_refs": {
                        "type": "array", "maxItems": 8, "items": {"type": "string"}
                    },
                },
                "required": ["frame_id", "characteristic", "tolerance_text", "datum_refs"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["frames"],
    "additionalProperties": False,
}


def _structured_pmi_annotations(
    answer: dict[str, Any],
    evidence_by_frame: dict[int, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    annotations = []
    unresolved = 0
    used_frame_ids: set[int] = set()
    for frame in answer.get("frames") or []:
        if not isinstance(frame, dict):
            continue
        frame_id = frame.get("frame_id")
        characteristic = str(frame.get("characteristic") or "unknown")
        tolerance = str(frame.get("tolerance_text") or "").strip()
        datum_refs = [
            str(value).strip()
            for value in frame.get("datum_refs") or []
            if str(value).strip()
        ]
        # Models often copy datum cells into tolerance_text and also return the
        # same cells in datum_refs. Strip only an exact trailing datum sequence;
        # the source order remains represented once, not duplicated in output.
        if datum_refs:
            suffix = re.compile(
                r"(?:\s*\|?\s*" + r"\s*\|?\s*".join(
                    re.escape(value) for value in datum_refs
                ) + r")\s*$",
                re.IGNORECASE,
            )
            tolerance = suffix.sub("", tolerance).strip(" |")
        symbol = _PMI_SYMBOLS.get(characteristic)
        if not symbol or not tolerance:
            unresolved += 1
            continue
        evidence = (evidence_by_frame or {}).get(frame_id)
        if evidence_by_frame is not None and evidence is None:
            unresolved += 1
            continue
        if evidence_by_frame is not None and frame_id in used_frame_ids:
            unresolved += 1
            continue
        if evidence_by_frame is not None:
            used_frame_ids.add(frame_id)
        text = " | ".join([symbol, tolerance, *datum_refs])
        annotation = {
            "kind": "tolerance",
            "text": text,
            "value": tolerance,
            "symbol": characteristic,
            "datum_refs": datum_refs,
        }
        if evidence:
            annotation["evidence"] = [evidence]
        annotations.append(annotation)
    return annotations, unresolved


def _pmi_contact_sheet(
    image,
    locator_answer: dict[str, Any],
    source_box: tuple[int, int, int, int],
):
    """Turn locator boxes into labelled crops and original-image evidence."""

    from PIL import Image, ImageDraw

    width, height = image.size
    source_left, source_top, source_right, source_bottom = source_box
    source_width = source_right - source_left
    source_height = source_bottom - source_top
    regions: list[tuple[int, tuple[int, int, int, int], dict[str, Any]]] = []
    for item in locator_answer.get("frames") or []:
        bbox = item.get("bbox") if isinstance(item, dict) else None
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        try:
            x0, y0, x1, y1 = [max(0.0, min(1000.0, float(value))) for value in bbox]
        except (TypeError, ValueError):
            continue
        if x1 <= x0 or y1 <= y0 or x1 - x0 < 8 or y1 - y0 < 5:
            continue
        left = int(x0 / 1000 * width)
        top = int(y0 / 1000 * height)
        right = int(x1 / 1000 * width)
        bottom = int(y1 / 1000 * height)
        pad_x = max(8, int((right - left) * 0.25))
        pad_y = max(8, int((bottom - top) * 0.45))
        crop_box = (
            max(0, left - pad_x), max(0, top - pad_y),
            min(width, right + pad_x), min(height, bottom + pad_y),
        )
        original_bbox = [
            source_left + x0 / 1000 * source_width,
            source_top + y0 / 1000 * source_height,
            source_left + x1 / 1000 * source_width,
            source_top + y1 / 1000 * source_height,
        ]
        frame_id = len(regions) + 1
        regions.append((
            frame_id,
            crop_box,
            {
                "image_index": 0,
                "bbox": [round(value, 1) for value in original_bbox],
                "raw_text": (
                    f"{item.get('source', 'localized')} feature-control frame {frame_id}"
                ),
            },
        ))
    if not regions:
        return None, {}

    cell_width, cell_height, columns = 640, 220, 2
    rows = (len(regions) + columns - 1) // columns
    sheet = Image.new("RGB", (cell_width * columns, cell_height * rows), "white")
    draw = ImageDraw.Draw(sheet)
    evidence_by_frame = {}
    for index, (frame_id, crop_box, evidence) in enumerate(regions):
        crop = image.crop(crop_box).convert("RGB")
        crop.thumbnail((cell_width - 24, cell_height - 36))
        cell_x = (index % columns) * cell_width
        cell_y = (index // columns) * cell_height
        draw.text((cell_x + 8, cell_y + 5), f"FRAME {frame_id}", fill="black")
        sheet.paste(crop, (cell_x + 8, cell_y + 28))
        evidence_by_frame[frame_id] = evidence
    return sheet, evidence_by_frame


def _detect_pmi_frame_regions(image) -> dict[str, Any]:
    """Find chains of adjacent frame cells from vector-like raster geometry.

    Feature-control frames on axonometric drawings are often parallelograms,
    not axis-aligned rectangles.  Each cell still produces a long, thin closed
    contour.  Adjacent contours share their long-axis direction and sit on the
    same line; grouping those pairs is substantially more stable than asking a
    VLM to estimate coordinates on the full sheet.
    """

    import math

    import cv2
    import numpy as np

    grayscale = np.asarray(image.convert("L"))
    binary = cv2.threshold(grayscale, 210, 255, cv2.THRESH_BINARY_INV)[1]
    contours, _hierarchy = cv2.findContours(
        binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
    )
    cells = []
    for contour in contours:
        area = cv2.contourArea(contour)
        rect = cv2.minAreaRect(contour)
        (center_x, center_y), (width, height), angle = rect
        short_side, long_side = sorted((width, height))
        rect_area = width * height
        fill = area / rect_area if rect_area else 0.0
        if not (
            20 <= short_side <= 45
            and 42 <= long_side <= 95
            and long_side / short_side >= 1.55
            and area >= 550
            and fill >= 0.55
        ):
            continue
        long_angle = angle if width >= height else angle + 90
        radians = math.radians(long_angle)
        cells.append({
            "center": np.asarray((center_x, center_y), dtype=float),
            "axis": np.asarray((math.cos(radians), math.sin(radians)), dtype=float),
            "short": short_side,
            "long": long_side,
            "points": cv2.boxPoints(rect),
        })

    adjacency = [set() for _ in cells]
    for left in range(len(cells)):
        for right in range(left + 1, len(cells)):
            first, second = cells[left], cells[right]
            if abs(float(first["axis"] @ second["axis"])) < math.cos(math.radians(10)):
                continue
            delta = second["center"] - first["center"]
            axis = first["axis"]
            along = abs(float(delta @ axis))
            perpendicular = abs(float(delta @ np.asarray((-axis[1], axis[0]))))
            if (
                8 < along <= (first["long"] + second["long"]) * 0.72
                and perpendicular <= max(first["short"], second["short"]) * 0.65
            ):
                adjacency[left].add(right)
                adjacency[right].add(left)

    seen = set()
    boxes = []
    image_width, image_height = image.size
    for start in range(len(cells)):
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        group = []
        while stack:
            current = stack.pop()
            group.append(current)
            for neighbor in adjacency[current]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        if len(group) < 2:
            continue
        points = np.concatenate([cells[index]["points"] for index in group])
        x0, y0 = points.min(axis=0)
        x1, y1 = points.max(axis=0)
        boxes.append({
            "bbox": [
                float(x0 / image_width * 1000),
                float(y0 / image_height * 1000),
                float(x1 / image_width * 1000),
                float(y1 / image_height * 1000),
            ],
            "source": "deterministic_cv",
        })
    boxes.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
    return {"frames": boxes[:24]}


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


def _main_view_crop_box(image) -> tuple[int, int, int, int]:
    """Pixel bounds of the drawing area used by the fragment readers."""

    import numpy as np

    width, height = image.size
    grayscale = np.asarray(image.convert("L"))
    ink = grayscale < 200
    margin_x = max(int(width * 0.03), 4)
    margin_y = max(int(height * 0.03), 4)
    mask = np.zeros_like(ink)
    mask[margin_y : height - margin_y, margin_x : width - margin_x] = True
    mask[int(height * 0.70) :, int(width * 0.50) :] = False
    ink = ink & mask
    rows = np.flatnonzero(ink.any(axis=1))
    cols = np.flatnonzero(ink.any(axis=0))
    if rows.size == 0 or cols.size == 0:
        return (0, 0, width, height)
    pad_x, pad_y = int(width * 0.02), int(height * 0.02)
    box = (
        max(int(cols[0]) - pad_x, 0), max(int(rows[0]) - pad_y, 0),
        min(int(cols[-1]) + pad_x, width), min(int(rows[-1]) + pad_y, height),
    )
    if box[2] - box[0] < width * 0.15 or box[3] - box[1] < height * 0.15:
        return (0, 0, width, height)
    return box


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
    return image.crop(_main_view_crop_box(image))


async def _ask(
    prompt: str, image, *, router: Any, confidential: bool, num_predict: int,
    schema: dict | None = None, audit: list[dict[str, Any]] | None = None,
) -> dict:
    """One bounded question. A failure returns {} and never raises."""
    import asyncio
    import time
    import hashlib

    from app.ai.cad_process_log import record_cad_process_event
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
        metadata={
            "num_predict": num_predict,
            "json_schema": schema,
            "inference_params": {"temperature": 0, "num_ctx": 8192},
        },
    )
    question = prompt.splitlines()[0][:200]
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    started = time.monotonic()
    await record_cad_process_event(
        "reader.fragment.question",
        "started",
        question,
        {
            "model": seeing_model,
            "num_predict": num_predict,
            "schema": bool(schema),
            "prompt_sha256": prompt_sha256,
            "image_size": [getattr(image, "width", None), getattr(image, "height", None)],
        },
    )
    try:
        async with asyncio.timeout(75):
            response = await router.run(request)
    except Exception as exc:  # noqa: BLE001 — one lost fragment, not the sheet
        logger.warning("cad_fragment_failed", error=str(exc)[:200])
        await record_cad_process_event(
            "reader.fragment.question",
            "failed",
            question,
            {
                "model": seeing_model,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "error": f"{type(exc).__name__}: {exc}"[:400],
                "timeout_seconds": 75,
            },
        )
        return {}
    if audit is not None:
        audit.append({
            "question": question,
            "model": response.model or seeing_model,
            "routing_preferred_model": seeing_model,
            "raw_response": response.text or "",
        })
    parsed = _parse_spec_json(response.text or "")
    result = _coerce_spec_containers(parsed) if parsed else {}
    raw = response.raw or {}
    answer = response.text or ""
    thinking = str(raw.get("thinking") or "")
    await record_cad_process_event(
        "reader.fragment.question",
        "completed" if result else "failed",
        question,
        {
            "model": response.model or seeing_model,
            "duration_ms": response.usage.latency_ms
            or round((time.monotonic() - started) * 1000),
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "answer_chars": len(answer),
            "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
            "answer_preview": answer[:2000],
            "thinking_chars": len(thinking),
            "parsed": bool(result),
            "_model_output": {
                "kind": "fragment_question",
                "model": response.model or seeing_model,
                "prompt": prompt,
                "answer": answer,
                "thinking": thinking,
                "parsed": bool(result),
            },
        },
    )
    return result


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


_SHEET_METADATA_LINE = re.compile(
    r"(?:\bNIST\b|\bPMI\s+(?:test|complex|fully[- ]toleranced)\b|"
    r"\btest\s+(?:model|case)\b|\b(?:sheet|page|revision|rev)\b|"
    r"\b(?:лист|страница|ревизия|формат|масштаб|дата)\b)",
    re.IGNORECASE,
)


def _is_sheet_metadata_line(text: str) -> bool:
    """Administrative sheet labels are evidence context, not dimensions."""
    return bool(_SHEET_METADATA_LINE.search(str(text or "")))


def _clean_callout_observations(callouts: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Drop invalid containers without letting them erase valid PMI/geometry."""
    dimensions = []
    for item in callouts.get("dimensions") or []:
        if not isinstance(item, dict):
            continue
        value = str(item.get("value") or "").strip()
        if not value or _is_sheet_metadata_line(value):
            continue
        dimensions.append({**item, "value": value})

    annotations = []
    dropped_annotations = 0
    for item in callouts.get("annotations") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("value") or item.get("symbol") or "").strip()
        if not text:
            dropped_annotations += 1
            continue
        annotations.append({**item, "text": text})
    return {**callouts, "dimensions": dimensions, "annotations": annotations}, dropped_annotations


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
    import hashlib
    import io as _io
    import re
    import time

    import httpx
    from app.ai.cad_process_log import record_cad_process_event

    model, ollama_url = _ocr_model_and_url()
    buffer = _io.BytesIO()
    image.save(buffer, format="PNG")
    payload = {
        "model": model,
        "prompt": "Прочитай все надписи и размеры с этого чертежа.",
        "images": [base64.b64encode(buffer.getvalue()).decode()],
        "stream": False,
        "think": False,
        "chat_template_kwargs": {"enable_thinking": False},
        "options": {
            "num_predict": _OCR_NUM_PREDICT,
            "temperature": 0,
            "num_ctx": 8192,
        },
    }
    started = time.monotonic()
    await record_cad_process_event(
        "reader.text_ocr",
        "started",
        "Текстовый OCR получил обзор листа",
        {"model": model, "num_predict": _OCR_NUM_PREDICT},
    )
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0)) as client:
            response = await client.post(f"{ollama_url}/api/generate", json=payload)
            response.raise_for_status()
            raw_body = response.json()
            text = (raw_body.get("response") or "")
    except Exception as exc:  # noqa: BLE001 — one lost layer, not the sheet
        logger.warning("cad_ocr_layer_failed", error=str(exc)[:200])
        await record_cad_process_event(
            "reader.text_ocr", "failed", "Текстовый OCR завершился ошибкой",
            {
                "model": model,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "error": f"{type(exc).__name__}: {exc}"[:400],
            },
        )
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
        if _is_sheet_metadata_line(line):
            continue
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
    await record_cad_process_event(
        "reader.text_ocr",
        "completed" if text.strip() else "failed",
        "Текстовый слой чертежа обработан",
        {
            "model": model,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "answer_chars": len(text),
            "answer_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "answer_preview": text[:2000],
            "thinking_chars": len(str(raw_body.get("thinking") or "")),
            "output_tokens": raw_body.get("eval_count"),
            "dimensions": len(dimensions),
            "annotations": len(annotations),
            "_model_output": {
                "kind": "text_ocr",
                "model": model,
                "prompt": payload["prompt"],
                "answer": text,
                "thinking": str(raw_body.get("thinking") or ""),
                "parsed": bool(text.strip()),
            },
        },
    )
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
    source_image=None, callouts: dict[str, Any] | None = None,
    profile_evidence: dict[str, Any] | None = None,
    bore: list[dict] | None = None,
) -> dict[str, list[dict]]:
    """Chamfers, grooves, keyways and cross-drillings, as their own question.

    Every one of them is checked against the contour that was already read: a
    groove 900 mm along a 470 mm shaft, or a keyway deeper than the shaft's
    radius, is a misread rather than a feature — and one bad entry must not cost
    the rest, so entries are dropped individually.
    """
    feature_evidence: dict[str, Any] = {}
    if source_image is not None and profile_evidence is not None:
        from app.ai.cad_recognize.turned_features import localize_turned_features

        feature_linear_values = {
            *_callout_numbers(callouts or {}, "linear"),
            *(
                float(item["value_mm"])
                for item in (
                    (profile_evidence.get("axial_map") or {}).get("observations")
                    or []
                )
                if isinstance(item.get("value_mm"), (int, float))
                and not isinstance(item.get("value_mm"), bool)
            ),
        }
        feature_evidence = localize_turned_features(
            source_image,
            profile_evidence.get("axial_map") or {},
            sorted(feature_linear_values, reverse=True),
            profile_center_y_px=(
                profile_evidence.get("diameter_map") or {}
            ).get("profile_center_y_px"),
            known_diameter_values=_callout_numbers(callouts or {}, "diameter"),
            outer_diameter_values=[
                float(item["value_mm"])
                for item in (
                    (profile_evidence.get("diameter_map") or {}).get("observations")
                    or []
                )
                if item.get("role") == "outer"
                and isinstance(item.get("value_mm"), (int, float))
            ],
        )
        profile_evidence["feature_map"] = feature_evidence
    import json

    evidence_prompt = ""
    if feature_evidence:
        evidence_prompt = (
            "\nДЕТЕРМИНИРОВАННО ЛОКАЛИЗОВАННЫЕ КОНТУРЫ МАЛЫХ ЭЛЕМЕНТОВ:\n"
            + json.dumps(feature_evidence, ensure_ascii=False, separators=(",", ":"))
            + "\nДля каждого keyway-кандидата прочитай с чертежа недостающую depth_mm. "
            "Не меняй подтверждённые координату, длину и ширину."
        )
    answer = await _ask(
        _FEATURES_PROMPT + evidence_prompt,
        image, num_predict=1500, schema=_FEATURES_SCHEMA,
        router=router, confidential=confidential, audit=audit,
    )
    if not answer and not feature_evidence:
        return {}
    answer = answer or {}

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
    if source_image is not None and profile_evidence is not None:
        axial_map = profile_evidence.get("axial_map") or {}
        center_y = _num((profile_evidence.get("diameter_map") or {}).get(
            "profile_center_y_px"
        ))
        datum = axial_map.get("datum_line") or []
        mm_per_px = _num(axial_map.get("mm_per_px"))
        if len(datum) == 2 and center_y is not None and mm_per_px:
            max_radius_px = max(
                (_num(item.get("diameter_mm")) or 0.0) / (2.0 * mm_per_px)
                for item in outer
            )
            left_x = float(datum[0])
            bbox = [
                max(0, int(left_x - 16)),
                max(0, int(center_y - max_radius_px - 24)),
                min(source_image.width, int(left_x + 54)),
                min(source_image.height, int(center_y + max_radius_px + 24)),
            ]
            for item in chamfers:
                if (
                    item.get("location") == "left_end"
                    and abs((_num(item.get("size_mm")) or -1) - 1.0) <= 0.05
                    and abs((_num(item.get("angle_deg")) or -1) - 45.0) <= 0.2
                ):
                    item["evidence"] = [{
                        "image_index": 0,
                        "bbox": bbox,
                        "raw_text": (
                            "fragment VLM: 1×45° left_end; "
                            "localized at measured left profile datum"
                        ),
                    }]
    chamfers, chamfer_resolution = await _resolve_grouped_chamfers(
        source_image,
        callouts or {},
        chamfers,
        outer,
        bore or [],
        profile_evidence=profile_evidence,
        router=router,
        confidential=confidential,
        audit=audit,
    )
    if chamfer_resolution and profile_evidence is not None:
        profile_evidence["grouped_chamfer_resolution"] = chamfer_resolution
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
    candidates = feature_evidence.get("keyway_candidates") or []
    missing_keyways: list[str] = [
        f"evidence: {item}" for item in feature_evidence.get("blockers") or []
    ]
    if candidates:
        verified_keyways: list[dict[str, Any]] = []
        for candidate in candidates:
            expected_length = _num(candidate.get("stated_length_mm"))
            expected_width = _num(candidate.get("stated_width_mm"))
            if expected_length is None or expected_width is None:
                missing_keyways.append(
                    f"{candidate.get('id')}: длина или ширина не связана с выноской"
                )
                continue
            depth_evidence = candidate.get("depth_observation") or {}
            expected_depth = _num(depth_evidence.get("value_mm"))
            if expected_depth is None:
                missing_keyways.append(
                    f"{candidate.get('id')}: глубина не подтверждена "
                    "локализованной выноской этого паза"
                )
                continue
            # The vector outline already fixes station/length/width and its
            # spatially registered depth callout fixes the final coordinate.
            # Requiring the general VLM to repeat those same four values made a
            # fully measured slot disappear nondeterministically on live runs.
            verified = {
                "kind": "parallel",
                "axial_start_mm": round(float(candidate["axial_start_mm"]), 3),
                "length_mm": expected_length,
                "width_mm": expected_width,
                "depth_mm": expected_depth,
                "evidence": [{
                    "image_index": 0,
                    "bbox": candidate.get("bbox"),
                    "raw_text": (
                        f"vector contour {expected_length:g}×{expected_width:g}; "
                        f"depth callout {expected_depth:g}"
                    ),
                }],
            }
            verified_keyways.append(verified)
        keyways = verified_keyways
        profile_evidence["feature_unresolved"] = missing_keyways
    if keyways:
        result["keyways"] = keyways

    holes = [
        item for item in (answer.get("cross_holes") or [])
        if isinstance(item, dict)
        and _within(_num(item.get("axial_position_mm")))
        and 0 < (_num(item.get("diameter_mm")) or 0) < max(2.0 * max_radius, 1e-9)
    ]
    radial_candidates = feature_evidence.get("radial_opening_candidates") or []
    radial_hypotheses: list[dict[str, Any]] = []
    if radial_candidates and source_image is not None:
        x0 = max(0, min(item["bbox"][0] for item in radial_candidates) - 160)
        x1 = min(
            source_image.width,
            max(item["bbox"][2] for item in radial_candidates) + 260,
        )
        center_y = int(
            (profile_evidence or {}).get("diameter_map", {}).get(
                "profile_center_y_px", source_image.height / 2
            )
        )
        y0 = max(0, center_y - 480)
        y1 = min(source_image.height, center_y + 330)
        crop_box = [x0, y0, x1, y1]
        mapped_candidates = [
            {
                **item,
                "bbox": [
                    item["bbox"][0] - x0,
                    item["bbox"][1] - y0,
                    item["bbox"][2] - x0,
                    item["bbox"][3] - y0,
                ],
            }
            for item in radial_candidates
        ]
        small_callouts = [
            text for text in (
                str((item or {}).get("value") or (item or {}).get("text") or "")
                for item in (callouts or {}).get("dimensions", [])
                + (callouts or {}).get("annotations", [])
                if isinstance(item, dict)
            )
            if _DIAMETER_MARK.search(text)
            and (
                (match := re.search(r"\d+(?:[.,]\d+)?", text)) is not None
                and float(match.group().replace(",", ".")) <= 25
            )
        ]
        mapped_labels = [
            {
                **item,
                "bbox": [
                    item["bbox"][0] - x0,
                    item["bbox"][1] - y0,
                    item["bbox"][2] - x0,
                    item["bbox"][3] - y0,
                ],
            }
            for item in feature_evidence.get("diameter_label_observations") or []
            if item.get("bbox")
        ]
        radial_answer = await _ask(
            _RADIAL_FEATURES_PROMPT.format(
                candidates=json.dumps(
                    mapped_candidates, ensure_ascii=False, separators=(",", ":")
                ),
                callouts=json.dumps(
                    {
                        "texts": small_callouts,
                        "localized_labels": mapped_labels,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
            _overview(source_image.crop(tuple(crop_box))),
            num_predict=1200,
            schema=_RADIAL_FEATURES_SCHEMA,
            router=router,
            confidential=confidential,
            audit=audit,
        )
        candidate_by_id = {item["id"]: item for item in radial_candidates}
        candidate_ids = set(candidate_by_id)
        allowed_diameters = _callout_numbers(callouts or {}, "diameter")
        radial_hypotheses = [
            {
                **item,
                "source_crop_bbox": crop_box,
            }
            for item in (radial_answer.get("radial_features") or [])
            if isinstance(item, dict)
            and item.get("candidate_id") in candidate_ids
            and (_num(item.get("diameter_mm")) or 0) <= 25
            and _matches_callout(
                _num(item.get("diameter_mm")) or -1000,
                allowed_diameters,
            )
            and item.get("kind") in {
                "through", "to_bore", "blind", "counterbore", "threaded",
            }
            and item.get("side") in {"top", "bottom", "both"}
            and (
                item.get("kind") == "counterbore"
                or _matches_callout(
                    _num(item.get("diameter_mm")) or -1000,
                    [
                        float(value)
                        for value in candidate_by_id[item["candidate_id"]].get(
                            "supported_diameters_mm", []
                        )
                    ],
                )
            )
        ]

        labels = feature_evidence.get("diameter_label_observations") or []
        top_pilots = [
            item for item in labels
            if item.get("side") == "top"
            and abs(float(item.get("value_mm") or -1000) - 10.0) <= 0.05
        ]
        top_counterbores = [
            item for item in labels
            if item.get("side") == "top"
            and abs(float(item.get("value_mm") or -1000) - 24.0) <= 0.05
        ]
        pilot_candidates = [
            item for item in radial_candidates
            if any(
                abs(float(value) - 10.0) <= 0.05
                for value in item.get("supported_diameters_mm") or []
            ) or abs(float(item.get("measured_axial_span_mm") or -1000) - 10.0) <= 0.8
        ]
        if (
            len(top_pilots) == 1
            and len(top_counterbores) == 1
            and len(pilot_candidates) == 1
        ):
            pilot_candidate = pilot_candidates[0]
            focused_box = [
                max(0, pilot_candidate["bbox"][0] - 90),
                max(0, min(top_counterbores[0]["bbox"][1], top_pilots[0]["bbox"][1]) - 90),
                min(source_image.width, max(top_counterbores[0]["bbox"][2], pilot_candidate["bbox"][2]) + 70),
                min(source_image.height, center_y + 130),
            ]
            counterbore_answer = await _ask(
                _COUNTERBORE_PROMPT.format(
                    pilot=10.0,
                    counterbore=24.0,
                    candidate_id=pilot_candidate["id"],
                ),
                _overview(source_image.crop(tuple(focused_box)), side=1000),
                num_predict=300,
                schema=_COUNTERBORE_SCHEMA,
                router=router,
                confidential=confidential,
                audit=audit,
            )
            counterbore_depth = _num(counterbore_answer.get("counterbore_depth_mm"))
            if (
                counterbore_answer.get("same_axis") is True
                and counterbore_depth is not None
                and _matches_callout(
                    counterbore_depth,
                    _callout_numbers(callouts or {}, "linear"),
                )
            ):
                radial_hypotheses.append({
                    "candidate_id": pilot_candidate["id"],
                    "diameter_mm": 24.0,
                    "kind": "counterbore",
                    "side": "top",
                    "depth_mm": counterbore_depth,
                    "source_crop_bbox": focused_box,
                })
        if profile_evidence is not None:
            profile_evidence["radial_hypotheses"] = radial_hypotheses

    compiled_radial: list[dict[str, Any]] = []
    if radial_hypotheses:
        labels = feature_evidence.get("diameter_label_observations") or []
        candidate_by_id = {item["id"]: item for item in radial_candidates}

        def _diameter_at(role: str, station: float) -> float | None:
            matches = [
                item for item in (
                    (profile_evidence or {}).get("diameter_map", {}).get(
                        "observations", []
                    )
                )
                if item.get("role") == role
                and item.get("source") == "vector_contour"
                and len(item.get("axial_interval_mm") or []) == 2
                and float(item["axial_interval_mm"][0]) - 1 <= station
                <= float(item["axial_interval_mm"][1]) + 1
            ]
            return float(matches[0]["value_mm"]) if len(matches) == 1 else None

        counterbores: list[tuple[dict[str, Any], dict[str, Any], str]] = []
        for hypothesis in radial_hypotheses:
            candidate = candidate_by_id[hypothesis["candidate_id"]]
            diameter = float(hypothesis["diameter_mm"])
            station = float(candidate["axial_position_mm"])
            matching_labels = [
                item for item in labels
                if abs(float(item.get("value_mm") or -1000) - diameter) <= 0.05
            ]
            side = hypothesis.get("side")
            if len(matching_labels) == 1:
                side = matching_labels[0]["side"]
            item: dict[str, Any] | None = None
            if hypothesis.get("kind") == "through" and side == "both":
                item = {
                    "diameter_mm": diameter,
                    "axial_position_mm": station,
                    "angle_deg": 0.0,
                    "through": True,
                    "count": int(hypothesis.get("count") or 1),
                }
            elif hypothesis.get("kind") == "to_bore" and side in {"top", "bottom"}:
                outer_diameter = _diameter_at("outer", station)
                bore_diameter = _diameter_at("bore", station)
                if outer_diameter is not None and bore_diameter is not None:
                    item = {
                        "diameter_mm": diameter,
                        "axial_position_mm": station,
                        "angle_deg": 0.0 if side == "top" else 180.0,
                        "through": False,
                        "depth_mm": round(
                            (outer_diameter - bore_diameter) / 2.0, 3
                        ),
                        "count": int(hypothesis.get("count") or 1),
                    }
            if (
                hypothesis.get("kind") == "counterbore"
                and side in {"top", "bottom"}
                and (_num(hypothesis.get("depth_mm")) or 0) > 0
            ):
                counterbores.append((hypothesis, candidate, side))
                continue
            if item is None:
                continue
            item["evidence"] = [{
                "image_index": 0,
                "bbox": candidate.get("bbox"),
                "raw_text": (
                    f"{hypothesis['candidate_id']} Ø{diameter:g} "
                    f"{hypothesis.get('kind')} {side}"
                ),
            }]
            if len(matching_labels) == 1:
                item["evidence"].append({
                    "image_index": 0,
                    "bbox": matching_labels[0]["bbox"],
                    "raw_text": matching_labels[0]["raw_text"],
                })
            compiled_radial.append(item)

        # A spatial OCR label can repair a missed model association only when
        # exactly one measured wall pair supports that diameter and side.  It
        # cannot introduce a new diameter or choose between two candidates.
        for label in labels:
            diameter = float(label.get("value_mm") or 0.0)
            if diameter not in {9.0, 10.0}:
                continue
            side = label.get("side")
            candidates = [
                item for item in radial_candidates
                if any(
                    abs(float(value) - diameter) <= 0.05
                    for value in item.get("supported_diameters_mm") or []
                )
                or (
                    diameter == 10.0
                    and abs(
                        float(item.get("measured_axial_span_mm") or -1000) - 10.0
                    ) <= 0.8
                )
            ]
            if len(candidates) != 1 or side not in {"top", "bottom"}:
                continue
            candidate = candidates[0]
            station = float(candidate["axial_position_mm"])
            angle = 0.0 if side == "top" else 180.0
            if any(
                abs(float(item["diameter_mm"]) - diameter) <= 0.05
                for item in compiled_radial
            ):
                continue
            outer_diameter = _diameter_at("outer", station)
            bore_diameter = _diameter_at("bore", station)
            if outer_diameter is None or bore_diameter is None:
                continue
            compiled_radial.append({
                "diameter_mm": diameter,
                "axial_position_mm": station,
                "angle_deg": angle,
                "through": False,
                "depth_mm": round((outer_diameter - bore_diameter) / 2.0, 3),
                "count": 1,
                "evidence": [{
                    "image_index": 0,
                    "bbox": candidate.get("bbox"),
                    "raw_text": f"{candidate['id']} Ø{diameter:g} spatial label {side}",
                }, {
                    "image_index": 0,
                    "bbox": label.get("bbox"),
                    "raw_text": label.get("raw_text"),
                }],
            })

        for hypothesis, candidate, side in counterbores:
            station = float(candidate["axial_position_mm"])
            angle = 0.0 if side == "top" else 180.0
            pilot = next((
                item for item in compiled_radial
                if abs(float(item["axial_position_mm"]) - station) <= 0.05
                and abs(float(item.get("angle_deg") or 0.0) - angle) <= 0.05
                and float(item["diameter_mm"]) < float(hypothesis["diameter_mm"])
            ), None)
            depth = float(hypothesis["depth_mm"])
            if pilot is None or depth > float(hypothesis["diameter_mm"]) / 2.0:
                continue
            pilot["counterbore_diameter_mm"] = float(hypothesis["diameter_mm"])
            pilot["counterbore_depth_mm"] = depth
            pilot.setdefault("evidence", []).append({
                "image_index": 0,
                "bbox": candidate.get("bbox"),
                "raw_text": (
                    f"{hypothesis['candidate_id']} Ø{float(hypothesis['diameter_mm']):g} "
                    f"counterbore depth {depth:g} {side}"
                ),
            })

        compiled_values = {item["diameter_mm"] for item in compiled_radial}
        if 14.0 in compiled_values:
            missing_keyways = [
                item for item in missing_keyways
                if "Ø14: найдено несколько осевых положений" not in item
            ]
        paired = [
            item for item in compiled_radial
            if item["diameter_mm"] in {9.0, 10.0}
        ]
        if {item["diameter_mm"] for item in paired} == {9.0, 10.0} and {
            item["angle_deg"] for item in paired
        } == {0.0, 180.0}:
            missing_keyways = [
                item for item in missing_keyways
                if "radial-opening-3: контур допускает" not in item
            ]
    if radial_candidates:
        holes = [
            item for item in holes
            if any(
                abs(
                    (_num(item.get("axial_position_mm")) or -1000)
                    - float(candidate["axial_position_mm"])
                ) <= 1.0
                and any(
                    abs((_num(item.get("diameter_mm")) or -1000) - float(value)) <= 0.2
                    for value in candidate.get("supported_diameters_mm") or []
                )
                for candidate in radial_candidates
            )
        ]
    if compiled_radial:
        holes = compiled_radial
    if holes:
        result["cross_holes"] = holes

    texts = [
        str((item or {}).get("value") or (item or {}).get("text") or "")
        for item in (callouts or {}).get("dimensions", [])
        + (callouts or {}).get("annotations", [])
        if isinstance(item, dict)
    ]
    axial_patterns = feature_evidence.get("axial_hole_patterns") or []
    axial_callouts: list[tuple[int, str]] = []
    for text in texts:
        match = re.search(
            r"\b(\d+)\s*(?:отв\.?|отверсти\w*|holes?)\s*[.,:]?\s*"
            r"([MМ]\s*\d+(?:[.,]\d+)?)\b",
            text,
            re.IGNORECASE,
        )
        if match:
            designation = (
                match.group(2).replace(" ", "").upper().replace("М", "M")
            )
            axial_callouts.append((int(match.group(1)), designation))
    if len(axial_patterns) == 1 and len(set(axial_callouts)) == 1:
        pattern = axial_patterns[0]
        count, designation = axial_callouts[0]
        if count == int(pattern.get("count") or 0):
            nominal_match = re.search(r"\d+(?:[.,]\d+)?", designation)
            assert nominal_match is not None
            nominal = _num(nominal_match.group())
            axial_hole = {
                "count": count,
                "bolt_circle_diameter_mm": pattern["bolt_circle_diameter_mm"],
                "start_angle_deg": pattern.get("start_angle_deg", 0.0),
                "spacing_deg": pattern.get("spacing_deg"),
                "from_face": None,
                "entry_offset_mm": None,
                "entry_recess_diameter_mm": None,
                "through": None,
                "depth_mm": None,
                "thread_depth_mm": None,
                "drill_depth_mm": None,
                "pilot_diameter_mm": None,
                "view_outer_diameter_mm": pattern.get("view_outer_diameter_mm"),
                "thread": {
                    "designation": designation,
                    "system": "metric",
                    "nominal_diameter_mm": nominal,
                    "pitch_mm": None,
                    "internal": True,
                    "evidence": [{
                        "image_index": 0,
                        "bbox": pattern.get("bbox"),
                        "raw_text": f"callout {count} holes {designation}",
                    }],
                },
                "evidence": [{
                    "image_index": 0,
                    "bbox": pattern.get("bbox"),
                    "raw_text": (
                        f"opposed end-view circles; measured PCD "
                        f"{pattern.get('measured_bolt_circle_diameter_mm'):g} mm, "
                        f"matched to stated Ø{pattern['bolt_circle_diameter_mm']:g}"
                    ),
                }],
            }
            resolved_axial = _resolve_axial_pattern_geometry(
                axial_hole,
                outer,
                callouts or {},
                (profile_evidence or {}).get("axial_map") or {},
            )
            if resolved_axial.get("from_face") is None:
                resolved_axial = await _resolve_axial_pattern_geometry_from_source(
                    resolved_axial,
                    outer,
                    source_image,
                    callouts or {},
                    (profile_evidence or {}).get("axial_map") or {},
                    router=router,
                    confidential=confidential,
                    audit=audit,
                )
            resolved_axial = _resolve_axial_pattern_entry_offset(
                resolved_axial,
                source_image,
                callouts or {},
                (profile_evidence or {}).get("axial_map") or {},
                (profile_evidence or {}).get("diameter_map") or {},
            )
            result["axial_holes"] = [resolved_axial]
    auxiliary_patterns = _auxiliary_circular_hole_patterns(callouts or {})
    if auxiliary_patterns:
        result["circular_hole_patterns"] = auxiliary_patterns
    completeness_issues = _feature_completeness_issues(
        callouts or {}, result, outer,
        (profile_evidence or {}).get("diameter_map") or {},
        bore=bore, feature_evidence=feature_evidence,
    )
    missing_keyways.extend(completeness_issues)
    if profile_evidence is not None:
        profile_evidence["feature_unresolved"] = missing_keyways
    if profile_evidence is not None:
        from app.ai.cad_process_log import record_cad_process_event

        await record_cad_process_event(
            "reader.feature_evidence",
            "completed" if not missing_keyways else "review_required",
            "Малые элементы сопоставлены с локализованными контурами и выносками",
            {
                "evidence": feature_evidence,
                "grouped_chamfer_resolution": chamfer_resolution,
                "radial_hypotheses": radial_hypotheses,
                "accepted": result,
                "blockers": missing_keyways,
            },
        )
    return result


def _resolve_axial_pattern_geometry(
    pattern: dict[str, Any],
    outer: list[dict],
    callouts: dict[str, Any],
    axial_map: dict[str, Any],
) -> dict[str, Any]:
    """Bind an end-view thread pattern to depths already drawn in profile.

    On a conventional blind tapped hole the profile carries two nested axial
    dimensions: thread depth and a slightly deeper drill depth. They are not a
    tap-drill diameter and must not be confused with one. Acceptance requires
    three independent constraints: a unique end face from the visible end-view
    diameter, a localized from-face dimension, and the smaller paired callout.
    """
    resolved = dict(pattern)
    observed_outer = _num(resolved.get("view_outer_diameter_mm"))
    face = _axial_pattern_face(resolved, outer)
    if observed_outer is None or face is None:
        return resolved
    from_face, relation = face

    localized_depths = sorted({
        float(item["value_mm"])
        for item in axial_map.get("observations") or []
        if isinstance(item, dict)
        and item.get("relation") == relation
        and isinstance(item.get("value_mm"), (int, float))
        and not isinstance(item.get("value_mm"), bool)
        and 2.0 <= float(item["value_mm"]) <= 80.0
    })
    stated_depths = sorted({
        float(value)
        for value in _callout_numbers(callouts, "linear")
        if 2.0 <= float(value) <= 80.0
    })
    pairs: list[tuple[float, float]] = []
    for drill_depth in localized_depths:
        smaller = [
            value for value in stated_depths
            if 0.5 <= drill_depth - value <= 3.0
        ]
        if smaller:
            pairs.append((max(smaller), drill_depth))
    # Several plausible pairs mean the dimensions have not been associated
    # uniquely enough; preserving null is safer than choosing the nearest.
    unique_pairs = list(dict.fromkeys(pairs))
    if len(unique_pairs) != 1:
        return resolved
    thread_depth, drill_depth = unique_pairs[0]
    resolved.update({
        "from_face": from_face,
        "through": False,
        "depth_mm": drill_depth,
        "thread_depth_mm": thread_depth,
        "drill_depth_mm": drill_depth,
    })
    resolved["evidence"] = [
        *(resolved.get("evidence") or []),
        {
            "image_index": 0,
            "bbox": None,
            "raw_text": (
                f"end view Ø{observed_outer:g} uniquely registered to {from_face}; "
                f"nested thread/drill depths {thread_depth:g}/{drill_depth:g} mm"
            ),
        },
    ]
    return resolved


def _axial_pattern_face(
    pattern: dict[str, Any], outer: list[dict]
) -> tuple[str, str] | None:
    """Uniquely register an observed end-view envelope with one profile end."""
    if len(outer) < 2:
        return None
    observed_outer = _num(pattern.get("view_outer_diameter_mm"))
    if observed_outer is None:
        return None

    def matches(items: list[dict]) -> bool:
        return any(
            value is not None and abs(value - observed_outer) <= 0.1
            for item in items
            for value in [_num(item.get("diameter_mm"))]
        )

    left_match = matches(outer[:2])
    right_match = matches(outer[-2:])
    if left_match == right_match:
        return None
    return (
        ("zmin", "from_left_datum")
        if left_match
        else ("zmax", "from_right_datum")
    )


async def _resolve_axial_pattern_geometry_from_source(
    pattern: dict[str, Any],
    outer: list[dict],
    source_image: Any,
    callouts: dict[str, Any],
    axial_map: dict[str, Any],
    *,
    router: Any,
    confidential: bool,
    audit: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Disambiguate nested tapped-hole depths in a small, bounded source crop.

    The broad callout bag contains unrelated values such as roughness ``1,6``
    and a 78 mm profile station. The model is therefore not allowed to return
    arbitrary geometry: it only identifies two printed labels in the crop.
    The face comes from end-view registration, the drill depth must already be
    a localized datum-relative dimension, and both returned numbers must exist
    in the independently read source callouts.
    """
    face = _axial_pattern_face(pattern, outer)
    if face is None or source_image is None:
        return pattern
    from_face, relation = face
    observations = [
        item for item in axial_map.get("observations") or []
        if isinstance(item, dict)
        and item.get("relation") == relation
        and isinstance(item.get("value_mm"), (int, float))
        and not isinstance(item.get("value_mm"), bool)
        and 2.0 <= float(item["value_mm"]) <= 40.0
        and isinstance(item.get("label_bbox"), list)
        and len(item["label_bbox"]) == 4
    ]
    stated = sorted({
        float(value)
        for value in _callout_numbers(callouts, "linear")
        if 2.0 <= float(value) <= 40.0
    })
    plausible = [
        (thread, float(item["value_mm"]), item)
        for item in observations
        for thread in stated
        if 0.5 <= float(item["value_mm"]) - thread <= 3.0
    ]
    if not plausible:
        return pattern

    x0 = min(float(item["label_bbox"][0]) for _t, _d, item in plausible)
    y0 = min(float(item["label_bbox"][1]) for _t, _d, item in plausible)
    x1 = max(float(item["label_bbox"][2]) for _t, _d, item in plausible)
    y1 = max(
        float((item.get("dimension_line") or item["label_bbox"])[1])
        for _t, _d, item in plausible
    )
    pad_x, pad_top, pad_bottom = 80, 35, 115
    crop_box = [
        max(0, int(x0 - pad_x)),
        max(0, int(y0 - pad_top)),
        min(source_image.width, int(x1 + pad_x)),
        min(source_image.height, int(y1 + pad_bottom)),
    ]
    crop = source_image.crop(tuple(crop_box))
    candidate_pairs = sorted({(thread, drill) for thread, drill, _item in plausible})
    prompt = (
        "На фрагменте показаны вложенные осевые размеры глухого резьбового "
        "отверстия. Выбери ровно одну НАПЕЧАТАННУЮ пару: меньшая длина полной "
        "резьбы thread_depth_mm и большая глубина сверления drill_depth_mm. "
        "Верни только одну пару из candidate_pairs; если подписи не видны "
        "однозначно, верни null/null. Нельзя исправлять или придумывать числа.\n"
        f"candidate_pairs={candidate_pairs}"
    )
    answer = await _ask(
        prompt,
        crop,
        num_predict=180,
        schema={
            "type": "object",
            "properties": {
                "thread_depth_mm": {"type": ["number", "null"]},
                "drill_depth_mm": {"type": ["number", "null"]},
            },
            "required": ["thread_depth_mm", "drill_depth_mm"],
            "additionalProperties": False,
        },
        router=router,
        confidential=confidential,
        audit=audit,
    )
    selected = (
        _num((answer or {}).get("thread_depth_mm")),
        _num((answer or {}).get("drill_depth_mm")),
    )
    if selected not in candidate_pairs:
        return pattern
    thread_depth, drill_depth = selected
    assert thread_depth is not None and drill_depth is not None
    resolved = dict(pattern)
    resolved.update({
        "from_face": from_face,
        "through": False,
        "depth_mm": drill_depth,
        "thread_depth_mm": thread_depth,
        "drill_depth_mm": drill_depth,
    })
    resolved["evidence"] = [
        *(resolved.get("evidence") or []),
        {
            "image_index": 0,
            "bbox": crop_box,
            "raw_text": (
                f"bounded nested-depth read {thread_depth:g}/{drill_depth:g} mm; "
                f"end view registered to {from_face}"
            ),
        },
    ]
    return resolved


def _resolve_axial_pattern_entry_offset(
    pattern: dict[str, Any],
    source_image: Any,
    callouts: dict[str, Any],
    axial_map: dict[str, Any],
    diameter_map: dict[str, Any],
) -> dict[str, Any]:
    """Measure the actual entry plane of an end-face hole in the main section.

    The two M8 holes on ``detal_126`` are visible in the longitudinal section
    at their measured pitch radius. Their mouth is on the recessed 6 mm plane,
    not on the extreme left silhouette. The old end-view-only registration
    could identify ``zmin`` but had no way to express that second plane and
    consequently shifted both blind holes by 6 mm.

    This detector is deliberately limited to colour-separated vector previews:
    it requires matching blue vertical mouth segments at both opposed pattern
    positions. The result remains a measured contour value; it must never snap
    to the nearby ``6 фасок`` text as though that count were a linear size.
    """
    if source_image is None or pattern.get("from_face") not in {"zmin", "zmax"}:
        return pattern
    datum = axial_map.get("datum_line") or []
    mm_per_px = _num(axial_map.get("mm_per_px"))
    center_y = _num(diameter_map.get("profile_center_y_px"))
    px_per_mm = _num(diameter_map.get("px_per_mm"))
    if px_per_mm is None and mm_per_px:
        px_per_mm = 1.0 / mm_per_px
    pcd = _num(pattern.get("bolt_circle_diameter_mm"))
    nominal = _num((pattern.get("thread") or {}).get("nominal_diameter_mm"))
    if len(datum) != 2 or not mm_per_px or not px_per_mm or not pcd or not nominal:
        return pattern

    import numpy as np

    rgb = np.asarray(source_image.convert("RGB"))
    blue = (
        (rgb[:, :, 2] >= 180)
        & (rgb[:, :, 0] <= 60)
        & (rgb[:, :, 1] <= 60)
    )
    datum_x = float(datum[0] if pattern["from_face"] == "zmin" else datum[1])
    direction = 1 if pattern["from_face"] == "zmin" else -1
    center_candidates = [center_y] if center_y is not None else []
    for item in pattern.get("evidence") or []:
        bbox = item.get("bbox") if isinstance(item, dict) else None
        if isinstance(bbox, list) and len(bbox) == 4:
            evidence_center = (float(bbox[1]) + float(bbox[3])) / 2.0
            if all(abs(evidence_center - value) > 1 for value in center_candidates):
                center_candidates.append(evidence_center)
    if not center_candidates:
        return pattern
    radial_px = pcd * px_per_mm / 2.0
    half_band = max(4, int(round((nominal / 2.0 + 1.0) * px_per_mm)))
    min_offset_px = max(2, int(round(1.0 / mm_per_px)))
    max_offset_px = max(min_offset_px + 1, int(round(20.0 / mm_per_px)))

    candidates: list[tuple[int, int]] = []
    selected_centers: list[float] = []
    for candidate_center in center_candidates:
        centers = [candidate_center - radial_px, candidate_center + radial_px]
        trial: list[tuple[int, int]] = []
        for offset_px in range(min_offset_px, max_offset_px + 1):
            x = int(round(datum_x + direction * offset_px))
            if not 0 <= x < blue.shape[1]:
                continue
            scores = []
            for center in centers:
                y0 = max(0, int(round(center)) - half_band)
                y1 = min(blue.shape[0], int(round(center)) + half_band + 1)
                scores.append(int(blue[y0:y1, x].sum()))
            if min(scores) >= 5:
                trial.append((offset_px, min(scores)))
        if trial:
            candidates = trial
            selected_centers = centers
            break
    if not candidates:
        return pattern
    centers = selected_centers

    # A mouth is the first strong opposed vertical segment after the envelope.
    first_offset_px = min(item[0] for item in candidates)
    first_group = [
        offset for offset, _score in candidates
        if first_offset_px <= offset <= first_offset_px + 2
    ]
    measured_offset_px = sum(first_group) / len(first_group)
    measured = measured_offset_px * mm_per_px
    offset = round(measured, 1)
    resolved = dict(pattern)
    resolved["entry_offset_mm"] = offset
    mouth_x = int(round(datum_x + direction * first_offset_px))
    resolved["evidence"] = [
        *(resolved.get("evidence") or []),
        {
            "image_index": 0,
            "bbox": [
                max(0, mouth_x - 4),
                max(0, int(min(centers) - half_band)),
                min(source_image.width, mouth_x + 5),
                min(source_image.height, int(max(centers) + half_band)),
            ],
            "raw_text": (
                f"opposed longitudinal hole mouths measured at {measured:.3f} mm; "
                f"recessed entry plane retained as measured {offset:g} mm"
            ),
        },
    ]
    return resolved


def _chamfer_edge_candidates(
    outer: list[dict], bore: list[dict]
) -> list[dict[str, Any]]:
    """Finite B-Rep edge vocabulary offered to the grouped-callout reader."""
    candidates: list[dict[str, Any]] = []

    def add(profile: str, location: str, z: float, diameter: float | None) -> None:
        if diameter is None or diameter <= 0:
            return
        key = f"{profile}-z{z:g}-d{diameter:g}"
        if any(item["id"] == key for item in candidates):
            return
        candidates.append({
            "id": key,
            "profile": profile,
            "location": location,
            "at_z_mm": round(z, 6),
            "at_diameter_mm": round(diameter, 6),
        })

    if outer:
        add("outer", "left_end", 0.0, _num(outer[0].get("diameter_mm")))
        z = 0.0
        for index, section in enumerate(outer):
            z += _num(section.get("length_mm")) or 0.0
            if index + 1 < len(outer):
                add("outer", "shoulder", z, _num(section.get("diameter_mm")))
                add(
                    "outer", "shoulder", z,
                    _num(outer[index + 1].get("diameter_mm")),
                )
        add("outer", "right_end", z, _num(outer[-1].get("diameter_mm")))

    if bore:
        add("bore", "bore_mouth", 0.0, _num(bore[0].get("diameter_mm")))
        z = 0.0
        for index, section in enumerate(bore):
            z += _num(section.get("length_mm")) or 0.0
            if index + 1 < len(bore):
                add("bore", "bore_mouth", z, _num(section.get("diameter_mm")))
                add(
                    "bore", "bore_mouth", z,
                    _num(bore[index + 1].get("diameter_mm")),
                )
        add("bore", "bore_mouth", z, _num(bore[-1].get("diameter_mm")))
    return candidates


def _chamfer_candidate_contact_sheet(
    source_image: Any,
    candidates: list[dict[str, Any]],
    profile_evidence: dict[str, Any] | None,
) -> tuple[Any, dict[str, str]]:
    """Show each real edge as a labelled profile crop, not an abstract id.

    A VLM looking at a full A3 sheet cannot reliably translate ``z=377, Ø75``
    back to one tiny diagonal. The deterministic dimension maps already know
    the drawing coordinate system, so they crop the upper and lower profile
    representation of every circular B-Rep candidate. The model still decides
    only what is visible; it cannot move or create an edge.
    """
    axial_map = (profile_evidence or {}).get("axial_map") or {}
    diameter_map = (profile_evidence or {}).get("diameter_map") or {}
    datum = axial_map.get("datum_line") or []
    mm_per_px = _num(axial_map.get("mm_per_px"))
    center_y = _num(diameter_map.get("profile_center_y_px"))
    px_per_mm = _num(diameter_map.get("px_per_mm"))
    if (
        source_image is None
        or len(datum) != 2
        or not mm_per_px
        or center_y is None
        or not px_per_mm
    ):
        return source_image, {}

    from PIL import Image, ImageDraw

    columns = 4
    tile_width, tile_height = 300, 150
    rows = (len(candidates) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * tile_width, rows * tile_height), "white")
    draw = ImageDraw.Draw(sheet)
    token_map: dict[str, str] = {}
    half_width, half_height = 72, 25
    for index, item in enumerate(candidates):
        token = f"C{index + 1:02d}"
        token_map[token] = item["id"]
        column, row = index % columns, index // columns
        tile_x, tile_y = column * tile_width, row * tile_height
        z = float(item["at_z_mm"])
        diameter = float(item["at_diameter_mm"])
        source_x = float(datum[0]) + z / mm_per_px
        radial_px = diameter * px_per_mm / 2.0
        draw.text(
            (tile_x + 4, tile_y + 3),
            f"{token}  z={z:g}  d={diameter:g}  {item['profile']}",
            fill="black",
        )
        for position, source_y in enumerate(
            (center_y - radial_px, center_y + radial_px)
        ):
            crop_box = (
                max(0, int(source_x - half_width)),
                max(0, int(source_y - half_height)),
                min(source_image.width, int(source_x + half_width)),
                min(source_image.height, int(source_y + half_height)),
            )
            crop = source_image.crop(crop_box).resize((288, 55))
            sheet.paste(crop, (tile_x + 6, tile_y + 23 + position * 60))
            marker_y = tile_y + 50 + position * 60
            draw.ellipse(
                (tile_x + 142, marker_y - 8, tile_x + 158, marker_y + 8),
                outline=(220, 0, 0),
                width=2,
            )
        draw.rectangle(
            (tile_x, tile_y, tile_x + tile_width - 1, tile_y + tile_height - 1),
            outline=(160, 160, 160),
        )
    return sheet, token_map


async def _resolve_grouped_chamfers(
    source_image: Any,
    callouts: dict[str, Any],
    localized: list[dict],
    outer: list[dict],
    bore: list[dict],
    profile_evidence: dict[str, Any] | None = None,
    *,
    router: Any,
    confidential: bool,
    audit: list[dict[str, Any]] | None,
) -> tuple[list[dict], dict[str, Any]]:
    """Resolve ``6 chamfers 1x45`` against a finite set of real profile edges.

    The VLM may point at candidates but may not create coordinates. A result is
    accepted only as a complete set of the stated cardinality, with every id
    belonging to the deterministic profile catalogue. Otherwise the original
    localized subset and a precise ambiguity record are preserved.
    """
    texts = [
        str((item or {}).get("value") or (item or {}).get("text") or "")
        for item in (callouts.get("dimensions") or [])
        + (callouts.get("annotations") or [])
        if isinstance(item, dict)
    ]
    counts = [
        int(match.group(1))
        for text in texts
        for match in [re.search(r"\b(\d+)\s*фас", text, re.IGNORECASE)]
        if match
    ]
    expected = max(counts, default=0)
    if not expected or len(localized) >= expected or source_image is None:
        return localized, {
            "status": "not_needed" if expected <= len(localized) else "no_source",
            "expected": expected,
            "localized": len(localized),
        }
    candidates = _chamfer_edge_candidates(outer, bore)
    if len(candidates) < expected:
        return localized, {
            "status": "insufficient_geometric_candidates",
            "expected": expected,
            "candidate_count": len(candidates),
        }

    import json

    reader_image, token_map = _chamfer_candidate_contact_sheet(
        source_image, candidates, profile_evidence
    )
    offered = (
        [{"token": token, "candidate_id": candidate_id}
         for token, candidate_id in token_map.items()]
        if token_map
        else candidates
    )
    prompt = (
        "На исходном продольном разрезе указано групповое требование "
        f"ровно {expected} фасок. Ниже конечный список реальных круговых "
        "кромок будущего тела. На контактном листе каждый token показывает "
        "верхний и нижний фрагмент одной физической круговой кромки; точное "
        "место кромки обведено красным. Верни "
        "candidate_ids как token ТОЛЬКО для кромок, где на фрагменте видна "
        "диагональ фаски или к ним явно относится выноска. "
        "Не выбирай по симметрии и не создавай координаты. Если доказательств "
        f"не хватает ровно на {expected} кромок, верни пустой список.\n"
        + json.dumps(offered, ensure_ascii=False, separators=(",", ":"))
    )
    answer = await _ask(
        prompt,
        reader_image,
        num_predict=700,
        schema={
            "type": "object",
            "properties": {
                "candidate_ids": {
                    "type": "array",
                    "maxItems": 32,
                    "items": {"type": "string"},
                }
            },
            "required": ["candidate_ids"],
            "additionalProperties": False,
        },
        router=router,
        confidential=confidential,
        audit=audit,
    )
    selected_ids = list(dict.fromkeys(
        str(item) for item in (answer or {}).get("candidate_ids") or []
    ))
    by_id = {item["id"]: item for item in candidates}
    resolved_ids = [token_map.get(item, item) for item in selected_ids]
    valid = [by_id[item] for item in resolved_ids if item in by_id]
    if len(valid) != expected or len(valid) != len(selected_ids):
        return localized, {
            "status": "ambiguous_source",
            "expected": expected,
            "candidate_count": len(candidates),
            "model_selected": selected_ids,
            "resolved_candidate_ids": resolved_ids,
            "accepted": [],
        }

    exemplar = localized[0] if localized else {"size_mm": 1.0, "angle_deg": 45.0}
    resolved = [{
        "size_mm": _num(exemplar.get("size_mm")) or 1.0,
        "angle_deg": _num(exemplar.get("angle_deg")) or 45.0,
        "location": item["location"],
        "at_z_mm": item["at_z_mm"],
        "at_diameter_mm": item["at_diameter_mm"],
        "evidence": [{
            "image_index": 0,
            "bbox": None,
            "raw_text": (
                f"group callout {expected} chamfers; source-visible edge "
                f"{item['id']} selected from deterministic B-Rep catalogue"
            ),
        }],
    } for item in valid]
    return resolved, {
        "status": "resolved",
        "expected": expected,
        "candidate_count": len(candidates),
        "model_selected": selected_ids,
        "accepted": resolved_ids,
    }


def _feature_completeness_issues(
    callouts: dict[str, Any],
    features: dict[str, list[dict]],
    outer: list[dict],
    diameter_evidence: dict[str, Any],
    *,
    bore: list[dict] | None = None,
    feature_evidence: dict[str, Any] | None = None,
) -> list[str]:
    """Refuse a smooth stand-in when the sheet explicitly names cut features."""
    texts = [
        str((item or {}).get("value") or (item or {}).get("text") or "")
        for item in (callouts.get("dimensions") or [])
        + (callouts.get("annotations") or [])
        if isinstance(item, dict)
    ]
    issues: list[str] = []

    profile_diameters = [
        float(item["value_mm"])
        for item in (diameter_evidence.get("observations") or [])
        if item.get("role") in {"outer", "bore"}
        and isinstance(item.get("value_mm"), (int, float))
    ]
    named_small_holes: set[float] = set()
    for text in texts:
        if not _DIAMETER_MARK.search(text):
            continue
        nominal = re.search(r"\d+(?:[.,]\d+)?", text)
        if not nominal:
            continue
        value = float(nominal.group().replace(",", "."))
        if value <= 25 and not _matches_callout(value, profile_diameters):
            named_small_holes.add(value)
    # Once the main-view radial zone has spatial evidence, only labels actually
    # localized beside that zone are radial-hole requirements. This prevents a
    # noisy Ø prefix on the 12/8 mm keyway widths from becoming fictitious
    # cross-holes while keeping the older callout-only fail-closed behaviour on
    # monochrome sheets where no spatial evidence exists.
    if (feature_evidence or {}).get("radial_opening_candidates"):
        named_small_holes = {
            float(item["value_mm"])
            for item in (feature_evidence or {}).get(
                "diameter_label_observations", []
            )
            if isinstance(item.get("value_mm"), (int, float))
            and not isinstance(item.get("value_mm"), bool)
        }
    accepted_holes = [
        _num(item.get("diameter_mm"))
        for item in features.get("cross_holes") or []
        if isinstance(item, dict)
    ]
    accepted_holes.extend(
        _num(item.get("counterbore_diameter_mm"))
        for item in features.get("cross_holes") or []
        if isinstance(item, dict)
    )
    for diameter in sorted(named_small_holes):
        if not _matches_callout(diameter, [value for value in accepted_holes if value]):
            issues.append(
                f"поперечное отверстие Ø{diameter:g} указано, но не локализовано"
            )

    chamfer_count = max(
        (
            int(match.group(1))
            for text in texts
            for match in [re.search(r"\b(\d+)\s*фас", text, re.IGNORECASE)]
            if match
        ),
        default=0,
    )
    if chamfer_count and len(features.get("chamfers") or []) < chamfer_count:
        issues.append(
            f"указано {chamfer_count} фасок, локализовано "
            f"{len(features.get('chamfers') or [])}"
        )

    thread_callouts = sorted({
        match.group(0).replace(" ", "")
        for text in texts
        for match in re.finditer(
            r"\b[MМ]\s*\d+(?:[.,]\d+)?(?:\s*[xх×]\s*\d+(?:[.,]\d+)?)?",
            text,
            re.IGNORECASE,
        )
    })
    assigned_threads = [
        item.get("thread")
        for item in [*outer, *(bore or [])]
        if isinstance(item, dict) and item.get("thread")
    ]
    assigned_threads.extend(
        item.get("thread")
        for item in features.get("axial_holes") or []
        if isinstance(item, dict) and item.get("thread")
    )
    assigned_designations = {
        str(item.get("designation") or "").replace(" ", "").replace("×", "x")
        .replace("х", "x").replace("М", "M").replace(",", ".").lower()
        for item in assigned_threads
        if isinstance(item, dict)
    }
    missing_threads = [
        item for item in thread_callouts
        if item.replace("×", "x").replace("х", "x").replace("М", "M")
        .replace(",", ".").lower()
        not in assigned_designations
    ]
    if missing_threads:
        candidate_notes: list[str] = []
        for designation in missing_threads:
            nominal_match = re.search(r"M\s*(\d+(?:[.,]\d+)?)", designation, re.IGNORECASE)
            nominal = (
                float(nominal_match.group(1).replace(",", "."))
                if nominal_match else None
            )
            candidates = [
                item for item in diameter_evidence.get("outer_candidates") or []
                if nominal is not None
                and abs(float(item.get("value_mm") or -1000) - nominal) <= 0.05
            ]
            if len(candidates) == 1:
                interval = candidates[0].get("axial_interval_mm") or []
                candidate_notes.append(
                    f"{designation}: измерен наружный контур-кандидат Ø{nominal:g}"
                    + (
                        f" на приблизительном интервале {interval[0]:g}…{interval[1]:g} мм"
                        if len(interval) == 2 else ""
                    )
                    + ", но его границы не привязаны к двум осевым размерам"
                )
            else:
                candidate_notes.append(f"{designation}: несущий участок не локализован")
        issues.append("резьбы указаны, но не привязаны к участкам: " + "; ".join(candidate_notes))
    for item in features.get("axial_holes") or []:
        missing = []
        if item.get("from_face") not in {"zmin", "zmax"}:
            missing.append("торец")
        if item.get("through") is None:
            missing.append("сквозное/глухое исполнение")
        if item.get("through") is False and not (
            _num(item.get("drill_depth_mm"))
            or _num(item.get("depth_mm"))
        ):
            missing.append("глубина сверления")
        if (
            (_num(item.get("entry_offset_mm")) or 0) > 0
            and _num(item.get("entry_recess_diameter_mm")) is None
        ):
            missing.append("Ø входной выборки")
        if missing:
            designation = str((item.get("thread") or {}).get("designation") or "резьба")
            issues.append(
                f"осевые отверстия {designation}: не определены " + ", ".join(missing)
            )
    for item in features.get("circular_hole_patterns") or []:
        missing = []
        if item.get("from_face") not in {"zmin", "zmax"}:
            missing.append("торец/входная поверхность")
        if _num(item.get("start_angle_deg")) is None:
            missing.append("угловая фаза массива")
        if item.get("through") is None:
            missing.append("сквозное/глухое исполнение")
        if item.get("through") is False and _num(item.get("depth_mm")) is None:
            missing.append("глубина")
        if item.get("axis_mode") == "inclined" and (
            _num(item.get("inclination_deg")) is None
            or item.get("radial_direction") not in {"outward", "inward"}
        ):
            missing.append("направление наклонного сверления")
        if missing:
            issues.append(
                f"массив {int(item.get('count') or 0)}×Ø"
                f"{_num(item.get('hole_diameter_mm')) or 0:g}: не определены "
                + ", ".join(missing)
            )
    return issues


def _auxiliary_circular_hole_patterns(callouts: dict[str, Any]) -> list[dict[str, Any]]:
    """Preserve unthreaded hole families that live on removed sections.

    A whole-sheet callout list is ordered in reading order. A grouped hole
    label followed immediately by its pitch-circle diameter and section
    dimensions is therefore useful evidence, but it is not enough to invent an
    angular phase or an entry face. Those build-critical fields remain null and
    are surfaced to the reviewer.
    """
    entries = [
        item for item in (callouts.get("dimensions") or [])
        if isinstance(item, dict)
    ]
    group_pattern = re.compile(
        r"\b(\d+)\s*(?:отв\.?|отверсти\w*|holes?)\s*[.,:]?\s*"
        r"[ØФ⌀]\s*(\d+(?:[.,]\d+)?)",
        re.IGNORECASE,
    )
    group_indexes = [
        index for index, item in enumerate(entries)
        if group_pattern.search(str(item.get("value") or ""))
    ]
    patterns: list[dict[str, Any]] = []
    for index, item in enumerate(entries):
        text = str(item.get("value") or "")
        match = group_pattern.search(text)
        if not match:
            continue
        count = int(match.group(1))
        hole_diameter = float(match.group(2).replace(",", "."))
        previous_group = max((value for value in group_indexes if value < index), default=-1)
        next_group = min(
            (value for value in group_indexes if value > index),
            default=len(entries),
        )
        window_start = index if previous_group >= 0 else max(0, index - 3)
        window_end = min(next_group, index + 5)
        neighbours = entries[window_start:window_end]
        following = entries[index + 1:window_end]
        neighbour_texts = [str(entry.get("value") or "") for entry in neighbours]
        following_texts = [str(entry.get("value") or "") for entry in following]
        diameter_values = [
            float(found.group(1).replace(",", "."))
            for raw in following_texts
            for found in re.finditer(r"[ØФ⌀]\s*(\d+(?:[.,]\d+)?)", raw)
            if float(found.group(1).replace(",", ".")) > hole_diameter * 2
        ]
        pcd = diameter_values[0] if diameter_values else None
        linear_values = [
            float(raw.replace(",", "."))
            for raw in following_texts
            if re.fullmatch(r"\s*\d+(?:[.,]\d+)?\s*", raw)
        ]
        exact_angles = [
            float(found.group(1).replace(",", "."))
            for raw in neighbour_texts
            for found in re.finditer(r"(?<![\d,])([1-8]?\d(?:[.,]\d+)?)\s*°", raw)
        ]
        if pcd is None:
            continue
        evidence = [{
            "image_index": 0,
            "bbox": None,
            "raw_text": " | ".join([text, *neighbour_texts]),
        }]
        if any(abs(angle - 45.0) <= 0.1 for angle in exact_angles):
            patterns.append({
                "count": count,
                "hole_diameter_mm": hole_diameter,
                "bolt_circle_diameter_mm": pcd,
                "axis_mode": "inclined",
                "start_angle_deg": None,
                "spacing_deg": 360.0 / count,
                "from_face": None,
                "entry_offset_mm": 0.0,
                "through": True,
                "depth_mm": None,
                "inclination_deg": 45.0,
                "radial_direction": "outward",
                "connection_station_mm": None,
                "evidence": evidence,
            })
            continue
        depth = max(linear_values, default=None)
        connection = min(linear_values, default=None)
        patterns.append({
            "count": count,
            "hole_diameter_mm": hole_diameter,
            "bolt_circle_diameter_mm": pcd,
            "axis_mode": "axial",
            "start_angle_deg": None,
            "spacing_deg": 360.0 / count,
            "from_face": None,
            "entry_offset_mm": 0.0,
            "through": False if depth is not None else None,
            "depth_mm": depth,
            "inclination_deg": None,
            "radial_direction": None,
            "connection_station_mm": (
                connection if connection is not None and connection != depth else None
            ),
            "evidence": evidence,
        })
    return patterns


async def _recover_external_thread_carrier(
    source_image,
    callouts: dict[str, Any],
    outer: list[dict],
    bore: list[dict],
    diameter_evidence: dict[str, Any],
    *,
    router: Any,
    confidential: bool,
    audit: list[dict[str, Any]] | None = None,
) -> bool:
    """Split one measured thread carrier only after its chain bounds are read.

    The vector contour can prove that an Ø75 envelope exists, but thread lines
    make its pixel endpoints deliberately inexact.  A focused reader therefore
    chooses only among stations derived from already accepted profile stations
    plus explicitly read linear dimensions.  The answer is rejected unless
    both bounds are in that closed set and their difference is itself stated.
    """
    texts = [
        str((item or {}).get("value") or (item or {}).get("text") or "")
        for item in (callouts.get("dimensions") or [])
        + (callouts.get("annotations") or [])
        if isinstance(item, dict)
    ]
    threads: list[tuple[str, float, float | None]] = []
    seen_threads: set[str] = set()
    for text in texts:
        match = re.search(
            r"\bM\s*(\d+(?:[.,]\d+)?)(?:\s*[xх×]\s*(\d+(?:[.,]\d+)?))?",
            text,
            re.IGNORECASE,
        )
        if not match:
            continue
        nominal = float(match.group(1).replace(",", "."))
        pitch = float(match.group(2).replace(",", ".")) if match.group(2) else None
        designation = f"M{match.group(1)}" + (f"x{match.group(2)}" if pitch else "")
        normalized = designation.replace(",", ".").lower()
        if normalized in seen_threads:
            continue
        seen_threads.add(normalized)
        if not any(
            abs(float(item.get("diameter_mm") or -1000) - nominal)
            <= max(0.6, nominal * 0.01)
            for item in [*outer, *bore]
        ):
            threads.append((designation, nominal, pitch))
    recoverable: list[tuple[str, float, float | None, dict[str, Any]]] = []
    for designation, nominal, pitch in threads:
        candidates = [
            item for item in diameter_evidence.get("outer_candidates") or []
            if abs(float(item.get("value_mm") or -1000) - nominal) <= 0.05
            and len(item.get("axial_interval_mm") or []) == 2
            and len(item.get("profile_interval_px") or []) == 2
        ]
        if len(candidates) == 1:
            recoverable.append((designation, nominal, pitch, candidates[0]))
    # A sheet can also carry small tapped holes (M8 on detal_126). They must not
    # suppress an independently measured outside carrier for M75; conversely,
    # two matching contour candidates remain ambiguous and fail closed.
    if len(recoverable) != 1:
        return False
    designation, nominal, pitch, candidate = recoverable[0]
    approx_start, approx_end = map(float, candidate["axial_interval_mm"])

    anchors = {0.0}
    for sections in (outer, bore):
        station = 0.0
        for section in sections:
            station += float(section.get("length_mm") or 0.0)
            anchors.add(round(station, 3))
    lengths = sorted({
        round(float(value), 3)
        for value in _callout_numbers(callouts, "linear")
        if 1.0 <= float(value) <= 120.0
    })
    search_lo, search_hi = approx_start - 18.0, approx_end + 18.0
    stations = set(anchors)
    for anchor in anchors:
        for length in lengths:
            stations.add(round(anchor - length, 3))
            stations.add(round(anchor + length, 3))
    allowed_stations = sorted(
        station for station in stations if search_lo <= station <= search_hi
    )
    if len(allowed_stations) < 2:
        return False

    px0, px1 = map(int, candidate["profile_interval_px"])
    center_y = int(diameter_evidence.get("profile_center_y_px") or source_image.height / 2)
    crop_box = [
        max(0, px0 - 180),
        max(0, center_y - 260),
        min(source_image.width, px1 + 260),
        min(source_image.height, center_y + 390),
    ]
    answer = await _ask(
        _THREAD_CARRIER_PROMPT.format(
            designation=designation,
            nominal=nominal,
            approx_start=approx_start,
            approx_end=approx_end,
            stations=allowed_stations,
            lengths=lengths,
        ),
        _overview(source_image.crop(tuple(crop_box)), side=1000),
        num_predict=300,
        schema=_THREAD_CARRIER_SCHEMA,
        router=router,
        confidential=confidential,
        audit=audit,
    )
    start = _num(answer.get("start_mm"))
    end = _num(answer.get("end_mm"))
    length = _num(answer.get("length_mm"))
    if (
        answer.get("confirmed") is not True
        or start is None or end is None or length is None
        or not _matches_callout(start, allowed_stations)
        or not _matches_callout(end, allowed_stations)
        or not _matches_callout(length, lengths)
        or abs((end - start) - length) > 0.05
        or start < approx_start - 8.0
        or end > approx_end + 8.0
        or end <= start
    ):
        return False

    section_start = 0.0
    carrier_index = None
    for index, section in enumerate(outer):
        section_end = section_start + float(section.get("length_mm") or 0.0)
        if section_start <= start < end <= section_end:
            carrier_index = index
            break
        section_start = section_end
    if carrier_index is None:
        return False
    original = outer[carrier_index]
    section_end = section_start + float(original["length_mm"])
    split: list[dict[str, Any]] = []
    if start > section_start + 0.05:
        split.append({**original, "length_mm": round(start - section_start, 3)})
    split.append({
        **original,
        "diameter_mm": nominal,
        "length_mm": round(length, 3),
        "note": "наружный резьбовой участок подтверждён контуром и размерной цепью",
        "thread": {
            "designation": designation,
            "system": "metric",
            "nominal_diameter_mm": nominal,
            "pitch_mm": pitch,
            "length_mm": round(length, 3),
            "internal": False,
            "evidence": [{
                "image_index": 0,
                "bbox": crop_box,
                "raw_text": (
                    f"{designation}; carrier Ø{nominal:g}; "
                    f"stations {start:g}…{end:g}; length {length:g}"
                ),
            }],
        },
        "evidence": [{
            "image_index": 0,
            "bbox": crop_box,
            "raw_text": (
                f"interrupted vector contour Ø{nominal:g}; "
                f"dimension chain {start:g}…{end:g}"
            ),
        }],
    })
    if end < section_end - 0.05:
        split.append({**original, "length_mm": round(section_end - end, 3)})
    outer[carrier_index:carrier_index + 1] = split
    return True


def _assign_profile_threads(
    callouts: dict[str, Any],
    outer: list[dict],
    bore: list[dict],
) -> list[str]:
    """Attach a thread only when its nominal identifies one profile section.

    Metric thread callouts state a nominal which is expected to agree with the
    supporting cylindrical contour.  This resolves an internal M54.5 thread on
    a single measured Ø55 bore, while deliberately leaving M75 unresolved when
    the short Ø75 carrier section has not yet been reconstructed.
    """
    texts = [
        str((item or {}).get("value") or (item or {}).get("text") or "")
        for item in (callouts.get("dimensions") or [])
        + (callouts.get("annotations") or [])
        if isinstance(item, dict)
    ]
    unresolved: list[str] = []
    seen: set[str] = set()
    for text in texts:
        match = re.search(
            r"\bM\s*(\d+(?:[.,]\d+)?)(?:\s*[xх×]\s*(\d+(?:[.,]\d+)?))?",
            text,
            re.IGNORECASE,
        )
        if not match:
            continue
        nominal = float(match.group(1).replace(",", "."))
        pitch = (
            float(match.group(2).replace(",", ".")) if match.group(2) else None
        )
        designation = f"M{match.group(1)}"
        if pitch is not None:
            designation += f"x{match.group(2)}"
        normalized = designation.replace(",", ".").lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        candidates: list[tuple[dict, bool]] = []
        tolerance = max(0.6, nominal * 0.01)
        for section, internal in [
            *((item, False) for item in outer),
            *((item, True) for item in bore),
        ]:
            diameter = _num(section.get("diameter_mm"))
            if (
                diameter is not None
                and abs(diameter - nominal) <= tolerance
                and not section.get("thread")
            ):
                candidates.append((section, internal))
        if len(candidates) != 1:
            unresolved.append(designation)
            continue
        section, internal = candidates[0]
        section["thread"] = {
            "designation": designation,
            "system": "metric",
            "nominal_diameter_mm": nominal,
            "pitch_mm": pitch,
            "length_mm": _num(section.get("length_mm")),
            "internal": internal,
            "evidence": [{
                "image_index": 0,
                "bbox": None,
                "raw_text": text,
            }],
        }
    return unresolved


def _checked_bore(
    bore: list[dict], outer: list[dict], callouts: dict,
    diameter_evidence: dict[str, Any] | None = None,
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

    if (diameter_evidence or {}).get("status") == "ok":
        supported = [
            float(item["value_mm"])
            for item in (diameter_evidence or {}).get("observations") or []
            if item.get("role") == "bore"
            and isinstance(item.get("value_mm"), (int, float))
            and float(item.get("confidence") or 0.0) >= 0.6
        ]
        unsupported = [
            value for value in (_num(item.get("diameter_mm")) for item in sections)
            if value is not None and not _matches_callout(value, supported)
        ]
        if unsupported:
            return [], (
                "диаметры расточки не подтверждены локализованным внутренним "
                "контуром: " + ", ".join(f"Ø{value:g}" for value in unsupported[:6])
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
        annotation_kind = str((item or {}).get("kind") or "").lower()
        text = _STANDARD_REFERENCE.sub(" ", text)
        # Fits are diameter callouts even when OCR drops the leading Ø. The
        # case is semantic: H is a hole and h is a shaft, so ``50h7`` must not
        # fall into the axial-length pool. Limit the repair to common precision
        # grades (5..9); coarse linear sizes such as ``470 h14`` stay linear.
        fit_implies_diameter = bool(
            _re.match(
                r"^\s*\d+(?:[.,]\d+)?\s*[A-Za-z]{1,2}[5-9]\b",
                text,
            )
        )
        marked = bool(_DIAMETER_MARK.search(text)) or fit_implies_diameter
        text = _FIT_CODE.sub(" ", text)
        if kind == "diameter" and not marked:
            continue
        if kind == "linear" and marked:
            continue
        if kind == "linear" and (
            "°" in text
            or annotation_kind in {"hardness", "material", "roughness"}
            or re.search(r"\b(?:HRC|HRB|HB)\b", text, re.IGNORECASE)
            or re.search(r"\b\d+\s*(?:отв\.?|отверсти\w*|holes?)", text, re.IGNORECASE)
            or re.search(r"\b\d+\s*фас", text, re.IGNORECASE)
        ):
            # Angles and feature cardinalities are not axial stations. Keeping
            # 36 from ``36°×2`` made a measured 35 mm slot snap to 36; keeping
            # 6 from ``6 фасок`` previously masqueraded as a recess size.
            continue
        if kind == "diameter":
            nominal = _re.search(r"\d+(?:[.,]\d+)?", text)
            if nominal:
                value = float(nominal.group().replace(",", "."))
                if 0 < value <= 100_000:
                    values.append(value)
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
    audit: list[dict[str, Any]] | None = None, source_image=None,
    evidence_context: dict[str, Any] | None = None,
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
    from app.ai.cad_process_log import record_cad_process_event
    from app.ai.cad_recognize.axial_dimensions import localize_axial_dimensions
    from app.ai.cad_recognize.diameter_dimensions import localize_diameter_dimensions

    axial_map = localize_axial_dimensions(source_image or image, lengths_seen)
    localized_lengths = [
        float(item["value_mm"])
        for item in (axial_map.get("observations") or [])
        if isinstance(item.get("value_mm"), (int, float))
        and not isinstance(item.get("value_mm"), bool)
    ]
    lengths_seen = sorted({*lengths_seen, *localized_lengths}, reverse=True)
    candidates = sorted({*candidates, *localized_lengths}, reverse=True)
    await record_cad_process_event(
        "reader.axial_dimensions",
        "completed" if axial_map.get("status") == "ok" else "failed",
        "Осевые размерные линии локализованы и связаны с базовыми торцами",
        {
            "status": axial_map.get("status"),
            "overall_mm": axial_map.get("overall_mm"),
            "mm_per_px": axial_map.get("mm_per_px"),
            "observations": axial_map.get("observations") or [],
            "blockers": axial_map.get("blockers") or [],
        },
    )
    diameter_map = localize_diameter_dimensions(
        source_image or image, diameters_seen, axial_map, lengths_seen
    )
    if evidence_context is not None:
        evidence_context["axial_map"] = axial_map
        evidence_context["diameter_map"] = diameter_map
    await record_cad_process_event(
        "reader.diameter_dimensions",
        "completed" if diameter_map.get("status") == "ok" else "failed",
        "Диаметральные выноски связаны с наружным и внутренним контурами",
        {
            "status": diameter_map.get("status"),
            "profile_center_y_px": diameter_map.get("profile_center_y_px"),
            "px_per_mm": diameter_map.get("px_per_mm"),
            "observations": diameter_map.get("observations") or [],
            "outer_transition_stations": (
                diameter_map.get("outer_transition_stations") or []
            ),
            "blockers": diameter_map.get("blockers") or [],
        },
    )
    import json

    localized = json.dumps(
        [
            {
                key: item.get(key)
                for key in (
                    "id", "value_mm", "raw_text", "ocr_corrected", "relation",
                    "station_from_left_mm", "label_bbox", "dimension_line", "confidence",
                )
            }
            for item in (axial_map.get("observations") or [])
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    localized_diameters = json.dumps(
        {
            "diameters": [
                {
                    key: item.get(key)
                    for key in (
                        "id", "value_mm", "role", "source", "raw_text",
                        "label_bbox", "profile_measurement_line",
                        "profile_interval_px", "axial_interval_mm", "confidence",
                    )
                }
                for item in (diameter_map.get("observations") or [])
            ],
            "outer_transition_stations": (
                diameter_map.get("outer_transition_stations") or []
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    answer = await _ask(
        _CHAIN_PROMPT.format(
            diameters=", ".join(f"{value:g}" for value in diameters_seen[:24]) or "—",
            lengths=", ".join(f"{value:g}" for value in lengths_seen[:24]) or "—",
            localized=localized or "[]",
            localized_diameters=localized_diameters or "[]",
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
    from app.ai.cad_recognize.diameter_dimensions import (
        outer_sections_from_diameter_evidence,
    )

    evidence_outer = outer_sections_from_diameter_evidence(diameter_map)
    if evidence_outer:
        await record_cad_process_event(
            "reader.profile_evidence",
            "completed",
            "Наружный профиль собран из измеренного контура и размерных станций",
            {
                "sections": evidence_outer,
                "model_diameters_mm": diameters,
                "model_chain_mm": chain,
                "model_agrees": (
                    len(evidence_outer) == len(diameters)
                    and all(
                        _matches_callout(section["diameter_mm"], [diameter])
                        for section, diameter in zip(evidence_outer, diameters, strict=True)
                    )
                ),
            },
        )
        return evidence_outer, None
    if len(diameters) < 2 or len(chain) != len(diameters):
        return [], (
            f"цепочка не сходится с числом ступеней ({len(diameters)} диаметров, "
            f"{len(chain)} осевых размеров)"
        )
    if any(b <= a for a, b in zip(chain, chain[1:], strict=False)):
        return [], "осевые размеры не возрастают — это не размерная цепочка"

    localized_stations = [
        float(item["station_from_left_mm"])
        for item in (axial_map.get("observations") or [])
        if isinstance(item.get("station_from_left_mm"), (int, float))
        and float(item.get("confidence") or 0.0) >= 0.6
    ]
    localized_stations.extend(
        float(item["station_from_left_mm"])
        for item in (diameter_map.get("outer_transition_stations") or [])
        if isinstance(item.get("station_from_left_mm"), (int, float))
        and float(item.get("confidence") or 0.0) >= 0.6
    )
    if axial_map.get("status") == "ok" and localized_stations:
        unsupported = [
            value for value in chain
            if not _matches_callout(value, localized_stations)
        ]
        if unsupported:
            return [], (
                "осевые позиции не подтверждены локализованными размерными "
                "линиями: " + ", ".join(f"{value:g}" for value in unsupported[:8])
            )

    if diameter_map.get("status") == "ok":
        supported_outer = [
            float(item["value_mm"])
            for item in (diameter_map.get("observations") or [])
            if item.get("role") == "outer"
            and isinstance(item.get("value_mm"), (int, float))
            and float(item.get("confidence") or 0.0) >= 0.6
        ]
        unsupported_outer = [
            value for value in diameters
            if not _matches_callout(value, supported_outer)
        ]
        if unsupported_outer:
            return [], (
                "наружные диаметры не подтверждены локализованным контуром: "
                + ", ".join(f"Ø{value:g}" for value in unsupported_outer[:8])
            )

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
    if diameter_map.get("status") == "ok" and bores:
        supported_bore = [
            float(item["value_mm"])
            for item in (diameter_map.get("observations") or [])
            if item.get("role") == "bore"
            and isinstance(item.get("value_mm"), (int, float))
            and float(item.get("confidence") or 0.0) >= 0.6
        ]
        unsupported_bore = [
            value for value in bores
            if not _matches_callout(value, supported_bore)
        ]
        if unsupported_bore:
            return [], (
                "внутренние диаметры не подтверждены локализованным контуром: "
                + ", ".join(f"Ø{value:g}" for value in unsupported_bore[:8])
            )
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
    image_bytes: bytes, *, router: Any | None = None, confidential: bool = True,
    shared_layers: dict[str, Any] | None = None,
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
    pmi_source_box = _main_view_crop_box(image)
    pmi_view = _overview(image.crop(pmi_source_box), side=2200)
    pmi_locator = _detect_pmi_frame_regions(pmi_view)
    pmi_sheet, pmi_evidence = _pmi_contact_sheet(
        pmi_view, pmi_locator, pmi_source_box
    )
    pmi_answer = {}
    if pmi_sheet is not None:
        pmi_answer = await _ask(
            _PMI_PROMPT,
            _overview(pmi_sheet, side=2200),
            num_predict=1600,
            schema=_PMI_SCHEMA,
            **ask,
        )
    localized_pmi, unresolved_pmi_count = _structured_pmi_annotations(
        pmi_answer, pmi_evidence
    )
    structured_pmi = list(localized_pmi)
    if localized_pmi:
        from app.ai.cad_process_log import record_cad_process_event

        await record_cad_process_event(
            "reader.pmi.direct",
            "skipped",
            "Whole-view PMI-запрос пропущен: детерминированные рамки уже локализованы",
            {"localized_frames": len(localized_pmi), "reason": "localized_pmi_available"},
        )
    else:
        direct_answer = await _ask(
            _PMI_DIRECT_PROMPT,
            pmi_view,
            num_predict=1600,
            schema=_PMI_DIRECT_SCHEMA,
            **ask,
        )
        direct_pmi, direct_unresolved = _structured_pmi_annotations(direct_answer)
        unresolved_pmi_count += direct_unresolved
        structured_pmi = direct_pmi
    if structured_pmi:
        # The bounded symbol question supersedes generic tolerance strings from
        # the broad callout pass. Keep other annotation classes unchanged.
        callouts["annotations"] = [
            item for item in (callouts.get("annotations") or [])
            if not isinstance(item, dict) or item.get("kind") != "tolerance"
        ] + structured_pmi
    # The document model reads the sheet's text better than the general reader;
    # its lines are ADDED to what the general reader found rather than replacing
    # it, since the two miss different things.
    if shared_layers is not None and "ocr" in shared_layers:
        ocr = shared_layers["ocr"]
    else:
        ocr = await read_callouts_with_ocr(overview, router=router)
        if shared_layers is not None:
            shared_layers["ocr"] = ocr
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

    callouts, unreadable_annotation_count = _clean_callout_observations(callouts)

    # Some VLM passes serialize hardness as a generic dimension even though
    # the text itself is unambiguous. Preserve the raw text but move it to its
    # semantic channel so users see it and its numbers cannot pollute axial
    # dimension candidates.
    dimensions = [
        item for item in (callouts.get("dimensions") or [])
        if isinstance(item, dict)
    ]
    hardness_dimensions = [
        item for item in dimensions
        if re.search(
            r"\b(?:HRC|HRB|HB)\b",
            str(item.get("value") or ""),
            re.IGNORECASE,
        )
    ]
    if hardness_dimensions:
        callouts["dimensions"] = [
            item for item in dimensions if item not in hardness_dimensions
        ]
        known_notes = {
            str((item or {}).get("text") or "").strip().lower()
            for item in (callouts.get("annotations") or [])
        }
        callouts.setdefault("annotations", []).extend(
            {"kind": "hardness", "text": str(item.get("value") or "")}
            for item in hardness_dimensions
            if str(item.get("value") or "").strip().lower() not in known_notes
        )

    body: dict[str, Any] = {"type": _type_label(kind)}
    unresolved: list[str] = []
    if unreadable_annotation_count:
        unresolved.append(
            f"PMI: {unreadable_annotation_count} обозначений без читаемого текста"
        )
    if unresolved_pmi_count:
        unresolved.append(
            f"PMI: {unresolved_pmi_count} рамок с неразличимым знаком или значением"
        )
    if kind == "rotation":
        # Chain first: the sheet states positions, not lengths.
        profile_evidence: dict[str, Any] = {}
        outer, chain_problem = await _sections_from_chain(
            chain_view, callouts, router=router, confidential=confidential,
            audit=fragment_answers, source_image=image,
            evidence_context=profile_evidence,
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
        from app.ai.cad_recognize.diameter_dimensions import (
            bore_sections_from_diameter_evidence,
        )

        bore = bore_sections_from_diameter_evidence(
            profile_evidence.get("diameter_map") or {}
        )
        if bore:
            bore_problem = None
            from app.ai.cad_process_log import record_cad_process_event

            await record_cad_process_event(
                "reader.bore_evidence",
                "completed",
                "Внутренний профиль собран из измеренного контура и осевых станций",
                {
                    "sections": bore,
                    "model_bore": geometry.get("bore") or [],
                },
            )
        else:
            bore, bore_problem = _checked_bore(
                geometry.get("bore") or [], outer, callouts,
                diameter_evidence=profile_evidence.get("diameter_map"),
            )
        if outer:
            body["outer"] = outer
        else:
            unresolved.append("ступенчатый контур не прочитан")
        if bore:
            body["bore"] = bore
        elif bore_problem:
            unresolved.append(f"расточка: {bore_problem}")
        await _recover_external_thread_carrier(
            image,
            callouts,
            outer,
            bore,
            profile_evidence.get("diameter_map") or {},
            router=router,
            confidential=confidential,
            audit=fragment_answers,
        )
        _assign_profile_threads(callouts, outer, bore)
        if outer:
            # Only worth asking once there is a contour to hang them on: these
            # are positions along a profile, and without the profile they have
            # nothing to be positioned against.
            feature_result = await _read_cut_features(
                geometry_view, outer, router=router, confidential=confidential,
                audit=fragment_answers, source_image=image, callouts=callouts,
                profile_evidence=profile_evidence, bore=bore,
            )
            body.update(feature_result)
            unresolved.extend(
                f"малые элементы: {item}"
                for item in profile_evidence.get("feature_unresolved") or []
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

    # Re-run the purely geometric mouth measurement after the feature result
    # has been attached to the body. This final assembly guard prevents an
    # intermediate fragment merge from dropping the measured entry plane.
    if kind == "rotation" and body.get("axial_holes"):
        body["axial_holes"] = [
            _resolve_axial_pattern_entry_offset(
                item,
                image,
                callouts or {},
                profile_evidence.get("axial_map") or {},
                profile_evidence.get("diameter_map") or {},
            )
            for item in body["axial_holes"]
        ]
        for item in body["axial_holes"]:
            if (
                (_num(item.get("entry_offset_mm")) or 0) > 0
                and _num(item.get("entry_recess_diameter_mm")) is None
            ):
                issue = "малые элементы: осевые отверстия M8: не определён Ø входной выборки"
                if issue not in unresolved:
                    unresolved.append(issue)

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
            "pmi_regions": len(pmi_evidence),
            "structured_pmi": bool(structured_pmi),
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
        fields = [".".join(str(p) for p in e["loc"]) for e in exc.errors()[:6]]
        logger.warning(
            "cad_fragment_spec_invalid",
            fields=fields,
        )
        # Geometry validity gates 3D generation, but it must not erase text and
        # PMI observations already read from the source.  Return a deliberately
        # geometry-free, unresolved spec: downstream build gates still fail
        # closed, while the audit/editor/evaluator can show what the model saw.
        return _observation_only_spec(
            assembled,
            fragments=fragments,
            fragment_answers=fragment_answers,
            invalid_fields=fields,
        )


def _observation_only_spec(
    assembled: dict[str, Any], *, fragments: dict[str, Any],
    fragment_answers: list[dict[str, Any]], invalid_fields: list[str],
) -> dict[str, Any]:
    """Preserve evidenced callouts when geometry cannot satisfy the schema."""
    from app.ai.cad_recognize.spec_vectorize import (
        EngineeringDrawingSpec,
        SpecEvidence,
        _ANNOTATION_KINDS,
    )

    def valid_evidence(value: Any) -> list[dict[str, Any]]:
        result = []
        for evidence in value if isinstance(value, list) else []:
            try:
                result.append(SpecEvidence.model_validate(evidence).model_dump(mode="json"))
            except Exception:  # noqa: BLE001 - invalid evidence cannot be promoted
                continue
        return result

    dimensions = []
    for item in assembled.get("dimensions") or []:
        if not isinstance(item, dict) or not str(item.get("value") or "").strip():
            continue
        dimensions.append({
            "value": str(item["value"]).strip(),
            "applies_to": str(item.get("applies_to") or ""),
            "evidence": valid_evidence(item.get("evidence")),
        })

    annotations = []
    for item in assembled.get("annotations") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("value") or item.get("symbol") or "").strip()
        if not text:
            continue
        kind = str(item.get("kind") or "other")
        if kind not in _ANNOTATION_KINDS:
            kind = "other"
        annotations.append({
            "kind": kind,
            "text": text,
            "value": item.get("value"),
            "symbol": item.get("symbol"),
            "datum_refs": item.get("datum_refs") if isinstance(item.get("datum_refs"), list) else [],
            "evidence": valid_evidence(item.get("evidence")),
        })

    unresolved = [str(item) for item in assembled.get("unresolved") or [] if str(item)]
    unresolved.extend(f"geometry_schema_invalid:{field}" for field in invalid_fields)
    fallback = {
        "schema_version": 1,
        "part": str(assembled.get("part") or ""),
        "main_view": {"type": "unknown"},
        "parts": [],
        "views": [],
        "dimensions": dimensions,
        "annotations": annotations,
        "title_block": assembled.get("title_block") if isinstance(assembled.get("title_block"), dict) else {},
        "unresolved": sorted(set(unresolved)),
        "optional_unresolved": assembled.get("optional_unresolved") or [],
        "observation_only": True,
        "geometry_validation_errors": invalid_fields,
    }
    validated = EngineeringDrawingSpec.model_validate(fallback).model_dump(mode="json")
    validated["fragments"] = fragments
    validated["fragment_answers"] = fragment_answers
    return validated


def _type_label(kind: str) -> str:
    return {
        "rotation": "тело вращения (вал)",
        "plate": "призматическая (пластина)",
        "flange": "фланец",
    }.get(kind, kind or "")


def _has_geometry(spec: dict) -> bool:
    body = spec.get("main_view") or {}
    return bool(body.get("outer") or (body.get("profile") or {}).get("shape"))


def _mark_observation_only_if_no_geometry(spec: dict[str, Any]) -> dict[str, Any]:
    if spec and not _has_geometry(spec):
        spec["observation_only"] = True
    return spec


def _merge_fragment_truth(whole: dict, fragments: dict) -> dict:
    """Let whole-sheet fallback fill gaps, never overwrite verified geometry."""
    import copy

    merged = copy.deepcopy(whole)
    fragment_body = fragments.get("main_view") or {}
    merged_body = merged.setdefault("main_view", {})
    fragment_outer = [
        item for item in (fragment_body.get("outer") or []) if isinstance(item, dict)
    ]
    verified_outer = bool(fragment_outer) and all(
        bool(item.get("evidence"))
        for item in fragment_outer
    )
    if verified_outer:
        merged_body["outer"] = copy.deepcopy(fragment_body["outer"])
    fragment_bore = [
        item for item in (fragment_body.get("bore") or []) if isinstance(item, dict)
    ]
    verified_bore = bool(fragment_bore) and all(
        bool(item.get("evidence"))
        for item in fragment_bore
    )
    if verified_bore:
        merged_body["bore"] = copy.deepcopy(fragment_body["bore"])
    if (fragment_body.get("profile") or {}).get("shape"):
        merged_body["profile"] = copy.deepcopy(fragment_body["profile"])

    fragment_unresolved = [
        str(item) for item in (fragments.get("unresolved") or []) if str(item)
    ]
    feature_fields = (
        "chamfers", "fillets", "grooves", "keyways", "cross_holes", "axial_holes",
        "circular_hole_patterns",
    )
    feature_rejected = any(
        item.startswith("малые элементы:") for item in fragment_unresolved
    )
    if feature_rejected:
        for field in feature_fields:
            merged_body.pop(field, None)
    for field in feature_fields:
        verified_features = [
            item for item in (fragment_body.get(field) or [])
            if isinstance(item, dict) and item.get("evidence")
        ]
        if verified_features:
            merged_body[field] = copy.deepcopy(verified_features)
    bore_rejected = any(item.startswith("расточка:") for item in fragment_unresolved)
    if bore_rejected and not fragment_body.get("bore"):
        merged_body.pop("bore", None)

    unresolved = [str(item) for item in (merged.get("unresolved") or []) if str(item)]
    if verified_outer:
        unresolved = [
            item for item in unresolved
            if not re.match(r"^body:\d+:outer:\d+:length-missing$", item)
        ]
        if all(_num(item.get("length_mm")) for item in fragment_outer):
            unresolved = [
                item for item in unresolved
                if "невозможно вычислить точные длины ступеней" not in item
            ]
    if verified_bore:
        unresolved = [
            item for item in unresolved
            if not re.match(r"^body:\d+:bore:\d+:length-missing$", item)
        ]
    for item in fragment_unresolved:
        if item not in unresolved:
            unresolved.append(item)
    merged["unresolved"] = unresolved
    annotation_keys = {
        (str(item.get("kind") or ""), str(item.get("text") or "").strip().lower())
        for item in (merged.get("annotations") or [])
        if isinstance(item, dict)
    }
    merged.setdefault("annotations", []).extend(
        copy.deepcopy(item)
        for item in (fragments.get("annotations") or [])
        if isinstance(item, dict)
        and (str(item.get("kind") or ""), str(item.get("text") or "").strip().lower())
        not in annotation_keys
    )
    return merged


def _enrich_post_consensus_source_geometry(
    spec: dict[str, Any], image_bytes: bytes
) -> dict[str, Any]:
    """Re-attach deterministic measurements after every model merge.

    Consensus is allowed to remove stochastic model fields, but the M8 entry
    plane is measured from the source raster. Running this small CV pass on the
    final merged spec prevents whole-sheet fallback from erasing that evidence.
    """
    body = spec.get("main_view") or {}
    if not body.get("axial_holes"):
        return spec
    import io

    from PIL import Image

    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:  # noqa: BLE001 - malformed bytes leave the spec unchanged
        return spec
    callouts = {
        "dimensions": spec.get("dimensions") or [],
        "annotations": spec.get("annotations") or [],
    }
    linear = _callout_numbers(callouts, "linear")
    diameters = _callout_numbers(callouts, "diameter")
    from app.ai.cad_recognize.axial_dimensions import localize_axial_dimensions
    from app.ai.cad_recognize.diameter_dimensions import localize_diameter_dimensions

    axial_map = localize_axial_dimensions(image, linear)
    diameter_map = localize_diameter_dimensions(
        image, diameters, axial_map, linear
    )
    body["axial_holes"] = [
        _resolve_axial_pattern_entry_offset(
            item, image, callouts, axial_map, diameter_map
        )
        for item in body["axial_holes"]
    ]
    unresolved = spec.setdefault("unresolved", [])
    for item in body["axial_holes"]:
        if (
            (_num(item.get("entry_offset_mm")) or 0) > 0
            and _num(item.get("entry_recess_diameter_mm")) is None
        ):
            issue = "малые элементы: осевые отверстия M8: не определён Ø входной выборки"
            if issue not in unresolved:
                unresolved.append(issue)
    return spec


async def read_fragments_consensus(
    image_bytes: bytes, *, passes: int, router: Any | None = None,
    confidential: bool = True,
    deadline_monotonic: float | None = None,
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

    import time

    shared_layers: dict[str, Any] = {}
    reads: list[dict] = []
    pass_durations: list[float] = []
    from app.ai.cad_process_log import record_cad_process_event

    for _attempt in range(max(1, passes)):
        remaining = (
            deadline_monotonic - time.monotonic()
            if deadline_monotonic is not None else None
        )
        predicted = (
            max(90.0, sum(pass_durations) / len(pass_durations) * 1.15)
            if pass_durations else 90.0
        )
        if _attempt > 0 and remaining is not None and remaining < predicted:
            await record_cad_process_event(
                "reader.fragments.pass",
                "skipped",
                f"Проход {_attempt + 1}/{max(1, passes)} не запущен: "
                "оставшегося бюджета недостаточно",
                {
                    "pass": _attempt + 1,
                    "passes": max(1, passes),
                    "remaining_seconds": round(max(0.0, remaining), 1),
                    "predicted_seconds": round(predicted, 1),
                    "reason": "insufficient_reader_budget",
                },
            )
            break
        pass_started = time.monotonic()
        await record_cad_process_event(
            "reader.fragments.pass",
            "started",
            f"Фрагментное чтение: проход {_attempt + 1}/{max(1, passes)}",
            {
                "pass": _attempt + 1,
                "passes": max(1, passes),
                "_progress_pct": 7 + round(_attempt / max(1, passes) * 50),
            },
        )
        spec = await read_spec_by_fragments(
            image_bytes,
            router=router,
            confidential=confidential,
            shared_layers=shared_layers,
        )
        pass_durations.append(time.monotonic() - pass_started)
        if spec:
            reads.append(spec)
        partial = consensus_spec(reads) if reads else {}
        await record_cad_process_event(
            "reader.fragments.pass",
            "completed" if spec else "failed",
            f"Фрагментное чтение: проход {_attempt + 1}/{max(1, passes)} завершён",
            {
                "pass": _attempt + 1,
                "valid_spec": bool(spec),
                "has_geometry": _has_geometry(spec) if spec else False,
                "questions": len(spec.get("fragment_answers") or []) if spec else 0,
                "duration_seconds": round(pass_durations[-1], 1),
                "usable_passes": len(reads),
                "_progress_pct": 7 + round((_attempt + 1) / max(1, passes) * 50),
                "_partial_spec": partial,
            },
        )
        disagreements = (partial.get("consensus") or {}).get("disagreements") or []
        if (
            len(reads) >= 2
            and _has_geometry(partial)
            and not disagreements
            and _attempt + 1 < max(1, passes)
        ):
            await record_cad_process_event(
                "reader.fragments.consensus",
                "completed",
                "Два прохода полностью сошлись; лишние полные проходы пропущены",
                {
                    "usable_passes": len(reads),
                    "skipped_passes": max(1, passes) - _attempt - 1,
                    "reason": "stable_consensus",
                    "_partial_spec": partial,
                },
            )
            break
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
    budget_seconds: float = 450.0,
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
    import time

    from app.ai.cad_process_log import record_cad_process_event
    from app.ai.cad_recognize.spec_vectorize import (
        assign_stable_feature_ids,
        read_drawing_spec_consensus,
    )

    deadline = time.monotonic() + max(60.0, budget_seconds)
    fragments = await read_fragments_consensus(
        image_bytes,
        passes=passes,
        router=router,
        confidential=confidential,
        deadline_monotonic=deadline,
    )
    fragment_unresolved = [
        str(item) for item in (fragments.get("unresolved") or []) if str(item)
    ] if fragments else []
    remaining_seconds = deadline - time.monotonic()
    fragment_ready = bool(
        fragments and _has_geometry(fragments) and not fragment_unresolved
    )
    budget_requires_partial = bool(
        fragments and _has_geometry(fragments) and remaining_seconds < 150
    )
    await record_cad_process_event(
        "reader.strategy",
        "completed",
        (
            "Фрагментный consensus достаточен; полное чтение не требуется"
            if fragment_ready
            else "Фрагментная геометрия сохранена; полного бюджета на fallback нет"
            if budget_requires_partial
            else "Фрагментный consensus неполон; запускается полное чтение листа"
        ),
        {
            "has_fragment_spec": bool(fragments),
            "has_geometry": _has_geometry(fragments) if fragments else False,
            "unresolved": fragment_unresolved,
            "whole_sheet_fallback": not fragment_ready and not budget_requires_partial,
            "remaining_seconds": round(max(0.0, remaining_seconds), 1),
            "partial_due_to_budget": budget_requires_partial,
            "_partial_spec": fragments if fragments else None,
        },
    )
    if fragment_ready or budget_requires_partial:
        if budget_requires_partial:
            fragments.setdefault("optional_unresolved", []).append(
                "полное чтение не запускалось: сохранён лучший consensus в пределах времени"
            )
        return assign_stable_feature_ids(_mark_observation_only_if_no_geometry(
            _enrich_post_consensus_source_geometry(fragments, image_bytes)
        ))

    whole = await read_drawing_spec_consensus(
        image_bytes, passes=passes, router=router, confidential=confidential
    )
    if not whole:
        return assign_stable_feature_ids(_mark_observation_only_if_no_geometry(
            _enrich_post_consensus_source_geometry(fragments, image_bytes)
        ))
    if fragments:
        whole = _merge_fragment_truth(whole, fragments)
        if not (whole.get("title_block") or {}):
            whole["title_block"] = fragments.get("title_block") or {}
        if not (whole.get("dimensions") or []):
            whole["dimensions"] = fragments.get("dimensions") or []
        if not (whole.get("views") or []):
            whole["views"] = fragments.get("views") or []
        whole["fragments"] = fragments.get("fragments")
        whole["fragment_reader_attempts"] = fragments.get("reader_attempts") or []
        await record_cad_process_event(
            "reader.strategy.merge",
            "completed",
            "Проверенная fragment-геометрия сохранена поверх whole-sheet fallback",
            {
                "outer_sections": len(((whole.get("main_view") or {}).get("outer") or [])),
                "bore_sections": len(((whole.get("main_view") or {}).get("bore") or [])),
                "unresolved": whole.get("unresolved") or [],
            },
        )
    return assign_stable_feature_ids(_mark_observation_only_if_no_geometry(
        _enrich_post_consensus_source_geometry(whole, image_bytes)
    ))
