"""Стадия проверки: прочитанный спек против самого листа (план, Ф1, P1.3).

Между чтением и сборкой каждая проверяемая гипотеза спека получает вердикт
проверяльщика. Прочитанное НЕ заменяется: опровергнутое остаётся как было и
уходит оператору замечанием вместе с замером — выбор между прочитанным и
измеренным делает согласование или человек, а не проверяльщик.

Проверяется:

* отверстия прямоугольной пластины (`plate_hole`) — система координат плана
  по контуру на листе (`locate_plate_frame`);
* у круглой детали — система координат по наружному контуру
  (`locate_circle_frame`), окружности болтов (`bolt_circle`) и центральное
  отверстие (`concentric_hole`, по радиальному профилю: поиск в полосе не
  отличит его от концентричной окружности центров).
"""

from __future__ import annotations

import io
import time
from typing import Any

from app.ai.cad_recognize.verifiers.contract import Hypothesis
from app.ai.cad_recognize.verifiers.registry import verify

_HOLE_KEYS = ("center_x_mm", "center_y_mm", "diameter_mm")
_PATTERN_KEYS = ("count", "bolt_circle_diameter_mm", "hole_diameter_mm", "start_angle_deg")


def verify_spec_against_sheet(image_bytes: bytes, spec: dict[str, Any]) -> dict[str, Any]:
    """Вердикты по проверяемым элементам спека.

    Возвращает ``{"items": [...], "summary": {...}, "frame": {...} | None,
    "notes": [...]}``. ``items`` — по элементу: вид проверки, путь в спеке,
    стабильный id, прочитанное, статус, измеренное (в тех же полях и
    координатах спека), причина, допуски. Пустой ``items`` — проверять нечего;
    ``summary.reason`` объясняет почему.
    """
    started = time.monotonic()
    profile = ((spec or {}).get("main_view") or {}).get("profile") or {}
    report: dict[str, Any] = {"items": [], "frame": None, "notes": []}
    shape = profile.get("shape")
    if shape == "rectangle":
        reason = _plate_holes(image_bytes, profile, report)
    elif shape == "circle":
        reason = _circular(image_bytes, profile, report)
    elif ((spec or {}).get("main_view") or {}).get("outer"):
        reason = _shaft(image_bytes, (spec or {}).get("main_view") or {}, report)
    else:
        reason = "нет проверяемых элементов (пластина, круглая деталь, тело вращения)"
    return _finish(report, started, reason)


def _gray(image_bytes: bytes) -> Any:
    import numpy as np
    from PIL import Image

    return np.asarray(Image.open(io.BytesIO(image_bytes)).convert("L"))


def _plate_holes(image_bytes: bytes, profile: dict[str, Any], report: dict[str, Any]) -> str | None:
    from app.ai.cad_recognize.verifiers.plate_frame import locate_plate_frame
    from app.ai.cad_recognize.verifiers.plate_hole import plate_hole_tolerances

    holes = [
        (index, hole)
        for index, hole in enumerate(profile.get("holes") or [])
        if isinstance(hole, dict) and all(_is_number(hole.get(key)) for key in _HOLE_KEYS)
    ]
    width, height = profile.get("width_mm"), profile.get("height_mm")
    if not holes:
        return "нет отверстий прямоугольной пластины"
    if not _is_number(width) or not _is_number(height):
        return "нет ширины и высоты плана"
    gray = _gray(image_bytes)
    frame = locate_plate_frame(gray, float(width), float(height))
    if frame is None:
        # Без системы координат вида проверки нет, но и молчать нельзя:
        # каждый элемент получает «не измеримо» с причиной.
        for index, hole in holes:
            report["items"].append(
                _hole_item(index, hole, "unmeasurable", {}, "план пластины на листе не найден")
            )
        return "план пластины на листе не найден"
    report["frame"] = _frame_payload(frame)
    position_tol, diameter_tol = plate_hole_tolerances(frame.scale_mean)
    half_w, half_h = float(width) / 2.0, float(height) / 2.0
    for index, hole in holes:
        verdict = verify(
            Hypothesis(
                "plate_hole",
                f"main_view.profile.holes[{index}]",
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
        item = _hole_item(index, hole, verdict.status, measured, verdict.reason)
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
    return None


def _circular(image_bytes: bytes, profile: dict[str, Any], report: dict[str, Any]) -> str | None:
    from app.ai.cad_recognize.verifiers.bolt_circle import _PHASE_DEG
    from app.ai.cad_recognize.verifiers.circle_frame import locate_circle_frame
    from app.ai.cad_recognize.verifiers.plate_hole import plate_hole_tolerances

    patterns = [
        (index, pattern)
        for index, pattern in enumerate(profile.get("hole_patterns") or [])
        if isinstance(pattern, dict)
        and (pattern.get("kind") or "bolt_circle") == "bolt_circle"
        and _is_number(pattern.get("bolt_circle_diameter_mm"))
        and _is_number(pattern.get("hole_diameter_mm"))
    ]
    central = [
        (index, hole)
        for index, hole in enumerate(profile.get("holes") or [])
        if isinstance(hole, dict)
        and all(_is_number(hole.get(key)) for key in _HOLE_KEYS)
        and abs(float(hole["center_x_mm"])) <= 0.01
        and abs(float(hole["center_y_mm"])) <= 0.01
    ]
    diameter = profile.get("diameter_mm")
    if not patterns and not central:
        return "нет окружностей болтов и центрального отверстия"
    if not _is_number(diameter):
        return "нет наружного диаметра"
    gray = _gray(image_bytes)
    frame = locate_circle_frame(gray, float(diameter))
    if frame is None:
        reason = "контур детали на листе не найден"
        for index, pattern in patterns:
            report["items"].append(_pattern_item(index, pattern, "unmeasurable", {}, reason))
        for index, hole in central:
            report["items"].append(_central_item(index, hole, "unmeasurable", {}, reason))
        return reason
    report["frame"] = _frame_payload(frame)
    position_tol, diameter_tol = plate_hole_tolerances(frame.scale_mean)
    for index, pattern in patterns:
        verdict = verify(
            Hypothesis(
                "bolt_circle",
                f"main_view.profile.hole_patterns[{index}]",
                {
                    "count": pattern.get("count"),
                    "pcd_mm": float(pattern["bolt_circle_diameter_mm"]),
                    "hole_diameter_mm": float(pattern["hole_diameter_mm"]),
                    "start_angle_deg": pattern.get("start_angle_deg"),
                },
            ),
            frame,
            gray,
        )
        measured = {}
        if verdict.measured:
            measured = {
                "count": verdict.measured["count"],
                "bolt_circle_diameter_mm": verdict.measured["pcd_mm"],
                "hole_diameter_mm": verdict.measured["hole_diameter_mm"],
                "start_angle_deg": verdict.measured["start_angle_deg"],
            }
        item = _pattern_item(index, pattern, verdict.status, measured, verdict.reason)
        item["tolerance_mm"] = {
            "count": 0,
            "pcd": round(2.0 * position_tol, 3),
            "diameter": round(diameter_tol, 3),
            "phase": _PHASE_DEG,
        }
        report["items"].append(item)
        if verdict.status == "refuted" and measured:
            read = item["read"]
            report["notes"].append(
                f"окружность болтов {index + 1}: прочитано {read['count']} отв. "
                f"Ø{_mm(read['hole_diameter_mm'])} на Ø{_mm(read['bolt_circle_diameter_mm'])}, "
                f"фаза {_mm(read['start_angle_deg'] or 0)}°; по листу — {measured['count']} отв. "
                f"Ø{_mm(measured['hole_diameter_mm'])} на "
                f"Ø{_mm(measured['bolt_circle_diameter_mm'])}, "
                f"фаза {_mm(measured['start_angle_deg'])}° — проверить"
            )
    for index, hole in central:
        verdict = verify(
            Hypothesis(
                "concentric_hole",
                f"main_view.profile.holes[{index}]",
                {"diameter_mm": float(hole["diameter_mm"])},
            ),
            frame,
            gray,
        )
        item = _central_item(index, hole, verdict.status, dict(verdict.measured), verdict.reason)
        item["tolerance_mm"] = {"diameter": round(diameter_tol, 3)}
        report["items"].append(item)
        if verdict.status == "refuted" and verdict.measured:
            report["notes"].append(
                f"центральное отверстие: прочитано Ø{_mm(hole['diameter_mm'])}, "
                f"по листу — Ø{_mm(verdict.measured['diameter_mm'])} — проверить"
            )
    return None


def _shaft(image_bytes: bytes, body: dict[str, Any], report: dict[str, Any]) -> str | None:
    """Наружный профиль тела вращения: Ø и длина каждой ступени по главному виду.

    Проверяльщик отвечает на весь профиль сразу; здесь вердикт раскладывается
    по ступеням. Грубый лист, «не тот вид» и прочие отказы профиля в целом —
    «не измеримо» у каждой ступени с той же причиной, а не молчание.
    """
    from app.ai.cad_recognize.verifiers.shaft_frame import locate_shaft_frame
    from app.ai.cad_recognize.verifiers.shaft_profile import shaft_tolerances

    outer = body.get("outer") or []
    steps = [
        (index, step)
        for index, step in enumerate(outer)
        if isinstance(step, dict)
        and _is_number(step.get("diameter_mm"))
        and _is_number(step.get("length_mm"))
    ]
    if len(steps) < 2 or len(steps) != len(outer):
        return "нет полного наружного профиля (Ø и длина каждой ступени)"
    total = sum(float(step["length_mm"]) for _, step in steps)
    gray = _gray(image_bytes)
    located = locate_shaft_frame(gray, total)
    if located is None:
        reason = "главный вид вала на листе не найден"
        for index, step in steps:
            report["items"].append(_step_item(index, step, "unmeasurable", {}, reason))
        return reason
    frame, profile = located
    verdict = verify(
        Hypothesis(
            "shaft_profile",
            "main_view.outer",
            {
                "steps": [
                    {
                        "diameter_mm": float(step["diameter_mm"]),
                        "length_mm": float(step["length_mm"]),
                    }
                    for _, step in steps
                ],
                "keyways": body.get("keyways") or [],
            },
        ),
        frame,
        profile,
    )
    report["frame"] = _frame_payload(frame)
    length_tol, diameter_tol = shaft_tolerances(frame.scale_mean)
    measured_steps = verdict.measured.get("steps") or []
    whole_unmeasurable = verdict.status == "unmeasurable"
    for index, step in steps:
        got = measured_steps[index] if index < len(measured_steps) else {}
        measured = {key: value for key, value in got.items() if value is not None}
        own = [
            part for part in verdict.reason.split("; ") if part.startswith(f"ступень {index + 1}:")
        ]
        wrong = any(
            key in measured and abs(measured[key] - float(step[key])) > tolerance
            for key, tolerance in (("diameter_mm", diameter_tol), ("length_mm", length_tol))
        )
        if whole_unmeasurable:
            status, reason, measured = "unmeasurable", verdict.reason, {}
        elif wrong:
            status, reason = "refuted", "; ".join(own)
        elif measured:
            status, reason = "confirmed", ""
        else:
            status, reason = "unmeasurable", "; ".join(own) or "ступень по виду не измерена"
        item = _step_item(index, step, status, measured, reason)
        item["tolerance_mm"] = {
            "diameter": round(diameter_tol, 3),
            "length": round(length_tol, 3),
        }
        report["items"].append(item)
        if status == "refuted":
            report["notes"].append(
                f"ступень {index + 1}: прочитано Ø{_mm(step['diameter_mm'])} × "
                f"{_mm(step['length_mm'])}, по листу — "
                f"Ø{_mm(measured['diameter_mm']) if 'diameter_mm' in measured else '—'} × "
                f"{_mm(measured['length_mm']) if 'length_mm' in measured else '—'} — проверить"
            )
    return verdict.reason if whole_unmeasurable else None


def _step_item(
    index: int, step: dict[str, Any], status: str, measured: dict[str, Any], reason: str
) -> dict[str, Any]:
    return {
        "kind": "shaft_step",
        "path": f"main_view.outer[{index}]",
        "feature_id": step.get("id"),
        "read": {key: step.get(key) for key in ("diameter_mm", "length_mm")},
        "status": status,
        "measured": measured,
        "reason": reason,
    }


# Поля вердикта в графе: поле спека → вид допуска.
_GRAPH_FIELDS = {
    "plate_hole": (
        ("center_x_mm", "position"),
        ("center_y_mm", "position"),
        ("diameter_mm", "diameter"),
    ),
    "bolt_circle": (
        ("count", "count"),
        ("bolt_circle_diameter_mm", "pcd"),
        ("hole_diameter_mm", "diameter"),
        ("start_angle_deg", "phase"),
    ),
    "concentric_hole": (("diameter_mm", "diameter"),),
    "shaft_step": (("diameter_mm", "diameter"), ("length_mm", "length")),
}


def apply_verification(graph: Any, report: dict[str, Any], *, pass_id: str) -> tuple[Any, int]:
    """Записать вердикты стадии в граф EMG — по утверждению на каждое поле.

    Вердикт по элементу раскладывается на поля: у plate-1 опровергнут только
    y, и помечать противоречивым верный Ø значило бы солгать графу; у
    фланца чаще всего опровергнута одна фаза. Поле, чьего утверждения в графе
    нет, пропускается. Возвращает новый граф и число записанных вердиктов;
    патчи применяются по одному — каждый строится на ревизии, оставленной
    предыдущим.
    """
    from app.ai.cad_recognize.verifiers.graph import verdict_patch
    from app.domain.engineering_model_graph import apply_graph_patch

    written = 0
    if report.get("frame"):
        graph = apply_graph_patch(graph, _view_scale_patch(graph, report, pass_id=pass_id))
    for item in report.get("items") or []:
        feature_id = item.get("feature_id")
        if not feature_id:
            continue
        for field, tolerance_kind in _GRAPH_FIELDS.get(item["kind"], ()):
            assertion_id = f"assertion:feature:{feature_id}:param:{field}"
            if not any(assertion.id == assertion_id for assertion in graph.assertions):
                continue
            read = item["read"].get(field)
            if read is None:
                continue
            verdict = _field_verdict(item, field, tolerance_kind)
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


def _view_scale_patch(graph: Any, report: dict[str, Any], *, pass_id: str) -> Any:
    """Масштаб проверенного вида — утверждением со свидетельством (план, P1.3).

    Путь «по описанию» масштаба в графе не имел вовсе (`assertion:sheet-scale`
    пишет только трассировка), хотя стадия проверки его находит по контуру.
    Масштаб по u — значением, по v и начало — в свидетельстве: выпрямленное
    фото анизотропно.
    """
    from app.domain.emg_predicates import PREDICATE
    from app.domain.engineering_model_graph import (
        Assertion,
        Evidence,
        ExactValue,
        GraphPatch,
    )

    frame = report["frame"]
    node_ids = {node.id for node in graph.nodes}
    subject = "sheet:0" if "sheet:0" in node_ids else "document-set:root"
    kinds = sorted({item["kind"] for item in report.get("items") or []})
    evidence = Evidence(
        id=f"evidence:trace:{pass_id}:view-frame",
        kind="trace_run",
        payload={"verifier": "view_frame", "checked_by": kinds, **frame},
    )
    assertion = Assertion(
        id=f"assertion:view-scale:{pass_id}",
        subject_id=subject,
        predicate=PREDICATE.SCALE_MM_PER_PX,
        value=ExactValue(kind="exact", value=frame["mm_per_px"]),
        unit="mm",
        origin="traced",
        assurance="observed",
        evidence_ids=[evidence.id],
        confidence=0.8,
    )
    return GraphPatch(
        patch_id=f"patch:trace:{pass_id}:view-frame",
        base_revision=graph.revision,
        base_sha256=graph.canonical_sha256,
        producer="tracer",
        pass_id=pass_id,
        idempotency_key=f"trace:{pass_id}:view-frame",
        add_evidence=[evidence],
        add_assertions=[assertion],
    )


def _field_verdict(item: dict[str, Any], field: str, tolerance_kind: str):
    from app.ai.cad_recognize.verifiers.bolt_circle import _angle_gap
    from app.ai.cad_recognize.verifiers.contract import Verdict

    measured = (item.get("measured") or {}).get(field)
    tolerance = (item.get("tolerance_mm") or {}).get(tolerance_kind)
    read = item["read"][field]
    if item["status"] == "unmeasurable" or measured is None or tolerance is None:
        return Verdict(status="unmeasurable", reason=item.get("reason") or "")
    if field == "start_angle_deg":
        # Фаза — по модулю шага массива: 0° и 90° у четырёх отверстий — одно.
        step = 360.0 / max(int(item["measured"].get("count") or 1), 1)
        gap = _angle_gap(float(read) % step, float(measured), step)
    else:
        gap = abs(float(measured) - float(read))
    wrong = gap > float(tolerance)
    return Verdict(
        status="refuted" if wrong else "confirmed",
        measured={field: measured},
        reason=(f"замер {measured:g}, прочитано {read:g}" if wrong else ""),
    )


def _hole_item(
    index: int, hole: dict[str, Any], status: str, measured: dict[str, Any], reason: str
) -> dict[str, Any]:
    return {
        "kind": "plate_hole",
        "path": f"main_view.profile.holes[{index}]",
        # Стабильный id элемента (`assign_stable_feature_ids`) — по нему
        # вердикт находит узел Feature в графе.
        "feature_id": hole.get("id"),
        "read": {key: hole[key] for key in _HOLE_KEYS},
        "status": status,
        "measured": measured,
        "reason": reason,
    }


def _central_item(
    index: int, hole: dict[str, Any], status: str, measured: dict[str, Any], reason: str
) -> dict[str, Any]:
    return {
        "kind": "concentric_hole",
        "path": f"main_view.profile.holes[{index}]",
        "feature_id": hole.get("id"),
        "read": {"diameter_mm": hole["diameter_mm"]},
        "status": status,
        "measured": measured,
        "reason": reason,
    }


def _pattern_item(
    index: int, pattern: dict[str, Any], status: str, measured: dict[str, Any], reason: str
) -> dict[str, Any]:
    return {
        "kind": "bolt_circle",
        "path": f"main_view.profile.hole_patterns[{index}]",
        "feature_id": pattern.get("id"),
        "read": {key: pattern.get(key) for key in _PATTERN_KEYS},
        "status": status,
        "measured": measured,
        "reason": reason,
    }


def _frame_payload(frame: Any) -> dict[str, Any]:
    return {
        "origin_px": [round(v, 1) for v in frame.origin_px],
        "mm_per_px": round(frame.mm_per_px, 5),
        "mm_per_px_v": round(frame.scale_v, 5),
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


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _mm(value: float) -> str:
    return f"{float(value):.1f}".rstrip("0").rstrip(".").replace(".", ",")
