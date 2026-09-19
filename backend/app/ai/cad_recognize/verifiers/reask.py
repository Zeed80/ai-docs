"""Точечный переспрос спорного значения по фрагменту листа (план, Ф8, E7).

Согласование (`reconcile`) отдаёт человеку то, где свидетельство неоднозначно:
живой shaft-1 — Ø ступени прочитан значением соседней ступени (Ø40 на Ø28), а
нужной надписи ридер не выписал вовсе. Здесь модели задаётся тот же узкий
вопрос, что у довопроса (`spec_followup`) — «диаметр ИМЕННО этой ступени», —
но по ВЫРЕЗУ листа вокруг ступени (система координат — из проверки), а не по
всему виду.

Прочитанное значение и замер модели НЕ показываются: иначе она повторит
подсказку. Решение по ответу:

* ответ совпал с замером, а с прочитанным — нет → два независимых
  свидетельства согласны, значение принимается (как «принято по листу»);
* иначе (повторила прочитанное, ответила третье, не ответила) — остаётся
  человеку, с записью, что спросили и что ответили.

Правило довопроса «ответ должен быть среди выписанных надписей» здесь не
годится — нужной надписи ридер как раз и не выписал; опора — совпадение с
замером.
"""

from __future__ import annotations

import io
import re
from collections.abc import Awaitable, Callable
from typing import Any

from app.ai.cad_recognize.verifiers.reconcile import apply_reconciliation

_PROMPTS = {
    "diameter_mm": (
        "Перед тобой фрагмент главного вида вала. Нужен ОДИН размер.\n"
        "Ступень — в ЦЕНТРЕ фрагмента. {context}\n"
        "Каков ДИАМЕТР именно этой ступени в миллиметрах по надписи на чертеже "
        "(выноска со знаком Ø)? Не выдумывай: если надписи не видно, верни null.\n"
        'Ответь ОДНОЙ строкой JSON: {{"value": 0}} или {{"value": null}}'
    ),
    "length_mm": (
        "Перед тобой фрагмент главного вида вала с размерами над и под ним. Нужен "
        "ОДИН размер.\nСтупень — в ЦЕНТРЕ фрагмента. {context}\n"
        "Какова ОСЕВАЯ ДЛИНА именно этой ступени в миллиметрах по надписям на "
        "чертеже? Если дана цепочка от торца — вычисли разность соседних значений. "
        "Не выдумывай: если определить нельзя, верни null.\n"
        'Ответь ОДНОЙ строкой JSON: {{"value": 0}} или {{"value": null}}'
    ),
}
_SCHEMA = {"type": "object", "properties": {"value": {"type": ["number", "null"]}}}
_INDEX = re.compile(r"\[(\d+)\]$")

Asker = Callable[[str, Any], Awaitable[dict]]


def _neighbours(outer: list[dict], index: int) -> str:
    parts = []
    if index > 0 and isinstance(outer[index - 1].get("diameter_mm"), (int, float)):
        parts.append(f"Слева — соседняя ступень Ø{float(outer[index - 1]['diameter_mm']):g}")
    if index + 1 < len(outer) and isinstance(outer[index + 1].get("diameter_mm"), (int, float)):
        parts.append(f"справа — Ø{float(outer[index + 1]['diameter_mm']):g}")
    return (", ".join(parts) + ".") if parts else ""


def step_crop_box(
    report: dict[str, Any],
    outer: list[dict],
    index: int,
    field: str,
    measured_diameter: float,
    widen: float = 1.0,
) -> tuple[int, int, int, int] | None:
    """Рамка выреза вокруг ступени по системе координат проверки.

    Для Ø — сама ступень и половина соседних по оси, по высоте — ступень с
    выносками Ø; для длины — ещё и ряды размеров над и под видом.
    """
    frame = report.get("frame") or {}
    origin = frame.get("origin_px")
    scale = frame.get("mm_per_px")
    if not origin or not scale:
        return None
    lengths = [float(step.get("length_mm") or 0.0) for step in outer]
    if index >= len(lengths) or not all(lengths):
        return None
    start = sum(lengths[:index])
    end = start + lengths[index]
    reach = 0.5 * max(lengths[index], 6.0) * widen
    radius = float(measured_diameter) / 2.0
    rise = radius + (40.0 if field == "length_mm" else max(12.0, 0.8 * radius)) * widen
    x0, axis = float(origin[0]), float(origin[1])
    return (
        int(x0 + (start - reach) / scale),
        int(axis - rise / scale),
        int(x0 + (end + reach) / scale),
        int(axis + rise / scale),
    )


async def reask_disputed(
    image_bytes: bytes,
    spec: dict[str, Any],
    report: dict[str, Any],
    decisions: list[dict[str, Any]],
    *,
    ask: Asker | None = None,
    max_questions: int = 6,
    crop_scale: float = 1.0,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Переспросить спорные Ø и длины ступеней; принять только согласное с замером.

    Возвращает спек и отчёт (с принятым), решения (принятые — ``adopt`` с
    пометкой переспроса) и журнал вопросов.
    """
    from PIL import Image

    if ask is None:
        ask = _default_ask
    try:
        sheet = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:  # noqa: BLE001 — нет картинки — нет переспроса
        return spec, report, decisions, []
    outer = [s for s in ((spec.get("main_view") or {}).get("outer") or []) if isinstance(s, dict)]
    items = {item.get("path"): item for item in report.get("items") or []}
    log: list[dict[str, Any]] = []
    adopted: list[dict[str, Any]] = []
    updated = []
    asked = 0
    for decision in decisions:
        if (
            decision.get("action") != "ask_human"
            or decision.get("kind") != "shaft_step"
            or decision.get("field") not in _PROMPTS
            or asked >= max_questions
        ):
            updated.append(decision)
            continue
        match = _INDEX.search(decision["path"])
        index = int(match.group(1)) if match else -1
        item = items.get(decision["path"]) or {}
        measured_d = (item.get("measured") or {}).get("diameter_mm") or decision["measured"]
        box = step_crop_box(report, outer, index, decision["field"], float(measured_d), crop_scale)
        if box is None:
            updated.append(decision)
            continue
        width, height = sheet.size
        crop = sheet.crop((max(0, box[0]), max(0, box[1]), min(width, box[2]), min(height, box[3])))
        prompt = _PROMPTS[decision["field"]].format(context=_neighbours(outer, index))
        asked += 1
        answer = await ask(prompt, crop)
        value = answer.get("value") if isinstance(answer, dict) else None
        entry = {
            "path": decision["path"],
            "field": decision["field"],
            "answer_mm": value,
            "read": decision["read"],
            "measured": decision["measured"],
        }
        tolerance = float(
            (item.get("tolerance_mm") or {}).get(
                "diameter" if decision["field"] == "diameter_mm" else "length", 0.5
            )
        )
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            entry["outcome"] = "модель не ответила"
        elif (
            abs(float(value) - float(decision["measured"])) <= tolerance
            and abs(float(value) - float(decision["read"])) > tolerance
        ):
            entry["outcome"] = "принято: переспрос согласен с замером"
            decision = {
                **decision,
                "action": "adopt",
                "source": "reask",
                "value": float(value),
                "reason": (
                    f"переспрос по фрагменту дал {float(value):g}, замер {decision['measured']:g} "
                    f"— согласны; прочитано было {decision['read']:g}"
                ),
            }
            adopted.append(decision)
        elif abs(float(value) - float(decision["read"])) <= tolerance:
            entry["outcome"] = "модель повторила прочитанное — решение за человеком"
        else:
            entry["outcome"] = "ответ не совпал ни с прочитанным, ни с замером"
        log.append(entry)
        updated.append(decision)
    if adopted:
        spec, report = apply_reconciliation(spec, report, adopted)
    return spec, report, updated, log


def _open_disputes(decisions: list[dict[str, Any]]) -> int:
    return sum(
        1
        for decision in decisions
        if decision.get("action") == "ask_human"
        and decision.get("kind") == "shaft_step"
        and decision.get("field") in _PROMPTS
    )


async def reask_until_settled(
    image_bytes: bytes,
    spec: dict[str, Any],
    report: dict[str, Any],
    decisions: list[dict[str, Any]],
    *,
    ask: Asker | None = None,
    max_rounds: int = 2,
    budget: int = 8,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], str]:
    """Круги переспроса с правилом остановки (план, Ф8).

    Каждый следующий круг спрашивает оставшееся по вырезу шире (соседние
    надписи и выноски попадают в кадр) и допускается, только если прошлый
    круг УМЕНЬШИЛ число расхождений: круг, ничего не решивший, повторённый с
    тем же вопросом, решит то же самое, а вызов модели локально — ~15 с.
    ``budget`` — вопросов на лист всего. Возвращает ещё и причину остановки.
    """
    log: list[dict[str, Any]] = []
    remaining = budget
    reason = "расхождений не осталось"
    for round_index in range(max_rounds):
        before = _open_disputes(decisions)
        if before == 0:
            reason = "расхождений не осталось"
            break
        if remaining <= 0:
            reason = f"исчерпан бюджет вопросов на лист ({budget})"
            break
        spec, report, decisions, asked = await reask_disputed(
            image_bytes,
            spec,
            report,
            decisions,
            ask=ask,
            max_questions=remaining,
            crop_scale=1.0 + 0.6 * round_index,
        )
        for entry in asked:
            entry["round"] = round_index + 1
        log.extend(asked)
        remaining -= len(asked)
        after = _open_disputes(decisions)
        if not asked:
            reason = "спрашивать не о чем: у спорного нет выреза"
            break
        if after >= before:
            reason = f"круг {round_index + 1} не уменьшил расхождений — стоп, решение человеку"
            break
        reason = (
            "расхождений не осталось" if after == 0 else f"достигнут предел кругов ({max_rounds})"
        )
    return spec, report, decisions, log, reason


async def _default_ask(prompt: str, crop: Any) -> dict:
    from app.ai.cad_recognize.spec_fragments import _ask, _overview
    from app.ai.router import ai_router

    return await _ask(
        prompt,
        _overview(crop),
        router=ai_router,
        confidential=True,
        num_predict=200,
        schema=_SCHEMA,
    )


_BENT_PROMPT = (
    "На фрагменте — сечение гнутой детали из листа: {flanges} полок и {bends} "
    "гибов между ними. Выпиши размеры полок, проставленные на листе, по порядку "
    "от одного конца сечения к другому — как они стоят на листе (до наружной "
    "поверхности). Только числа с листа. ОДНОЙ строкой JSON: "
    '{{"flanges_mm": [40, 60, 40]}}. Только JSON.'
)
_BENT_SCHEMA = {
    "type": "object",
    "properties": {"flanges_mm": {"type": "array", "items": {"type": "number"}}},
}


def _outer_flanges(sheet: dict[str, Any]) -> list[float]:
    """Полки спека (прямые участки) → размеры, как они стоят на листе."""
    import math

    flanges = [float(v) for v in sheet.get("flanges_mm") or []]
    turns = sheet.get("turns") or []
    angles = sheet.get("bend_angles_deg") or [90.0] * len(turns)
    reach = [
        (float(sheet["radius_mm"]) + float(sheet["thickness_mm"])) * math.tan(math.radians(a) / 2)
        for a in angles
    ]
    return [
        value + (reach[i - 1] if i > 0 else 0.0) + (reach[i] if i < len(flanges) - 1 else 0.0)
        for i, value in enumerate(flanges)
    ]


def bent_flanges_fit(
    answer: list[float],
    flanges_px: list[float],
    thickness_mm: float,
    already_read: list[float],
) -> list[float] | None:
    """Ответ о полках — в порядке листа, если он согласен с листом, иначе None.

    Согласие — одним масштабом с длинами по осевой (размер по наружной
    поверхности длиннее осевой на полтолщины у каждого гиба), и в ответе все
    полки, которые ридер уже прочитал с листа: иначе масштаб подогнался бы
    под любые пропорциональные числа.
    """
    count = len(flanges_px)
    if len(answer) != count or count < 2 or any(v <= 0 for v in answer + flanges_px):
        return None
    left = list(answer)
    for value in already_read:
        match = next((i for i, v in enumerate(left) if abs(v - value) <= 0.05), None)
        if match is None:
            return None
        left.pop(match)
    for order in (list(answer), list(reversed(answer))):
        centre = [
            value - 0.5 * thickness_mm * ((index > 0) + (index < count - 1))
            for index, value in enumerate(order)
        ]
        ratios = sorted(c / px for c, px in zip(centre, flanges_px, strict=True))
        scale = ratios[len(ratios) // 2]
        if all(
            abs(c - scale * px) <= max(0.5, 0.04 * c) + 0.5 * thickness_mm
            for c, px in zip(centre, flanges_px, strict=True)
        ):
            return order
    return None


async def reask_bent_section(
    image_bytes: bytes,
    spec: dict[str, Any],
    report: dict[str, Any],
    *,
    ask: Any = None,
) -> dict[str, Any] | None:
    """Недостающая полка гнутой детали — переспрос по вырезу сечения (X4, Ф8).

    Ридер терял полку Z-профиля («уголок» вместо Z): число гибов по листу
    другое, и раньше это уходило человеку — размеров полки лист замером не
    даёт. Но форма и пропорции видны: модель спрашивается ещё раз по вырезу,
    уже зная число полок, и ответ принимается, только если согласен с листом.
    """
    import io

    from PIL import Image

    item = next((i for i in report.get("items") or [] if i.get("kind") == "bent_section"), None)
    sheet = ((spec.get("main_view") or {}).get("sheet_metal")) or {}
    if (
        item is None
        or item.get("status") != "refuted"
        or not sheet
        or not item.get("evidence_bbox_px")
    ):
        return None
    measured = item.get("measured") or {}
    flanges_px = [float(v) for v in measured.get("flanges_px") or []]
    turns = list(measured.get("turns") or [])
    angles = list(measured.get("angles_deg") or [])
    if len(flanges_px) != len(turns) + 1 or len(turns) == len(sheet.get("turns") or []):
        return None
    if None in angles:
        return None
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    x0, y0, x1, y1 = item["evidence_bbox_px"]
    reach = 0.6 * max(x1 - x0, y1 - y0)
    crop = image.crop(
        (
            max(0, int(x0 - reach)),
            max(0, int(y0 - reach)),
            min(image.width, int(x1 + reach)),
            min(image.height, int(y1 + reach)),
        )
    )
    answer = await (ask or _default_bent_ask)(
        _BENT_PROMPT.format(flanges=len(flanges_px), bends=len(turns)), crop
    )
    values = [
        float(v)
        for v in (answer or {}).get("flanges_mm") or []
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    ]
    thickness = float(sheet["thickness_mm"])
    order = bent_flanges_fit(values, flanges_px, thickness, _outer_flanges(sheet))
    if order is None:
        return {
            "kind": "bent_section",
            "path": "main_view.sheet_metal",
            "action": "ask_human",
            "asked": values,
            "reason": "переспрос о полках не согласуется с сечением на листе — решение человеку",
        }
    import math

    # Углы — так же, как их положит в спек согласование (целые, 90° ± 5 — 90°):
    # по углу с десятыми прямые участки уходили на 0,5 мм (корпус, лист 5).
    angles = [90.0 if abs(a - 90.0) <= 5.0 else float(round(a)) for a in angles]
    reach_mm = [
        (float(sheet["radius_mm"]) + thickness) * math.tan(math.radians(a) / 2) for a in angles
    ]
    straight = [
        round(
            value
            - (reach_mm[i - 1] if i > 0 else 0.0)
            - (reach_mm[i] if i < len(order) - 1 else 0.0),
            3,
        )
        for i, value in enumerate(order)
    ]
    return {
        "kind": "bent_section",
        "path": "main_view.sheet_metal",
        "field": "turns",
        "action": "adopt",
        "read": {"flanges_mm": sheet.get("flanges_mm"), "turns": sheet.get("turns")},
        "value": {"flanges_mm": straight, "turns": turns, "bend_angles_deg": angles},
        "reason": (
            f"форма сечения — по листу ({len(order)} полок), размеры полок "
            f"{', '.join(f'{v:g}' for v in order)} — переспросом по вырезу, "
            "согласны с сечением"
        ),
    }


async def _default_bent_ask(prompt: str, crop: Any) -> dict:
    from app.ai.cad_recognize.spec_fragments import _ask, _overview
    from app.ai.router import ai_router

    return await _ask(
        prompt,
        _overview(crop),
        router=ai_router,
        confidential=True,
        num_predict=300,
        schema=_BENT_SCHEMA,
    )


_HOLE_THREAD_PROMPT = (
    "В центре фрагмента — отверстие детали. Если оно резьбовое, рядом стоит "
    "обозначение резьбы (M6, M8, M10×1, иногда с числом отверстий «2 отв. M8»). "
    "Выпиши обозначение резьбы ЭТОГО отверстия ровно как на листе; если "
    "отверстие не резьбовое — null. ОДНОЙ строкой JSON: "
    '{"thread": "M8"} или {"thread": null}. Только JSON.'
)
_HOLE_THREAD_SCHEMA = {"type": "object", "properties": {"thread": {"type": ["string", "null"]}}}


def _thread_by_minor(drawn_mm: float, tolerance_mm: float) -> tuple[str, float] | None:
    """Единственная стандартная резьба (крупный шаг), чей Ø впадин — это окружность."""
    from app.ai.cad_solid import _METRIC_COARSE_PITCH_MM

    fits = [
        (f"M{nominal:g}", nominal)
        for nominal, pitch in _METRIC_COARSE_PITCH_MM.items()
        if abs(nominal - 1.082532 * pitch - drawn_mm) <= tolerance_mm
    ]
    return fits[0] if len(fits) == 1 else None


async def reask_hole_threads(
    image_bytes: bytes, spec: dict[str, Any], report: dict[str, Any], *, ask: Any = None
) -> list[dict[str, Any]]:
    """Резьба отверстия, прочитанного гладким, — переспросом по вырезу (X1, Ф8).

    Живая пластина: «M8» прочитано как «M6» и Ø6, замер окружности 6,69 —
    Ø впадин M8. Если замер указывает ровно на одну стандартную резьбу, модель
    спрашивается по вырезу у отверстия; ответ принимается, только если его
    номинал — эта резьба.
    """
    import io
    import re

    from PIL import Image

    holes = (((spec.get("main_view") or {}).get("profile")) or {}).get("holes") or []
    image = None
    decisions: list[dict[str, Any]] = []
    for item in report.get("items") or []:
        if item.get("kind") != "plate_hole" or item.get("status") != "refuted":
            continue
        index = int(item["path"].split("[")[1].split("]")[0])
        measured, read = item.get("measured") or {}, item.get("read") or {}
        tolerance = item.get("tolerance_mm") or {}
        if index >= len(holes) or holes[index].get("thread") or not item.get("evidence_bbox_px"):
            continue
        if any(
            abs(float(measured.get(k, 1e9)) - float(read.get(k, -1e9)))
            > float(tolerance.get("position") or 0.5)
            for k in ("center_x_mm", "center_y_mm")
        ):
            continue
        if not isinstance(measured.get("diameter_mm"), (int, float)):
            continue
        expected = _thread_by_minor(
            float(measured["diameter_mm"]), max(0.25, float(tolerance.get("diameter") or 0.3))
        )
        if expected is None:
            continue
        if image is None:
            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        x0, y0, x1, y1 = item["evidence_bbox_px"]
        reach = 4.0 * max(x1 - x0, y1 - y0)
        crop = image.crop(
            (
                max(0, int(x0 - reach)),
                max(0, int(y0 - reach)),
                min(image.width, int(x1 + reach)),
                min(image.height, int(y1 + reach)),
            )
        )
        answer = await (ask or _default_thread_ask)(_HOLE_THREAD_PROMPT, crop)
        text = str((answer or {}).get("thread") or "").replace("М", "M")
        match = re.search(r"M\s*(\d+(?:[.,]\d+)?)", text)
        if not match or abs(float(match.group(1).replace(",", ".")) - expected[1]) > 0.05:
            continue
        decisions.append(
            {
                "kind": "plate_hole",
                "path": item["path"],
                "field": "thread",
                "action": "adopt",
                "read": read.get("diameter_mm"),
                "value": {"designation": expected[0], "nominal_diameter_mm": expected[1]},
                "reason": (
                    f"окружность Ø{float(measured['diameter_mm']):g} — Ø впадин {expected[0]}, "
                    f"переспрос по вырезу: «{text.strip()}» — отверстие резьбовое {expected[0]}"
                ),
            }
        )
    return decisions


async def _default_thread_ask(prompt: str, crop: Any) -> dict:
    from app.ai.cad_recognize.spec_fragments import _ask, _overview
    from app.ai.router import ai_router

    return await _ask(
        prompt,
        _overview(crop),
        router=ai_router,
        confidential=True,
        num_predict=200,
        schema=_HOLE_THREAD_SCHEMA,
    )
