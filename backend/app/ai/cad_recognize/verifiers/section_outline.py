"""Контур вынесенного сечения тела вращения: лыски и радиальные отверстия (дорожка У, У3).

Сечение поперёк оси — окружность ступени; элементы поперёк оси портят её
контур: лыска — плавной хордой (граница ближе радиуса по закону s / cos),
радиальное отверстие — узким провалом (канал уходит к оси). Контур меряется
лучами СНАРУЖИ внутрь: первое попадание в чернила — граница; штриховка
внутри не мешает, а размерные линии стоят дальше полосы поиска.

Один примитив вместо отдельного проверяльщика на элемент: вердикт лыске
(угол и глубина) и отверстию (угол и Ø) — по одному профилю границы.
Сечение ищет `section_disk` по Ø ступени на станции элемента.
"""

from __future__ import annotations

import math
from typing import Any

# Отклонение границы от окружности, при котором угол считается занятым
# элементом: доля радиуса, но не меньше пары пикселей.
_DEVIATION_SHARE = 0.03
# Совпадение прочитанного угла с найденным (градусы).
_ANGLE_TOLERANCE = 6.0
# Дальше этого угла найденное — другой элемент (E28: «не тот объект»).
_OTHER_OBJECT_DEG = 25.0
# Основная линия тоньше — лист грубый (та же мера, что у профиля вала).
_MIN_LINE_PX = 4.5


def boundary_profile(ink: Any, cx: float, cy: float, radius: float) -> list[float | None]:
    """Расстояние от центра до границы по углу 0…359° (угол — в осях вида, v вверх).

    Луч идёт снаружи (1,12 R) внутрь; первая точка чернил — граница.
    ``None`` — чернил на луче нет до самого центра (сквозной канал).
    """
    height, width = ink.shape
    result: list[float | None] = []
    outer = 1.12 * radius
    for step in range(360):
        angle = math.radians(step)
        ux, uy = math.cos(angle), -math.sin(angle)
        found = None
        distance = outer
        while distance > 1.0:
            x, y = int(round(cx + ux * distance)), int(round(cy + uy * distance))
            if 0 <= y < height and 0 <= x < width and ink[y, x]:
                found = distance
                break
            distance -= 0.5
        result.append(found)
    return result


def deviations(profile: list[float | None], radius: float) -> list[dict[str, Any]]:
    """Участки, где граница ближе окружности: угол середины, глубина, ширина."""
    limit = max(_DEVIATION_SHARE * radius, 2.0)
    inside = [value is None or value < radius - limit for value in profile]
    if all(inside) or not any(inside):
        return []
    start = next(i for i in range(360) if not inside[i])
    segments: list[list[int]] = []
    current: list[int] = []
    for offset in range(1, 361):
        i = (start + offset) % 360
        if inside[i]:
            current.append(i)
        elif current:
            segments.append(current)
            current = []
    if current:
        segments.append(current)
    found = []
    for segment in segments:
        depths = [radius - (profile[i] if profile[i] is not None else 0.0) for i in segment]
        deepest = max(depths)
        # Середина участка — круговое среднее углов.
        sx = sum(math.cos(math.radians(i)) for i in segment)
        sy = sum(math.sin(math.radians(i)) for i in segment)
        middle = math.degrees(math.atan2(sy, sx)) % 360.0
        span = len(segment)
        # Хорда лыски: глубина по закону s/cos — у края участка граница
        # подходит к окружности плавно; ширина хорды 2·√(R² − s²).
        chord = 2.0 * radius * math.sin(math.radians(span / 2.0))
        found.append(
            {
                "angle_deg": round(middle, 1),
                "depth_px": round(deepest, 1),
                "span_deg": span,
                "chord_px": round(chord, 1),
                "through": any(profile[i] is None for i in segment),
            }
        )
    return found


def refine_circle(ink: Any, cx: float, cy: float, radius: float) -> tuple[float, float, float]:
    """Центр и радиус обводки сечения (до середины штриха) — см. `_refine`."""
    return _refine(ink, cx, cy, radius)[:3]


def _refine(ink: Any, cx: float, cy: float, radius: float) -> tuple[float, float, float, float]:
    """Окружность обводки сечения — подгонкой по её точкам, с отбросом выбросов.

    Хаф по сечению с лыской или каналом уводит центр и радиус (лыска
    «сплющивает» круг): точки хорды и стенок канала далеко от окружности и
    отбрасываются; остальное — подгонка Касы, три круга. Точка — наружный
    край штриха (внутри к обводке подходит штриховка); радиус затем
    уменьшается на полтолщины линии — до середины штриха, как у размеров.
    """
    import numpy as np

    height, width = ink.shape
    thickness = 0.0
    for _round in range(3):
        band = max(3.0, 0.08 * radius)
        points, runs = [], []
        for tenth in range(720):
            angle = math.radians(tenth / 2.0)
            ux, uy = math.cos(angle), -math.sin(angle)
            best = None
            for step in np.arange(band, -band - 0.5, -0.5):
                x = int(round(cx + ux * (radius + step)))
                y = int(round(cy + uy * (radius + step)))
                if 0 <= y < height and 0 <= x < width and ink[y, x]:
                    best = (cx + ux * (radius + step), cy + uy * (radius + step), step)
                    break
            if best is None:
                continue
            points.append(best[:2])
            run, step = 0.0, best[2]
            while run < band:
                x = int(round(cx + ux * (radius + step - run - 0.5)))
                y = int(round(cy + uy * (radius + step - run - 0.5)))
                if not (0 <= y < height and 0 <= x < width and ink[y, x]):
                    break
                run += 0.5
            runs.append(run + 0.5)
        if len(points) < 40:
            break
        xs = np.array([p[0] for p in points], dtype=float)
        ys = np.array([p[1] for p in points], dtype=float)
        residual = np.abs(np.hypot(xs - cx, ys - cy) - radius)
        keep = residual <= max(2.0, np.percentile(residual, 70))
        xs, ys = xs[keep], ys[keep]
        a = np.column_stack([xs, ys, np.ones_like(xs)])
        b = xs**2 + ys**2
        solution, *_ = np.linalg.lstsq(a, b, rcond=None)
        cx, cy = solution[0] / 2.0, solution[1] / 2.0
        radius = float(math.sqrt(max(solution[2] + cx**2 + cy**2, 1.0)))
        thickness = float(np.median(runs))
    return float(cx), float(cy), float(radius - thickness / 2.0), thickness


def outline_gaps(ink: Any, cx: float, cy: float, radius: float) -> list[dict[str, Any]]:
    """Разрывы обводки сечения на окружности радиуса R: угол середины и хорда.

    Лыска срезает дугу — обводки нет на хорде 2·√(R² − s²); радиальное
    отверстие — на ширине своего канала. Лучи от центра этого не меряют:
    стенки канала и выносные линии размера у лыски перехватывают их раньше.
    Размерные линии только добавляют чернил и разрыв не укорачивают;
    перемычки короче 3° (выносная через разрыв) склеиваются.
    """
    height, width = ink.shape
    band = max(2.0, 0.02 * radius)
    present = []
    for tenth in range(3600):
        angle = math.radians(tenth / 10.0)
        ux, uy = math.cos(angle), -math.sin(angle)
        hit = False
        # Только к центру от середины штриха: выносные, касательные к
        # окружности снаружи (размер глубины лыски), разрыв не перекрывают.
        for offset in (-band, -band / 2, 0.0):
            x = int(round(cx + ux * (radius + offset)))
            y = int(round(cy + uy * (radius + offset)))
            if 0 <= y < height and 0 <= x < width and ink[y, x]:
                hit = True
                break
        present.append(hit)
    if all(present) or not any(present):
        return []
    for _round in range(2):
        runs = []
        start = 0
        for i in range(1, 3601):
            if i == 3600 or present[i % 3600] != present[start % 3600]:
                runs.append((start, i, present[start % 3600]))
                start = i
        for a, b, value in runs:
            if value and b - a < 30:
                for i in range(a, b):
                    present[i % 3600] = False
    first = next(i for i in range(3600) if present[i])
    gaps, current = [], []
    for offset in range(1, 3601):
        i = (first + offset) % 3600
        if not present[i]:
            current.append(i)
        elif current:
            gaps.append(current)
            current = []
    found = []
    for gap in gaps:
        if len(gap) < 15:
            continue
        sx = sum(math.cos(math.radians(i / 10.0)) for i in gap)
        sy = sum(math.sin(math.radians(i / 10.0)) for i in gap)
        span = len(gap) / 10.0
        found.append(
            {
                "angle_deg": round(math.degrees(math.atan2(sy, sx)) % 360.0, 1),
                "span_deg": span,
                "chord_px": round(2.0 * radius * math.sin(math.radians(span / 2.0)), 2),
            }
        )
    return found


def locate_sections(
    ink: Any,
    main_view_bbox: tuple[float, float, float, float],
    mm_per_px: float,
    step_diameters: list[float],
) -> list[dict[str, Any]]:
    """Сечения вала вне главного вида: заштрихованные круги радиуса ступени.

    Хаф по уменьшенному листу мелкие сечения (R ≈ 35 px на листе 1:4) не
    видит, а по сечению с лыской уводит центр. Здесь радиус известен (Ø
    ступени в масштабе вида): кандидаты — залитые пятна чернил, центр —
    перебором по контрасту «обводка на R есть, снаружи на R + δ пусто»
    (окружность внутри штриховки контраста не даёт), затем подгонка по обводке.
    """
    import cv2
    import numpy as np

    diameters = sorted({float(d) for d in step_diameters if d and d > 0})
    if not diameters or mm_per_px <= 0:
        return []
    mask = ink.astype(np.uint8)
    height, width = mask.shape
    closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    outside = closed.copy()
    cv2.floodFill(outside, np.zeros((height + 2, width + 2), np.uint8), (0, 0), 1)
    filled = closed | (1 - outside)
    r_min = diameters[0] / 2.0 / mm_per_px
    kernel = max(3, int(0.6 * r_min)) | 1
    opened = cv2.morphologyEx(
        filled, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel, kernel))
    )
    count, _labels, stats, _centres = cv2.connectedComponentsWithStats(opened)
    wide = cv2.dilate(mask, np.ones((3, 3), np.uint8)).astype(bool)
    angles = np.linspace(0.0, 2.0 * np.pi, 360, endpoint=False)
    cos, sin = np.cos(angles), np.sin(angles)

    def ring(cx: Any, cy: Any, r: float) -> Any:
        xs = np.clip(np.round(cx[..., None] + r * cos).astype(int), 0, width - 1)
        ys = np.clip(np.round(cy[..., None] - r * sin).astype(int), 0, height - 1)
        return wide[ys, xs].mean(axis=-1)

    x0, y0, x1, y1 = main_view_bbox
    found: list[dict[str, Any]] = []
    for index in range(1, count):
        bx, by, bw, bh, _area = (int(v) for v in stats[index])
        cx0, cy0 = bx + bw / 2.0, by + bh / 2.0
        if x0 <= cx0 <= x1 and y0 <= cy0 <= y1:
            continue
        best = None
        for diameter in diameters:
            r = diameter / 2.0 / mm_per_px
            # Половина сечения, разрезанного каналом, — пятно шириной от 1,2 R.
            if max(bw, bh) < 1.2 * r or min(bw, bh) > 3.5 * r:
                continue
            delta = max(3.0, 0.12 * r)
            # Центр — где угодно в пятне: сквозной канал режет сечение на две
            # половины, и центр оказывается у края каждой.
            xs = np.arange(bx, bx + bw + 1.0, 1.0)
            ys = np.arange(by, by + bh + 1.0, 1.0)
            if not xs.size or not ys.size:
                continue
            gx, gy = np.meshgrid(xs, ys)
            score = ring(gx, gy, r) - ring(gx, gy, r + delta)
            at = np.unravel_index(int(np.argmax(score)), score.shape)
            value = float(score[at])
            if best is None or value > best[0]:
                best = (value, float(gx[at]), float(gy[at]), r, diameter)
        if best is None or best[0] < 0.45:
            continue
        _score, cx, cy, r, diameter = best
        cx, cy, fitted, line = _refine(ink, cx, cy, r)
        if abs(fitted - r) > 0.08 * r:
            fitted = r
        # Ступень — по подогнанному радиусу: соседние Ø (20 и 22) перебор
        # по контрасту различает хуже, чем подгонка по обводке.
        diameter = min(diameters, key=lambda d: abs(d - 2.0 * fitted * mm_per_px))
        if any(math.hypot(cx - f["center_px"][0], cy - f["center_px"][1]) < r for f in found):
            continue
        found.append(
            {
                "center_px": [round(cx, 2), round(cy, 2)],
                "radius_px": round(fitted, 2),
                "step_diameter_mm": diameter,
                "contrast": round(best[0], 2),
                "line_px": round(line, 2),
            }
        )
    return found


def channel_width(
    ink: Any, cx: float, cy: float, radius: float, angle_deg: float, line: float
) -> float | None:
    """Ширина канала радиального отверстия на сечении (px), по его стенкам.

    Разрыв обводки у маленького отверстия закрывают стрелки и размерная
    линия его Ø; стенки канала внутри сечения видны чисто: поперёк канала —
    штриховка, стенка, пусто, стенка, штриховка. Ширина — между внутренними
    краями стенок плюс толщина линии (стенка нарисована по середине штриха).
    """
    import numpy as np

    height, width = ink.shape
    theta = math.radians(angle_deg)
    ux, uy = math.cos(theta), -math.sin(theta)
    nx, ny = -uy, ux
    offsets = np.arange(-0.45 * radius, 0.45 * radius + 0.25, 0.5)
    middle = int(np.argmin(np.abs(offsets)))
    widths = []
    # У поверхности: глухое отверстие к дну сужается конусом сверла.
    for t in np.linspace(0.55 * radius, 0.9 * radius, 12):
        xs = np.round(cx + ux * t + nx * offsets).astype(int)
        ys = np.round(cy + uy * t + ny * offsets).astype(int)
        inside = (xs >= 0) & (xs < width) & (ys >= 0) & (ys < height)
        if not inside.all():
            continue
        row = ink[ys, xs].copy()
        # Тонкие линии поперёк канала (размерная Ø, выноска) — не стенки:
        # стенка нарисована основной линией.
        start = None
        for position in range(len(row) + 1):
            if position < len(row) and row[position]:
                if start is None:
                    start = position
            elif start is not None:
                if (position - start) * 0.5 < 0.6 * line:
                    row[start:position] = False
                start = None
        if row[middle]:
            continue
        left = middle
        while left > 0 and not row[left - 1]:
            left -= 1
        right = middle
        while right < len(row) - 1 and not row[right + 1]:
            right += 1
        if left == 0 or right == len(row) - 1:
            continue
        # Середина стенки — по её собственному штриху: стенка канала бывает
        # толще обводки; слитая со штрихом штриховки — не толще 1,5 линии.
        walls = []
        for edge, direction in ((left - 1, -1), (right + 1, 1)):
            run = 0
            while 0 <= edge + direction * run < len(row) and row[edge + direction * run]:
                run += 1
            walls.append(min(run * 0.5, 1.5 * line))
        widths.append((offsets[right] - offsets[left]) + 0.5 + (walls[0] + walls[1]) / 2.0)
    if len(widths) < 4:
        return None
    return float(np.median(widths))


def _flat_edge(
    ink: Any, cx: float, cy: float, radius: float, angle_deg: float, half_chord: float
) -> list[tuple[float, float, float]]:
    """Точки наружного края хорды лыски (px листа) и толщина штриха внутрь.

    Снаружи к хорде вплотную стоит выносная размера глубины (касательная к
    исходной окружности), и луч снаружи ловит её. Поэтому изнутри: область
    сечения — обводка со штриховкой, залитая (замкнутые ячейки между
    штрихами); выносная отделена от хорды просветом и в область не входит.
    Лучи от центра по нормали к хорде идут до конца области.
    """
    import cv2
    import numpy as np

    height, width = ink.shape
    reach = int(1.3 * radius) + 4
    x0, y0 = max(0, int(cx) - reach), max(0, int(cy) - reach)
    x1, y1 = min(width, int(cx) + reach + 1), min(height, int(cy) + reach + 1)
    patch = cv2.morphologyEx(
        ink[y0:y1, x0:x1].astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8)
    )
    outside = np.pad(patch, 1)
    mask = np.zeros((outside.shape[0] + 2, outside.shape[1] + 2), np.uint8)
    cv2.floodFill(outside, mask, (0, 0), 1)
    region = patch.astype(bool) | ~outside[1:-1, 1:-1].astype(bool)
    theta = math.radians(angle_deg)
    ux, uy = math.cos(theta), -math.sin(theta)
    nx, ny = -uy, ux
    points = []
    for offset in np.linspace(-0.6 * half_chord, 0.6 * half_chord, 15):
        distance, last = 0.2 * radius, None
        while distance < 1.1 * radius:
            x = int(round(cx + ux * distance + nx * offset)) - x0
            y = int(round(cy + uy * distance + ny * offset)) - y0
            if not (0 <= y < region.shape[0] and 0 <= x < region.shape[1]) or not region[y, x]:
                break
            last = distance
            distance += 0.5
        if last is None or last >= 1.1 * radius - 0.5:
            continue
        # Штрих края внутрь: у хорды, слипшейся с соседней линией, он толще.
        run = 0.0
        while run < 0.5 * radius:
            x = int(round(cx + ux * (last - run - 0.5) + nx * offset))
            y = int(round(cy + uy * (last - run - 0.5) + ny * offset))
            if not (0 <= y < height and 0 <= x < width and ink[y, x]):
                break
            run += 0.5
        points.append((cx + ux * last + nx * offset, cy + uy * last + ny * offset, run + 0.5))
    return points


def flat_line(
    ink: Any, cx: float, cy: float, radius: float, angle_deg: float, half_chord: float, line: float
) -> tuple[float, float] | None:
    """Хорда лыски: угол её нормали (градусы, v вверх) и расстояние от центра (px).

    Середина разрыва обводки несимметрична (выносная закрывает один край),
    а хорда — прямая: нормаль — подгонкой прямой по точкам края, с отбросом
    дальних; расстояние — до середины штриха (на полтолщины ближе края).
    """
    import numpy as np

    points = _flat_edge(ink, cx, cy, radius, angle_deg, half_chord)
    if len(points) < 7:
        return None
    xs = np.array([p[0] for p in points]) - cx
    ys = np.array([p[1] for p in points]) - cy
    stroke = float(np.median([p[2] for p in points]))
    keep = np.ones(len(xs), dtype=bool)
    normal = None
    for _round in range(2):
        mx, my = xs[keep].mean(), ys[keep].mean()
        _u, _s, vt = np.linalg.svd(np.column_stack([xs[keep] - mx, ys[keep] - my]))
        normal = vt[1]
        if normal[0] * mx + normal[1] * my < 0:
            normal = -normal
        residual = np.abs((xs - mx) * normal[0] + (ys - my) * normal[1])
        keep = residual <= max(1.5, float(np.percentile(residual, 70)))
        if keep.sum() < 5:
            return None
    distance = float(np.median(xs[keep] * normal[0] + ys[keep] * normal[1]))
    angle = math.degrees(math.atan2(-normal[1], normal[0])) % 360.0
    # Штрих края много толще обводки — хорда слиплась с соседней линией
    # (выносной, дугой у мелкой лыски): какой край её, по листу не различить.
    # Правило «от внутреннего края» верно на одном листе и врёт на увеличенном
    # (лыска 1,3 мм при линии 1,2 мм: 2,4 вместо 1,3) — не измеримо.
    if stroke > 1.3 * line:
        return None
    return round(angle, 1), distance - line / 2.0


def _measure(
    ink: Any, disk: dict[str, Any], found: dict[str, Any], pocket: bool, mm_per_px: float
) -> dict[str, float]:
    """Замер фигуры у разрыва обводки: угол и глубина хорды или Ø канала."""
    cx, cy, r = disk["center_px"][0], disk["center_px"][1], disk["radius_px"]
    line = disk["line_px"]
    measured: dict[str, float] = {"angle_deg": found["angle_deg"]}
    if pocket:
        chord = flat_line(ink, cx, cy, r, found["angle_deg"], found["chord_px"] / 2.0, line)
        # Хорда обязана объяснять разрыв обводки: разрыв бывает короче
        # (выносные закрывают края), но хорда много глубже короткого
        # разрыва — это линии размеров поперёк лыски, а не её край.
        by_gap = r * math.cos(math.radians(found["span_deg"] / 2.0))
        if chord is not None and by_gap - chord[1] <= max(2.5 * line, 0.2 * r):
            measured["angle_deg"] = chord[0]
            measured["depth_mm"] = round((r - chord[1]) * mm_per_px, 2)
    else:
        size = channel_width(ink, cx, cy, r, found["angle_deg"], line)
        if size is not None:
            measured["diameter_mm"] = round(size * mm_per_px, 2)
    return measured


def _claimed(angle: float, disk: dict[str, Any], own: int, items: list) -> bool:
    """Разрыв под этим углом объясняет другой прочитанный элемент той же ступени.

    Тогда расхождение с прочитанным углом указывает на другой объект (E28) —
    «не измеримо»; разрыв, который не объясняет никто, — этот элемент, и
    прочитанный угол опровергается.
    """
    for index, item in items:
        if index == own:
            continue
        ox, oy, _oz = (float(v) for v in item["origin_mm"])
        if (
            abs(disk["step_diameter_mm"] - 2.0 * math.hypot(ox, oy)) > 0.5
            and item.get("kind") != "pocket"
        ):
            continue
        other = math.degrees(math.atan2(oy, ox)) % 360.0
        if _angle_gap(angle, other) <= _OTHER_OBJECT_DEG:
            return True
        if item.get("through") and _angle_gap(angle, other + 180.0) <= _OTHER_OBJECT_DEG:
            return True
    return False


def _angle_gap(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _radial_items(body: dict) -> list[tuple[int, dict]]:
    """Лыски и отверстия по размещению поперёк оси (ось элемента ⟂ оси детали)."""
    return [
        (index, item)
        for index, item in enumerate(body.get("placed_features") or [])
        if isinstance(item, dict)
        and item.get("kind") in ("hole", "pocket")
        and len(item.get("axis") or []) == 3
        and len(item.get("origin_mm") or []) == 3
        and abs(float(item["axis"][2])) < 0.2
    ]


def unmeasurable_placed(body: dict, reason: str) -> list[dict[str, Any]]:
    """Каждому элементу поперёк оси — «не измеримо» с причиной: ни одна
    гипотеза не выходит из стадии без вердикта (вид вала не найден, лист груб)."""
    return [
        {
            "kind": "placed_feature",
            "path": f"main_view.placed_features[{index}]",
            "feature_id": item.get("id"),
            "read": {},
            "status": "unmeasurable",
            "measured": {},
            "reason": reason,
        }
        for index, item in _radial_items(body)
    ]


def verify_placed_on_sections(
    gray: Any,
    main_view_bbox: tuple[float, float, float, float],
    mm_per_px: float,
    body: dict,
    *,
    line_px: float = 0.0,
) -> list[dict[str, Any]]:
    """Вердикты элементам по размещению поперёк оси — по сечениям листа.

    ``line_px`` — толщина основной линии главного вида: грубее 4,5 px (ниже
    250 dpi) — «не измеримо», как у ступеней, пазов и поперечных отверстий:
    на 200 dpi и шуме стрелки размеров сливаются со стенками канала и хордой.
    """
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _ink

    outer = [s for s in body.get("outer") or [] if isinstance(s, dict)]
    items = _radial_items(body)
    if not items or not outer:
        return []
    if 0.0 < line_px < _MIN_LINE_PX:
        return unmeasurable_placed(
            body,
            f"лист слишком грубый: основная линия {line_px:.1f} px (нужно от {_MIN_LINE_PX:g})",
        )
    gray = np.asarray(gray)
    ink = _ink(gray)
    diameters = [float(s["diameter_mm"]) for s in outer if s.get("diameter_mm")]
    disks = locate_sections(ink, main_view_bbox, mm_per_px, diameters)
    gaps = {
        id(disk): outline_gaps(ink, disk["center_px"][0], disk["center_px"][1], disk["radius_px"])
        for disk in disks
    }
    results = []
    for index, item in items:
        ox, oy, _oz = (float(v) for v in item["origin_mm"])
        radius_mm = math.hypot(ox, oy)
        read_angle = round(math.degrees(math.atan2(oy, ox)) % 360.0, 1)
        pocket = item.get("kind") == "pocket"
        key = "depth_mm" if pocket else "diameter_mm"
        entry = {
            "kind": "placed_feature",
            "path": f"main_view.placed_features[{index}]",
            # Стабильный id элемента — по нему вердикт находит узел в графе.
            "feature_id": item.get("id"),
            "read": {"angle_deg": read_angle, key: float(item.get(key) or 0.0)},
            "measured": {},
        }
        # Сечение ступени этого элемента: радиус лыски — до хорды, поэтому
        # ступень узнаётся по Ø, ближайшему к удвоенному радиусу размещения
        # сверху; сечений одной ступени бывает несколько — берётся то, где
        # у прочитанного угла есть разрыв обводки.
        if pocket:
            fits = [d for d in diameters if d >= 2.0 * radius_mm - 0.5]
            step = min(fits) if fits else None
        else:
            step = min(diameters, key=lambda d: abs(d - 2.0 * radius_mm))
            if abs(step - 2.0 * radius_mm) > 0.5:
                step = None
        candidates = sorted(
            (
                (_angle_gap(found["angle_deg"], read_angle), found, disk)
                for disk in disks
                if step is not None and abs(disk["step_diameter_mm"] - step) <= 0.01
                for found in gaps[id(disk)]
            ),
            key=lambda candidate: candidate[0],
        )
        if not candidates:
            results.append(
                {
                    **entry,
                    "status": "unmeasurable",
                    "reason": "сечения с этим элементом на листе не найдено",
                }
            )
            continue
        # Ближайший разрыв, у которого есть фигура своего вида: у отверстия —
        # канал, у лыски — хорда. Разрыв лыски рядом с прочитанным углом
        # отверстия (на той же ступени) — не отверстие.
        chosen = None
        for gap, found, disk in candidates:
            if gap > 90.0:
                break
            measured = _measure(ink, disk, found, pocket, mm_per_px)
            if key in measured:
                chosen = (found, disk, measured)
                break
        if chosen is None:
            _gap, found, disk = candidates[0]
            chosen = (found, disk, {"angle_deg": found["angle_deg"]})
        found, disk, measured = chosen
        cx, cy, r = disk["center_px"][0], disk["center_px"][1], disk["radius_px"]
        line = disk["line_px"]
        box = [round(cx - r, 1), round(cy - r, 1), round(cx + r, 1), round(cy + r, 1)]
        gap = _angle_gap(measured["angle_deg"], read_angle)
        if gap > _OTHER_OBJECT_DEG and _claimed(measured["angle_deg"], disk, index, items):
            results.append(
                {
                    **entry,
                    "status": "unmeasurable",
                    "measured": measured,
                    "evidence_bbox_px": box,
                    "reason": "на сечении элемент под другим углом — вероятно, не этот",
                }
            )
            continue
        problems = []
        if gap > _ANGLE_TOLERANCE:
            problems.append(f"угол {measured['angle_deg']:g}°, прочитано {read_angle:g}°")
        # Допуск размера: замер по стенкам и хорде точен до ±0,65 мм на листе
        # 1:4 (штрих ≈ 1,4 мм) — допуск в полтолщины линии, не меньше 0,8 мм.
        tolerance = max(0.8, 0.5 * line * mm_per_px)
        entry["tolerance_mm"] = {
            "depth" if pocket else "diameter": round(tolerance, 3),
            "angle": _ANGLE_TOLERANCE,
        }
        name = "глубина лыски" if pocket else "Ø"
        if key in measured and abs(measured[key] - entry["read"][key]) > tolerance:
            problems.append(f"{name} {measured[key]:g} мм, прочитано {entry['read'][key]:g}")
        reason = "; ".join(problems)
        # Без фигуры своего вида угол по середине разрыва не доказательство:
        # разрыв лыски несимметричен (выносные закрывают край) — до 15° на
        # корпусе, а разрыв без канала — не отверстие.
        if key not in measured:
            results.append(
                {
                    **entry,
                    "status": "unmeasurable",
                    "measured": measured,
                    "evidence_bbox_px": box,
                    "reason": f"{name} на сечении не измерить — линии размеров закрывают фигуру",
                }
            )
            continue
        results.append(
            {
                **entry,
                "status": "refuted" if problems else "confirmed",
                "measured": measured,
                "evidence_bbox_px": box,
                "reason": reason,
            }
        )
    return results
