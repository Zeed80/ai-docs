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
