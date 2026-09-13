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
