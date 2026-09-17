#!/usr/bin/env python3
"""Харнесс проверяльщиков на корпусе с эталоном (план, задача M1).

Детерминированный режим: проверяльщику подаются ИСТИННЫЕ гипотезы из эталона —
без модели. Так мерится сам проверяльщик, отдельно от того, насколько точно
модель даёт рамки (это эксперимент E0, живой режим). По ступеням лестницы
деградации выходит кривая — это эксперимент E2: порог измеримости.

    python scripts/eval_verify.py --corpus ../cad-dataset-out/verify-corpus-v1-ladder \\
        --verifier dimension_line --split dev

Проверяльщики:
* ``dimension_line`` — длина горизонтальной размерной линии по чернилам рядом с
  подписью (`axial_dimensions._span_from_ink`). Истина — расстояние между
  точками привязки размера по горизонтали.
"""

from __future__ import annotations

import argparse
import io
import json
import pathlib
import statistics
import sys
from collections import defaultdict
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def product_normalize(png: bytes, truth: dict) -> tuple[bytes, dict, bool]:
    """Подготовить лист так же, как стадия 0.9 продукта (`cad_trace._dewarp_photo`).

    Проверяльщик в продукте видит фото уже выпрямленным — харнесс обязан мерить
    то же самое, иначе он оценивает не продукт. Эталон переносится той же
    гомографией. Для скана выпрямление не срабатывает и лист не меняется.
    """
    import copy

    import numpy as np
    from PIL import Image

    from app.ai.drawing_cleanup import dewarp_sheet_with_transform
    from app.ai.verify_corpus.degrade import _apply_homography, _encode, _map_points

    arr = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
    result = dewarp_sheet_with_transform(arr)
    if result is None:
        return png, truth, False
    warped, homography = result
    moved = copy.deepcopy(truth)
    _map_points(moved, lambda points: _apply_homography(np.asarray(homography), points))
    moved["image_size_px"] = [int(warped.shape[1]), int(warped.shape[0])]
    return _encode(warped), moved, True


def _horizontal_dimensions(truth: dict) -> list[dict[str, Any]]:
    """Линейные размеры с горизонтальной размерной линией и подписью."""
    result = []
    for item in truth.get("labels") or []:
        if item.get("kind") != "dimension" or item.get("dimension_kind") != "linear":
            continue
        label = item.get("label") or {}
        anchors = item.get("anchors_px") or []
        if not label.get("bbox_px") or len(anchors) != 2:
            continue
        (x1, y1), (x2, y2) = anchors
        # Вертикальные размеры (высота пластины и т. п.) сюда не идут: этот
        # проверяльщик сканирует строки и вертикальную линию не ищет по
        # устройству. Первая версия харнесса подавала их ему и засчитывала
        # провалом — 54 «ненайденных» из 216 были ошибкой харнесса.
        if abs(x2 - x1) < abs(y2 - y1):
            continue
        # Размерная линия длины горизонтальна по построению листа, даже если
        # точки привязки на разной высоте; на фото она наклонена перспективой.
        result.append({"label_bbox": label["bbox_px"], "span_px": abs(x2 - x1), "dy": abs(y2 - y1)})
    return result


def _vertical_dimensions(truth: dict) -> list[dict[str, Any]]:
    """Линейные размеры с вертикальной размерной линией и подписью."""
    result = []
    for item in truth.get("labels") or []:
        if item.get("kind") != "dimension" or item.get("dimension_kind") != "linear":
            continue
        label = item.get("label") or {}
        anchors = item.get("anchors_px") or []
        if not label.get("bbox_px") or len(anchors) != 2:
            continue
        (x1, y1), (x2, y2) = anchors
        if abs(x2 - x1) >= abs(y2 - y1):
            continue
        result.append({"label_bbox": label["bbox_px"], "span_px": abs(y2 - y1)})
    return result


def eval_dimension_line(png: bytes, truth: dict) -> list[dict[str, Any]]:
    from PIL import Image

    from app.ai.cad_recognize.axial_dimensions import (
        _ink_rows,
        _span_from_ink,
        _vertical_span_from_ink,
    )

    image = Image.open(io.BytesIO(png)).convert("RGB")
    ink = _ink_rows(image)
    outcomes = []
    for dim in _horizontal_dimensions(truth):
        x0, y0, x1, y1 = dim["label_bbox"]
        unit = max(4.0, y1 - y0)
        line = _span_from_ink(ink, [x0, y0, x1, y1], unit)
        expected = float(dim["span_px"])
        if line is None:
            outcomes.append({"found": False, "expected_px": expected, "unit_px": unit})
            continue
        measured = float(line[2] - line[0])
        error = abs(measured - expected)
        tolerance = max(2.0, 0.02 * expected)
        outcomes.append(
            {
                "found": True,
                "correct": error <= tolerance,
                "expected_px": expected,
                "measured_px": measured,
                "error_px": error,
                "error_rel": error / expected if expected else None,
                "unit_px": unit,
            }
        )
    # Вертикальные — отдельным случаем: счётчики горизонтальных в базе гейта
    # не сдвигаются.
    for dim in _vertical_dimensions(truth):
        x0, y0, x1, y1 = dim["label_bbox"]
        unit = max(4.0, x1 - x0)
        line = _vertical_span_from_ink(ink, [x0, y0, x1, y1], unit)
        expected = float(dim["span_px"])
        if line is None:
            outcomes.append({"case": "vertical", "found": False, "expected_px": expected})
            continue
        error = abs(float(line[3] - line[1]) - expected)
        outcomes.append(
            {
                "case": "vertical",
                "found": True,
                "correct": error <= max(2.0, 0.02 * expected),
                "expected_px": expected,
                "error_px": error,
            }
        )
    return outcomes


def _plate_frame(truth: dict):
    """Система координат плана пластины — по эталонным размерам ширины и высоты.

    Точки размеров в эталоне лежат на размерных линиях: у ширины это левая и
    правая кромки плана по x, у высоты — нижняя и верхняя по y.
    """
    from app.ai.cad_recognize.verifiers import ViewFrame

    profile = (truth["spec"].get("main_view") or {}).get("profile") or {}
    if profile.get("shape") != "rectangle":
        return None
    width, height = float(profile["width_mm"]), float(profile["height_mm"])
    width_dim = height_dim = None
    for item in truth.get("labels") or []:
        if item.get("kind") != "dimension" or item.get("dimension_kind") != "linear":
            continue
        anchors = item.get("anchors_px") or []
        if len(anchors) != 2 or item.get("value_mm") is None:
            continue
        (x1, y1), (x2, y2) = anchors
        horizontal = abs(x2 - x1) >= abs(y2 - y1)
        if horizontal and width_dim is None and abs(item["value_mm"] - width) <= 0.05:
            width_dim = (min(x1, x2), max(x1, x2))
        if not horizontal and height_dim is None and abs(item["value_mm"] - height) <= 0.05:
            height_dim = (min(y1, y2), max(y1, y2))
    if width_dim is None or height_dim is None:
        return None
    # Масштаб по каждой оси своим размером: выпрямленное фото не изотропно,
    # и масштаб одной ширины уводил верх плана на 16 px (plate-3@photo).
    return ViewFrame(
        bbox_px=(width_dim[0] - 5, height_dim[0] - 5, width_dim[1] + 5, height_dim[1] + 5),
        mm_per_px=width / (width_dim[1] - width_dim[0]),
        origin_px=(width_dim[0], height_dim[1]),
        mm_per_px_v=height / (height_dim[1] - height_dim[0]),
    )


def eval_plate_hole(png: bytes, truth: dict, frame_source: str = "truth") -> list[dict[str, Any]]:
    """Отверстия пластины: гипотезы из эталона и искажённые, как ошибается ридер.

    ``truth`` — должно подтвердиться; ``swap_y`` — y от соседнего отверстия
    (plate-1: все x верны, y переставлены парами) и ``diameter`` — Ø на 1,1 мм
    больше: оба должны опровергнуться, а измеренное — совпасть с эталоном.
    """
    import numpy as np
    from PIL import Image

    from app.ai.cad_recognize.verifiers import Hypothesis, verify
    from app.ai.cad_recognize.verifiers.plate_hole import plate_hole_tolerances
    from app.ai.verify_corpus.score import expand_holes

    reference = _plate_frame(truth)
    if reference is None:
        return []
    profile = truth["spec"]["main_view"]["profile"]
    width, height = float(profile["width_mm"]), float(profile["height_mm"])
    holes = [(x + width / 2.0, y + height / 2.0, d) for x, y, d in expand_holes(profile)]
    gray = np.asarray(Image.open(io.BytesIO(png)).convert("L"))
    frame, frame_error = reference, None
    if frame_source == "sheet":
        # Как в продукте: ширину и высоту дал ридер (на пластинах — 100 %),
        # план ищется по листу. Эталонная система — только для оценки.
        from app.ai.cad_recognize.verifiers.plate_frame import locate_plate_frame

        frame = locate_plate_frame(gray, width, height)
        if frame is not None:
            frame_error = {
                "scale_rel": abs(frame.mm_per_px / reference.mm_per_px - 1.0),
                "origin_px": max(
                    abs(frame.origin_px[0] - reference.origin_px[0]),
                    abs(frame.origin_px[1] - reference.origin_px[1]),
                ),
            }
    cases: list[tuple[str, tuple[float, float, float], tuple[float, float, float]]] = []
    for index, hole in enumerate(holes):
        cases.append(("truth", hole, hole))
        # Перестановка y — как у plate-1: y соседнего отверстия из ДРУГОГО
        # столбца, но такой, на котором в столбце этого отверстия отверстия нет.
        # Иначе (массив 2×2) гипотеза описывала существующее отверстие и
        # законно подтверждалась — первый прогон v7 считал это промахом.
        column = [h for h in holes if abs(h[0] - hole[0]) <= 1.0]
        other = next(
            (
                h
                for h in holes[index + 1 :] + holes[:index]
                if abs(h[0] - hole[0]) > h[2] + hole[2]
                and all(abs(h[1] - c[1]) > 2.0 for c in column)
            ),
            None,
        )
        if other is not None:
            # Верный замер — отверстие столбца, ближайшее к заявленному y: так
            # проверяльщик и устроен (x выбирает столбец, y — ближайшее в нём).
            nearest = min(column, key=lambda c: abs(c[1] - other[1]))
            cases.append(("swap_y", (hole[0], other[1], hole[2]), nearest))
        cases.append(("diameter", (hole[0], hole[1], hole[2] + 1.1), hole))
    from app.ai.cad_recognize.verifiers.contract import Verdict
    from app.ai.cad_recognize.verifiers.plate_hole import frame_supported

    verdicts = [
        verify(
            Hypothesis("plate_hole", "holes", {"x_mm": x, "y_mm": y, "diameter_mm": d}),
            frame,
            gray,
        )
        for _case, (x, y, d), _real in cases
    ]
    # Как в продукте (`stage._plate_holes`): система координат, не подтверждённая
    # окружностями на прочитанных x, опровергать не может. Прочитанные x — у
    # случая «верное чтение».
    if (
        frame_source == "sheet"
        and frame is not None
        and not frame_supported(
            [(v, h[0]) for (case, h, _r), v in zip(cases, verdicts) if case == "truth"],
            plate_hole_tolerances(frame.scale_mean)[0],
        )
    ):
        verdicts = [
            v if v.status == "unmeasurable" else Verdict(status="unmeasurable", reason="frame")
            for v in verdicts
        ]
    outcomes = []
    for (case, (x, y, d), real), verdict in zip(cases, verdicts, strict=True):
        measured = verdict.measured
        position_tol, diameter_tol = plate_hole_tolerances(reference.scale_mean)
        accurate = bool(measured) and (
            abs(measured["y_mm"] - real[1]) <= position_tol
            and abs(measured["diameter_mm"] - real[2]) <= diameter_tol
        )

        wanted = "confirmed" if case == "truth" else "refuted"
        outcomes.append(
            {
                "case": case,
                "found": verdict.status != "unmeasurable",
                "correct": verdict.status == wanted and accurate,
                "error_rel": (
                    abs(measured["diameter_mm"] - real[2]) / real[2] if measured else None
                ),
                "unit_px": real[2] / 2.0 / reference.scale_mean,
                "frame_found": frame is not None,
                "frame_error": frame_error,
                "status": verdict.status,
                "reason": verdict.reason,
                "measured": dict(measured),
                "real": {"x_mm": real[0], "y_mm": real[1], "diameter_mm": real[2]},
            }
        )
    return outcomes


def eval_bolt_circle(png: bytes, truth: dict) -> list[dict[str, Any]]:
    """Окружность болтов фланца: система координат — по контуру на листе.

    Случаи: ``truth`` — должно подтвердиться; ``count`` (+2 отверстия),
    ``pcd`` (+6 мм), ``diameter`` (Ø +1,1 мм) и ``phase_zero`` — фаза 0°, когда
    настоящая не кратна шагу (так ошибается ридер: углового размера на листе
    нет) — должны опровергнуться, а измеренное — совпасть с эталоном.
    Точность системы координат — против середины точек размера наружного Ø.
    """
    import math

    import numpy as np
    from PIL import Image

    from app.ai.cad_recognize.verifiers import Hypothesis, verify
    from app.ai.cad_recognize.verifiers.bolt_circle import _angle_gap
    from app.ai.cad_recognize.verifiers.circle_frame import locate_circle_frame
    from app.ai.cad_recognize.verifiers.plate_hole import plate_hole_tolerances

    profile = (truth["spec"].get("main_view") or {}).get("profile") or {}
    patterns = [
        item
        for item in profile.get("hole_patterns") or []
        if (item.get("kind") or "bolt_circle") == "bolt_circle" and item.get("count")
    ]
    if profile.get("shape") != "circle" or not patterns or not profile.get("diameter_mm"):
        return []
    outer_mm = float(profile["diameter_mm"])
    outer = next(
        (
            item
            for item in truth.get("labels") or []
            if item.get("kind") == "dimension"
            and item.get("dimension_kind") == "diameter"
            and item.get("value_mm") is not None
            and abs(item["value_mm"] - outer_mm) <= 0.05
            and len(item.get("anchors_px") or []) == 2
        ),
        None,
    )
    if outer is None:
        return []
    (ax, ay), (bx, by) = outer["anchors_px"]
    ref_scale = outer_mm / math.hypot(bx - ax, by - ay)
    gray = np.asarray(Image.open(io.BytesIO(png)).convert("L"))
    frame = locate_circle_frame(gray, outer_mm)
    frame_error = None
    if frame is not None:
        frame_error = {
            "scale_rel": abs(frame.mm_per_px / ref_scale - 1.0),
            "origin_px": max(
                abs(frame.origin_px[0] - (ax + bx) / 2.0),
                abs(frame.origin_px[1] - (ay + by) / 2.0),
            ),
        }
    position_tol, diameter_tol = plate_hole_tolerances(ref_scale)
    outcomes = []
    for pattern in patterns:
        count = int(pattern["count"])
        pcd = float(pattern["bolt_circle_diameter_mm"])
        hole = float(pattern["hole_diameter_mm"])
        step = 360.0 / count
        phase = float(pattern.get("start_angle_deg") or 0.0) % step
        read = {"count": count, "pcd_mm": pcd, "hole_diameter_mm": hole, "start_angle_deg": phase}
        cases = [
            ("truth", read),
            ("count", {**read, "count": count + 2}),
            ("pcd", {**read, "pcd_mm": pcd + 6.0}),
            ("diameter", {**read, "hole_diameter_mm": hole + 1.1}),
        ]
        if _angle_gap(phase, 0.0, step) > 5.0:
            cases.append(("phase_zero", {**read, "start_angle_deg": 0.0}))
        for case, expected in cases:
            verdict = verify(Hypothesis("bolt_circle", "hole_patterns", expected), frame, gray)
            measured = verdict.measured
            accurate = bool(measured) and (
                measured["count"] == count
                and abs(measured["pcd_mm"] - pcd) <= 2.0 * position_tol
                and abs(measured["hole_diameter_mm"] - hole) <= diameter_tol
                and _angle_gap(measured["start_angle_deg"], phase, step) <= 2.0
            )
            wanted = "confirmed" if case == "truth" else "refuted"
            outcomes.append(
                {
                    "case": case,
                    "found": verdict.status != "unmeasurable",
                    "correct": verdict.status == wanted and accurate,
                    "error_rel": (abs(measured["pcd_mm"] - pcd) / pcd if measured else None),
                    "unit_px": hole / 2.0 / ref_scale,
                    "frame_found": frame is not None,
                    "frame_error": frame_error,
                    "status": verdict.status,
                    "reason": verdict.reason,
                    "measured": dict(measured),
                    "real": {**read, "pcd_mm": pcd},
                }
            )
    return outcomes


def eval_concentric_hole(png: bytes, truth: dict) -> list[dict[str, Any]]:
    """Центральное отверстие круглой детали: ``truth`` и ``diameter`` (Ø +1,1 мм).

    Система координат — по контуру на листе; эталон её точности, как у
    окружности болтов, — середина точек размера наружного Ø.
    """
    import math

    import numpy as np
    from PIL import Image

    from app.ai.cad_recognize.verifiers import Hypothesis, verify
    from app.ai.cad_recognize.verifiers.circle_frame import locate_circle_frame
    from app.ai.cad_recognize.verifiers.plate_hole import plate_hole_tolerances

    profile = (truth["spec"].get("main_view") or {}).get("profile") or {}
    bores = [
        float(hole["diameter_mm"])
        for hole in profile.get("holes") or []
        if abs(float(hole.get("center_x_mm") or 0.0)) <= 0.01
        and abs(float(hole.get("center_y_mm") or 0.0)) <= 0.01
        and hole.get("diameter_mm")
    ]
    if profile.get("shape") != "circle" or not bores or not profile.get("diameter_mm"):
        return []
    outer_mm = float(profile["diameter_mm"])
    outer = next(
        (
            item
            for item in truth.get("labels") or []
            if item.get("kind") == "dimension"
            and item.get("dimension_kind") == "diameter"
            and item.get("value_mm") is not None
            and abs(item["value_mm"] - outer_mm) <= 0.05
            and len(item.get("anchors_px") or []) == 2
        ),
        None,
    )
    if outer is None:
        return []
    (ax, ay), (bx, by) = outer["anchors_px"]
    ref_scale = outer_mm / math.hypot(bx - ax, by - ay)
    gray = np.asarray(Image.open(io.BytesIO(png)).convert("L"))
    frame = locate_circle_frame(gray, outer_mm)
    _position_tol, diameter_tol = plate_hole_tolerances(ref_scale)
    outcomes = []
    for bore in bores:
        for case, read in (("truth", bore), ("diameter", bore + 1.1)):
            verdict = verify(
                Hypothesis("concentric_hole", "holes", {"diameter_mm": read}), frame, gray
            )
            measured = verdict.measured.get("diameter_mm")
            accurate = measured is not None and abs(measured - bore) <= diameter_tol
            wanted = "confirmed" if case == "truth" else "refuted"
            outcomes.append(
                {
                    "case": case,
                    "found": verdict.status != "unmeasurable",
                    "correct": verdict.status == wanted and accurate,
                    "error_rel": (abs(measured - bore) / bore if measured else None),
                    "unit_px": bore / 2.0 / ref_scale,
                }
            )
    return outcomes


def eval_shaft_profile(png: bytes, truth: dict) -> list[dict[str, Any]]:
    """Наружный профиль вала: система координат и профиль — по листу.

    Случаи: ``truth`` — должно подтвердиться; ``diameter`` — Ø средней
    ступени +1,5 мм; ``length`` — граница за средней ступенью сдвинута на
    2 мм (эта длина +2, следующая −2). Опровергнутое должно измериться как
    эталон. Точность системы координат — против размера общей длины.
    """
    import numpy as np
    from PIL import Image

    from app.ai.cad_recognize.verifiers import Hypothesis, verify
    from app.ai.cad_recognize.verifiers.shaft_frame import locate_shaft_frame
    from app.ai.cad_recognize.verifiers.shaft_profile import shaft_tolerances

    steps = [
        {"diameter_mm": float(item["diameter_mm"]), "length_mm": float(item["length_mm"])}
        for item in (truth["spec"].get("main_view") or {}).get("outer") or []
    ]
    if len(steps) < 2:
        return []
    total = sum(item["length_mm"] for item in steps)
    overall = next(
        (
            item
            for item in truth.get("labels") or []
            if item.get("kind") == "dimension"
            and item.get("dimension_kind") == "linear"
            and item.get("value_mm") is not None
            and abs(item["value_mm"] - total) <= 0.05
        ),
        None,
    )
    if overall is None:
        return []
    (ax, _ay), (bx, _by) = overall["anchors_px"]
    ref_scale = total / abs(bx - ax)
    gray = np.asarray(Image.open(io.BytesIO(png)).convert("L"))
    located = locate_shaft_frame(gray, total)
    frame, profile = located if located else (None, None)
    frame_error = None
    if frame is not None:
        frame_error = {
            "scale_rel": abs(frame.mm_per_px / ref_scale - 1.0),
            "origin_px": abs(frame.origin_px[0] - min(ax, bx)),
        }
    length_tol, diameter_tol = shaft_tolerances(ref_scale)
    middle = len(steps) // 2
    # Ступени под пазом Ø не мерят (см. ниже `keyed`).
    stations_all = [0.0]
    for item in steps:
        stations_all.append(stations_all[-1] + item["length_mm"])
    keyways_read = (truth["spec"].get("main_view") or {}).get("keyways") or []

    def under_keyway(index: int) -> bool:
        return any(
            isinstance(key.get("axial_start_mm"), (int, float))
            and key["axial_start_mm"] < stations_all[index + 1]
            and key["axial_start_mm"] + float(key.get("length_mm") or 0.0) > stations_all[index]
            for key in keyways_read
        )

    # «Неверный Ø» — на ступени, где Ø измерим: под пазом случай проверял бы
    # неизмеримое и засчитывал «подтверждено» промахом проверяльщика.
    measurable = [i for i in range(len(steps)) if not under_keyway(i)]
    wrong_index = min(measurable, key=lambda i: abs(i - middle)) if measurable else middle
    wrong_d = [dict(item) for item in steps]
    wrong_d[wrong_index]["diameter_mm"] += 1.5
    wrong_l = [dict(item) for item in steps]
    if middle + 1 < len(steps):
        wrong_l[middle]["length_mm"] += 2.0
        wrong_l[middle + 1]["length_mm"] -= 2.0
    # Неверное звено цепочки — и габарит мимо (живой shaft-1: 98 вместо 80):
    # система координат из прочитанной суммы уезжает, как в продукте.
    wrong_link = [dict(item) for item in steps]
    wrong_link[middle]["length_mm"] += 8.0
    outcomes = []
    # Ступени под пазом Ø не мерят (проверяльщик честно отдаёт пустой замер) —
    # их Ø из точности исключается, как в продукте.
    stations = [0.0]
    for item in steps:
        stations.append(stations[-1] + item["length_mm"])
    keyed = {
        index
        for index in range(len(steps))
        for key in (truth["spec"].get("main_view") or {}).get("keyways") or []
        if isinstance(key.get("axial_start_mm"), (int, float))
        and key["axial_start_mm"] < stations[index + 1]
        and key["axial_start_mm"] + float(key.get("length_mm") or 0.0) > stations[index]
    }
    cases = (("truth", steps), ("diameter", wrong_d), ("length", wrong_l), ("link", wrong_link))
    for case, read in cases:
        read_total = sum(item["length_mm"] for item in read)
        if abs(read_total - total) > 1e-6:
            # Как в продукте: система координат — из прочитанной суммы.
            located = locate_shaft_frame(gray, read_total)
            frame, profile = located if located else (None, None)
        verdict = verify(
            Hypothesis(
                "shaft_profile",
                "outer",
                {
                    "steps": read,
                    # Пазы — из прочитанного спека, как в продукте.
                    "keyways": (truth["spec"].get("main_view") or {}).get("keyways") or [],
                },
            ),
            frame,
            profile,
        )
        measured = verdict.measured.get("steps") or []
        accurate = len(measured) == len(steps) and all(
            (
                index in keyed
                or (
                    got["diameter_mm"] is not None
                    and abs(got["diameter_mm"] - real["diameter_mm"]) <= diameter_tol
                )
            )
            and got["length_mm"] is not None
            and abs(got["length_mm"] - real["length_mm"]) <= length_tol
            for index, (got, real) in enumerate(zip(measured, steps))
        )
        wanted = "confirmed" if case == "truth" else "refuted"
        errors = [
            abs(got["diameter_mm"] - real["diameter_mm"]) / real["diameter_mm"]
            for got, real in zip(measured, steps)
            if got["diameter_mm"] is not None
        ]
        outcomes.append(
            {
                "case": case,
                "found": verdict.status != "unmeasurable",
                "correct": verdict.status == wanted and accurate,
                "error_rel": (sorted(errors)[len(errors) // 2] if errors else None),
                "unit_px": min(item["diameter_mm"] for item in steps) / 2.0 / ref_scale,
                "frame_found": frame is not None,
                "frame_error": frame_error,
                "status": verdict.status,
                "reason": verdict.reason,
                "measured_steps": measured,
                "real_steps": steps,
                "keyed": sorted(keyed),
            }
        )
    return outcomes


def eval_keyway(png: bytes, truth: dict) -> list[dict[str, Any]]:
    """Шпоночные пазы вала: система координат — по листу (`locate_shaft_frame`).

    Случаи: ``truth``; ``start`` (+3 мм), ``length`` (+5 мм), ``width``
    (+2 мм) — должны опровергнуться, а измеренное — совпасть с эталоном.
    """
    import numpy as np
    from PIL import Image

    from app.ai.cad_recognize.verifiers import Hypothesis
    from app.ai.cad_recognize.verifiers.shaft_frame import locate_shaft_views
    from app.ai.cad_recognize.verifiers.shaft_profile import shaft_tolerances
    from app.ai.cad_recognize.verifiers.stage import _first_measured

    main_view = truth["spec"].get("main_view") or {}
    keys = [key for key in main_view.get("keyways") or [] if key.get("length_mm")]
    outer = main_view.get("outer") or []
    if not keys or len(outer) < 2:
        return []
    total = sum(float(step["length_mm"]) for step in outer)
    gray = np.asarray(Image.open(io.BytesIO(png)).convert("L"))
    # Как в стадии: виды вала по очереди (у полого первый — разрез).
    frames = [frame for frame, _profile in locate_shaft_views(gray, total)]
    scale = frames[0].scale_mean if frames else 0.2
    length_tol, width_tol = shaft_tolerances(scale)
    outcomes = []
    for key in keys:
        real = {
            "axial_start_mm": float(key["axial_start_mm"]),
            "length_mm": float(key["length_mm"]),
            "width_mm": float(key["width_mm"]),
        }
        cases = [
            ("truth", real),
            ("start", {**real, "axial_start_mm": real["axial_start_mm"] + 3.0}),
            ("length", {**real, "length_mm": real["length_mm"] + 5.0}),
            ("width", {**real, "width_mm": real["width_mm"] + 2.0}),
        ]
        for case, read in cases:
            _frame, verdict = _first_measured(Hypothesis("keyway", "keyways", read), frames, gray)
            measured = verdict.measured
            accurate = bool(measured) and (
                abs(measured["axial_start_mm"] - real["axial_start_mm"]) <= length_tol
                and abs(measured["length_mm"] - real["length_mm"]) <= length_tol
                and abs(measured["width_mm"] - real["width_mm"]) <= width_tol
            )
            wanted = "confirmed" if case == "truth" else "refuted"
            outcomes.append(
                {
                    "case": case,
                    "found": verdict.status != "unmeasurable",
                    "correct": verdict.status == wanted and accurate,
                    "error_rel": (
                        abs(measured["length_mm"] - real["length_mm"]) / real["length_mm"]
                        if measured
                        else None
                    ),
                    "unit_px": real["width_mm"] / 2.0 / scale,
                }
            )
    return outcomes


def eval_cross_hole(png: bytes, truth: dict) -> list[dict[str, Any]]:
    """Поперечные отверстия вала: система координат — по листу, виды по очереди.

    Случаи: ``truth``; ``position`` (+3 мм), ``diameter`` (+2 мм) — должны
    опровергнуться, а измеренное — совпасть с эталоном.
    """
    import numpy as np
    from PIL import Image

    from app.ai.cad_recognize.verifiers import Hypothesis
    from app.ai.cad_recognize.verifiers.plate_hole import plate_hole_tolerances
    from app.ai.cad_recognize.verifiers.shaft_frame import locate_shaft_views
    from app.ai.cad_recognize.verifiers.stage import _first_measured

    main_view = truth["spec"].get("main_view") or {}
    holes = [hole for hole in main_view.get("cross_holes") or [] if hole.get("diameter_mm")]
    outer = main_view.get("outer") or []
    if not holes or len(outer) < 2:
        return []
    total = sum(float(step["length_mm"]) for step in outer)
    gray = np.asarray(Image.open(io.BytesIO(png)).convert("L"))
    frames = [frame for frame, _profile in locate_shaft_views(gray, total)]
    scale = frames[0].scale_mean if frames else 0.2
    position_tol, diameter_tol = plate_hole_tolerances(scale)
    outcomes = []
    for hole in holes:
        real = {
            "axial_position_mm": float(hole["axial_position_mm"]),
            "diameter_mm": float(hole["diameter_mm"]),
        }
        cases = [
            ("truth", real),
            ("position", {**real, "axial_position_mm": real["axial_position_mm"] + 3.0}),
            ("diameter", {**real, "diameter_mm": real["diameter_mm"] + 2.0}),
        ]
        for case, read in cases:
            _frame, verdict = _first_measured(
                Hypothesis("cross_hole", "cross_holes", read), frames, gray
            )
            measured = verdict.measured
            accurate = bool(measured) and (
                abs(measured["axial_position_mm"] - real["axial_position_mm"]) <= position_tol
                and abs(measured["diameter_mm"] - real["diameter_mm"]) <= diameter_tol
            )
            wanted = "confirmed" if case == "truth" else "refuted"
            outcomes.append(
                {
                    "case": case,
                    "found": verdict.status != "unmeasurable",
                    "correct": verdict.status == wanted and accurate,
                    "error_rel": (
                        abs(measured["diameter_mm"] - real["diameter_mm"]) / real["diameter_mm"]
                        if measured
                        else None
                    ),
                    "unit_px": real["diameter_mm"] / 2.0 / scale,
                }
            )
    return outcomes


def _shaft_sheets(png: bytes, outer: list[dict]) -> tuple[Any, list[tuple[Any, Any]]]:
    """Серый лист и виды вала (система координат, профиль) — как в стадии."""
    import numpy as np
    from PIL import Image

    from app.ai.cad_recognize.verifiers.shaft_frame import locate_shaft_views

    gray = np.asarray(Image.open(io.BytesIO(png)).convert("L"))
    total = sum(float(step["length_mm"]) for step in outer)
    return gray, locate_shaft_views(gray, total)


def _first_view_verdict(kind: str, read: dict, gray: Any, views: list) -> Any:
    from app.ai.cad_recognize.verifiers import Hypothesis, verify

    first = None
    for frame, profile in views or [(None, None)]:
        verdict = verify(Hypothesis(kind, kind, read), frame, (gray, profile))
        # Как в стадии: вид, давший замер, — последний.
        if verdict.status != "unmeasurable" or verdict.measured:
            return verdict
        first = first or verdict
    return first


def eval_groove(png: bytes, truth: dict) -> list[dict[str, Any]]:
    """Канавки вала: случаи ``truth``; ``position`` (+1,5), ``width`` (+1), ``depth`` (+0,5)."""
    from app.ai.cad_recognize.verifiers.groove import groove_tolerances

    main_view = truth["spec"].get("main_view") or {}
    grooves = [
        g for g in main_view.get("grooves") or [] if g.get("depth_mm") and not g.get("internal")
    ]
    outer = main_view.get("outer") or []
    if not grooves or len(outer) < 2:
        return []
    gray, views = _shaft_sheets(png, outer)
    scale = views[0][0].scale_mean if views else 0.2
    position_tol, width_tol, depth_tol = groove_tolerances(scale)
    outcomes = []
    for groove in grooves:
        real = {k: float(groove[k]) for k in ("axial_position_mm", "width_mm", "depth_mm")}
        cases = [
            ("truth", real),
            ("position", {**real, "axial_position_mm": real["axial_position_mm"] + 1.5}),
            ("width", {**real, "width_mm": real["width_mm"] + 1.0}),
            ("depth", {**real, "depth_mm": real["depth_mm"] + 0.5}),
        ]
        for case, read in cases:
            verdict = _first_view_verdict("groove", read, gray, views)
            got = verdict.measured
            accurate = bool(got) and (
                abs(got["axial_position_mm"] - real["axial_position_mm"]) <= position_tol
                and abs(got["width_mm"] - real["width_mm"]) <= width_tol
                and abs(got["depth_mm"] - real["depth_mm"]) <= depth_tol
            )
            wanted = "confirmed" if case == "truth" else "unmeasurable"
            outcomes.append(
                {
                    "case": case,
                    "found": bool(got),
                    "correct": verdict.status == wanted and accurate,
                    "error_rel": (
                        abs(got["width_mm"] - real["width_mm"]) / real["width_mm"] if got else None
                    ),
                    "unit_px": real["width_mm"] / scale,
                }
            )
    return outcomes


def eval_chamfer(png: bytes, truth: dict) -> list[dict[str, Any]]:
    """Фаски на торцах: случаи ``truth``; ``size`` (+0,6 мм)."""
    from app.ai.cad_recognize.verifiers.chamfer import chamfer_tolerance

    main_view = truth["spec"].get("main_view") or {}
    chamfers = [
        c for c in main_view.get("chamfers") or [] if c.get("location") in ("left_end", "right_end")
    ]
    outer = main_view.get("outer") or []
    if not chamfers or len(outer) < 2:
        return []
    gray, views = _shaft_sheets(png, outer)
    scale = views[0][0].scale_mean if views else 0.2
    tolerance = chamfer_tolerance(scale)
    outcomes = []
    for chamfer in chamfers:
        real = {"size_mm": float(chamfer["size_mm"]), "location": chamfer["location"]}
        for case, read in (("truth", real), ("size", {**real, "size_mm": real["size_mm"] + 0.6})):
            verdict = _first_view_verdict("chamfer", read, gray, views)
            got = verdict.measured
            accurate = bool(got) and abs(got["size_mm"] - real["size_mm"]) <= tolerance
            wanted = "confirmed" if case == "truth" else "unmeasurable"
            outcomes.append(
                {
                    "case": case,
                    "found": bool(got),
                    "correct": verdict.status == wanted and accurate,
                    "error_rel": (
                        abs(got["size_mm"] - real["size_mm"]) / real["size_mm"] if got else None
                    ),
                    "unit_px": real["size_mm"] / scale,
                }
            )
    return outcomes


def eval_sheet_profile(png: bytes, truth: dict) -> list[dict[str, Any]]:
    """Профиль вала по листу: надписи — как выписал бы ридер, прочитанный профиль — мимо.

    Случай ``garbage``: габарит прочитан на 8 мм больше (как у z4-r4 и
    shaft-1), ступени не сходятся; длину и ширину паза ридер отдал пазу.
    Предложение обязано совпасть с эталоном точно — или его нет: найдено —
    предложение есть, верно — совпало (найдено и не верно — ошибка).
    """
    from app.ai.cad_recognize.verifiers.sheet_profile import propose_profile, sheet_labels

    main_view = truth["spec"].get("main_view") or {}
    outer = main_view.get("outer") or []
    if len(outer) < 2:
        return []
    real = [(float(step["diameter_mm"]), float(step["length_mm"])) for step in outer]
    read_total = sum(length for _d, length in real) + 8.0
    labels = sheet_labels(
        {
            "dimensions": [
                {"value": label["text"], "bbox": label.get("bbox_px")}
                for label in truth.get("labels") or []
                if label.get("kind") == "dimension"
            ]
        }
    )
    reserved = tuple(
        float(keyway[key])
        for keyway in main_view.get("keyways") or []
        for key in ("length_mm", "width_mm")
        if keyway.get(key)
    )
    gray, views = _shaft_sheets(png, [{"length_mm": read_total}])
    proposal = None
    if views:
        proposal, _why = propose_profile(
            views[0][1],
            labels,
            read_total,
            allow_threads=not main_view.get("bore"),
            reserved=reserved,
        )
    got = [(step["diameter_mm"], step["length_mm"]) for step in proposal.steps] if proposal else []
    correct = len(got) == len(real) and all(
        abs(a - c) <= 0.01 and abs(b - d) <= 0.01 for (a, b), (c, d) in zip(got, real)
    )
    scale = views[0][0].scale_mean if views else 0.2
    return [
        {
            "case": "garbage",
            "found": proposal is not None,
            "correct": correct,
            "error_rel": None,
            "unit_px": min(d for d, _length in real) / 2.0 / scale,
        }
    ]


def eval_keyway_discovery(png: bytes, truth: dict) -> list[dict[str, Any]]:
    """Пазы, которых ридер не выписал: стадия ищет их на листе сама.

    Случай ``unread``: из эталона убраны все пазы; каждый настоящий паз —
    найден ли и точно ли (±0,6 мм), каждая лишняя находка — ошибка
    (найдено, не верно).
    """
    from app.ai.cad_recognize.verifiers.stage import verify_spec_against_sheet

    main_view = truth["spec"].get("main_view") or {}
    outer = main_view.get("outer") or []
    if len(outer) < 2:
        return []
    keys = main_view.get("keyways") or []
    spec = {"main_view": {"outer": [dict(step) for step in outer]}}
    report = verify_spec_against_sheet(png, spec)
    proposals = list(report.get("keyway_proposals") or [])
    scale = float((report.get("frame") or {}).get("mm_per_px") or 0.2)
    outcomes = []
    for key in keys:
        match = next(
            (
                p
                for p in proposals
                if abs(p["axial_start_mm"] - float(key["axial_start_mm"])) <= 0.6
                and abs(p["length_mm"] - float(key["length_mm"])) <= 0.6
            ),
            None,
        )
        if match is not None:
            proposals.remove(match)
        outcomes.append(
            {
                "case": "unread",
                "found": match is not None,
                "correct": match is not None,
                "error_rel": None,
                "unit_px": float(key.get("width_mm") or 4.0) / scale,
            }
        )
    outcomes.extend(
        {
            "case": "unread",
            "found": True,
            "correct": False,
            "error_rel": None,
            "unit_px": float(p["width_mm"]) / scale,
        }
        for p in proposals
    )
    return outcomes


_VERIFIERS = {
    "keyway_discovery": eval_keyway_discovery,
    "sheet_profile": eval_sheet_profile,
    "groove": eval_groove,
    "chamfer": eval_chamfer,
    "cross_hole": eval_cross_hole,
    "bolt_circle": eval_bolt_circle,
    "keyway": eval_keyway,
    "concentric_hole": eval_concentric_hole,
    "shaft_profile": eval_shaft_profile,
    "dimension_line": eval_dimension_line,
    "plate_hole": eval_plate_hole,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=pathlib.Path, required=True)
    parser.add_argument("--verifier", choices=sorted(_VERIFIERS), required=True)
    parser.add_argument("--split", default="dev", choices=("dev", "holdout", "all"))
    parser.add_argument("--report", type=pathlib.Path)
    parser.add_argument(
        "--raw", action="store_true", help="без нормализации продукта (выпрямления фото)"
    )
    parser.add_argument(
        "--min-correct",
        type=float,
        help="гейт: код 1, если доля верных на какой-либо ступени ниже порога",
    )
    parser.add_argument(
        "--frame",
        default="truth",
        choices=("truth", "sheet"),
        help="plate_hole: система координат плана из эталона или найденная по листу",
    )
    args = parser.parse_args()

    rows = [
        json.loads(line)
        for line in (args.corpus / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    evaluate = _VERIFIERS[args.verifier]
    if args.verifier == "plate_hole":
        evaluate = lambda png, truth: eval_plate_hole(png, truth, args.frame)  # noqa: E731
    by_step: dict[str, list[dict]] = defaultdict(list)
    by_kind_step: dict[tuple[str, str], list[dict]] = defaultdict(list)
    dewarp_count: dict[str, int] = defaultdict(int)
    for row in rows:
        if args.split != "all" and row.get("split") != args.split:
            continue
        png = (args.corpus / f"{row['name']}.png").read_bytes()
        truth = json.loads((args.corpus / f"{row['name']}.json").read_text(encoding="utf-8"))
        if not args.raw:
            png, truth, dewarped = product_normalize(png, truth)
            dewarp_count[row["step"]] += int(dewarped)
        outcomes = evaluate(png, truth)
        by_step[row["step"]].extend(outcomes)
        by_kind_step[(row.get("kind") or "?", row["step"])].extend(outcomes)

    def summary(outcomes: list[dict]) -> dict[str, Any]:
        found = [item for item in outcomes if item["found"]]
        correct = [item for item in found if item.get("correct")]
        errors = [item["error_rel"] for item in found if item.get("error_rel") is not None]
        units = [item["unit_px"] for item in outcomes]
        return {
            "n": len(outcomes),
            "found": len(found) / len(outcomes) if outcomes else None,
            "correct": len(correct) / len(outcomes) if outcomes else None,
            "median_error_rel": statistics.median(errors) if errors else None,
            "median_text_px": statistics.median(units) if units else None,
        }

    report = {
        "verifier": args.verifier,
        "split": args.split,
        "normalized": not args.raw,
        "dewarped_by_step": dict(dewarp_count),
        "by_step": {step: summary(items) for step, items in by_step.items()},
        "by_kind_step": {
            f"{kind}@{step}": summary(items) for (kind, step), items in by_kind_step.items()
        },
    }
    print(
        f"{'ступень':<11} {'n':>4} {'найдено':>8} {'верно':>7} {'медиана ошибки':>15} "
        f"{'текст px':>9} {'выпрямлено':>11}"
    )
    for step, item in report["by_step"].items():
        error = item["median_error_rel"]
        print(
            f"{step:<11} {item['n']:>4} {item['found']:>8.0%} {item['correct']:>7.0%} "
            f"{(f'{error:.1%}' if error is not None else '—'):>15} {item['median_text_px']:>9.1f} "
            f"{dewarp_count[step]:>11}"
        )
    if any(item.get("frame_error") for items in by_step.values() for item in items):
        print("\nсистема координат плана по листу:")
        for step, items in by_step.items():
            truth_cases = [item for item in items if item.get("case") == "truth"]
            located = [item["frame_error"] for item in truth_cases if item["frame_error"]]
            scale = statistics.median(e["scale_rel"] for e in located) if located else None
            origin = statistics.median(e["origin_px"] for e in located) if located else None
            print(
                f"  {step:<11} найдена {len(located)}/{len(truth_cases)}"
                + (f"  масштаб ±{scale:.2%}  начало ±{origin:.1f} px" if located else "")
            )
    cases = sorted({item["case"] for items in by_step.values() for item in items if "case" in item})
    for case in cases:
        print(f"\n{case}:")
        for step, items in by_step.items():
            picked = [item for item in items if item.get("case") == case]
            if picked:
                item = summary(picked)
                print(
                    f"  {step:<11} {item['n']:>4} найдено {item['found']:>5.0%} верно {item['correct']:>5.0%}"
                )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.min_correct is not None:
        below = {
            step: item["correct"]
            for step, item in report["by_step"].items()
            if item["correct"] is not None and item["correct"] < args.min_correct
        }
        if below:
            print(f"ГЕЙТ НЕ ПРОЙДЕН (< {args.min_correct:.0%}): {below}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
