"""Стадия проверки: прочитанный спек против самого листа (план, Ф1, P1.3).

Между чтением и сборкой каждая проверяемая гипотеза спека получает вердикт
проверяльщика. Прочитанное НЕ заменяется: опровергнутое остаётся как было и
уходит оператору замечанием вместе с замером — выбор между прочитанным и
измеренным делает согласование или человек, а не проверяльщик.

Пока проверяются отверстия прямоугольной пластины (`plate_hole`), система
координат плана — по контуру на листе (`locate_plate_frame`). Массивы
отверстий и прочие элементы — следующими проверяльщиками.
"""

from __future__ import annotations

import io
import time
from typing import Any

from app.ai.cad_recognize.verifiers.contract import Hypothesis
from app.ai.cad_recognize.verifiers.registry import verify


def verify_spec_against_sheet(image_bytes: bytes, spec: dict[str, Any]) -> dict[str, Any]:
    """Вердикты по проверяемым элементам спека.

    Возвращает ``{"items": [...], "summary": {...}, "frame": {...} | None,
    "notes": [...]}``. ``items`` — по элементу: путь в спеке, прочитанное,
    статус, измеренное (в тех же координатах спека), причина. Пустой
    ``items`` — проверять нечего; ``summary.reason`` объясняет почему.
    """
    started = time.monotonic()
    profile = ((spec or {}).get("main_view") or {}).get("profile") or {}
    holes = [
        (index, hole)
        for index, hole in enumerate(profile.get("holes") or [])
        if isinstance(hole, dict)
        and all(
            isinstance(hole.get(key), (int, float))
            for key in ("center_x_mm", "center_y_mm", "diameter_mm")
        )
    ]
    width, height = profile.get("width_mm"), profile.get("height_mm")
    report: dict[str, Any] = {"items": [], "frame": None, "notes": []}
    if profile.get("shape") != "rectangle" or not holes:
        return _finish(report, started, "нет отверстий прямоугольной пластины")
    if not isinstance(width, (int, float)) or not isinstance(height, (int, float)):
        return _finish(report, started, "нет ширины и высоты плана")

    import numpy as np
    from PIL import Image

    from app.ai.cad_recognize.verifiers.plate_frame import locate_plate_frame
    from app.ai.cad_recognize.verifiers.plate_hole import plate_hole_tolerances

    gray = np.asarray(Image.open(io.BytesIO(image_bytes)).convert("L"))
    frame = locate_plate_frame(gray, float(width), float(height))
    if frame is None:
        # Без системы координат вида проверки нет, но и молчать нельзя:
        # каждый элемент получает «не измеримо» с причиной.
        for index, hole in holes:
            report["items"].append(
                _item(index, hole, "unmeasurable", {}, "план пластины на листе не найден")
            )
        return _finish(report, started, "план пластины на листе не найден")
    report["frame"] = {
        "origin_px": [round(v, 1) for v in frame.origin_px],
        "mm_per_px": round(frame.mm_per_px, 5),
        "mm_per_px_v": round(frame.scale_v, 5),
    }
    half_w, half_h = float(width) / 2.0, float(height) / 2.0
    for index, hole in holes:
        path = f"main_view.profile.holes[{index}]"
        verdict = verify(
            Hypothesis(
                "plate_hole",
                path,
                {
                    "x_mm": float(hole["center_x_mm"]) + half_w,
                    "y_mm": float(hole["center_y_mm"]) + half_h,
                    "diameter_mm": float(hole["diameter_mm"]),
                },
            ),
            frame,
            gray,
        )
        measured = {}
        if verdict.measured:
            measured = {
                "center_x_mm": round(verdict.measured["x_mm"] - half_w, 3),
                "center_y_mm": round(verdict.measured["y_mm"] - half_h, 3),
                "diameter_mm": verdict.measured["diameter_mm"],
            }
        item = _item(index, hole, verdict.status, measured, verdict.reason)
        position_tol, diameter_tol = plate_hole_tolerances(frame.scale_mean)
        item["tolerance_mm"] = {
            "position": round(position_tol, 3),
            "diameter": round(diameter_tol, 3),
        }
        report["items"].append(item)
        if verdict.status == "refuted" and measured:
            report["notes"].append(
                f"отверстие {index + 1}: прочитано Ø{_mm(hole['diameter_mm'])} "
                f"в ({_mm(float(hole['center_x_mm']) + half_w)}; "
                f"{_mm(float(hole['center_y_mm']) + half_h)}) от левой и нижней кромки, "
                f"по листу — Ø{_mm(measured['diameter_mm'])} в "
                f"({_mm(measured['center_x_mm'] + half_w)}; "
                f"{_mm(measured['center_y_mm'] + half_h)}) — проверить"
            )
    return _finish(report, started, None)


_HOLE_FIELDS = (
    ("center_x_mm", "position"),
    ("center_y_mm", "position"),
    ("diameter_mm", "diameter"),
)


def apply_verification(graph: Any, report: dict[str, Any], *, pass_id: str) -> tuple[Any, int]:
    """Записать вердикты стадии в граф EMG — по утверждению на каждое поле.

    Вердикт по отверстию раскладывается на x, y и Ø: у plate-1 опровергнут
    только y, и помечать противоречивым верный Ø значило бы солгать графу.
    Поле, чьего утверждения в графе нет, пропускается. Возвращает новый граф
    и число записанных вердиктов; патчи применяются по одному — каждый
    строится на ревизии, оставленной предыдущим.
    """
    from app.ai.cad_recognize.verifiers.contract import Verdict
    from app.ai.cad_recognize.verifiers.graph import verdict_patch
    from app.domain.engineering_model_graph import apply_graph_patch

    written = 0
    for item in report.get("items") or []:
        feature_id = item.get("feature_id")
        if not feature_id:
            continue
        for field, tolerance_kind in _HOLE_FIELDS:
            assertion_id = f"assertion:feature:{feature_id}:param:{field}"
            if not any(assertion.id == assertion_id for assertion in graph.assertions):
                continue
            measured = (item.get("measured") or {}).get(field)
            tolerance = (item.get("tolerance_mm") or {}).get(tolerance_kind)
            read = item["read"][field]
            if item["status"] == "unmeasurable" or measured is None or tolerance is None:
                verdict = Verdict(status="unmeasurable", reason=item.get("reason") or "")
            else:
                wrong = abs(float(measured) - float(read)) > float(tolerance)
                verdict = Verdict(
                    status="refuted" if wrong else "confirmed",
                    measured={field: measured},
                    reason=(f"замер {measured:g}, прочитано {read:g}" if wrong else ""),
                )
            patch = verdict_patch(
                graph,
                Hypothesis(item["kind"], f"{item['path']}.{field}", {field: read}),
                verdict,
                assertion_id=assertion_id,
                pass_id=pass_id,
                measured_key=field,
            )
            graph = apply_graph_patch(graph, patch)
            written += 1
    return graph, written


def _item(
    index: int, hole: dict[str, Any], status: str, measured: dict[str, Any], reason: str
) -> dict[str, Any]:
    return {
        "kind": "plate_hole",
        "path": f"main_view.profile.holes[{index}]",
        # Стабильный id элемента (`assign_stable_feature_ids`) — по нему
        # вердикт находит узел Feature в графе.
        "feature_id": hole.get("id"),
        "read": {key: hole[key] for key in ("center_x_mm", "center_y_mm", "diameter_mm")},
        "status": status,
        "measured": measured,
        "reason": reason,
    }


def _finish(report: dict[str, Any], started: float, reason: str | None) -> dict[str, Any]:
    counts = {"confirmed": 0, "refuted": 0, "unmeasurable": 0}
    for item in report["items"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    report["summary"] = {
        "checked": len(report["items"]),
        **counts,
        "reason": reason,
        "cost_ms": round((time.monotonic() - started) * 1000.0, 1),
    }
    return report


def _mm(value: float) -> str:
    return f"{float(value):.1f}".rstrip("0").rstrip(".").replace(".", ",")
