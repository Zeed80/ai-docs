"""Карманы и приливы на гранях корпуса — замером по листу (план, Ф5).

Базовая линия чтения на корпусах: элементы граней 0 из 11. Узкий вопрос ридеру
помог наполовину — размер он берёт с листа верно (полость 70,8 × 70,8), а
глубину и положение приписывает не тому виду: центр (−42, −42) вместо (0, 0),
глубина 6 вместо 32. Место и размер элемента — это замер, а не чтение.

Элемент ищется НА СВОЁМ ВИДЕ, который дала проекционная связь
(`housing_views`): грань `top`/`bottom` видна на плане, `front`/`back` — на
виде спереди, `left`/`right` — на виде слева. Круг — тем же приёмом, что у
отверстий пластины (Хаф в окне ожидаемого радиуса + уточнение по лучам),
прямоугольник — парами основных линий нужной длины внутри вида.

Возвращается измеренное в тех же полях, что у спека (размер и центр от центра
грани), чтобы согласование могло принять замер вместо прочитанного.
"""

from __future__ import annotations

from typing import Any

# Допуски замера: в толщинах основной линии листа (замер идёт по её середине,
# а соседние линии её утолщают) и не мельче этого в миллиметрах.
_SIZE_MM = 0.5
_POSITION_MM = 1.0
_SIZE_LINES = 1.5
_POSITION_LINES = 2.0
# Расхождение размера больше этой доли — найден ДРУГОЙ элемент, а не ошибка
# чтения: это «не измеримо», а не опровержение (принцип E28).
_ANOTHER_OBJECT = 0.1
# Запас поиска за кромками вида: элемент ищется по ВСЕМУ виду, а ближайший к
# прочитанному месту выбирается из найденных. Узкое окно вокруг прочитанного
# прятало грубую ошибку чтения: смещённый на 45 мм прилив просто «не
# находился», вместо того чтобы быть опровергнутым с замером.
_SEARCH_MARGIN = 0.15
# Сторона прямоугольника считается найденной, если линия накрывает её на эту
# долю: угол элемента на листе может быть скруглён или перекрыт размерной.
_SIDE_COVERAGE = 0.7


# Грань → как её оси лежат НА ЛИСТЕ: (меняются ли u и v местами, знак u,
# знак v). Замерено на корпусах: у передней и задней стенки ось v на листе
# перевёрнута, у боковых оси меняются местами (читано u=11, v=−2 — замер
# u=2,15, v=11,0). Те же перестановки, что при рисовании: у каждой грани свой
# порядок осей.
_FACE_AXES = {
    "top": (False, 1.0, 1.0),
    "bottom": (False, 1.0, 1.0),
    "front": (False, 1.0, -1.0),
    "back": (False, 1.0, -1.0),
    "left": (True, -1.0, 1.0),
    "right": (True, -1.0, 1.0),
}


def _to_sheet(plane: str, u: float, v: float) -> tuple[float, float]:
    """Координаты элемента в системе спека → в оси вида на листе."""
    swap, sign_u, sign_v = _FACE_AXES.get(plane, (False, 1.0, 1.0))
    if swap:
        return sign_u * v, sign_v * u
    return sign_u * u, sign_v * v


def _from_sheet(plane: str, u: float, v: float) -> tuple[float, float]:
    """Обратно: оси листа → система спека."""
    swap, sign_u, sign_v = _FACE_AXES.get(plane, (False, 1.0, 1.0))
    if swap:
        return v / sign_v, u / sign_u
    return u / sign_u, v / sign_v


def _faces(width: float, height: float, thickness: float) -> dict[str, tuple[float, float]]:
    return {
        "top": (width, height),
        "bottom": (width, height),
        "front": (width, thickness),
        "back": (width, thickness),
        "left": (height, thickness),
        "right": (height, thickness),
    }


def measure_wall_feature(
    gray: Any,
    view_bbox: tuple[float, float, float, float],
    mm_per_px: float,
    face: tuple[float, float],
    item: dict[str, Any],
) -> dict[str, Any] | None:
    """Замер элемента на его виде или ``None``, если он там не найден.

    ``view_bbox`` — кромки ТЕЛА на этом виде (не рамка вида: за неё выходят
    приливы), ``face`` — размеры грани в мм.
    """
    import numpy as np

    gray = np.asarray(gray)
    x0, y0, x1, y1 = (float(value) for value in view_bbox)
    face_u, face_v = face
    if x1 - x0 <= 4 or y1 - y0 <= 4 or mm_per_px <= 0:
        return None
    plane = str(item.get("on_plane") or "")
    read_u, read_v = _to_sheet(
        plane, float(item.get("center_u_mm") or 0.0), float(item.get("center_v_mm") or 0.0)
    )
    # Центр грани — середина кромок тела; ось v листа идёт вниз.
    centre_x = (x0 + x1) / 2.0 + read_u / mm_per_px
    centre_y = (y0 + y1) / 2.0 - read_v / mm_per_px
    swap = _FACE_AXES.get(plane, (False, 1.0, 1.0))[0]
    if item.get("profile") == "rectangle":
        size_u = float(item.get("width_mm") or 0.0)
        size_v = float(item.get("height_mm") or 0.0)
        measured = _rectangle(
            gray,
            (x0, y0, x1, y1),
            mm_per_px,
            (centre_x, centre_y),
            size_v if swap else size_u,
            size_u if swap else size_v,
        )
    else:
        measured = _circle(
            gray,
            (x0, y0, x1, y1),
            mm_per_px,
            (centre_x, centre_y),
            float(item.get("diameter_mm") or 0.0),
        )
    if measured is None:
        return None
    found_x, found_y, sizes = measured
    sheet_u = (found_x - (x0 + x1) / 2.0) * mm_per_px
    sheet_v = ((y0 + y1) / 2.0 - found_y) * mm_per_px
    spec_u, spec_v = _from_sheet(plane, sheet_u, sheet_v)
    result = {"center_u_mm": round(spec_u, 3), "center_v_mm": round(spec_v, 3)}
    if swap and "width_mm" in sizes:
        sizes = {"width_mm": sizes["height_mm"], "height_mm": sizes["width_mm"]}
    result.update(sizes)
    # Элемент лежит на своей грани — иначе это чужая линия.
    if abs(result["center_u_mm"]) > face_u / 2.0 or abs(result["center_v_mm"]) > face_v / 2.0:
        return None
    return result


def _circle(
    gray: Any,
    box: tuple[float, float, float, float],
    mm_per_px: float,
    centre: tuple[float, float],
    diameter_mm: float,
) -> tuple[float, float, dict[str, float]] | None:
    import numpy as np

    from app.ai.cad_recognize.verifiers.cross_hole import _ring
    from app.ai.cad_recognize.verifiers.plate_frame import _ink
    from app.ai.cad_recognize.verifiers.plate_hole import _hough

    if diameter_mm <= 0:
        return None
    radius_px = diameter_mm / 2.0 / mm_per_px
    x0, y0, x1, y1 = box
    margin = _SEARCH_MARGIN * max(x1 - x0, y1 - y0)
    left = int(max(0, x0 - margin))
    top = int(max(0, y0 - margin))
    right = int(min(gray.shape[1], x1 + margin))
    bottom = int(min(gray.shape[0], y1 + margin))
    if right - left < 6 or bottom - top < 6:
        return None
    window = gray[top:bottom, left:right]
    found = _hough(window, radius_px)
    if not found:
        return None
    best = min(
        found,
        key=lambda circle: (circle[0] + left - centre[0]) ** 2 + (circle[1] + top - centre[1]) ** 2,
    )
    ink = _ink(np.asarray(window))
    refined = _ring(ink, best[0], best[1], best[2])
    if refined is not None:
        cu, cv, radius = refined[0], refined[1], refined[2]
    else:
        cu, cv, radius = best
    return (
        cu + left,
        cv + top,
        {"diameter_mm": round(2.0 * radius * mm_per_px, 3)},
    )


def _rectangle(
    gray: Any,
    box: tuple[float, float, float, float],
    mm_per_px: float,
    centre: tuple[float, float],
    width_mm: float,
    height_mm: float,
) -> tuple[float, float, dict[str, float]] | None:
    from app.ai.cad_recognize.verifiers.plate_frame import _ink, _lines

    if width_mm <= 0 or height_mm <= 0:
        return None
    want_u, want_v = width_mm / mm_per_px, height_mm / mm_per_px
    # Допуск поиска — доля стороны, но решает вердикт: искать надо шире
    # допуска замера и уже, чем половина элемента.
    tolerance = max(3.0, 0.03 * min(want_u, want_v))
    x0, y0, x1, y1 = box
    # Окно вокруг ожидаемого места: по всему виду соседние линии сливаются с
    # кромкой элемента, и высота выходила короче на толщину линии (корпуса:
    # 52,2 вместо 53, 69,5 вместо 70,8).
    margin = max(8.0, 0.3 * max(want_u, want_v))
    left_edge = int(max(x0 - margin, 0))
    right_edge = int(min(x1 + margin, gray.shape[1]))
    top_edge = int(max(y0 - margin, 0))
    bottom_edge = int(min(y1 + margin, gray.shape[0]))
    if right_edge - left_edge < 6 or bottom_edge - top_edge < 6:
        return None
    window = gray[top_edge:bottom_edge, left_edge:right_edge]
    ink = _ink(window)
    min_length = max(6, int(round(0.5 * min(want_u, want_v))))
    horizontal = _lines(ink, min_length, axis=0)
    vertical = _lines(ink, min_length, axis=1)

    def covers(lines: list, position: float, low: float, high: float) -> bool:
        return any(
            abs(line.position - position) <= tolerance
            and line.overlap(low, high) >= _SIDE_COVERAGE * (high - low)
            for line in lines
        )

    best = None
    for top in horizontal:
        for bottom in horizontal:
            if abs((bottom.position - top.position) - want_v) > tolerance:
                continue
            middle_y = (top.position + bottom.position) / 2.0
            for left in vertical:
                right_x = left.position + want_u
                if not covers(vertical, right_x, top.position, bottom.position):
                    continue
                if not (
                    covers(horizontal, top.position, left.position, right_x)
                    and covers(horizontal, bottom.position, left.position, right_x)
                ):
                    continue
                middle_x = (left.position + right_x) / 2.0 + left_edge
                distance = (middle_x - centre[0]) ** 2 + (middle_y + top_edge - centre[1]) ** 2
                if best is None or distance < best[0]:
                    best = (
                        distance,
                        middle_x,
                        middle_y,
                        right_x - left.position,
                        bottom.position - top.position,
                    )
    if best is None:
        return None
    _distance, middle_x, middle_y, span_u, span_v = best
    return (
        middle_x,
        middle_y + top_edge,
        {
            "width_mm": round(span_u * mm_per_px, 3),
            "height_mm": round(span_v * mm_per_px, 3),
        },
    )


def wall_feature_verdict(
    read: dict[str, Any], measured: dict[str, Any] | None, line_mm: float = 0.0
) -> dict[str, Any]:
    """Сравнение прочитанного с замером: подтверждено / опровергнуто / не измеримо.

    Размер, разошедшийся больше чем на десятую долю, — признак того, что на
    виде найден другой элемент (соседний прилив того же ряда): такое
    расхождение не опровергает чтение, а остаётся «не измеримо».
    """
    if measured is None:
        return {
            "status": "unmeasurable",
            "measured": {},
            "reason": "элемент не найден на своём виде",
        }
    size_tolerance = max(_SIZE_MM, _SIZE_LINES * line_mm)
    position_tolerance = max(_POSITION_MM, _POSITION_LINES * line_mm)
    problems = []
    for key, tolerance in (
        ("diameter_mm", size_tolerance),
        ("width_mm", size_tolerance),
        ("height_mm", size_tolerance),
        ("center_u_mm", position_tolerance),
        ("center_v_mm", position_tolerance),
    ):
        if key not in measured:
            continue
        expected = read.get(key)
        if not isinstance(expected, (int, float)) or isinstance(expected, bool):
            continue
        difference = abs(float(measured[key]) - float(expected))
        if key.endswith("_mm") and not key.startswith("center"):
            if difference > _ANOTHER_OBJECT * max(float(expected), 1e-6):
                return {
                    "status": "unmeasurable",
                    "measured": measured,
                    "reason": (
                        f"на виде найден другой элемент: {key} {measured[key]:g} мм "
                        f"против {float(expected):g}"
                    ),
                }
        if difference > tolerance:
            problems.append(f"{key}: {measured[key]:g} мм, прочитано {float(expected):g}")
    return {
        "status": "refuted" if problems else "confirmed",
        "measured": measured,
        "reason": "; ".join(problems) or "размер и положение совпали с листом",
    }


# Боковая стенка кармана видна с ребра как линия от кромки грани до дна:
# она должна тянуться не меньше чем на эту долю глубины.
_SIDE_REACH = 0.7


def measure_depth_on_edge_view(
    gray: Any,
    edges: tuple[float, float],
    mm_per_px: float,
    span_px: float,
    *,
    axis: str,
    outward: bool,
) -> tuple[float, float] | None:
    """Глубина кармана или вылет прилива на виде, где грань видна с ребра.

    Дно кармана — не «ближайшая к кромке линия» (так замер давал 0,008 мм при
    58): это линия ДЛИНОЙ В САМ ЭЛЕМЕНТ, опирающаяся на две боковые, которые
    идут от кромки грани внутрь. Ищется по длине, а не по предсказанному
    месту: центры видов на листе не совпадают — в габарит вида входят выступы
    приливов.

    ``edges`` — кромки тела на этом виде вдоль оси глубины, ``span_px`` —
    длина элемента вдоль кромки. Возвращает (глубина в мм, кромка в px): по
    кромке видно, с какой стороны элемент — а значит, на какой он грани.
    """
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _ink, _lines

    gray = np.asarray(gray)
    low_edge, high_edge = sorted(edges)
    if span_px < 4 or mm_per_px <= 0 or high_edge - low_edge < 4:
        return None
    ink = _ink(gray)
    tolerance = max(3.0, 0.03 * span_px)
    along = _lines(ink, max(4, int(round(0.5 * span_px))), axis=0 if axis == "v" else 1)

    def side_ink(position: float, start: float, end: float) -> bool:
        """Идёт ли от кромки к линии сплошной штрих — боковая стенка элемента.

        Считаются сами чернила, а не «линии»: дно и боковые стенки нарисованы
        одним контуром, и поиск связных компонент возвращал их одной фигурой с
        центром посередине — боковых «не находилось» вовсе.
        """
        low_i, high_i = int(min(start, end)), int(max(start, end))
        if high_i - low_i < 3:
            return False
        column = int(round(position))
        band = (
            ink[low_i:high_i, max(0, column - 2) : column + 3]
            if axis == "v"
            else ink[max(0, column - 2) : column + 3, low_i:high_i]
        )
        if band.size == 0:
            return False
        covered = band.any(axis=1 if axis == "v" else 0)
        return float(covered.mean()) >= _SIDE_REACH

    best: tuple[float, float, float] | None = None
    for line in along:
        length = line.end - line.start
        if abs(length - span_px) > tolerance:
            continue
        inside = low_edge + tolerance < line.position < high_edge - tolerance
        if outward == inside:
            continue
        for edge in (low_edge, high_edge):
            depth_px = abs(line.position - edge)
            if depth_px < tolerance:
                continue
            if not (
                side_ink(line.start, edge, line.position)
                and side_ink(line.end, edge, line.position)
            ):
                continue
            if best is None or depth_px < best[0]:
                best = (depth_px, line.position, edge)
    if best is None:
        return None
    return round(best[0] * mm_per_px, 3), best[2]
