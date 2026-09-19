"""Согласование: опровергнутое прочитанное против замера по листу (план, Ф8).

Проверка по листу не заменяет прочитанное — она ставит рядом замер.
Согласование решает, чему верить, и делает это только при сильном
свидетельстве, иначе отдаёт решение человеку с обоими вариантами:

* замер принимается, если рядом с ним (в допуске проверки) есть число,
  которое ридер САМ выписал с листа (`spec["dimensions"]`), а прочитанного
  значения среди этих чисел нет. В спек идёт число с листа, а не сырой замер:
  замер выбирает надпись, надпись даёт точное значение (80, а не 80,035).
  Живой shaft-1: длина 98 → 80 («80» на листе есть, «98» — нет);
* иначе — человеку: и прочитанное есть на листе (Ø40 при замере 27,9 — «40»
  стоит у соседней ступени), и замер не совпал ни с одной надписью;
* только поля, которые на листе стоят надписью: Ø и длины ступеней, длина и
  ширина паза, Ø отверстий, окружность болтов. Положения (станции, координаты)
  на листе — цепочки и выносные, их — всегда человеку.

Фаска и канавка сюда не попадают: они не опровергаются (ГОСТ 2.305).
"""

from __future__ import annotations

import copy
import re
from typing import Any

# Поле → вид допуска (как в `stage._GRAPH_FIELDS`); только надписанные величины.
ADOPTABLE: dict[str, tuple[tuple[str, str], ...]] = {
    "shaft_step": (("diameter_mm", "diameter"), ("length_mm", "length")),
    "keyway": (("length_mm", "length"), ("width_mm", "width")),
    "cross_hole": (("diameter_mm", "diameter"),),
    "plate_hole": (("diameter_mm", "diameter"),),
    "concentric_hole": (("diameter_mm", "diameter"),),
    "bolt_circle": (("bolt_circle_diameter_mm", "pcd"), ("hole_diameter_mm", "diameter")),
    # Корпуса (Ф5): размер и положение элемента грани. Положение здесь —
    # надписанное поле: лист несёт координаты элемента от кромок его грани,
    # и замер принимается по тому же правилу, что и размер.
    # Толщина корпуса и пластины, измеренная по видам листа (Ф5).
    "plate_thickness": (("thickness_mm", "thickness"),),
    "wall_feature": (
        ("diameter_mm", "size"),
        ("width_mm", "size"),
        ("height_mm", "size"),
        ("center_u_mm", "position"),
        ("center_v_mm", "position"),
    ),
}
# Число не после цифры и не после латинской буквы, кроме M (резьба) и R
# (радиус): «Ø80js6» — 80, а не 80 и 6 (поле допуска посадки).
_NUMBER = re.compile(r"(?<![\d.,A-LN-QS-Za-z])(\d+(?:[.,]\d+)?)(?!\s*отв)")
_THICKNESS_LABEL = re.compile(r"(?<![A-Za-zА-Яа-я])[sS]\s*=?\s*(\d+(?:[.,]\d+)?)")
_ENUMERATION = re.compile(r"^\s*\d+[.)]\s+")


def sheet_numbers(spec: dict[str, Any]) -> list[float]:
    """Числа, которые ридер выписал с листа (надписи размеров), без повторов.

    «Ø80js6» → 80, «M24×1,5» → 24 и 1,5, «2 отв. Ø5.5» → 5,5 (число перед
    «отв.» — количество, не размер), «1. Ø120 — наружный» → 120 (номер пункта
    и пояснение после «—» отбрасываются).
    """
    values: set[float] = set()
    for item in spec.get("dimensions") or []:
        text = item.get("value") if isinstance(item, dict) else item
        head = _ENUMERATION.sub("", str(text or "").split("—")[0])
        for match in _NUMBER.finditer(head):
            values.add(round(float(match.group(1).replace(",", ".")), 3))
    return sorted(values)


def reconcile(spec: dict[str, Any], report: dict[str, Any]) -> list[dict[str, Any]]:
    """Решения по опровергнутым надписанным полям: принять замер или человеку."""
    numbers = sheet_numbers(spec)
    decisions: list[dict[str, Any]] = []
    for item in report.get("items") or []:
        if item.get("status") != "refuted":
            continue
        tolerances = item.get("tolerance_mm") or {}
        swap = _keyway_swap(spec, item)
        decisions.extend(swap)
        decisions.extend(_keyway_position(spec, item, report))
        for field, tolerance_kind in ADOPTABLE.get(item.get("kind"), ()):
            if swap and field == "width_mm":
                continue
            read = (item.get("read") or {}).get(field)
            measured = (item.get("measured") or {}).get(field)
            tolerance = tolerances.get(tolerance_kind)
            if not all(isinstance(v, (int, float)) for v in (read, measured, tolerance)):
                continue
            if abs(float(measured) - float(read)) <= float(tolerance):
                continue  # это поле сходится — опровергнуто другое
            near = sorted(
                (n for n in numbers if abs(n - float(measured)) <= float(tolerance)),
                key=lambda n: abs(n - float(measured)),
            )
            read_on_sheet = any(abs(n - float(read)) <= 0.05 for n in numbers)
            decision = {
                "kind": item["kind"],
                "path": item["path"],
                "feature_id": item.get("feature_id"),
                "field": field,
                "read": float(read),
                "measured": float(measured),
            }
            if near and not read_on_sheet:
                decision.update(
                    action="adopt",
                    value=near[0],
                    reason=(
                        f"замер {float(measured):g} совпал с надписью «{near[0]:g}» на листе, "
                        f"а прочитанного {float(read):g} на листе нет"
                    ),
                )
            else:
                decision.update(
                    action="ask_human",
                    reason=(
                        f"прочитанное {float(read):g} тоже есть на листе"
                        if read_on_sheet
                        else f"замер {float(measured):g} не совпал ни с одной надписью на листе"
                    ),
                )
            decisions.append(decision)
    return decisions


def _keyway_swap(spec: dict[str, Any], item: dict[str, Any]) -> list[dict[str, Any]]:
    """Ширина и глубина паза переставлены — поменять местами.

    Живой z4-r4: ридер дал паз 4 × 8 (ширина × глубина), на листе и по
    ГОСТ 23360 для Ø30 — 8 × 4. Правило общего вида («прочитанное тоже есть
    на листе» → человеку) здесь отдало бы решение человеку: «4» на листе
    есть — это глубина. Но свидетельств три и независимых: замер ширины
    совпал с прочитанной глубиной, а пара (глубина, ширина) — с табличным
    сечением для Ø ступени паза. Тогда перестановка принимается.
    """
    from app.ai.cad_recognize.keyway_standard import (
        _SECTION_TOLERANCE,
        standard_section,
        step_for,
        steps_with_stations,
    )

    if item.get("kind") != "keyway":
        return []
    key = _resolve(spec, str(item.get("path") or ""))
    read = item.get("read") or {}
    width = read.get("width_mm")
    measured = (item.get("measured") or {}).get("width_mm")
    depth = (key or {}).get("depth_mm")
    tolerance = (item.get("tolerance_mm") or {}).get("width")
    start, length = read.get("axial_start_mm"), read.get("length_mm")
    if not all(
        isinstance(v, (int, float)) for v in (width, measured, depth, tolerance, start, length)
    ):
        return []
    if abs(measured - width) <= tolerance or abs(measured - depth) > tolerance:
        return []
    outer = [s for s in ((spec.get("main_view") or {}).get("outer") or []) if isinstance(s, dict)]
    holder, _inside = step_for(steps_with_stations(outer), float(start), float(length))
    diameter = holder[2].get("diameter_mm") if holder else None
    standard = standard_section(float(diameter)) if isinstance(diameter, (int, float)) else None
    if standard is None:
        return []
    b, t = standard
    if abs(depth - b) > b * _SECTION_TOLERANCE or abs(width - t) > t * _SECTION_TOLERANCE:
        return []
    reason = (
        f"ширина и глубина паза переставлены: замер ширины {float(measured):g} совпал с "
        f"прочитанной глубиной {float(depth):g}, ГОСТ 23360 для Ø{float(diameter):g} — "
        f"{b:g} × {t:g}"
    )
    base = {
        "kind": "keyway",
        "path": item["path"],
        "feature_id": item.get("feature_id"),
        "action": "adopt",
        "source": "keyway_swap",
        "reason": reason,
    }
    return [
        {
            **base,
            "field": "width_mm",
            "read": float(width),
            "measured": float(measured),
            "value": float(depth),
        },
        {
            **base,
            "field": "depth_mm",
            "read": float(depth),
            "measured": float(width),
            "value": float(width),
        },
    ]


def _keyway_position(
    spec: dict[str, Any], item: dict[str, Any], report: dict[str, Any]
) -> list[dict[str, Any]]:
    """Начало паза по надписям листа, выбранным замером.

    Положения на листе — цепочки и выносные, их общее правило отдаёт
    человеку. Но у паза опора известна: он в одной ступени, и его место
    задано надписью от её уступа. Живой z4-r4: прочитано начало 63, замер
    69,3…91,5; на листе «2» от уступа 95 до конца паза — конец 93, начало
    93 − 22 = 71. Кандидаты — уступы ступени (по замеру середины паза) ±
    надпись; надпись должна стоять между своими опорами (рамка ридера), в
    полосе главного вида (не на выносном виде); и начало, и конец обязаны
    сойтись с замером в точности самого листа. Кандидат один — принимается,
    иначе — человеку.
    """
    from app.ai.cad_recognize.keyway_standard import step_for, steps_with_stations

    if item.get("kind") != "keyway" or item.get("status") != "refuted":
        return []
    read = item.get("read") or {}
    measured = item.get("measured") or {}
    frame = report.get("frame") or {}
    start_read, length = read.get("axial_start_mm"), read.get("length_mm")
    start, span = measured.get("axial_start_mm"), measured.get("length_mm")
    origin, scale, box = frame.get("origin_px"), frame.get("mm_per_px"), frame.get("bbox_px")
    if not all(isinstance(v, (int, float)) for v in (start_read, length, start, span, scale)):
        return []
    if not origin or not box:
        return []
    tolerance = float((item.get("tolerance_mm") or {}).get("length") or 0.5)
    if abs(start - start_read) <= tolerance:
        return []
    outer = [s for s in ((spec.get("main_view") or {}).get("outer") or []) if isinstance(s, dict)]
    holder, _inside = step_for(steps_with_stations(outer), float(start), float(span))
    if holder is None:
        return []
    low, high = float(holder[0]), float(holder[1])
    total = sum(float(s.get("length_mm") or 0.0) for s in outer)
    sheet_error = float((report.get("profile_adoption") or {}).get("station_error_mm") or 0.0)
    accuracy = max(tolerance, 1.25 * sheet_error)
    margin = 0.08 * total
    key = _resolve(spec, str(item.get("path") or "")) or {}
    own = {float(v) for v in (length, key.get("width_mm"), key.get("depth_mm"), total) if v}
    labels = [(v, c) for v, c in _axial_labels(spec, frame) if v not in own]
    found: dict[float, str] = {}
    for value, column in labels:
        for anchor, start_c, first, second in (
            (low, low + value, low, low + value),
            (high, high - value - float(length), high - value, high),
        ):
            if not (low <= start_c and start_c + float(length) <= high):
                continue
            if not first - margin <= column <= second + margin:
                continue
            if (
                abs(start_c - start) > accuracy
                or abs(start_c + float(length) - (start + span)) > accuracy
            ):
                continue
            side = "начала" if anchor == low else "конца"
            found[round(start_c, 3)] = (
                f"«{value:g}» от уступа {anchor:g} до {side} паза: начало {start_c:g}, "
                f"конец {start_c + float(length):g}; замер {start:g}…{start + span:g}"
            )
    if len(found) != 1:
        return []
    value, detail = next(iter(found.items()))
    item.setdefault("tolerance_mm", {})["length"] = round(accuracy, 3)
    return [
        {
            "kind": "keyway",
            "path": item["path"],
            "feature_id": item.get("feature_id"),
            "field": "axial_start_mm",
            "read": float(start_read),
            "measured": float(start),
            "action": "adopt",
            "source": "keyway_position",
            "value": value,
            "reason": f"положение паза по надписи листа: {detail} (лист точен до {accuracy:.1f} мм)",
        }
    ]


def _axial_labels(spec: dict[str, Any], frame: dict[str, Any]) -> list[tuple[float, float]]:
    """Осевые надписи главного вида: ``(значение, центр вдоль оси, мм)``.

    Только с рамкой ридера и в полосе главного вида: надписи выносных видов и
    сечений (z4-r4: «3» канавки, «3,5» сечения Б-Б) — не про положения вала.
    """
    from app.ai.cad_recognize.verifiers.sheet_profile import sheet_labels

    origin, scale, box = frame.get("origin_px"), frame.get("mm_per_px"), frame.get("bbox_px")
    if not origin or not isinstance(scale, (int, float)) or not box:
        return []
    band = (box[3] - box[1]) * 0.6
    labels = []
    for entry in spec.get("dimensions") or []:
        if not isinstance(entry, dict):
            continue
        parsed = sheet_labels({"dimensions": [entry]}).axial
        bbox = entry.get("bbox")
        if not parsed or not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
            continue
        row = (float(bbox[1]) + float(bbox[3])) / 2.0
        if not box[1] - band <= row <= box[3] + band:
            continue
        column = ((float(bbox[0]) + float(bbox[2])) / 2.0 - float(origin[0])) * float(scale)
        labels.append((parsed[0][0], column))
    return labels


def keyway_additions(spec: dict[str, Any], report: dict[str, Any]) -> list[dict[str, Any]]:
    """Пазы, которые проверка нашла на листе, а ридер не выписал, — по надписям.

    Живой z4-r4: второй паз на Ø22 (149,7…175 × 6,1 по замеру) ридер
    пропустил; на листе «25» — его длина, «10» — от его конца до торца 185.
    Паз принимается, только если длину даёт надпись над ним, а положение —
    надпись от уступа ступени до его начала или конца, и оба сходятся с
    замером в точности листа; вариант один. Сечение — по ГОСТ 23360 для Ø
    ступени (ширина по замеру ему отвечает, это условие предложения).
    """
    proposals = report.get("keyway_proposals") or []
    frame = report.get("frame") or {}
    outer = [s for s in ((spec.get("main_view") or {}).get("outer") or []) if isinstance(s, dict)]
    if not proposals or not outer:
        return []
    stations = [0.0]
    for step in outer:
        stations.append(stations[-1] + float(step.get("length_mm") or 0.0))
    total = stations[-1]
    sheet_error = float((report.get("profile_adoption") or {}).get("station_error_mm") or 0.0)
    accuracy = max(0.5, 1.25 * sheet_error)
    margin = 0.08 * total
    labels = [(v, c) for v, c in _axial_labels(spec, frame) if abs(v - total) > 1e-6]
    additions = []
    for proposal in proposals:
        index = proposal.get("step_index")
        if not isinstance(index, int) or not 0 <= index < len(outer):
            continue
        low, high = stations[index], stations[index + 1]
        start, span = float(proposal["axial_start_mm"]), float(proposal["length_mm"])
        found: dict[tuple[float, float], str] = {}
        for i, (length, length_column) in enumerate(labels):
            if abs(length - span) > accuracy:
                continue
            if not start - margin <= length_column <= start + span + margin:
                continue
            for j, (value, column) in enumerate(labels):
                if i == j:
                    continue
                for start_c, first, second, anchor, side in (
                    (low + value, low, low + value, low, "начала"),
                    (high - value - length, high - value, high, high, "конца"),
                ):
                    if not (low <= start_c and start_c + length <= high):
                        continue
                    if not first - margin <= column <= second + margin:
                        continue
                    if (
                        abs(start_c - start) > accuracy
                        or abs(start_c + length - (start + span)) > accuracy
                    ):
                        continue
                    found[(round(start_c, 3), length)] = (
                        f"«{length:g}» — длина, «{value:g}» — от уступа {anchor:g} до {side} "
                        f"паза: {start_c:g}…{start_c + length:g}; замер {start:g}…{start + span:g}"
                    )
        if len(found) != 1:
            continue
        (value, length), detail = next(iter(found.items()))
        width, depth = proposal["standard_mm"]
        additions.append(
            {
                "step_index": index,
                "axial_start_mm": value,
                "length_mm": length,
                "width_mm": float(width),
                "depth_mm": float(depth),
                "evidence_bbox_px": proposal.get("evidence_bbox_px"),
                "reason": (
                    f"паз найден на листе, ридер его не выписал: {detail}; сечение "
                    f"{width:g} × {depth:g} по ГОСТ 23360 для Ø"
                    f"{float(outer[index].get('diameter_mm') or 0):g} "
                    f"(ширина по замеру {float(proposal['width_mm']):g}); "
                    f"лист точен до {accuracy:.1f} мм"
                ),
            }
        )
    return additions


def apply_keyway_additions(spec: dict[str, Any], additions: list[dict[str, Any]]) -> dict[str, Any]:
    """Найденные по листу пазы — в копию спека, с происхождением и свидетельством."""
    from app.ai.cad_recognize.keyway_standard import ground_keyways

    spec = copy.deepcopy(spec)
    main = spec.setdefault("main_view", {})
    keyways = main.setdefault("keyways", [])
    outer = main.get("outer") or []
    provenance = spec.setdefault("provenance", {})
    if not isinstance(provenance, dict):
        provenance = {}
        spec["provenance"] = provenance
    for addition in additions:
        index = len(keyways)
        box = addition.get("evidence_bbox_px")
        keyways.append(
            {
                "id": f"sheet:keyways:{index}",
                "kind": "parallel",
                "end_type": "closed",
                "axial_start_mm": addition["axial_start_mm"],
                "length_mm": addition["length_mm"],
                "width_mm": addition["width_mm"],
                "depth_mm": addition["depth_mm"],
                "angle_deg": 0.0,
                "standard_ref": "ГОСТ 23360",
                "on_section_id": (outer[addition["step_index"]] or {}).get("id"),
                "evidence": (
                    [{"image_index": 0, "bbox": list(box), "raw_text": "паз найден по листу"}]
                    if box
                    else []
                ),
                "review_required": False,
            }
        )
        for field in ("axial_start_mm", "length_mm", "width_mm", "depth_mm"):
            provenance[f"main_view.keyways[{index}].{field}"] = {
                "origin": "sheet_measurement",
                "detail": addition["reason"],
                "value_mm": addition[field],
            }
    spec["unresolved"] = [
        note for note in spec.get("unresolved") or [] if not str(note).startswith("шпоночный паз ")
    ]
    ground_keyways(main, spec["unresolved"])
    return spec


# Положение → вид допуска стадии (как в `stage._GRAPH_FIELDS`).
_OTHER_TOLERANCE = {
    "axial_start_mm": "length",
    "axial_position_mm": "position",
    "center_x_mm": "position",
    "center_y_mm": "position",
    "start_angle_deg": "phase",
    "count": "count",
}

_INDEX = re.compile(r"^(\w+)\[(\d+)\]$")


def _resolve(spec: dict[str, Any], path: str) -> dict[str, Any] | None:
    node: Any = spec
    for part in path.split("."):
        match = _INDEX.match(part)
        if match:
            node = (node or {}).get(match.group(1)) if isinstance(node, dict) else None
            index = int(match.group(2))
            node = node[index] if isinstance(node, list) and 0 <= index < len(node) else None
        else:
            node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            return None
    return node if isinstance(node, dict) else None


def apply_reconciliation(
    spec: dict[str, Any], report: dict[str, Any], decisions: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Принятые замеры — в копию спека (с происхождением) и в отчёт проверки.

    В отчёте у поля прочитанное меняется на принятое, а прежнее прочитанное
    уходит в ``reconciled``: граф получает подтверждение принятого значения, а
    не «противоречие» с уже исправленным. Элемент без оставшихся расхождений
    становится подтверждённым с причиной «принято по листу».
    """
    spec = copy.deepcopy(spec)
    report = copy.deepcopy(report)
    provenance = spec.setdefault("provenance", {})
    if not isinstance(provenance, dict):
        provenance = {}
        spec["provenance"] = provenance
    items = {item.get("path"): item for item in report.get("items") or []}
    for decision in decisions:
        if decision.get("action") != "adopt":
            continue
        node = _resolve(spec, decision["path"])
        if node is None:
            continue
        node[decision["field"]] = decision["value"]
        provenance[f"{decision['path']}.{decision['field']}"] = {
            "origin": "sheet_measurement",
            "detail": decision["reason"],
            "value_mm": decision["value"],
            "read_mm": decision["read"],
        }
        item = items.get(decision["path"])
        if item is None:
            continue
        item.setdefault("reconciled", {})[decision["field"]] = {
            "read": decision["read"],
            "adopted": decision["value"],
        }
        item["read"][decision["field"]] = decision["value"]
    for item in items.values():
        if item.get("status") != "refuted" or not item.get("reconciled"):
            continue
        tolerances = item.get("tolerance_mm") or {}
        fields = ADOPTABLE.get(item.get("kind"), ())
        remaining = [
            field
            for field, kind in fields
            if isinstance((item.get("read") or {}).get(field), (int, float))
            and isinstance((item.get("measured") or {}).get(field), (int, float))
            and isinstance(tolerances.get(kind), (int, float))
            and abs(item["measured"][field] - item["read"][field]) > tolerances[kind]
        ]
        # Опровергнутое не из надписанных полей (положение) остаётся
        # опровергнутым; совпавшее с замером положение — не расхождение (паз
        # z4-r4 после перестановки ширины и глубины: начало сошлось).
        other = [
            key
            for key in (item.get("measured") or {})
            if key not in {field for field, _ in fields}
            and key in (item.get("read") or {})
            and isinstance(item["read"][key], (int, float))
            and isinstance(item["measured"][key], (int, float))
            and not (
                isinstance(tolerances.get(_OTHER_TOLERANCE.get(key, "")), (int, float))
                and abs(item["measured"][key] - item["read"][key])
                <= tolerances[_OTHER_TOLERANCE[key]]
            )
        ]
        if not remaining and not other:
            item["status"] = "confirmed"
            item["reason"] = "принято по листу: " + "; ".join(
                f"{field} {value['read']:g} → {value['adopted']:g}"
                for field, value in item["reconciled"].items()
            )
    # Принятое у паза (живой z4-r4: ширина и глубина переставлены) делает
    # прежние замечания ГОСТ 23360 и «паз выходит за ступень» устаревшими —
    # они считаются заново по исправленному пазу.
    if any(d.get("kind") == "keyway" and d.get("action") == "adopt" for d in decisions):
        from app.ai.cad_recognize.keyway_standard import ground_keyways

        spec["unresolved"] = [
            note
            for note in spec.get("unresolved") or []
            if not str(note).startswith("шпоночный паз ")
        ]
        ground_keyways(spec.get("main_view") or {}, spec["unresolved"])
    # Сводка — по статусам после согласования: иначе панель показывает
    # опровергнутым то, что уже принято по листу.
    summary = report.get("summary")
    if isinstance(summary, dict):
        for status in ("confirmed", "refuted", "unmeasurable"):
            summary[status] = sum(
                1 for item in report.get("items") or [] if item.get("status") == status
            )
    return spec, report


def cavity_addition(spec: dict[str, Any], report: dict[str, Any]) -> dict[str, Any] | None:
    """Полость корпуса, найденная по листу, — в спек, если её числа на листе.

    Правило то же, что у паза, найденного по листу (E19): предложение строгое
    (ширина и глубина — из разреза, высота и положение — по штриховым кромкам
    плана), и каждое его число обязано стоять надписью на листе.
    """
    proposal = report.get("cavity_proposal")
    if not proposal:
        return None
    numbers = sheet_numbers(spec)
    values = [float(proposal[key]) for key in ("width_mm", "height_mm", "depth_mm")]
    if not all(
        any(abs(number - value) <= max(0.05, 0.01 * value) for number in numbers)
        for value in values
    ):
        return None
    return {**proposal, "action": "add"}


def apply_cavity(spec: dict[str, Any], addition: dict[str, Any]) -> dict[str, Any]:
    """Добавить найденную полость в контур детали."""
    import copy

    if addition.get("action") != "add":
        return spec
    spec = copy.deepcopy(spec)
    profile = ((spec.get("main_view") or {}).get("profile")) or {}
    if not profile:
        return spec
    walls = profile.setdefault("wall_features", [])
    walls.append(
        {
            "id": "sheet:wall_features:0",
            "kind": "pocket",
            "on_plane": "top",
            "profile": "rectangle",
            "width_mm": float(addition["width_mm"]),
            "height_mm": float(addition["height_mm"]),
            "depth_mm": float(addition["depth_mm"]),
            "center_u_mm": float(addition["center_u_mm"]),
            "center_v_mm": float(addition["center_v_mm"]),
            "evidence": (
                [
                    {
                        "image_index": 0,
                        "bbox": list(addition["bbox_px"]),
                        "raw_text": "полость по листу",
                    }
                ]
                if addition.get("bbox_px")
                else []
            ),
        }
    )
    spec.setdefault("optional_unresolved", []).append("найдено по листу: " + addition["reason"])
    return spec


def bent_section_decision(spec: dict[str, Any], report: dict[str, Any]) -> dict[str, Any] | None:
    """Направления гибов — по листу, если ридер ошибся только в них (X4).

    Модель путает швеллер и Z-профиль: полки и их размеры прочитаны верно, а
    направление гиба — нет. Число гибов совпало с листом, направления — нет:
    форма сечения видна на листе без чтения, и она принимается. Другое число
    гибов или неизмеренный угол — решение человеку: размеров недостающей полки
    лист замером не даёт.
    """
    item = next(
        (i for i in report.get("items") or [] if i.get("kind") == "bent_section"),
        None,
    )
    sheet = ((spec.get("main_view") or {}).get("sheet_metal")) or {}
    if item is None or item.get("status") != "refuted" or not sheet:
        return None
    measured = item.get("measured") or {}
    turns = list(measured.get("turns") or [])
    angles = list(measured.get("angles_deg") or [])
    read_turns = list(sheet.get("turns") or [])
    if len(turns) != len(read_turns) or None in angles:
        return {
            "kind": "bent_section",
            "path": "main_view.sheet_metal",
            "field": "turns",
            "action": "ask_human",
            "read": read_turns,
            "measured": turns,
            "reason": f"{item.get('reason')}: размеров полок лист замером не даёт — решение человеку",
        }
    return {
        "kind": "bent_section",
        "path": "main_view.sheet_metal",
        "field": "turns",
        "action": "adopt",
        "read": read_turns,
        "measured": turns,
        "value": {"turns": turns, "bend_angles_deg": angles},
        "reason": f"{item.get('reason')}: форма сечения принята по листу, размеры полок прочитаны",
    }


def apply_bent_section(spec: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    """Направления и углы гибов с листа — в копию спека."""
    import copy

    if decision.get("action") != "adopt":
        spec = copy.deepcopy(spec)
        spec.setdefault("optional_unresolved", []).append(decision["reason"])
        return spec
    spec = copy.deepcopy(spec)
    sheet = spec["main_view"]["sheet_metal"]
    sheet["turns"] = [int(t) for t in decision["value"]["turns"]]
    if decision["value"].get("flanges_mm"):
        # Полки — переспросом по вырезу (reask_bent_section), согласованным с листом.
        sheet["flanges_mm"] = [float(v) for v in decision["value"]["flanges_mm"]]
    angles = [float(round(a)) for a in decision["value"]["bend_angles_deg"]]
    if all(abs(a - 90.0) <= 5.0 for a in angles):
        sheet.pop("bend_angles_deg", None)
    else:
        sheet["bend_angles_deg"] = angles
    spec.setdefault("optional_unresolved", []).append(decision["reason"])
    return spec


def contradicting_patterns(spec: dict[str, Any], report: dict[str, Any]) -> list[dict[str, Any]]:
    """Массивы отверстий, которые противоречат отверстиям, подтверждённым листом.

    Живой корпус 4638b638: четыре отверстия Ø13,5 по углам ридер выписал и
    поштучно (все подтверждены проверкой), и массивом «окружность болтов Ø69»
    той же серии. Окружности на листе нет, она не помещается в контур, и гейт
    заблокировал сборку. Массив той же серии, чьи отверстия не совпали с
    подтверждёнными, — ошибка чтения, а не второй набор отверстий.
    """
    from app.ai.verify_corpus.score import expand_holes

    profile = ((spec.get("main_view") or {}).get("profile")) or {}
    patterns = [p for p in profile.get("hole_patterns") or [] if isinstance(p, dict)]
    confirmed = [
        item.get("read") or {}
        for item in report.get("items") or []
        if item.get("kind") == "plate_hole" and item.get("status") == "confirmed"
    ]
    holes = [
        (float(h["center_x_mm"]), float(h["center_y_mm"]), float(h["diameter_mm"]))
        for h in confirmed
        if all(
            isinstance(h.get(k), (int, float))
            for k in ("center_x_mm", "center_y_mm", "diameter_mm")
        )
    ]
    if not patterns or len(holes) < 2:
        return []
    decisions = []
    for index, pattern in enumerate(patterns):
        diameter = pattern.get("hole_diameter_mm")
        if not isinstance(diameter, (int, float)):
            continue
        same = [hole for hole in holes if abs(hole[2] - float(diameter)) <= 0.05]
        expanded = expand_holes({"hole_patterns": [pattern]})
        if len(same) < 2 or not expanded:
            continue
        matched = sum(
            1
            for x, y, _d in expanded
            if any(abs(x - hx) <= 1.0 and abs(y - hy) <= 1.0 for hx, hy, _hd in same)
        )
        if matched == len(expanded):
            continue
        decisions.append(
            {
                "kind": "hole_pattern",
                "path": f"main_view.profile.hole_patterns[{index}]",
                "action": "drop",
                "reason": (
                    f"массив Ø{float(diameter):g} × {len(expanded)} не совпал с "
                    f"{len(same)} отверстиями той же серии, подтверждёнными листом "
                    f"(совпало {matched}) — ошибка чтения, не построен"
                ),
            }
        )
    return decisions


def apply_pattern_drops(spec: dict[str, Any], decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """Снять массивы, противоречащие подтверждённым отверстиям."""
    import copy

    drops = {
        int(_INDEX_TAIL.search(d["path"]).group(1)) for d in decisions if d.get("action") == "drop"
    }
    if not drops:
        return spec
    spec = copy.deepcopy(spec)
    profile = ((spec.get("main_view") or {}).get("profile")) or {}
    patterns = profile.get("hole_patterns") or []
    # Замечания о снятом массиве устаревают вместе с ним: живой корпус
    # 26270edd — массив снят, а «делительная окружность Ø69 не помещается»
    # и «параметры массива не заданы» остались блокерами сборки.
    markers = [f"hole_patterns.{index}" for index in drops] + [
        f"окружность Ø{float(patterns[index]['bolt_circle_diameter_mm']):g} "
        for index in drops
        if index < len(patterns)
        and isinstance(patterns[index], dict)
        and isinstance(patterns[index].get("bolt_circle_diameter_mm"), (int, float))
    ]
    profile["hole_patterns"] = [
        pattern for index, pattern in enumerate(patterns) if index not in drops
    ]
    spec["unresolved"] = [
        note
        for note in spec.get("unresolved") or []
        if not any(marker in str(note) for marker in markers)
    ]
    spec.setdefault("optional_unresolved", []).extend(d["reason"] for d in decisions)
    return spec


_INDEX_TAIL = re.compile(r"\[(\d+)\]$")


def housing_decision(spec: dict[str, Any], report: dict[str, Any]) -> dict[str, Any] | None:
    """Принять габарит корпуса по листу вместо прочитанного.

    Правило то же, что у профиля вала: предложение строгое (три вида в
    проекционной связи, обе стороны плана легли на надписи листа), и
    принимается оно, только если ПРОЧИТАННОЕ листом не подтвердилось —
    толщина по видам не сошлась или план по прочитанным габаритам не найден.
    Подтверждённое проверкой не заменяется.

    Требовать, чтобы прочитанного не было среди надписей, здесь нельзя: у
    корпуса надписей много (координаты элементов), и «50» с «16» на листе
    есть — просто не как габарит (живой корпус: прочитано 50 × 80 × 16 при
    80 × 80 × 50).
    """
    proposal = report.get("housing_proposal")
    if not proposal:
        return None
    numbers = sheet_numbers(spec)
    proposed = [float(proposal[key]) for key in ("width_mm", "height_mm", "thickness_mm")]
    if not all(
        any(abs(number - value) <= max(0.05, 0.01 * value) for number in numbers)
        for value in proposed
    ):
        return {
            **proposal,
            "action": "ask_human",
            "reason": proposal["reason"] + "; часть чисел по листу не подтверждена надписями",
        }
    confirmed = any(
        item.get("kind") == "plate_thickness" and item.get("status") == "confirmed"
        for item in report.get("items") or []
    )
    if confirmed:
        return None  # прочитанное подтверждено замером — не заменяем
    return {**proposal, "action": "adopt"}


def apply_housing(spec: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    """Габарит корпуса по листу — в спек (контур прямоугольный)."""
    import copy

    if decision.get("action") != "adopt":
        return spec
    spec = copy.deepcopy(spec)
    profile = ((spec.get("main_view") or {}).get("profile")) or {}
    if not profile:
        return spec
    profile["shape"] = "rectangle"
    profile.pop("sketch", None)
    for key in ("width_mm", "height_mm", "thickness_mm"):
        profile[key] = float(decision[key])
    notes = spec.setdefault("optional_unresolved", [])
    notes.append(
        "принято по листу: габарит корпуса "
        f"{decision['width_mm']:g} × {decision['height_mm']:g} × {decision['thickness_mm']:g}"
    )
    return spec


def profile_decision(spec: dict[str, Any], report: dict[str, Any]) -> dict[str, Any] | None:
    """Принять профиль вала, собранный по листу, вместо прочитанного.

    Предложение `sheet_profile` строгое: каждый уступ вида объяснён своей
    надписью, каждый Ø — надписью Ø или резьбой, неоднозначность — отказ
    (корпус v9, чистые листы: 20 верных, 0 неверных). Принимается, только
    если прочитанный профиль по листу не подтвердился — хотя бы одна ступень
    опровергнута или не измерена (живой z4-r4: «не тот вид», 12 расхождений
    из 13). Подтверждённое проверкой не заменяется.
    """
    proposal = report.get("profile_proposal") or {}
    steps = proposal.get("steps")
    items = [item for item in report.get("items") or [] if item.get("kind") == "shaft_step"]
    if not steps or not items or all(item.get("status") == "confirmed" for item in items):
        return None
    outer = ((spec.get("main_view") or {}).get("outer")) or []
    read = [[step.get("diameter_mm"), step.get("length_mm")] for step in outer]
    return {
        "kind": "shaft_profile",
        "path": "main_view.outer",
        "field": "outer",
        "action": "adopt",
        "read": read,
        "value": [dict(step) for step in steps],
        "station_error_mm": float(proposal.get("station_error_mm") or 0.0),
        "reason": (
            f"уступы вида и надписи листа дают профиль {_profile_text(steps)} "
            f"(габарит {proposal.get('total_mm'):g}, уступы до "
            f"{proposal.get('station_error_mm', 0.0):.1f} мм от надписей); "
            f"прочитанный {_profile_text(outer)} "
            "по листу не подтвердился"
        ),
    }


def sleeve_decision(spec: dict[str, Any], report: dict[str, Any]) -> dict[str, Any] | None:
    """Принять втулку по листу (`sleeve_section`) вместо прочитанного.

    Предложение строгое: каждый уступ наружного профиля и расточки — надписью,
    грани фланца — надписями цепочки, контур фланца — гипотезой по надписям с
    покрытием линии, отверстия — надписями Ø и «N отв.»; масштаб вида с торца
    обязан совпасть с разрезом. Принимается, если прочитанный профиль по листу
    не подтвердился (живая втулка part_03: Ø16×4 · Ø15×2 · Ø29×6 — «не тот вид»).
    """
    proposal = report.get("sleeve_proposal") or {}
    if not proposal.get("outer"):
        return None
    items = [item for item in report.get("items") or [] if item.get("kind") == "shaft_step"]
    if items and all(item.get("status") == "confirmed" for item in items):
        return None
    main = spec.get("main_view") or {}
    flange = proposal.get("flange")
    flange_text = ""
    if isinstance(flange, dict):
        outline = flange.get("outline") or {}
        patterns = (flange.get("profile") or {}).get("hole_patterns") or []
        flange_text = (
            f"; фланец {flange['thickness_mm']:g} мм на {flange['axial_start_mm']:g}: "
            f"Ø{outline.get('diameter_mm', 0):g}"
            + (
                f" с {outline['flats']} лысками на {outline['flat_distance_mm']:g}"
                if outline.get("flats")
                else ""
            )
            + "".join(
                f", {p['count']} отв. Ø{p['hole_diameter_mm']:g} на Ø{p['bolt_circle_diameter_mm']:g}"
                for p in patterns
            )
        )
    return {
        "kind": "sleeve",
        "path": "main_view",
        "field": "outer+bore+flanges",
        "action": "adopt",
        "read": {
            "outer": [[s.get("diameter_mm"), s.get("length_mm")] for s in main.get("outer") or []],
            "bore": [[s.get("diameter_mm"), s.get("length_mm")] for s in main.get("bore") or []],
        },
        "value": proposal,
        "reason": (
            f"разрез и надписи дают втулку {_profile_text(proposal['outer'])}, расточка "
            f"{_profile_text(proposal['bore'])}{flange_text}; прочитанный "
            f"{_profile_text(main.get('outer') or [])} по листу не подтвердился"
        ),
    }


def apply_sleeve(spec: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    """Втулка по листу — в копию спека: профиль (как `apply_profile`), расточка
    от левого торца насквозь, фланец; происхождение каждой величины."""
    value = decision["value"]
    spec = apply_profile(spec, {"value": value["outer"], "reason": decision["reason"]})
    main = spec.setdefault("main_view", {})
    bore = []
    for index, step in enumerate(value["bore"]):
        bore.append(
            {
                "id": f"0:bore:{index}",
                "diameter_mm": float(step["diameter_mm"]),
                "length_mm": float(step["length_mm"]),
                "note": None,
                "evidence": (
                    [
                        {
                            "image_index": 0,
                            "bbox": list(step["bbox_px"]),
                            "raw_text": "расточка разреза и надписи листа",
                        }
                    ]
                    if isinstance(step.get("bbox_px"), (list, tuple)) and len(step["bbox_px"]) == 4
                    else []
                ),
                "review_required": False,
            }
        )
    main["bore"] = bore
    main["bore_start_mm"] = 0.0
    main["bore_from_end"] = "left"
    main["bore_blind"] = False
    provenance = spec.setdefault("provenance", {})
    for index, entry in enumerate(bore):
        for field in ("diameter_mm", "length_mm"):
            provenance[f"main_view.bore[{index}].{field}"] = {
                "origin": "sheet_measurement",
                "detail": decision["reason"],
                "value_mm": entry[field],
            }
    flange = value.get("flange")
    if isinstance(flange, dict):
        main["flanges"] = [
            {
                "id": "0:flanges:0",
                "axial_start_mm": float(flange["axial_start_mm"]),
                "thickness_mm": float(flange["thickness_mm"]),
                "sketch_origin_mm": list(flange.get("sketch_origin_mm") or (0.0, 0.0)),
                "profile": copy.deepcopy(flange["profile"]),
                "evidence": [],
            }
        ]
        provenance["main_view.flanges[0]"] = {
            "origin": "sheet_measurement",
            "detail": decision["reason"],
        }
    main["type"] = main.get("type") or "тело вращения"
    # Замечания ридера о расточке и «поперечных отверстиях» с Ø листа устарели:
    # расточка и отверстия фланца теперь из разреза и вида с торца.
    spec["unresolved"] = [
        note
        for note in spec.get("unresolved") or []
        if not any(marker in str(note).lower() for marker in _STALE_SLEEVE)
    ]
    return spec


_STALE_SLEEVE = ("расточк", "поперечное отверстие", "профиль короче листа", "ступен")


def contour_decision(spec: dict[str, Any], report: dict[str, Any]) -> dict[str, Any] | None:
    """Принять контур пластины по листу (`plate_contour`) вместо прочитанного.

    Предложение строгое: каждая сторона — надписью, каждое сопряжение —
    надписью R, каждое отверстие — надписью Ø и координатами по надписям,
    неоднозначность — отказ. Принимается, если прочитанные отверстия по листу
    не подтвердились (живая планка part_04: прямоугольник 90 × 100 с двумя
    отверстиями «на заявленном x окружности нет»). Подтверждённое не
    заменяется.
    """
    proposal = report.get("contour_proposal") or {}
    sheet = proposal.get("profile")
    if not sheet:
        return None
    items = [item for item in report.get("items") or [] if item.get("kind") == "plate_hole"]
    if items and all(item.get("status") == "confirmed" for item in items):
        return None
    read = (spec.get("main_view") or {}).get("profile") or {}
    arcs = sorted(
        {
            round(
                ((s["to"][0] - s["center"][0]) ** 2 + (s["to"][1] - s["center"][1]) ** 2) ** 0.5, 1
            )
            for s in sheet.get("sketch") or []
            if s.get("kind") == "arc"
        }
    )
    holes = "; ".join(
        f"Ø{h['diameter_mm']:g} ({h['center_x_mm']:g}; {h['center_y_mm']:g})"
        for h in sheet.get("holes") or []
    )
    return {
        "kind": "plate_contour",
        "path": "main_view.profile",
        "field": "profile",
        "action": "adopt",
        "read": {
            "shape": read.get("shape"),
            "width_mm": read.get("width_mm"),
            "height_mm": read.get("height_mm"),
            "holes": len(read.get("holes") or []),
        },
        "value": sheet,
        "side_error_mm": float(proposal.get("side_error_mm") or 0.0),
        "reason": (
            f"контур по листу {sheet.get('width_mm'):g} × {sheet.get('height_mm'):g}, "
            f"{len(sheet.get('sketch') or [])} участков"
            + (f", сопряжения {', '.join(f'R{r:g}' for r in arcs)}" if arcs else "")
            + f"; отверстия {holes or 'нет'}; прочитано: {read.get('shape')} "
            f"{read.get('width_mm')} × {read.get('height_mm')}, отверстий "
            f"{len(read.get('holes') or [])} — по листу не подтвердились"
        ),
    }


def apply_contour(spec: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    """Контур пластины по листу — в копию спека, с происхождением."""
    spec = copy.deepcopy(spec)
    main = spec.setdefault("main_view", {})
    read = main.get("profile") or {}
    profile = copy.deepcopy(decision["value"])
    if not profile.get("thickness_mm") and read.get("thickness_mm"):
        profile["thickness_mm"] = read["thickness_mm"]
    for index, hole in enumerate(profile.get("holes") or []):
        hole.setdefault("id", f"0:profile.holes:{index}")
    main["profile"] = profile
    provenance = spec.setdefault("provenance", {})
    if not isinstance(provenance, dict):
        provenance = {}
        spec["provenance"] = provenance
    provenance["main_view.profile"] = {
        "origin": "sheet_measurement",
        "detail": decision["reason"],
    }
    # Замечания ридера о прежнем контуре, отверстиях и толщине устарели:
    # живая планка part_04 держала «положение Ø10 не проставлено», «координаты
    # относительно центра (45, 50)», «толщина не указана» при S3 на листе.
    spec["unresolved"] = [
        note
        for note in spec.get("unresolved") or []
        if not _stale_contour_note(str(note), bool(profile.get("thickness_mm")))
    ]
    return spec


_STALE_CONTOUR = (
    "отверсти",
    "выходит за контур",
    "пропорции окружностей",
    "контур",
    "скруглени",
    "радиус угл",
)


def _stale_contour_note(note: str, has_thickness: bool) -> bool:
    lowered = note.lower()
    if "толщин" in lowered:
        return has_thickness
    return any(marker in lowered for marker in _STALE_CONTOUR)


def apply_profile(spec: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    """Профиль по листу — в копию спека, с происхождением каждой величины.

    Паз переносится на ступень, в которую попадает его начало: прежняя
    ссылка указывала на ступень прочитанного профиля.
    """
    spec = copy.deepcopy(spec)
    main = spec.setdefault("main_view", {})
    outer = []
    for index, step in enumerate(decision["value"]):
        entry: dict[str, Any] = {
            "id": f"0:outer:{index}",
            "diameter_mm": float(step["diameter_mm"]),
            "length_mm": float(step["length_mm"]),
            "note": None,
            # Свидетельство — участок главного вида, где стоит ступень: гейт
            # сборки не пускает геометрию без него.
            "evidence": (
                [
                    {
                        "image_index": 0,
                        "bbox": list(step["bbox_px"]),
                        "raw_text": "уступы вида и надписи листа",
                    }
                ]
                if isinstance(step.get("bbox_px"), (list, tuple)) and len(step["bbox_px"]) == 4
                else []
            ),
            "review_required": False,
        }
        thread = step.get("thread")
        if isinstance(thread, dict):
            entry["thread"] = {**thread, "length_mm": float(step["length_mm"]), "evidence": []}
            entry["note"] = f"резьба {thread.get('designation')}"
        outer.append(entry)
    main["outer"] = outer
    stations = [0.0]
    for entry in outer:
        stations.append(stations[-1] + entry["length_mm"])
    for keyway in main.get("keyways") or []:
        start = keyway.get("axial_start_mm") if isinstance(keyway, dict) else None
        if isinstance(start, (int, float)):
            index = next(
                (i for i in range(len(outer)) if stations[i] <= start < stations[i + 1]), None
            )
            if index is not None:
                keyway["on_section_id"] = outer[index]["id"]
    provenance = spec.setdefault("provenance", {})
    if not isinstance(provenance, dict):
        provenance = {}
        spec["provenance"] = provenance
    for index, entry in enumerate(outer):
        for field in ("diameter_mm", "length_mm"):
            provenance[f"main_view.outer[{index}].{field}"] = {
                "origin": "sheet_measurement",
                "detail": decision["reason"],
                "value_mm": entry[field],
            }
    # Замечания ридера о прежнем профиле устарели: резьбы теперь на своих
    # ступенях, Ø ступеней объяснены надписями (живой z4-r4: «резьбы не
    # привязаны», «поперечное отверстие Ø25 не локализовано» — это Ø ступени).
    diameters = {round(entry["diameter_mm"], 3) for entry in outer}
    spec["unresolved"] = [
        note
        for note in spec.get("unresolved") or []
        if not _stale_profile_note(str(note), diameters)
    ]
    # Паз против ступеней и ГОСТ 23360 — заново, по принятому профилю:
    # прежние замечания называли ступени прочитанного (z4-r4: «Ø35 34..74»).
    from app.ai.cad_recognize.keyway_standard import ground_keyways

    ground_keyways(main, spec["unresolved"])
    # Голоса проходов чтения по прежним ступеням к новым не относятся.
    votes = spec.get("value_provenance")
    if isinstance(votes, dict):
        spec["value_provenance"] = {
            key: value for key, value in votes.items() if not key.startswith("main_view/outer")
        }
    return spec


_STALE_PROFILE = (
    "резьбы указаны, но не привязаны",
    "наружные диаметры не подтверждены",
    "шпоночный паз ",
)
_CROSS_HOLE_NOTE = re.compile(
    r"поперечное отверстие Ø(\d+(?:[.,]\d+)?) указано, но не локализовано"
)


def _stale_profile_note(note: str, diameters: set[float]) -> bool:
    if any(marker in note for marker in _STALE_PROFILE):
        return True
    match = _CROSS_HOLE_NOTE.search(note)
    if not match:
        return False
    value = round(float(match.group(1).replace(",", ".")), 3)
    # Ø ступени — это не отверстие; Ø чуть меньше Ø ступени — дно канавки
    # или проточки на выносном виде (z4-r4: Ø24,5 у Ø25, Ø21,7 у Ø22, Ø15,7
    # у M18). Поперечное отверстие много меньше вала, в котором сверлится.
    return any(0.8 * d <= value <= d for d in diameters)


def _profile_text(steps: list[dict[str, Any]]) -> str:
    def number(value: Any) -> str:
        return f"{float(value):g}" if isinstance(value, (int, float)) else "?"

    parts = []
    for step in steps:
        thread = step.get("thread") if isinstance(step.get("thread"), dict) else None
        head = thread.get("designation") if thread else f"Ø{number(step.get('diameter_mm'))}"
        parts.append(f"{head}×{number(step.get('length_mm'))}")
    return " · ".join(parts)


def sheet_thickness_decision(spec: dict[str, Any], item: dict[str, Any]) -> dict[str, Any] | None:
    """Толщина листа по сечению — принимается, только если это число с листа.

    Живой Z-профиль: s прочитано 2 при 2,5 на листе — все полки вышли на
    0,5 мм мимо (прямой участок = размер − (R + s) у гиба). Ширина сечения
    даёт толщину с точностью ~0,15 мм; принимается ближайшая к ней надпись,
    если она одна в допуске, иначе — человеку.
    """
    from app.ai.cad_recognize.verifiers.bent_section import (
        THICKNESS_TOLERANCE_MM,
        THICKNESS_TOLERANCE_SHARE,
    )

    if item.get("status") != "refuted":
        return None
    measured = float(item["measured"]["thickness_mm"])
    tolerance = max(THICKNESS_TOLERANCE_MM, THICKNESS_TOLERANCE_SHARE * measured)
    # «s2.5» — обозначение толщины листа; общий разбор надписей букву перед
    # числом не пропускает (чтобы «R2» не шло размером), здесь она своя.
    stated = set(sheet_numbers(spec))
    for entry in spec.get("dimensions") or []:
        text = entry.get("value") if isinstance(entry, dict) else entry
        for match in _THICKNESS_LABEL.finditer(str(text or "")):
            stated.add(round(float(match.group(1).replace(",", ".")), 3))
    near = sorted(v for v in stated if abs(v - measured) <= tolerance)
    base = {
        "kind": "sheet_thickness",
        "path": "main_view.sheet_metal",
        "field": "thickness_mm",
        "read": item["read"]["thickness_mm"],
        "measured": measured,
    }
    if len(near) != 1:
        return {
            **base,
            "action": "ask_human",
            "reason": f"{item['reason']}: надписи с такой толщиной на листе "
            + ("нет" if not near else "не одна")
            + " — решение человеку",
        }
    return {
        **base,
        "action": "adopt",
        "value": near[0],
        "reason": f"{item['reason']}: принята толщина {near[0]:g} с листа",
    }


def apply_sheet_thickness(spec: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    """Толщина с листа; прямые участки полок пересчитаны под неё (размеры полок
    на листе те же — по наружной поверхности)."""
    import copy
    import math

    spec = copy.deepcopy(spec)
    if decision.get("action") != "adopt":
        spec.setdefault("optional_unresolved", []).append(decision["reason"])
        return spec
    sheet = spec["main_view"]["sheet_metal"]
    old, new = float(sheet["thickness_mm"]), float(decision["value"])
    angles = sheet.get("bend_angles_deg") or [90.0] * len(sheet.get("turns") or [])
    shift = [(new - old) * math.tan(math.radians(angle) / 2.0) for angle in angles]
    count = len(sheet["flanges_mm"])
    sheet["flanges_mm"] = [
        round(
            float(value) - (shift[i - 1] if i > 0 else 0.0) - (shift[i] if i < count - 1 else 0.0),
            3,
        )
        for i, value in enumerate(sheet["flanges_mm"])
    ]
    sheet["thickness_mm"] = new
    spec.setdefault("optional_unresolved", []).append(decision["reason"])
    return spec


def settle_provisional_sheet_metal(spec: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """Снять пометки предварительно принятого в сечении, подтверждённого листом.

    Форма, названная ридером не та, и толщина, которой нет среди выписанных
    надписей, принимаются предварительно (``spec_fragments``) и блокируют
    сборку. Снимает пометку только лист: сечение подтверждено (или форма
    взята с него), толщина подтверждена (или взята надпись по замеру).
    """
    import copy

    from app.ai.cad_recognize.spec_fragments import PROVISIONAL_THICKNESS, PROVISIONAL_TURNS

    settled = set()
    for item in report.get("items") or []:
        if item.get("kind") == "bent_section" and item.get("status") == "confirmed":
            settled.add(PROVISIONAL_TURNS)
        if item.get("kind") == "sheet_thickness" and (
            item.get("status") == "confirmed" or item.get("adopted")
        ):
            settled.add(PROVISIONAL_THICKNESS)
    notes = [str(n) for n in spec.get("unresolved") or []]
    if not settled or not any(n in settled for n in notes):
        return spec
    spec = copy.deepcopy(spec)
    spec["unresolved"] = [n for n in notes if n not in settled]
    spec.setdefault("optional_unresolved", []).extend(
        f"подтверждено по листу: {n}" for n in notes if n in settled
    )
    return spec
