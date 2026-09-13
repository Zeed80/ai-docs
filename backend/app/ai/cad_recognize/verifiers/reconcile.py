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
}
# Число не после цифры и не после латинской буквы, кроме M (резьба) и R
# (радиус): «Ø80js6» — 80, а не 80 и 6 (поле допуска посадки).
_NUMBER = re.compile(r"(?<![\d.,A-LN-QS-Za-z])(\d+(?:[.,]\d+)?)(?!\s*отв)")
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
        for field, tolerance_kind in ADOPTABLE.get(item.get("kind"), ()):
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
        # Опровергнутое не из надписанных полей (положение) остаётся опровергнутым.
        other = [
            key
            for key in (item.get("measured") or {})
            if key not in {field for field, _ in fields}
            and key in (item.get("read") or {})
            and isinstance(item["read"][key], (int, float))
            and isinstance(item["measured"][key], (int, float))
        ]
        if not remaining and not other:
            item["status"] = "confirmed"
            item["reason"] = "принято по листу: " + "; ".join(
                f"{field} {value['read']:g} → {value['adopted']:g}"
                for field, value in item["reconciled"].items()
            )
    # Сводка — по статусам после согласования: иначе панель показывает
    # опровергнутым то, что уже принято по листу.
    summary = report.get("summary")
    if isinstance(summary, dict):
        for status in ("confirmed", "refuted", "unmeasurable"):
            summary[status] = sum(
                1 for item in report.get("items") or [] if item.get("status") == status
            )
    return spec, report


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
        "reason": (
            f"уступы вида и надписи листа дают профиль {_profile_text(steps)} "
            f"(габарит {proposal.get('total_mm'):g}, уступы до "
            f"{proposal.get('station_error_mm', 0.0):.1f} мм от надписей); "
            f"прочитанный {_profile_text(outer)} "
            "по листу не подтвердился"
        ),
    }


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
            "evidence": [],
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
    # Голоса проходов чтения по прежним ступеням к новым не относятся.
    votes = spec.get("value_provenance")
    if isinstance(votes, dict):
        spec["value_provenance"] = {
            key: value for key, value in votes.items() if not key.startswith("main_view/outer")
        }
    return spec


_STALE_PROFILE = ("резьбы указаны, но не привязаны", "наружные диаметры не подтверждены")
_CROSS_HOLE_NOTE = re.compile(
    r"поперечное отверстие Ø(\d+(?:[.,]\d+)?) указано, но не локализовано"
)


def _stale_profile_note(note: str, diameters: set[float]) -> bool:
    if any(marker in note for marker in _STALE_PROFILE):
        return True
    match = _CROSS_HOLE_NOTE.search(note)
    return bool(match) and round(float(match.group(1).replace(",", ".")), 3) in diameters


def _profile_text(steps: list[dict[str, Any]]) -> str:
    def number(value: Any) -> str:
        return f"{float(value):g}" if isinstance(value, (int, float)) else "?"

    parts = []
    for step in steps:
        thread = step.get("thread") if isinstance(step.get("thread"), dict) else None
        head = thread.get("designation") if thread else f"Ø{number(step.get('diameter_mm'))}"
        parts.append(f"{head}×{number(step.get('length_mm'))}")
    return " · ".join(parts)
