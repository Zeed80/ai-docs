"""Прорезь пластины — по плану (X1, Ф4).

Прорезь (капсула) на плане — две прямые на расстоянии ширины и дуги на
концах. Та же фигура, что паз вала на главном виде, и тот же разбор
(`keyway._capsules`): прямые — пара линий на ширину, концы — дуги. Вертикальная
прорезь меряется на транспонированной области. Система координат — план
пластины (`locate_plate_frame`), как у отверстий.

Без этой проверки прорезь не получала свидетельства, и гейт свидетельств
сборки её не держал: геометрия собиралась такой, какой её прочла модель.
"""

from __future__ import annotations

from typing import Any

# Допуск найденной прорези к прочитанному центру: дальше — другой объект
# (E28: «не тот объект» — не измеримо, а не опровергнуто).
_OTHER_OBJECT = 1.5
# Полуширина ищется в этих долях прочитанной.
_HALF_SPAN = (0.6, 1.6)


def verify_plate_slots(gray: Any, profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Вердикт каждой прямой (0° / 90°) прорези прямоугольной пластины."""
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import locate_plate_frame
    from app.ai.cad_recognize.verifiers.plate_hole import plate_hole_tolerances

    slots = [
        (index, slot)
        for index, slot in enumerate(profile.get("slots") or [])
        if isinstance(slot, dict)
        and all(
            isinstance(slot.get(key), (int, float))
            for key in ("center_x_mm", "center_y_mm", "length_mm", "width_mm")
        )
    ]
    width, height = profile.get("width_mm"), profile.get("height_mm")
    if not slots or not isinstance(width, (int, float)) or not isinstance(height, (int, float)):
        return []
    gray = np.asarray(gray)
    frame = locate_plate_frame(gray, float(width), float(height))
    items = []
    for index, slot in slots:
        item = {
            "kind": "plate_slot",
            "path": f"main_view.profile.slots[{index}]",
            "read": {
                key: float(slot[key])
                for key in ("center_x_mm", "center_y_mm", "length_mm", "width_mm")
            },
            "measured": {},
        }
        rotation = float(slot.get("rotation_deg") or 0.0) % 180.0
        if frame is None:
            items.append(
                {**item, "status": "unmeasurable", "reason": "план пластины на листе не найден"}
            )
            continue
        if min(abs(rotation), abs(rotation - 90.0)) > 0.5:
            items.append(
                {**item, "status": "unmeasurable", "reason": "наклонная прорезь не проверяется"}
            )
            continue
        found = _measure(
            gray, frame, slot, float(width), float(height), vertical=abs(rotation - 90.0) <= 0.5
        )
        if found is None:
            items.append(
                {**item, "status": "unmeasurable", "reason": "прорези на плане не найдено"}
            )
            continue
        measured, box = found
        position_tol, width_tol = plate_hole_tolerances(frame.scale_mean)
        if (
            max(
                abs(measured["center_x_mm"] - item["read"]["center_x_mm"]),
                abs(measured["center_y_mm"] - item["read"]["center_y_mm"]),
            )
            > _OTHER_OBJECT * item["read"]["width_mm"]
        ):
            items.append(
                {
                    **item,
                    "status": "unmeasurable",
                    "measured": measured,
                    "evidence_bbox_px": box,
                    "reason": "найденная капсула далеко от прочитанной — вероятно, другая прорезь",
                }
            )
            continue
        problems = []
        for key, tolerance in (
            ("center_x_mm", position_tol),
            ("center_y_mm", position_tol),
            ("length_mm", position_tol),
            ("width_mm", width_tol),
        ):
            if abs(measured[key] - item["read"][key]) > tolerance:
                problems.append(f"{key[:-3]} {measured[key]:g} мм, прочитано {item['read'][key]:g}")
        if len(problems) >= 2:
            # Две величины разом — найдена не та капсула (E28): на грубом листе
            # прямая прорези с осевой через её середину дают «прорезь» вдвое
            # уже и со сдвигом (75…100 dpi: ширина −3, центр +1,5 мм). Ошибка
            # ридера портит одну величину.
            items.append(
                {
                    **item,
                    "status": "unmeasurable",
                    "measured": measured,
                    "evidence_bbox_px": box,
                    "reason": "замер разошёлся в нескольких величинах — вероятно, не та капсула: "
                    + "; ".join(problems),
                }
            )
            continue
        items.append(
            {
                **item,
                "status": "refuted" if problems else "confirmed",
                "measured": measured,
                "evidence_bbox_px": box,
                "reason": "; ".join(problems),
            }
        )
    return items


def _measure(
    gray: Any, frame: Any, slot: dict, width: float, height: float, *, vertical: bool
) -> tuple[dict[str, float], list[float]] | None:
    """Капсула у прочитанного места: центр, длина, ширина (мм, от центра плана)."""
    import cv2
    import numpy as np

    from app.ai.cad_recognize.verifiers.keyway import (
        _arc_centre,
        _capsules,
        _refined_half,
    )
    from app.ai.cad_recognize.verifiers.plate_frame import _ink

    cx_mm = float(slot["center_x_mm"]) + width / 2.0
    cy_mm = float(slot["center_y_mm"]) + height / 2.0
    cx, cy = frame.to_px(cx_mm, cy_mm)
    length_px = float(slot["length_mm"]) / frame.scale_mean
    half_px = float(slot["width_mm"]) / 2.0 / frame.scale_mean
    along, across = length_px / 2.0 + 2.0 * half_px, 3.0 * half_px
    if vertical:
        box = (cx - across, cy - along, cx + across, cy + along)
    else:
        box = (cx - along, cy - across, cx + along, cy + across)
    x0, y0 = max(0, int(box[0])), max(0, int(box[1]))
    x1, y1 = min(gray.shape[1], int(box[2]) + 1), min(gray.shape[0], int(box[3]) + 1)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    ink = _ink(np.ascontiguousarray(gray[y0:y1, x0:x1]))
    if vertical:
        ink = np.ascontiguousarray(ink.T)
    mask = cv2.dilate(ink.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
    middle = (cx - x0) if vertical else (cy - y0)
    read_centre_along = (cy - y0) if vertical else (cx - x0)
    best = None
    for centre in np.arange(middle - half_px, middle + half_px + 0.5, 1.0):
        for h in np.arange(_HALF_SPAN[0] * half_px, _HALF_SPAN[1] * half_px + 0.25, 0.5):
            for cl, cr, support in _capsules(mask, float(centre), float(h)):
                offset = abs((cl + cr) / 2.0 - read_centre_along)
                key = (-round(support, 1), round(offset / max(half_px, 1.0), 1))
                if best is None or key < best[0]:
                    best = (key, float(centre), float(h), cl, cr)
    if best is None or best[0][0] > -1.5:
        return None
    _key, centre, h, cl, cr = best
    # Центр — середина между прямыми по самим чернилам: перебор шёл по сетке
    # строк, и центр у всех девяти прорезей корпуса уезжал на 0,3…0,6 мм.
    centre, h = _straight_lines(ink, centre, h, cl, cr)
    h = _refined_half(ink, centre, h, cl, cr)
    cl = _arc_centre(ink, centre, h, cl, side=-1)
    cr = _arc_centre(ink, centre, h, cr, side=1)
    mid_along = (cl + cr) / 2.0
    if vertical:
        px_x, px_y = x0 + centre, y0 + mid_along
    else:
        px_x, px_y = x0 + mid_along, y0 + centre
    u, v = frame.to_mm(px_x, px_y)
    scale_along = frame.scale_v if vertical else frame.mm_per_px
    scale_across = frame.mm_per_px if vertical else frame.scale_v
    measured = {
        "center_x_mm": round(u - width / 2.0, 3),
        "center_y_mm": round(v - height / 2.0, 3),
        "length_mm": round((cr - cl + 2.0 * h) * scale_along, 3),
        "width_mm": round(2.0 * h * scale_across, 3),
    }
    if vertical:
        found = [x0 + centre - h, y0 + cl - h, x0 + centre + h, y0 + cr + h]
    else:
        found = [x0 + cl - h, y0 + centre - h, x0 + cr + h, y0 + centre + h]
    return measured, [round(value, 1) for value in found]


def _straight_lines(ink: Any, centre: float, h: float, cl: float, cr: float) -> tuple[float, float]:
    """Прямые капсулы по чернилам: середины штриха у ``centre ± h`` по столбцам."""
    import numpy as np

    reach = max(2.0, 0.25 * h)
    tops, bottoms = [], []
    for column in range(int(round(cl)), int(round(cr)) + 1):
        if not 0 <= column < ink.shape[1]:
            continue
        for target, bucket in ((centre - h, tops), (centre + h, bottoms)):
            lo = max(0, int(target - reach))
            hi = min(ink.shape[0], int(target + reach) + 1)
            rows = np.nonzero(ink[lo:hi, column])[0]
            if rows.size:
                bucket.append((rows[0] + rows[-1]) / 2.0 + lo)
    if len(tops) < 3 or len(bottoms) < 3:
        return centre, h
    top, bottom = float(np.median(tops)), float(np.median(bottoms))
    return (top + bottom) / 2.0, (bottom - top) / 2.0
