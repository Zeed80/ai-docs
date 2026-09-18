"""Сечение гнутой детали по листу (план, X4): полки, направления и углы гибов.

Базовая линия ридера: швеллер и уголок модель читает верно, а Z-профиль
путает — называет уголком и теряет полку или швеллером. Форма сечения видна
на листе без всякого чтения: сечение — единственная тонкая ПОЛОСА основной
линии (у видов и развёртки — прямоугольники с пустым нутром). Её осевая —
ломаная; длинные участки — полки, повороты между ними — гибы со знаком.

Как найдено на корпусе (24 листа, 300 dpi):

* размыкание ядром между тонкой и основной линией (середина 25-го и 75-го
  перцентилей толщин штрихов, не толще ¾ основной) снимает размерные и
  выносные; ядро по 90-му перцентилю стирало тонкое сечение целиком;
* сечение — компонент с наименьшим отношением «залитая площадь / чернила»:
  у полосы оно ≈ 1–2, у прямоугольника вида — десятки;
* прореживание и расстояния — на картинке с ПОЛЕМ: у края обрезки осевая
  «прилипала» к рамке, а толщина полосы выходила вдвое больше;
* угол гиба — между прямыми, подогнанными по средней половине каждой полки:
  по вершинам упрощённой ломаной он гулял 66–101° у прямых гибов.

Итог на корпусе: верное чтение — 24/24 «подтверждено»; перевёрнутый гиб
(швеллер ↔ Z) — 19/19 опровергнуто, потерянная полка — 19/19, неверный угол
— 24/24.

Подбор наружных размеров полок по надписям (один масштаб на все полки)
пробовался и отвергнут: крайние полки осевая перебегает на 0,3–0,9 мм, и на
близких надписях (43 и 46) подбор уверенно выбирал неверную — выдумывать
размер нельзя. Размеры остаются прочитанными.
"""

from __future__ import annotations

import math
from typing import Any

# Полка — участок осевой не короче стольких толщин полосы.
_FLANGE_BANDS = 2.5
# Середина полки для подгонки прямой — не короче стольких толщин полосы.
_FIT_BANDS = 1.0
# Угол гиба сходится с прочитанным в пределах, градусы.
ANGLE_TOLERANCE_DEG = 5.0


def measure_bent_section(gray: Any) -> dict[str, Any] | None:
    """Полки и гибы сечения на листе или None, если сечения не нашлось.

    ``turns`` — знак поворота каждого гиба по ходу осевой (+1 влево, −1
    вправо; зеркальный лист меняет все знаки сразу), ``angles_deg`` — угол
    гиба (отклонение полки) или None, где середина полки коротка для подгонки,
    ``flanges_px`` — длины полок по осевой между условными вершинами.
    """
    import cv2
    import numpy as np

    ink = (np.asarray(gray) < 140).astype(np.uint8)
    if not ink.any():
        return None
    dt = cv2.distanceTransform(ink, cv2.DIST_L2, 3)
    skeleton = cv2.ximgproc.thinning(ink * 255) > 0
    widths = 2 * dt[skeleton]
    if widths.size < 50:
        return None
    thin_w = float(np.percentile(widths, 25))
    main_w = float(np.percentile(widths, 75))
    # Не толще трёх четвертей основной: где тонких линий почти нет, середина
    # перцентилей равна основной, и наклонная полка рвалась размыканием.
    kernel_size = max(2, int(math.floor(min((thin_w + main_w) / 2.0, 0.75 * main_w))))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    strong = cv2.morphologyEx(ink, cv2.MORPH_OPEN, kernel)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(strong, 8)
    best = None
    for index in range(1, count):
        x, y, w, h, _area = stats[index]
        if max(w, h) < 20 * main_w:
            continue
        component = (labels[y : y + h, x : x + w] == index).astype(np.uint8)
        padded = np.pad(component, 1) * 255
        mask = np.zeros((padded.shape[0] + 2, padded.shape[1] + 2), np.uint8)
        cv2.floodFill(padded, mask, (0, 0), 128)
        filled = (padded != 128).astype(np.uint8)[1:-1, 1:-1]
        ratio = float(filled.sum()) / max(1.0, float(component.sum()))
        if best is None or ratio < best[0]:
            best = (ratio, x, y, filled)
    if best is None:
        return None
    _ratio, x0, y0, filled = best
    # Поле: заливка обрезана по рамке компонента, а у края картинки и
    # прореживание не снимает пиксели (осевая «прилипала» к рамке, крайние
    # полки выходили кривыми), и расстояния не видят фона (полоса — вдвое
    # толще). Всё — на картинке с полем, координаты — со сдвигом.
    filled = np.pad(filled, 2)
    x0, y0 = x0 - 2, y0 - 2
    thin = cv2.ximgproc.thinning(filled * 255) > 0
    distance = cv2.distanceTransform(filled, cv2.DIST_L2, 3)
    band = float(2 * np.median(distance[thin]))
    path = _longest_path(thin)
    if path is None or len(path) < 10:
        return None
    points = np.array([[c + x0, r + y0] for r, c in path], np.float32)
    polyline = cv2.approxPolyDP(points.reshape(-1, 1, 2), max(2.0, 0.6 * band), False)
    polyline = polyline.reshape(-1, 2)
    at = [int(np.argmin(np.hypot(*(points - vertex).T))) for vertex in polyline]
    lines = []
    for (start, end), (i, j) in zip(zip(polyline[:-1], polyline[1:]), zip(at[:-1], at[1:])):
        chord = end - start
        length = float(np.hypot(*chord))
        if length < _FLANGE_BANDS * band:
            continue
        low, high = sorted((i, j))
        cut = (high - low) // 4
        middle = points[low + cut : high - cut + 1]
        direction = chord / (length or 1.0)
        fitted = None
        if len(middle) >= 5 and length / 2.0 >= _FIT_BANDS * band:
            vx, vy, px, py = cv2.fitLine(middle, cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
            if vx * chord[0] + vy * chord[1] < 0:
                vx, vy = -vx, -vy
            fitted = (np.array([px, py]), np.array([vx, vy]))
        lines.append({"start": start, "end": end, "direction": direction, "fit": fitted})
    if not lines:
        return None
    turns: list[int] = []
    angles: list[float | None] = []
    for first, second in zip(lines[:-1], lines[1:]):
        a = first["fit"][1] if first["fit"] is not None else first["direction"]
        b = second["fit"][1] if second["fit"] is not None else second["direction"]
        # Ось y листа растёт вниз: знак берётся в обычной системе (y вверх).
        cross = a[0] * (-b[1]) - (-a[1]) * b[0]
        dot = a[0] * b[0] + a[1] * b[1]
        angle = math.degrees(math.atan2(cross, dot))
        turns.append(1 if angle > 0 else -1)
        exact = first["fit"] is not None and second["fit"] is not None
        angles.append(round(abs(angle), 1) if exact else None)
    flanges = _flange_lengths(lines, points)
    return {
        "turns": turns,
        "angles_deg": angles,
        "flanges_px": flanges,
        # Полка, по середине которой подогнана прямая: у неё длина между
        # условными вершинами точна (±0,1 мм на корпусе), у прочих — нет.
        "fitted": [line["fit"] is not None for line in lines],
        "band_px": round(band, 2),
        "bbox_px": [
            float(points[:, 0].min()),
            float(points[:, 1].min()),
            float(points[:, 0].max()),
            float(points[:, 1].max()),
        ],
    }


def _longest_path(skeleton: Any) -> list[tuple[int, int]] | None:
    """Самый длинный путь по осевой (от одного свободного конца до другого)."""
    import numpy as np

    pixels = {(int(r), int(c)) for r, c in zip(*np.nonzero(skeleton), strict=True)}

    def neighbours(pixel: tuple[int, int]) -> list[tuple[int, int]]:
        r, c = pixel
        return [
            (r + dr, c + dc)
            for dr in (-1, 0, 1)
            for dc in (-1, 0, 1)
            if (dr or dc) and (r + dr, c + dc) in pixels
        ]

    ends = [pixel for pixel in pixels if len(neighbours(pixel)) == 1]
    if not ends:
        return None

    def farthest(source: tuple[int, int]):
        previous: dict[tuple[int, int], tuple[int, int] | None] = {source: None}
        frontier = [source]
        last = source
        while frontier:
            nxt = []
            for pixel in frontier:
                for other in neighbours(pixel):
                    if other not in previous:
                        previous[other] = pixel
                        nxt.append(other)
                        last = other
            frontier = nxt
        return last, previous

    first, _ = farthest(ends[0])
    second, previous = farthest(first)
    path = []
    pixel: tuple[int, int] | None = second
    while pixel is not None:
        path.append(pixel)
        pixel = previous[pixel]
    return path


def _flange_lengths(lines: list[dict[str, Any]], points: Any) -> list[float]:
    """Полки по осевой между условными вершинами (пересечениями соседних прямых);
    у крайних — от конца осевой."""
    import numpy as np

    def line_of(item):
        if item["fit"] is not None:
            return item["fit"]
        return (np.asarray(item["start"], float), np.asarray(item["direction"], float))

    sharps = []
    for first, second in zip(lines[:-1], lines[1:]):
        (p, u), (q, v) = line_of(first), line_of(second)
        matrix = np.array([[u[0], -v[0]], [u[1], -v[1]]], float)
        if abs(np.linalg.det(matrix)) < 1e-9:
            sharps.append(
                (np.asarray(first["end"], float) + np.asarray(second["start"], float)) / 2
            )
            continue
        t, _s = np.linalg.solve(matrix, np.asarray(q, float) - np.asarray(p, float))
        sharps.append(np.asarray(p, float) + t * np.asarray(u, float))
    ends = [np.asarray(points[0], float), np.asarray(points[-1], float)]
    # Осевая начинается там же, где первая полка ломаной.
    if np.hypot(*(ends[0] - lines[0]["start"])) > np.hypot(*(ends[1] - lines[0]["start"])):
        ends.reverse()
    nodes = [ends[0], *sharps, ends[1]]
    return [round(float(np.hypot(*(b - a))), 2) for a, b in zip(nodes[:-1], nodes[1:])]


def _mirror_equal(read: list[int], measured: list[int]) -> bool:
    """Взаимные направления совпали (зеркальный лист и обход с другого конца —
    та же деталь)."""
    if len(read) != len(measured):
        return False
    candidates = [
        measured,
        [-t for t in measured],
        [-t for t in reversed(measured)],
        list(reversed(measured)),
    ]
    return any(list(read) == candidate for candidate in candidates)


def bent_section_verdict(read: dict[str, Any], measured: dict[str, Any] | None) -> dict[str, Any]:
    """Вердикт прочитанному сечению: число гибов, направления, углы."""
    if measured is None:
        return {"status": "unmeasurable", "reason": "сечение на листе не найдено", "measured": {}}
    read_turns = [int(t) for t in read.get("turns") or []]
    read_angles = [float(a) for a in read.get("bend_angles_deg") or [90.0] * len(read_turns)]
    shown = {
        "bends": len(measured["turns"]),
        "turns": measured["turns"],
        "angles_deg": measured["angles_deg"],
    }
    if len(read_turns) != len(measured["turns"]):
        return {
            "status": "refuted",
            "reason": f"гибов на листе {len(measured['turns'])}, прочитано {len(read_turns)}",
            "measured": shown,
        }
    if not _mirror_equal(read_turns, measured["turns"]):
        return {
            "status": "refuted",
            "reason": "направления гибов на листе другие (швеллер ↔ Z-профиль)",
            "measured": shown,
        }
    # Порядок углов — по ходу прочитанного; при обходе с другого конца листа
    # углы идут в обратном порядке, симметричное сравнение берёт лучшее.
    measured_angles = measured["angles_deg"]
    best = None
    for order in (measured_angles, list(reversed(measured_angles))):
        off = [abs(a - r) for a, r in zip(order, read_angles, strict=True) if a is not None]
        worst = max(off) if off else None
        if best is None or (worst is not None and (best[0] is None or worst < best[0])):
            best = (worst, order)
    worst, _order = best
    if worst is not None and worst > ANGLE_TOLERANCE_DEG:
        return {
            "status": "refuted",
            "reason": f"угол гиба на листе отличается на {worst:.0f}°",
            "measured": shown,
        }
    if None in measured_angles:
        # Форма совпала, но угол короткой полки не измерен — подтверждать
        # непроверенное нельзя (корпус: 4 неверных угла «подтверждались»).
        return {
            "status": "unmeasurable",
            "reason": "форма сечения совпала, угол гиба у короткой полки не измерить",
            "measured": shown,
        }
    return {"status": "confirmed", "reason": "", "measured": shown}
