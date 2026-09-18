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

# Толщина по виду и надпись сходятся в пределах этого (лист рисуется в
# масштабе, но линия имеет толщину, а замер идёт по её середине).
_THICKNESS_TOLERANCE_MM = 0.5
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
        _sleeve(image_bytes, spec or {}, report)
        if report.get("sleeve_confirmed"):
            reason = None
    else:
        reason = "нет проверяемых элементов (пластина, круглая деталь, тело вращения)"
    if shape in {"rectangle", "sketch"}:
        _plate_contour(image_bytes, spec or {}, profile, report)
    if shape in {"rectangle", "sketch"}:
        # Эскиз тоже: живой корпус прочитан «пластиной» 50 × 80 × 16 (контур по
        # листу дал sketch), и толщину не проверял никто — а по видам она 50.
        _housing_by_sheet(image_bytes, spec or {}, profile, report)
        _housing_thickness(image_bytes, profile, report)
        _wall_features_on_sheet(image_bytes, profile, report)
    _sheet_scale(image_bytes, spec or {}, report)
    return _finish(report, started, reason)


def _sheet_scale(image_bytes: bytes, spec: dict[str, Any], report: dict[str, Any]) -> None:
    """Масштаб чертежа по листу (`sheet_scale`): штамп ЕСКД — линейка бумаги,
    проверенный вид — линейка детали. Только когда вид проверкой найден."""
    from app.ai.cad_recognize.verifiers.sheet_scale import (
        drawing_scale,
        locate_title_block,
        same_scale,
    )

    frame = report.get("frame") or {}
    if not frame.get("mm_per_px"):
        return
    block = locate_title_block(_gray(image_bytes))
    if block is None:
        return
    measured = drawing_scale(float(frame["mm_per_px"]), block)
    stated = (spec.get("title_block") or {}).get("scale")
    measured["stated"] = stated
    measured["agrees"] = bool(stated) and same_scale(stated, measured["label"])
    report["sheet_scale"] = measured
    if stated and measured["label"] and not measured["agrees"]:
        report["notes"].append(
            f"масштаб в штампе {stated}, по листу {measured['label']} "
            f"(вид {measured['view_px_per_mm']:g} px/мм, бумага "
            f"{measured['paper_px_per_mm']:g} px/мм) — проверить"
        )


def attach_scale_evidence(spec: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """Масштаб штампа, подтверждённый листом, — со свидетельством: рамкой штампа.

    Граф помечал масштаб «без свидетельства» у каждой механической сборки
    (`drawing_scale_evidence_missing`). Свидетельство даётся, только если
    масштаб, измеренный отношением вида к штампу, совпал с масштабом в штампе.
    """
    import copy

    measured = report.get("sheet_scale") or {}
    if not measured.get("agrees"):
        return spec
    spec = copy.deepcopy(spec)
    provenance = spec.setdefault("value_provenance", {})
    if not isinstance(provenance, dict):
        return spec
    entry = provenance.setdefault("title_block/scale", {})
    entry["evidence"] = [
        {
            "source_bbox": list(measured["stamp_bbox_px"]),
            "image_index": 0,
            "pass": "sheet_scale",
            "raw_text": (
                f"основная надпись 185 × 55 мм: {measured['paper_px_per_mm']:g} px/мм бумаги; "
                f"вид {measured['view_px_per_mm']:g} px/мм детали → {measured['label']}"
            ),
        }
    ]
    return spec


def _sleeve(image_bytes: bytes, spec: dict[str, Any], report: dict[str, Any]) -> None:
    """Втулка по листу (`sleeve_section`): разрез + вид с торца + надписи.

    Только когда прочитанный профиль вала листом не подтвердился и профиль
    вала по листу не собрался: у разреза втулки через ось не идёт ничего, и
    поиск вала вставал на штамп (живая втулка part_03). Совпало с прочитанным
    — предложения нет.
    """
    from app.ai.cad_recognize.verifiers.sleeve_section import propose_sleeve

    steps = [item for item in report["items"] if item["kind"] == "shaft_step"]
    if steps and all(item["status"] == "confirmed" for item in steps):
        return
    if (report.get("profile_proposal") or {}).get("steps"):
        return
    proposal, why = propose_sleeve(_gray(image_bytes), spec)
    if proposal is None:
        report["sleeve_proposal"] = {"outer": None, "reason": why}
        return
    main = spec.get("main_view") or {}

    def sections(items: list[Any]) -> list[tuple[float, float]]:
        return [
            (round(float(i.get("diameter_mm") or 0), 3), round(float(i.get("length_mm") or 0), 3))
            for i in items
            if isinstance(i, dict)
        ]

    same = (
        sections(main.get("outer") or []) == sections(list(proposal.outer))
        and sections(main.get("bore") or []) == sections(list(proposal.bore))
        and bool(main.get("flanges")) == bool(proposal.flange)
    )
    if not same:
        report["sleeve_proposal"] = proposal.as_payload()
        return
    # Совпало с прочитанным — ступени подтверждены по разрезу и надписям, как у
    # профиля вала по листу; вид вала здесь «не тот» по устройству.
    reason = "разрез втулки и надписи листа дают тот же профиль, расточку и фланец"
    for item in steps:
        if item["status"] != "confirmed":
            item["status"], item["reason"], item["measured"] = "confirmed", reason, {}
    report["notes"] = [note for note in report["notes"] if not note.startswith("ступень ")]
    report["sleeve_confirmed"] = True
    # Система координат — разреза втулки: вид вала здесь стоял на штампе
    # (живая втулка: масштаб вида в графе 0,0324 мм/px вместо 0,0218).
    if proposal.frame is not None:
        report["frame"] = _frame_payload(proposal.frame)
    if proposal.end_view_bbox_px is not None:
        report["sleeve_end_view"] = {"bbox_px": list(proposal.end_view_bbox_px)}


def _plate_contour(
    image_bytes: bytes, spec: dict[str, Any], profile: dict[str, Any], report: dict[str, Any]
) -> None:
    """Контур пластины по листу (`plate_contour`): предложение, если он не как прочитан.

    Живая планка part_04: Г-образная деталь прочитана прямоугольником с двумя
    отверстиями не на местах — контур и отверстия по листу собираются точно.
    Совпал с прочитанным — предложения нет.
    """
    from app.ai.cad_recognize.verifiers.plate_contour import propose_contour

    holes = [h for h in profile.get("holes") or [] if isinstance(h, dict)]
    proposal, why = propose_contour(_gray(image_bytes), spec, read_holes=len(holes))
    if proposal is None:
        report["contour_proposal"] = {"profile": None, "reason": why}
        return
    if _same_plate(profile, proposal.profile):
        return
    report["contour_proposal"] = {
        "profile": proposal.profile,
        "side_error_mm": round(proposal.side_error_mm, 3),
        "bbox_px": [round(float(v), 1) for v in proposal.bbox_px],
    }


def _same_plate(read: dict[str, Any], sheet: dict[str, Any]) -> bool:
    """Прочитанный прямоугольник совпадает с контуром по листу — и отверстия тоже."""
    if read.get("shape") != "rectangle":
        return False
    width, height = read.get("width_mm"), read.get("height_mm")
    if not (_is_number(width) and _is_number(height)):
        return False
    rectangle = [
        {"kind": "line", "to": (float(width), 0.0)},
        {"kind": "line", "to": (float(width), float(height))},
        {"kind": "line", "to": (0.0, float(height))},
        {"kind": "line", "to": (0.0, 0.0)},
    ]
    if [(s["kind"], tuple(s["to"])) for s in sheet.get("sketch") or []] != [
        (s["kind"], s["to"]) for s in rectangle
    ]:
        return False
    read_holes = sorted(
        (
            round(float(h.get("diameter_mm") or 0), 2),
            round(float(h.get("center_x_mm") or 0) + float(width) / 2.0, 1),
            round(float(h.get("center_y_mm") or 0) + float(height) / 2.0, 1),
        )
        for h in read.get("holes") or []
    )
    sheet_holes = sorted(
        (round(h["diameter_mm"], 2), round(h["center_x_mm"], 1), round(h["center_y_mm"], 1))
        for h in sheet.get("holes") or []
    )
    return read_holes == sheet_holes


def _gray(image_bytes: bytes) -> Any:
    import numpy as np
    from PIL import Image

    return np.asarray(Image.open(io.BytesIO(image_bytes)).convert("L"))


def _housing_by_sheet(
    image_bytes: bytes, spec: dict[str, Any], profile: dict[str, Any], report: dict[str, Any]
) -> None:
    """Корпус по листу: три вида по геометрии, размеры — по надписям (Ф5).

    Когда габариты прочитаны неверно, план по ним не найти, и проверка честно
    молчит: живой корпус прочитан «пластиной» 50 × 80 × 16 при 80 × 80 × 50.
    Предложение по листу решает согласование, как у профиля вала и контура
    пластины.
    """
    from app.ai.cad_recognize.verifiers.housing_views import discover_housing_views
    from app.ai.cad_recognize.verifiers.reconcile import sheet_numbers

    read_size = (profile.get("width_mm"), profile.get("height_mm"))
    found = discover_housing_views(
        _gray(image_bytes),
        sheet_numbers(spec),
        read_size if all(_is_number(v) for v in read_size) else None,
    )
    if not found or not found.get("thickness_mm"):
        return
    report["housing_by_sheet"] = found
    cavity = found.get("cavity")
    walls = [item for item in (profile.get("wall_features") or []) if isinstance(item, dict)]
    has_cavity = any(
        item.get("on_plane") in ("top", "bottom") and item.get("kind") == "pocket" for item in walls
    )
    if cavity and cavity.get("height_mm") and not has_cavity:
        # Полость ридер не выписывает вовсе (живой корпус: ни одного элемента
        # грани), а лист её несёт: в разрезе — ширина и глубина, в плане —
        # штриховые кромки. Решает согласование.
        report["cavity_proposal"] = {
            "kind": "pocket",
            "on_plane": "top",
            "profile": "rectangle",
            "width_mm": cavity["width_mm"],
            "height_mm": cavity["height_mm"],
            "depth_mm": cavity["depth_mm"],
            "center_u_mm": cavity["center_u_mm"],
            "center_v_mm": cavity["center_v_mm"],
            "bbox_px": cavity.get("bbox_px"),
            "reason": (
                f"полость по листу {cavity['width_mm']:g} × {cavity['height_mm']:g} "
                f"глубиной {cavity['depth_mm']:g} — ридер её не выписал"
            ),
        }
    read = {
        "width_mm": profile.get("width_mm"),
        "height_mm": profile.get("height_mm"),
        "thickness_mm": profile.get("thickness_mm"),
    }
    differs = [
        key
        for key, value in read.items()
        if not _is_number(value)
        or abs(float(value) - float(found[key])) > max(0.5, 0.01 * float(found[key]))
    ]
    if not differs:
        return
    report["housing_proposal"] = {
        "width_mm": found["width_mm"],
        "height_mm": found["height_mm"],
        "thickness_mm": found["thickness_mm"],
        "read": read,
        "differs": differs,
        "reason": (
            "по листу габарит "
            f"{found['width_mm']:g} × {found['height_mm']:g} × {found['thickness_mm']:g}; "
            "прочитано "
            + " × ".join(
                f"{float(read[key]):g}" if _is_number(read[key]) else "?"
                for key in ("width_mm", "height_mm", "thickness_mm")
            )
        ),
    }


def _housing_thickness(image_bytes: bytes, profile: dict[str, Any], report: dict[str, Any]) -> None:
    """Толщина по видам листа (`housing_views`), а не по надписи (Ф5).

    Базовая линия на корпусах: толщину ридер читает неверно на КАЖДОМ листе
    (20 вместо 40, 16 вместо 60) — на листе три вида, и число приписывается
    не тому. Толщина — это высота вида спереди и ширина вида слева; когда оба
    вида согласны, замер надёжнее надписи.
    """
    from app.ai.cad_recognize.verifiers.housing_views import locate_housing_views

    width, height = profile.get("width_mm"), profile.get("height_mm")
    read = profile.get("thickness_mm")
    if not _is_number(width) or not _is_number(height) or not _is_number(read):
        return
    views = locate_housing_views(_gray(image_bytes), float(width), float(height))
    if views is None:
        return
    report["housing_views"] = views.as_dict()
    item = {
        "kind": "plate_thickness",
        "path": "main_view.profile",
        "feature_id": "profile:thickness",
        "read": {"thickness_mm": float(read)},
        "measured": {},
        "tolerance_mm": {"thickness": _THICKNESS_TOLERANCE_MM},
    }
    if views.thickness_mm is None:
        report["items"].append({**item, "status": "unmeasurable", "reason": views.reason})
        return
    measured = round(float(views.thickness_mm), 3)
    item["measured"] = {"thickness_mm": measured}
    if abs(measured - float(read)) <= _THICKNESS_TOLERANCE_MM:
        report["items"].append({**item, "status": "confirmed", "reason": views.reason})
        return
    report["items"].append(
        {
            **item,
            "status": "refuted",
            "reason": f"{views.reason}; прочитано {float(read):g}",
        }
    )


def _points_at_another(
    item: dict[str, Any], measured: dict[str, Any] | None, walls: list[dict[str, Any]]
) -> bool:
    """Замер ближе к другому элементу той же грани, чем к проверяемому."""
    if not measured:
        return False
    found = (
        float(measured.get("center_u_mm") or 0.0),
        float(measured.get("center_v_mm") or 0.0),
    )

    def distance(entry: dict[str, Any]) -> float:
        return (float(entry.get("center_u_mm") or 0.0) - found[0]) ** 2 + (
            float(entry.get("center_v_mm") or 0.0) - found[1]
        ) ** 2

    own = distance(item)
    return any(
        other is not item
        and str(other.get("on_plane")) == str(item.get("on_plane"))
        and distance(other) < own
        for other in walls
    )


def _wall_features_on_sheet(
    image_bytes: bytes, profile: dict[str, Any], report: dict[str, Any]
) -> None:
    """Карманы и приливы граней — замером на своём виде (`wall_feature`, Ф5).

    Ридер берёт размер с листа верно, а положение и глубину приписывает не
    тому виду (полость корпуса: центр (−42, −42) вместо (0, 0)). Вид элемента
    даёт проекционная связь (`housing_views`), замер — сам лист.
    """
    from app.ai.cad_recognize.verifiers.wall_feature import (
        _faces,
        measure_wall_feature,
        wall_feature_verdict,
    )

    walls = [item for item in (profile.get("wall_features") or []) if isinstance(item, dict)]
    views = report.get("housing_views") or {}
    if not walls or not views.get("plan_bbox_px"):
        return
    width, height = profile.get("width_mm"), profile.get("height_mm")
    thickness = views.get("thickness_mm") or profile.get("thickness_mm")
    if not _is_number(width) or not _is_number(height) or not _is_number(thickness):
        return
    gray = _gray(image_bytes)
    frame = report.get("frame") or {}
    mm_per_px = float(frame.get("mm_per_px") or 0.0)
    if mm_per_px <= 0:
        plan = views["plan_bbox_px"]
        mm_per_px = float(width) / max(plan[2] - plan[0], 1e-6)
    from app.ai.cad_recognize.sheet_upscale import main_line_px

    line_mm = max(0.0, float(main_line_px(gray))) * mm_per_px
    faces = _faces(float(width), float(height), float(thickness))
    boxes = {
        "top": views.get("plan_bbox_px"),
        "bottom": views.get("plan_bbox_px"),
        "front": views.get("front_bbox_px"),
        "back": views.get("front_bbox_px"),
        "left": views.get("side_bbox_px"),
        "right": views.get("side_bbox_px"),
    }
    for index, item in enumerate(walls):
        plane = str(item.get("on_plane") or "")
        box = boxes.get(plane)
        face = faces.get(plane)
        entry = {
            "kind": "wall_feature",
            "path": f"main_view.profile.wall_features[{index}]",
            "feature_id": str(item.get("id") or f"profile:wall:{index}"),
            "read": {
                key: item[key]
                for key in ("diameter_mm", "width_mm", "height_mm", "center_u_mm", "center_v_mm")
                if _is_number(item.get(key))
            },
            "tolerance_mm": {"size": 0.5, "position": 1.0},
        }
        if box is None or face is None:
            report["items"].append(
                {
                    **entry,
                    "status": "unmeasurable",
                    "measured": {},
                    "reason": f"вида грани «{plane}» на листе не найдено",
                }
            )
            continue
        measured = measure_wall_feature(gray, tuple(box), mm_per_px, face, item)
        # Глубина элемента пока не меряется: на виде с ребра нужно отличить
        # дно кармана от любой другой линии внутри тела, а поиск ближайшей
        # линии к кромке давал мусор (0,008 мм при 58). Проверяются размер и
        # положение; глубина остаётся прочитанной.
        verdict = wall_feature_verdict(entry["read"], measured, line_mm)
        if verdict["status"] == "refuted" and _points_at_another(item, measured, walls):
            # Замер указывает на СОСЕДНИЙ элемент той же грани: расхождение,
            # указывающее на другой объект, не опровергает чтение (E28).
            verdict = {
                **verdict,
                "status": "unmeasurable",
                "reason": "замер указывает на соседний элемент той же грани",
            }
        report["items"].append({**entry, **verdict})


def _plate_holes(image_bytes: bytes, profile: dict[str, Any], report: dict[str, Any]) -> str | None:
    from app.ai.cad_recognize.verifiers.contract import Verdict
    from app.ai.cad_recognize.verifiers.plate_frame import locate_plate_frame
    from app.ai.cad_recognize.verifiers.plate_hole import frame_supported, plate_hole_tolerances

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
    verdicts = [
        verify(
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
        for index, hole in holes
    ]
    read_x = [float(hole["center_x_mm"]) + half_w for _index, hole in holes]
    if not frame_supported(list(zip(verdicts, read_x, strict=True)), position_tol):
        # Не та система координат плана: опровергать чтение ею нельзя.
        doubt = (
            "система координат плана, вероятно, неверна: у большинства прочитанных x окружности нет"
        )
        verdicts = [
            Verdict(status="unmeasurable", reason=doubt) if v.status != "unmeasurable" else v
            for v in verdicts
        ]
        report["frame"] = None
    for (index, hole), verdict in zip(holes, verdicts, strict=True):
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
    # Диаметры листа — чтобы главным видом стал вал, а не рамка или штамп
    # (живой part_01): прочитанные Ø ступеней и надписи Ø и резьб.
    from app.ai.cad_recognize.verifiers.sheet_profile import sheet_labels

    labels = sheet_labels(spec)
    diameters = [float(step["diameter_mm"]) for _, step in steps]
    diameters += [*labels.diameters, *(nominal for nominal, _pitch in labels.threads)]
    views = locate_shaft_views(gray, total, diameters)
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
    _unclaimed_keyways(gray, [view_frame for view_frame, _profile in views], body, spans, report)
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
        elif measured and "diameter_mm" not in measured:
            # Ø под пазом по виду не мерится: длина совпала, но «подтверждено»
            # спрятало бы неверный Ø (корпус v9: Ø +1,5 под пазом — «подтверждён»).
            status = "unmeasurable"
            reason = "Ø ступени по виду не измерен (паз); длина совпала с листом"
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
    _sections(gray, frame, body, spec, report)
    if _sheet_profile(profile, total, body, spec, report):
        return None
    return verdict.reason if whole_unmeasurable else None


_SECTION_KINDS = {"section", "cut", "разрез", "сечение"}


def _sections(
    gray: Any, frame: Any, body: dict[str, Any], spec: dict[str, Any], report: dict[str, Any]
) -> None:
    """Сечения вала на листе: сплошные ли они (`section_disk`).

    Только когда на листе есть сечения, а полость не прочитана: гейт сборки
    держит тогда «разрез не прочитан». Сплошными сечения признаются, если
    найдено не меньше кругов, чем видов-сечений, все — сплошные, колец нет.
    """
    from app.ai.cad_recognize.verifiers.section_disk import section_disks

    expected = sum(
        1
        for view in spec.get("views") or []
        if isinstance(view, dict) and str(view.get("kind") or "").lower() in _SECTION_KINDS
    )
    if not expected or body.get("bore") or frame is None:
        return
    diameters = [
        float(s["diameter_mm"])
        for s in body.get("outer") or []
        if isinstance(s, dict) and _is_number(s.get("diameter_mm"))
    ]
    disks = section_disks(gray, frame.bbox_px, frame.mm_per_px, diameters)
    solid = sum(1 for disk in disks if disk["solid"])
    rings = sum(1 for disk in disks if disk["ring"])
    report["sections"] = {
        "expected": expected,
        "disks": disks,
        "solid": bool(solid >= expected and not rings),
        "reason": (f"сечений на листе {expected}, найдено сплошных кругов {solid}, колец {rings}"),
    }


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
        _record_verdict(item, verdict)
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
    # Возврата не было с 551e257f: пролёты собирались и терялись, и проверка
    # профиля не знала о пазах — «Ø под пазом не мерится» не работало.
    return spans


# Капсула паза не касается уступов своей ступени: контур самой короткой
# ступени между уступами — тоже «капсула» (корпус v9: 24 ложные находки).
_SHOULDER_CLEAR_MM = 1.5


def _unclaimed_keyways(
    gray: Any,
    frames: list[Any],
    body: dict[str, Any],
    spans: list[dict[str, float]],
    report: dict[str, Any],
) -> None:
    """Пазы на листе, которых ридер не выписал (живой z4-r4: второй паз на Ø22).

    Та же проверка капсулы, но гипотеза — «паз где-то на этой ступени шириной
    по ГОСТ 23360 для её Ø». Строго: капсула не касается уступов ступени,
    ширина — в пределах допуска таблицы. Корпус v9, чистые листы: 29 из 31
    паза найдено, ложных 0. Находка — только предложение
    (``keyway_proposals``): принимает его согласование, и только по надписям.
    """
    from app.ai.cad_recognize.keyway_standard import _SECTION_TOLERANCE, standard_section

    outer = body.get("outer") or []
    if not outer or not all(
        isinstance(step, dict)
        and _is_number(step.get("length_mm"))
        and _is_number(step.get("diameter_mm"))
        for step in outer
    ):
        return
    proposals = []
    station = 0.0
    for index, step in enumerate(outer):
        low, high = station, station + float(step["length_mm"])
        station = high
        if any(
            span["axial_start_mm"] < high and span["axial_start_mm"] + span["length_mm"] > low
            for span in spans or []
            if span
        ):
            continue
        standard = standard_section(float(step["diameter_mm"]))
        if standard is None:
            continue
        hypothesis = Hypothesis(
            "keyway",
            f"main_view.outer[{index}]",
            {"axial_start_mm": low, "length_mm": high - low, "width_mm": standard[0]},
        )
        _frame, verdict = _first_measured(hypothesis, frames, gray)
        got = verdict.measured
        if verdict.status not in ("confirmed", "refuted") or not got:
            continue
        if (
            got["axial_start_mm"] < low + _SHOULDER_CLEAR_MM
            or got["axial_start_mm"] + got["length_mm"] > high - _SHOULDER_CLEAR_MM
            or abs(got["width_mm"] - standard[0]) > standard[0] * _SECTION_TOLERANCE
        ):
            continue
        proposals.append(
            {
                "step_index": index,
                "axial_start_mm": float(got["axial_start_mm"]),
                "length_mm": float(got["length_mm"]),
                "width_mm": float(got["width_mm"]),
                "standard_mm": [standard[0], standard[1]],
                "evidence_bbox_px": (
                    [round(float(v), 1) for v in verdict.evidence_bbox_px]
                    if verdict.evidence_bbox_px
                    else None
                ),
            }
        )
    if proposals:
        report["keyway_proposals"] = proposals


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
        _record_verdict(item, verdict)
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
        _record_verdict(item, verdict)
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


def _record_verdict(item: dict[str, Any], verdict: Any) -> None:
    """Вердикт — в элемент отчёта; с рамкой находки, если элемент на листе найден."""
    item.update(status=verdict.status, measured=dict(verdict.measured), reason=verdict.reason)
    if verdict.measured and verdict.evidence_bbox_px:
        item["evidence_bbox_px"] = [round(float(v), 1) for v in verdict.evidence_bbox_px]


def attach_sheet_evidence(spec: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """Элементы, найденные проверкой на листе, получают свидетельство — место находки.

    Гейт сборки не пускает геометрию без локализованного свидетельства (живой
    z4-r4: паз и фаски — «без evidence»). Проверка нашла их на листе и
    измерила — рамка находки и есть такое свидетельство. Прочитанное
    свидетельство не заменяется; не найденное — не получает ничего.
    """
    import copy

    from app.ai.cad_recognize.verifiers.reconcile import _resolve

    spec = copy.deepcopy(spec)
    for item in report.get("items") or []:
        box = item.get("evidence_bbox_px")
        if not box or not item.get("measured"):
            continue
        node = _resolve(spec, str(item.get("path") or ""))
        if node is None or node.get("evidence"):
            continue
        node["evidence"] = [
            {
                "image_index": 0,
                "bbox": list(box),
                "raw_text": f"найдено проверкой по листу ({item['kind']})",
            }
        ]
    return spec


def attach_verified_views(spec: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """Элементы, подтверждённые листом, — в ``features_shown`` вида, где их мерили.

    Рёбра графа `represented_by` строятся только из ``features_shown`` вида,
    а ридер его не заполняет: у каждой механической сборки все элементы были
    «без вида» (`mechanical_feature_without_view`). Проверка знает, на каком
    виде элемент измерен и подтверждён, — это и есть свидетельство. Вид —
    главный вид тела 0 из спека; если ридер видов не выписал, добавляется
    вид с рамкой, найденной проверкой. Не подтверждённое — не добавляется.
    """
    import copy

    # Показан на виде — найден на нём: подтверждённый или измеренный (фаска
    # «не измерима» по размеру — ГОСТ 2.305 рисует её условно, — но на виде
    # найдена: живой z4-r4). Не найденное проверкой — не добавляется.
    shown = [
        str(item["feature_id"])
        for item in report.get("items") or []
        if item.get("feature_id") and (item.get("status") == "confirmed" or item.get("measured"))
    ]
    main = spec.get("main_view") or {}
    # Пазы, найденные по листу (не выписанные ридером), — найдены на виде.
    shown.extend(
        str(key["id"])
        for key in main.get("keyways") or []
        if isinstance(key, dict) and str(key.get("id") or "").startswith("sheet:")
    )
    if report.get("sleeve_confirmed"):
        for group in ("bore", "flanges"):
            shown.extend(
                str(entry["id"])
                for entry in main.get(group) or []
                if isinstance(entry, dict) and entry.get("id")
            )
    if report.get("housing_views"):
        # Элемент грани стоит на СВОИХ видах (`_attach_housing_views`), а не на
        # общем «проверенном виде»: иначе он оказывается сразу на трёх видах.
        walls = {
            str(item.get("id"))
            for item in ((main.get("profile") or {}).get("wall_features") or [])
            if isinstance(item, dict) and item.get("id")
        }
        shown = [item for item in shown if item not in walls]
    if not shown:
        spec = copy.deepcopy(spec)
        _attach_housing_views(spec, report, spec.setdefault("views", []))
        return spec
    spec = copy.deepcopy(spec)
    views = spec.setdefault("views", [])
    # Главный вид тела 0: `front`; иначе — единственный вид спека (втулка:
    # разрез А-А). Несколько разрезов и сечений без `front` — не угадывать:
    # у z4-r4 первым шло сечение Б-Б через паз, и профиль лёг на него.
    body_views = [
        view for view in views if isinstance(view, dict) and int(view.get("body_index") or 0) == 0
    ]
    primary = next((view for view in body_views if view.get("kind") == "front"), None)
    if (
        primary is None
        and len(body_views) == 1
        and body_views[0].get("kind")
        in {
            "front",
            "section",
        }
    ):
        primary = body_views[0]
    if primary is None:
        frame = report.get("frame") or {}
        if not frame.get("bbox_px"):
            return spec
        primary = {
            "kind": "front",
            "view_id": "sheet-verified",
            "label": "вид, проверенный по листу",
            "relation": "primary",
            "body_index": 0,
            "features_shown": [],
            "evidence": [
                {
                    "image_index": 0,
                    "bbox": list(frame["bbox_px"]),
                    "raw_text": "вид найден проверкой по листу",
                }
            ],
        }
        views.append(primary)
    primary["features_shown"] = list(
        dict.fromkeys([*(primary.get("features_shown") or []), *shown])
    )
    # Вид с торца втулки: там измерены контур фланца и его отверстия — фланец
    # показан и на нём (разрез дал станцию и толщину).
    end = report.get("sleeve_end_view") or {}
    flanges = [
        str(entry["id"])
        for entry in main.get("flanges") or []
        if isinstance(entry, dict) and entry.get("id")
    ]
    if report.get("sleeve_confirmed") and end.get("bbox_px") and flanges:
        views.append(
            {
                "kind": "side",
                "view_id": "sheet-verified-end",
                "label": "вид с торца, проверенный по листу",
                "relation": "orthographic",
                "body_index": 0,
                "features_shown": flanges,
                "evidence": [
                    {
                        "image_index": 0,
                        "bbox": list(end["bbox_px"]),
                        "raw_text": "контур фланца найден проверкой по листу",
                    }
                ],
            }
        )
    _attach_housing_views(spec, report, views)
    return spec


def _attach_housing_views(
    spec: dict[str, Any], report: dict[str, Any], views: list[dict[str, Any]]
) -> None:
    """Виды корпуса и элементы граней на них (Ф5).

    Виды найдены проекционной связью (`housing_views`), элементы — замерены на
    них. Элемент показан на ДВУХ видах: на своём (размер и положение) и на
    соседнем с ребра (глубина и вылет) — это и есть межвидовое соответствие
    корпуса.
    """
    housing = report.get("housing_views") or {}
    if not housing.get("plan_bbox_px"):
        return
    profile = ((spec.get("main_view") or {}).get("profile")) or {}
    walls = [item for item in (profile.get("wall_features") or []) if isinstance(item, dict)]
    confirmed = {
        str(item.get("feature_id"))
        for item in report.get("items") or []
        if item.get("kind") == "wall_feature" and item.get("status") == "confirmed"
    }
    if not walls or not confirmed:
        return
    boxes = {
        "plan": ("sheet-verified-plan", "план, проверенный по листу", housing.get("plan_bbox_px")),
        "front": (
            "sheet-verified-front",
            "вид спереди, проверенный по листу",
            housing.get("front_bbox_px"),
        ),
        "side": (
            "sheet-verified-side",
            "вид слева, проверенный по листу",
            housing.get("side_bbox_px"),
        ),
    }
    # Грань → вид лицом и вид с ребра.
    faces = {
        "top": ("plan", "front"),
        "bottom": ("plan", "front"),
        "front": ("front", "plan"),
        "back": ("front", "plan"),
        "left": ("side", "plan"),
        "right": ("side", "plan"),
    }
    shown: dict[str, list[str]] = {}
    for index, item in enumerate(walls):
        feature_id = str(item.get("id") or f"profile:wall:{index}")
        if feature_id not in confirmed:
            continue
        for key in faces.get(str(item.get("on_plane")), ()):  # лицом и с ребра
            if boxes.get(key, (None, None, None))[2]:
                shown.setdefault(key, []).append(feature_id)
    by_id = {str(view.get("view_id")): view for view in views if isinstance(view, dict)}
    for key, ids in shown.items():
        view_id, label, bbox = boxes[key]
        view = by_id.get(view_id)
        if view is None:
            view = {
                "kind": "front" if key == "plan" else "side",
                "view_id": view_id,
                "label": label,
                "relation": "primary" if key == "plan" else "orthographic",
                "body_index": 0,
                "features_shown": [],
                "evidence": [
                    {
                        "image_index": 0,
                        "bbox": [float(value) for value in bbox],
                        "raw_text": "вид найден проекционной связью по листу",
                    }
                ],
            }
            views.append(view)
        view["features_shown"] = list(dict.fromkeys([*(view.get("features_shown") or []), *ids]))


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
        # Не `traced`: каждое такое утверждение граф помечал ошибкой уровня 8
        # «trace_verification_incomplete» (нет visual_verification) — замер
        # вида по листу — наблюдение, см. `graph.verdict_patch`.
        origin="observed",
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
        # Рамка вида на листе: где искать штриховку полости и прочее «в виде».
        "bbox_px": [round(float(v), 1) for v in frame.bbox_px],
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
