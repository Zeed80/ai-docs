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
        reason = _shaft(image_bytes, (spec or {}).get("main_view") or {}, report, spec or {})
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


def _shaft(
    image_bytes: bytes, body: dict[str, Any], report: dict[str, Any], spec: dict[str, Any]
) -> str | None:
    """Наружный профиль тела вращения: Ø и длина каждой ступени по главному виду.

    Проверяльщик отвечает на весь профиль сразу; здесь вердикт раскладывается
    по ступеням. Грубый лист, «не тот вид» и прочие отказы профиля в целом —
    «не измеримо» у каждой ступени с той же причиной, а не молчание.
    """
    from app.ai.cad_recognize.verifiers.shaft_frame import locate_shaft_views
    from app.ai.cad_recognize.verifiers.shaft_profile import chain_frame, shaft_tolerances

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
    views = locate_shaft_views(gray, total)
    if not views:
        reason = "главный вид вала на листе не найден"
        for index, step in steps:
            report["items"].append(_step_item(index, step, "unmeasurable", {}, reason))
        _keyways(gray, [], body, report)
        _cross_holes(gray, [], body, report)
        _turned_details(gray, [], body, report)
        return reason
    # Масштаб — по цепочке, а не по прочитанной сумме (`chain_frame`): одно
    # неверное звено иначе уводит всё — и ступени, и пазы.
    lengths = [float(step["length_mm"]) for _, step in steps]
    views = [(chain_frame(view, shape, lengths), shape) for view, shape in views]
    frame, profile = views[0]
    spans = _keyways(gray, [view_frame for view_frame, _profile in views], body, report)
    _cross_holes(gray, [view_frame for view_frame, _profile in views], body, report)
    _turned_details(gray, views, body, report)
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
                # Пролёты пазов — по проверке пазов, а не как прочитаны:
                # несуществующий паз прятал неверный Ø ступени под собой.
                "keyways": spans,
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
    if _sheet_profile(profile, total, body, spec, report):
        return None
    return verdict.reason if whole_unmeasurable else None


def _sheet_profile(
    profile: Any,
    read_total: float,
    body: dict[str, Any],
    spec: dict[str, Any],
    report: dict[str, Any],
) -> bool:
    """Профиль по листу (`sheet_profile`): уступы вида + надписи, которые выписал ридер.

    Совпал с прочитанным — ступени подтверждены по листу, даже если замер
    разошёлся с надписью больше допуска: нарисованный не точно в масштабе
    лист (z4-r4: уступы до 1,7 мм от надписей, Ø на 1,5 % шире по фото)
    иначе опровергал бы верное чтение. Не совпал — предложение ложится в
    отчёт (``profile_proposal``), решает согласование. Возвращает True, если
    прочитанное подтверждено так.
    """
    from app.ai.cad_recognize.verifiers.sheet_profile import propose_profile, sheet_labels

    reserved = tuple(
        float(keyway[key])
        for keyway in body.get("keyways") or []
        if isinstance(keyway, dict)
        for key in ("length_mm", "width_mm")
        if _is_number(keyway.get(key))
    )
    proposal, why = propose_profile(
        profile,
        sheet_labels(spec),
        read_total,
        allow_threads=not body.get("bore"),
        reserved=reserved,
    )
    if proposal is None:
        report["profile_proposal"] = {"steps": None, "reason": why}
        return False
    read = [
        (float(step["diameter_mm"]), float(step["length_mm"])) for step in body.get("outer") or []
    ]
    same = len(read) == len(proposal.steps) and all(
        abs(d - step["diameter_mm"]) <= 0.01 and abs(length - step["length_mm"]) <= 0.01
        for (d, length), step in zip(read, proposal.steps)
    )
    if not same:
        report["profile_proposal"] = proposal.as_payload()
        return False
    reason = (
        "уступы и Ø вида объясняются надписями листа "
        f"(уступы до {proposal.station_error_mm:.1f} мм от надписей)"
    )
    for item in report["items"]:
        if item["kind"] == "shaft_step" and item["status"] != "confirmed":
            item["status"], item["reason"] = "confirmed", reason
    report["notes"] = [note for note in report["notes"] if not note.startswith("ступень ")]
    return True


_KEYWAY_KEYS = ("axial_start_mm", "length_mm", "width_mm")


def _keyways(
    gray: Any, frames: list[Any], body: dict[str, Any], report: dict[str, Any]
) -> list[dict[str, float]]:
    """Шпоночные пазы главного вида: капсула — начало, длина, ширина (Ф3).

    Лицом на главном виде виден только закрытый призматический паз; у
    открытого и сегментного контур другой — «не измеримо» с причиной.
    Виды вала — по очереди: у полого вала первый — разрез, где паз лицом не
    виден (капсулу расточки отсекает штриховка), паз — на виде под ним.

    Возвращает пролёты пазов для проверки профиля (под пазом Ø ступени не
    мерится): найденный паз — измеренным пролётом; не найденный на
    прочитанном месте — никаким (живой shaft-1: ридер поставил паз на ступень
    3, и её неверный Ø40 при 25 на листе прошёл без вердикта); честно не
    измеримый — прочитанным.
    """
    from app.ai.cad_recognize.verifiers.keyway import NOT_FOUND_REASON
    from app.ai.cad_recognize.verifiers.shaft_profile import shaft_tolerances

    spans: list[dict[str, float]] = []
    for index, key in enumerate(body.get("keyways") or []):
        if not isinstance(key, dict):
            continue
        read_span = (
            {"axial_start_mm": float(key["axial_start_mm"]), "length_mm": float(key["length_mm"])}
            if _is_number(key.get("axial_start_mm")) and _is_number(key.get("length_mm"))
            else None
        )
        if not all(_is_number(key.get(k)) for k in _KEYWAY_KEYS):
            if read_span:
                spans.append(read_span)
            continue
        item = {
            "kind": "keyway",
            "path": f"main_view.keyways[{index}]",
            "feature_id": key.get("id"),
            "read": {k: key.get(k) for k in _KEYWAY_KEYS},
            "status": "unmeasurable",
            "measured": {},
            "reason": "",
        }
        report["items"].append(item)
        if (key.get("kind") or "parallel") != "parallel" or (
            key.get("end_type") or "closed"
        ) != "closed":
            item["reason"] = "проверяется только закрытый призматический паз"
            spans.append(read_span)
            continue
        hypothesis = Hypothesis("keyway", item["path"], {k: float(key[k]) for k in _KEYWAY_KEYS})
        frame, verdict = _first_measured(hypothesis, frames, gray)
        if verdict.reason == NOT_FOUND_REASON:
            frame, verdict = _keyway_other_width(
                key, body, item["path"], frames, gray, frame, verdict
            )
        item.update(status=verdict.status, measured=dict(verdict.measured), reason=verdict.reason)
        if verdict.status in ("confirmed", "refuted"):
            spans.append(
                {
                    "axial_start_mm": float(verdict.measured["axial_start_mm"]),
                    "length_mm": float(verdict.measured["length_mm"]),
                }
            )
        elif verdict.reason != NOT_FOUND_REASON:
            spans.append(read_span)
        if frame is not None:
            length_tol, width_tol = shaft_tolerances(frame.scale_mean)
            item["tolerance_mm"] = {"length": round(length_tol, 3), "width": round(width_tol, 3)}
        if verdict.status == "refuted":
            got = verdict.measured
            report["notes"].append(
                f"паз {index + 1}: прочитано {_mm(key['axial_start_mm'])}…"
                f"{_mm(float(key['axial_start_mm']) + float(key['length_mm']))} × "
                f"{_mm(key['width_mm'])}, по листу — {_mm(got['axial_start_mm'])}…"
                f"{_mm(got['axial_start_mm'] + got['length_mm'])} × {_mm(got['width_mm'])}"
                " — проверить"
            )


def _keyway_other_width(
    key: dict[str, Any],
    body: dict[str, Any],
    path: str,
    frames: list[Any],
    gray: Any,
    frame: Any,
    verdict: Any,
) -> tuple[Any, Any]:
    """Паз не найден по прочитанной ширине — искать по другой, судить по прочитанному.

    Окно поиска капсулы — от прочитанной ширины (×0,4…1,8). Живой z4-r4:
    ридер переставил ширину и глубину (4 и 8 вместо 8 и 4), паз 8 мм в окно
    не попадал и «не находился», хотя на листе он есть. Другие ширины —
    прочитанная глубина (перестановка) и ГОСТ 23360 для Ø ступени паза.
    Найденное сравнивается с прочитанным, как обычно.
    """
    from app.ai.cad_recognize.keyway_standard import (
        standard_section,
        step_for,
        steps_with_stations,
    )
    from app.ai.cad_recognize.verifiers.contract import Verdict
    from app.ai.cad_recognize.verifiers.shaft_profile import shaft_tolerances

    read = {k: float(key[k]) for k in _KEYWAY_KEYS}
    options: list[tuple[float, str]] = []
    if _is_number(key.get("depth_mm")) and float(key["depth_mm"]) > 0:
        options.append(
            (
                float(key["depth_mm"]),
                "прочитанной глубине — ширина и глубина, похоже, переставлены",
            )
        )
    holder, _inside = step_for(
        steps_with_stations([s for s in body.get("outer") or [] if isinstance(s, dict)]),
        read["axial_start_mm"],
        read["length_mm"],
    )
    diameter = holder[2].get("diameter_mm") if holder else None
    standard = standard_section(float(diameter)) if _is_number(diameter) else None
    if standard:
        options.append((standard[0], f"ширине по ГОСТ 23360 для Ø{_mm(float(diameter))}"))
    for width, why in options:
        if abs(width - read["width_mm"]) <= 0.25 * read["width_mm"]:
            continue  # то же окно — уже искали
        found_frame, found = _first_measured(
            Hypothesis("keyway", path, {**read, "width_mm": width}), frames, gray
        )
        if found.status not in ("confirmed", "refuted") or found_frame is None:
            continue
        length_tol, width_tol = shaft_tolerances(found_frame.scale_mean)
        got = found.measured
        problems = [
            f"{title} {got[field]:g} мм, прочитано {read[field]:g}"
            for field, title, tolerance in (
                ("axial_start_mm", "начало", length_tol),
                ("length_mm", "длина", length_tol),
                ("width_mm", "ширина", width_tol),
            )
            if abs(got[field] - read[field]) > tolerance
        ]
        note = f"паз найден по {why}"
        return found_frame, Verdict(
            status="refuted" if problems else "confirmed",
            measured=dict(got),
            evidence_bbox_px=found.evidence_bbox_px,
            reason=f"{'; '.join(problems)} ({note})" if problems else note,
        )
    return frame, verdict


_CROSS_HOLE_KEYS = ("axial_position_mm", "diameter_mm")


def _cross_holes(
    gray: Any, frames: list[Any], body: dict[str, Any], report: dict[str, Any]
) -> None:
    """Поперечные отверстия главного вида: положение по оси и Ø (Ф3).

    Лицом — окружностью на оси — на главном виде видно только одиночное
    отверстие под 0°/180°; остальные — «не измеримо» с причиной. Виды вала —
    по очереди, как у пазов: на разрезе полого вала отверстие — прорезь в
    штриховке, окружность — на виде под ним.
    """
    from app.ai.cad_recognize.verifiers.plate_hole import plate_hole_tolerances

    for index, hole in enumerate(body.get("cross_holes") or []):
        if not isinstance(hole, dict) or not all(_is_number(hole.get(k)) for k in _CROSS_HOLE_KEYS):
            continue
        item = {
            "kind": "cross_hole",
            "path": f"main_view.cross_holes[{index}]",
            "feature_id": hole.get("id"),
            "read": {k: hole.get(k) for k in _CROSS_HOLE_KEYS},
            "status": "unmeasurable",
            "measured": {},
            "reason": "",
        }
        report["items"].append(item)
        angle = float(hole.get("angle_deg") or 0.0) % 180.0
        if min(angle, 180.0 - angle) > 1.0 or int(hole.get("count") or 1) != 1:
            item["reason"] = "проверяется только одиночное отверстие лицом к главному виду"
            continue
        hypothesis = Hypothesis(
            "cross_hole", item["path"], {k: float(hole[k]) for k in _CROSS_HOLE_KEYS}
        )
        frame, verdict = _first_measured(hypothesis, frames, gray)
        item.update(status=verdict.status, measured=dict(verdict.measured), reason=verdict.reason)
        if frame is not None:
            position_tol, diameter_tol = plate_hole_tolerances(frame.scale_mean)
            item["tolerance_mm"] = {
                "position": round(position_tol, 3),
                "diameter": round(diameter_tol, 3),
            }
        if verdict.status == "refuted":
            got = verdict.measured
            report["notes"].append(
                f"поперечное отверстие {index + 1}: прочитано Ø{_mm(hole['diameter_mm'])} на "
                f"{_mm(hole['axial_position_mm'])}, по листу — Ø{_mm(got['diameter_mm'])} на "
                f"{_mm(got['axial_position_mm'])} — проверить"
            )


def _turned_details(
    gray: Any, views: list[tuple[Any, Any]], body: dict[str, Any], report: dict[str, Any]
) -> None:
    """Канавки и фаски на торцах — по профилю главного вида (Ф3.0c).

    Проверяльщику нужен и лист, и профиль вида (`ShaftProfile`: стенки
    канавки — его грани, дно — его полувысота); виды — по очереди.
    """
    from app.ai.cad_recognize.verifiers.chamfer import chamfer_tolerance
    from app.ai.cad_recognize.verifiers.groove import groove_tolerances

    def check(kind: str, path: str, feature: dict, read: dict[str, Any]) -> dict[str, Any]:
        item = {
            "kind": kind,
            "path": path,
            "feature_id": feature.get("id"),
            "read": read,
            "status": "unmeasurable",
            "measured": {},
            "reason": "",
        }
        report["items"].append(item)
        verdict = None
        frame = None
        for view_frame, profile in views or [(None, None)]:
            candidate = verify(Hypothesis(kind, path, read), view_frame, (gray, profile))
            # Вид, давший замер, — последний, даже если вердикт «не измеримо»
            # (изображение не в масштабе): иначе следующий вид мерил что-то
            # другое и «подтверждал» неверное чтение (корпус v9: канавка, 2
            # случая из 26).
            measured = candidate.status != "unmeasurable" or bool(candidate.measured)
            if verdict is None or measured:
                verdict, frame = candidate, view_frame
            if measured:
                break
        item.update(status=verdict.status, measured=dict(verdict.measured), reason=verdict.reason)
        if frame is not None:
            if kind == "groove":
                position_tol, width_tol, depth_tol = groove_tolerances(frame.scale_mean)
                item["tolerance_mm"] = {
                    "position": round(position_tol, 3),
                    "width": round(width_tol, 3),
                    "depth": round(depth_tol, 3),
                }
            else:
                item["tolerance_mm"] = {"size": round(chamfer_tolerance(frame.scale_mean), 3)}
        return item

    for index, groove in enumerate(body.get("grooves") or []):
        if (
            not isinstance(groove, dict)
            or groove.get("internal")
            or not _is_number(groove.get("axial_position_mm"))
            or not _is_number(groove.get("width_mm"))
        ):
            continue
        read = {
            key: groove.get(key)
            for key in ("axial_position_mm", "width_mm", "depth_mm")
            if _is_number(groove.get(key))
        }
        item = check("groove", f"main_view.grooves[{index}]", groove, read)
        if item["status"] == "refuted":
            got = item["measured"]
            report["notes"].append(
                f"канавка {index + 1}: прочитано {_mm(read['width_mm'])} на "
                f"{_mm(read['axial_position_mm'])}, по листу — {_mm(got['width_mm'])}×"
                f"{_mm(got['depth_mm'])} на {_mm(got['axial_position_mm'])} — проверить"
            )
    for index, chamfer in enumerate(body.get("chamfers") or []):
        if (
            not isinstance(chamfer, dict)
            or chamfer.get("location") not in ("left_end", "right_end")
            or not _is_number(chamfer.get("size_mm"))
        ):
            continue
        read = {"size_mm": chamfer["size_mm"], "location": chamfer["location"]}
        item = check("chamfer", f"main_view.chamfers[{index}]", chamfer, read)
        if item["status"] == "refuted":
            report["notes"].append(
                f"фаска {index + 1}: прочитано {_mm(chamfer['size_mm'])}, по листу — "
                f"{_mm(item['measured']['size_mm'])} — проверить"
            )


def _first_measured(hypothesis: Hypothesis, frames: list[Any], sheet: Any) -> tuple[Any, Any]:
    """Первый вид, на котором гипотеза измерима; иначе — отказ первого вида."""
    if not frames:
        return None, verify(hypothesis, None, sheet)
    first = None
    for frame in frames:
        verdict = verify(hypothesis, frame, sheet)
        if verdict.status != "unmeasurable":
            return frame, verdict
        first = first or (frame, verdict)
    return first


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
    "keyway": (("axial_start_mm", "length"), ("length_mm", "length"), ("width_mm", "width")),
    "cross_hole": (("axial_position_mm", "position"), ("diameter_mm", "diameter")),
    "groove": (("axial_position_mm", "position"), ("width_mm", "width"), ("depth_mm", "depth")),
    "chamfer": (("size_mm", "size"),),
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
