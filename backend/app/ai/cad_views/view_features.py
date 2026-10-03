"""Этап D3: элементы тела вращения по второму виду — отличия вида от профиля.

Вид сбоку тела вращения — силуэт его наружного профиля. Где силуэт уже
профиля, симметрично оси, — лыски: материал срезан с двух сторон до
ширины силуэта («Опора пружин»: хвостовик до 5 — вилка). Окружность внутри
силуэта с центром на оси — сквозное отверстие вдоль направления взгляда
(Ø5 и Ø1,5). Вид совмещается с профилем по торцам (проекционная связь).

Система детали: ось Z от левого торца, Y — направление взгляда этого вида
(в него уходят отверстия), X — поперёк вида (лыски срезают по X).
"""

from __future__ import annotations

import math
from typing import Any

from app.ai.cad_views.revolve_profile import HalfProfile


def _silhouette(ink: Any, axis_y: int, line: float) -> list[float | None]:
    import cv2
    import numpy as np

    horizontal = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN, np.ones((1, max(3, int(3 * line))), np.uint8)
    )
    half: list[float | None] = []
    for x in range(horizontal.shape[1]):
        edges = np.diff(np.concatenate([[0], horizontal[:, x], [0]]))
        # Только основные линии: размерные линии Ø тоже симметричны оси и
        # давали ложные «бурты» до 9 мм шириной (z4-r4: Ø37, Ø40, Ø44).
        centres = [
            (a + b - 1) / 2.0
            for a, b in zip(np.where(edges == 1)[0], np.where(edges == -1)[0])
            if b - a >= 0.6 * line
        ]
        up = [axis_y - c for c in centres if c < axis_y - line]
        down = [c - axis_y for c in centres if c > axis_y + line]
        pairs = [r for r in up if any(abs(r - q) <= 1.5 * line for q in down)]
        if not pairs and up and down:
            # Лыска или паз с одной стороны: пары нет, а кромка напротив
            # ниже, но не глубже половины радиуса. Без этого столбцы лыски
            # пропускались, и силуэт соединял соседние ступени конусом
            # (многоосевой вал 5: Ø35 → Ø28 скосом на 20 мм).
            outer_up, outer_down = max(up), max(down)
            radius, other = max(outer_up, outer_down), min(outer_up, outer_down)
            if 0.5 * radius <= other < radius - 1.5 * line:
                pairs = [radius]
        half.append(max(pairs) if pairs else None)
    return half


def _circles(
    ink: Any,
    line: float,
    *,
    threshold: int = 18,
    radius: tuple[float, float] | None = None,
    min_radius_lines: float = 3.0,
) -> list[tuple[float, float, float]]:
    """Окружности (центр x, y, радиус по середине штриха).

    Центры — Хафом; контуром нельзя: центровые линии перечёркивают
    окружность, и контур перестаёт быть замкнутым. Радиус Хафа неточен —
    уточняется по кольцу чернил: радиус, на котором окружность покрыта
    чернилами наибольшей долей (не меньше 0,7)."""
    import cv2
    import numpy as np

    image = ((1 - ink) * 255).astype(np.uint8)
    found = cv2.HoughCircles(
        cv2.GaussianBlur(image, (5, 5), 0),
        cv2.HOUGH_GRADIENT,
        dp=1,
        minDist=3 * line,
        param1=120,
        param2=threshold,
        minRadius=int(radius[0]) if radius else int(1.5 * line),
        maxRadius=int(radius[1]) + 1 if radius else int(40 * line),
    )
    if found is None:
        return []
    angles = np.radians(np.arange(0, 360, 2))
    height, width = ink.shape
    out: list[tuple[float, float, float]] = []
    # С мягким порогом Хаф отдаёт тысячи кандидатов (по убыванию голосов):
    # лучшие 200 и уточнение радиуса в заданном окне — иначе минуты на лист.
    for cx, cy, r0 in found[0][:200]:
        low, high = (radius[0], radius[1]) if radius else (max(line, 0.5 * r0), 1.5 * r0 + line)
        radii = np.arange(low, high, 0.5)
        shares = []
        for r in radii:
            xs = np.clip(np.round(cx + r * np.cos(angles)).astype(int), 0, width - 1)
            ys = np.clip(np.round(cy + r * np.sin(angles)).astype(int), 0, height - 1)
            shares.append(float(ink[ys, xs].mean()))
        best = (0.0, None)
        if shares:
            top = max(shares)
            # Толстая обводка даёт плато максимума шириной в штрих — середина
            # плато и есть середина линии (первый максимум — внутренний край).
            peak = int(np.argmax(shares))
            left, right = peak, peak
            while left > 0 and shares[left - 1] >= top - 0.02:
                left -= 1
            while right < len(shares) - 1 and shares[right + 1] >= top - 0.02:
                right += 1
            best = (top, float(radii[left] + radii[right]) / 2.0)
        if best[1] is None or best[0] < 0.7 or best[1] < min_radius_lines * line:
            continue
        if any(
            math.hypot(cx - ox, cy - oy) < line and abs(best[1] - orad) < 2 * line
            for ox, oy, orad in out
        ):
            continue
        out.append((float(cx), float(cy), best[1]))
    return out


def snap(value: float, labels: list[float], share: float = 0.12) -> float:
    """Номинал — из надписи, если замер её объясняет (ближайшая в пределах доли)."""
    near = min(labels, key=lambda v: abs(v - value), default=None)
    return near if near is not None and abs(near - value) <= share * near else value


def side_view_features(
    view_gray: Any,
    profile: HalfProfile,
    axial_mm_per_px: float,
    radial_mm_per_px: float,
    diameter_labels: list[float] | None = None,
    linear_labels: list[float] | None = None,
) -> list[dict[str, Any]]:
    """Элементы по размещению (как `placed_features` спека) по второму виду."""
    import numpy as np

    from app.ai.cad_recognize.keyway_standard import standard_section
    from app.ai.cad_views.section_material import ink_mask, symmetry_axis

    line = profile.line_px
    ink = ink_mask(view_gray, line)
    import cv2

    horizontal = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN, np.ones((1, max(3, int(3 * line))), np.uint8)
    )
    axis_y = symmetry_axis(horizontal)
    half = _silhouette(ink, axis_y, line)
    present = [x for x, h in enumerate(half) if h is not None]
    if not present:
        return []
    # Проекционная связь: вид в том же масштабе, что профиль, — ищется только
    # сдвиг вдоль оси, при котором силуэт лучше всего совпадает с профилем.
    xs_p = np.arange(profile.x0, profile.x1 + 1)
    expected = np.interp(xs_p, [x for x, _r in profile.outer], [r for _x, r in profile.outer])
    silhouette = np.array([np.nan if h is None else h for h in half], dtype=float)
    best = (np.inf, 0)
    for shift in range(-len(half), len(half)):
        cols = xs_p + shift
        inside = (cols >= 0) & (cols < len(half))
        if inside.sum() < 0.6 * len(xs_p):
            continue
        values = silhouette[cols[inside]]
        known = ~np.isnan(values)
        if known.sum() < 0.5 * len(xs_p):
            continue
        # Совпадение — доля столбцов, где силуэт равен профилю (лыски и
        # отверстия — отличия, их не штрафуем сверх доли).
        match = np.abs(values[known] - expected[inside][known]) <= 1.5 * line
        cost = -match.mean() * known.sum() / len(xs_p)
        if cost < best[0]:
            best = (cost, shift)
    shift = best[1]
    v0, v1 = profile.x0 + shift, profile.x1 + shift

    def z_of(x: float) -> float:
        return (x - v0) * axial_mm_per_px

    xs = [x for x, _r in profile.outer]
    rs = [r for _x, r in profile.outer]

    def outer_at(z: float) -> float:
        x = profile.x0 + z / axial_mm_per_px
        return float(np.interp(x, xs, rs)) * radial_mm_per_px

    features: list[dict[str, Any]] = []
    # Лыски: силуэт уже профиля на 0,2 мм и более, участок не короче 3 линий.
    tolerance = max(0.2, 2 * line * radial_mm_per_px)
    run: list[tuple[float, float]] = []

    def flush() -> None:
        if len(run) * 1.0 < 3 * line:
            run.clear()
            return
        z0, z1 = run[0][0], run[-1][0]
        width = snap(float(np.median([w for _z, w in run])), linear_labels or [])
        radius = max(outer_at(z) for z, _w in run)
        for sign in (1.0, -1.0):
            features.append(
                {
                    "kind": "pocket",
                    "profile": "rectangle",
                    "origin_mm": [sign * radius, 0.0, round((z0 + z1) / 2, 3)],
                    "axis": [-sign, 0.0, 0.0],
                    "ref": [0.0, 0.0, 1.0],
                    "width_mm": round(z1 - z0, 3),
                    "height_mm": round(2 * radius + 1.0, 3),
                    "depth_mm": round(radius - width / 2, 3),
                    "source": "лыски по второму виду",
                }
            )
        run.clear()

    for x in range(max(0, v0), min(len(half) - 1, v1) + 1):
        h = half[x]
        z = z_of(x)
        if h is None:
            continue
        width = 2 * h * radial_mm_per_px
        if width < 2 * outer_at(z) - tolerance:
            run.append((z, width))
        elif run:
            flush()
    if run:
        flush()
    # Отверстия: окружность в силуэте с центром на оси.
    # Мелкое отверстие (Ø1,5 «Опоры» — 19 px при линии 7,6) — от двух толщин.
    # Дуги паза узкие (паз 5 мм на листе 1:2,5 — радиус 1,8 линии):
    # окружности — от полутора линий, отверстия ниже — от двух.
    circles = [
        (cx, cy, r)
        for cx, cy, r in _circles(ink, line, min_radius_lines=1.5)
        if v0 <= cx <= v1 and abs(cy - axis_y) <= max(2 * line, 0.1 * r)
    ]
    # Шпоночный паз лицом: пара параллельных основных линий, симметричных
    # оси, строго внутри ступени, закрытая дугами — вершины дуг лежат на оси
    # за концами прямых. Это капсула, а не отверстия: вал shaft-7 строил паз
    # 8×33 двумя сквозными Ø8 по дугам концов, shaft-6 пазов не видел вовсе.
    used: set[int] = set()
    # Прямые паза — основные линии: тонкие линии резьбы (внутренний Ø) тоже
    # пара, симметричная оси, внутри ступени.
    from app.ai.cad_views.extrude_body import main_line_mask

    _all, thick_lines, _l = main_line_mask(view_gray)
    thick_horizontal = cv2.morphologyEx(
        thick_lines, cv2.MORPH_OPEN, np.ones((1, max(3, int(3 * line))), np.uint8)
    )
    for a, b, radius in _keyway_runs(
        thick_horizontal, ink, thick_lines, axis_y, line, v0, v1, xs, rs
    ):
        # Концы капсулы — вершины дуг.
        z0, z1 = z_of(a), z_of(b)
        # Ø ступени — номинал надписи: сырой замер 22,3 при Ø22 выбирал
        # сечение ГОСТ из диапазона 22–30 (8×4 вместо 6×3,5, shaft-6).
        # Ø ступени — медиана по длине паза: в середине паза контур
        # «вспухает» следом самого паза (z4-r4: 31,2 при Ø30 — сечение ГОСТ
        # 10×5 вместо 8×4).
        samples = sorted(2 * outer_at(z0 + (z1 - z0) * k / 20.0) for k in range(21))
        step = snap(samples[10], diameter_labels or [], share=0.03)
        width = 2 * radius * radial_mm_per_px
        standard = standard_section(step)
        depth = None
        if standard is not None and abs(standard[0] - width) <= 0.2 * standard[0]:
            width, depth = standard
        if depth is None:
            depth = round(0.5 * width, 3)
        features.append(
            {
                "kind": "pocket",
                "profile": "slot",
                # Вход с поверхности ступени, в глубину к оси; паз лицом к
                # наблюдателю вида — на −X, как вырезы пазов на сечениях.
                "origin_mm": [round(-step / 2, 3), 0.0, round((z0 + z1) / 2, 3)],
                "axis": [1.0, 0.0, 0.0],
                "ref": [0.0, 0.0, 1.0],
                # Длина — ближайшая надпись в пределах толщины линии (конец
                # дуги у уступа уезжает на линию: 13,71 при «13», shaft-6).
                "width_mm": round(
                    snap(
                        z1 - z0,
                        linear_labels or [],
                        share=max(0.05, line * axial_mm_per_px / max(z1 - z0, 1e-6)),
                    ),
                    3,
                ),
                "height_mm": round(width, 3),
                "depth_mm": round(depth, 3),
                "keyway": [round(z0, 3), round(z1, 3)],
                "source": "шпоночный паз лицом",
            }
        )
        # Дуги концов паза — не отверстия.
        for index, (cx, _cy, r) in enumerate(circles):
            if a - 2 * radius <= cx <= b + 2 * radius and abs(r - radius) <= max(
                line, 0.3 * radius
            ):
                used.add(index)
    for index, (cx, cy, r) in enumerate(circles):
        if index in used or r < 2.0 * line:
            continue
        z = z_of(cx)
        diameter = 2 * r * radial_mm_per_px  # радиус уже по середине штриха
        if diameter <= 0 or diameter >= 2 * outer_at(z):
            continue
        features.append(
            {
                "kind": "hole",
                # Вход снаружи детали: сквозное строится от точки входа.
                "origin_mm": [0.0, round(max(rs) * radial_mm_per_px + 0.5, 3), round(z, 3)],
                "axis": [0.0, -1.0, 0.0],
                "ref": [0.0, 0.0, 1.0],
                "diameter_mm": round(snap(diameter, diameter_labels or []), 3),
                "through": True,
                "source": "окружность на втором виде",
            }
        )
    return features


def _bridge_occluded(
    runs: list[tuple[int, int, float]],
    per_column: list[list[float]],
    start: int,
    line: float,
    xs: list[float],
    rs: list[float],
) -> list[tuple[int, int, float]]:
    """Куски прямых одного паза, разорванные подписью Ø и стрелками поверх
    паза (shaft-6: «Ø22» со стрелками посреди паза 83…96 — прямые распались
    на куски короче ширины, паз не строился, а вырез на сечении стал
    сквозным отверстием). Склеиваются куски одной полуширины на одной
    ступени с разрывом не длиннее ширины паза, каждый не короче толщины линии,
    если прямые видны хотя бы на 40 % склеенного участка (обрывок у уступа
    той же полуширины уводил начало паза shaft-4 на 6 мм)."""
    import numpy as np

    merged: list[tuple[int, int, float]] = []
    for a, b, radius in sorted(runs):
        previous = next(
            (
                index
                for index in range(len(merged) - 1, -1, -1)
                if abs(merged[index][2] - radius) <= 0.5 * line
            ),
            None,
        )
        if previous is not None:
            pa, pb, pr = merged[previous]
            span = range(int(pa), int(b) + 1)
            level = [float(np.interp(x, xs, rs)) for x in span]
            seen = sum(
                1
                for x in span
                if 0 <= x - start < len(per_column)
                and any(abs(r - pr) <= line for r in per_column[x - start])
            )
            if (
                0 < a - pb <= 2.0 * pr
                and min(pb - pa, b - a) >= line
                and max(level) - min(level) <= 1.5 * line
                and seen >= 0.4 * len(span)
            ):
                merged[previous] = (pa, max(pb, b), pr)
                continue
        merged.append((a, b, radius))
    return merged


def _keyway_runs(
    horizontal: Any,
    ink: Any,
    thick: Any,
    axis_y: int,
    line: float,
    v0: int,
    v1: int,
    xs: list[float],
    rs: list[float],
) -> list[tuple[int, int, float]]:
    """Прямые участки пазов лицом: (x начала, x конца, полуширина), px."""
    import numpy as np

    height, width = horizontal.shape[:2]
    per_column: list[list[float]] = []
    for x in range(max(0, v0), min(width, v1 + 1)):
        edges = np.diff(np.concatenate([[0], horizontal[:, x], [0]]))
        centres = [
            (a + b - 1) / 2.0
            for a, b in zip(np.where(edges == 1)[0], np.where(edges == -1)[0])
            if b - a >= 0.6 * line
        ]
        outer = float(np.interp(x, xs, rs))
        up = [axis_y - c for c in centres if line < axis_y - c < outer - 1.5 * line]
        down = [c - axis_y for c in centres if line < c - axis_y < outer - 1.5 * line]
        per_column.append([r for r in up if any(abs(r - q) <= line for q in down)])
    runs: list[tuple[int, int, float]] = []
    start = max(0, v0)
    open_runs: dict[float, list[int]] = {}
    for offset, radii in enumerate(per_column):
        x = start + offset
        seen = set()
        for r in radii:
            # Поллинии: рядом с прямыми паза (±28 px) идут штриховые
            # невидимой расточки (±22 px) — сливаясь, паз получал её ширину.
            key = next((k for k in open_runs if abs(k - r) <= 0.5 * line), None)
            if key is None:
                open_runs[r] = [x, x]
                key = r
            elif x - open_runs[key][1] <= line:
                open_runs[key][1] = x
            else:
                runs.append((open_runs[key][0], open_runs[key][1], key))
                open_runs[key] = [x, x]
            seen.add(key)
        for key in [k for k in open_runs if k not in seen and x - open_runs[k][1] > line]:
            runs.append((open_runs[key][0], open_runs[key][1], key))
            del open_runs[key]
    runs += [(a, b, k) for k, (a, b) in open_runs.items()]
    runs = _bridge_occluded(runs, per_column, start, line, xs, rs)

    def arc_end(x_end: int, radius: float, sign: int) -> int | None:
        """Крайний столбец дуги; если за концом прямых дуги нет — поиск на
        радиус раньше: маска основных линий продлевает прямые по пологой
        вершине дуги, и у короткого паза конец «прямых» уже за вершиной
        (shaft-6: паз 83…96 — прямые до 572 px при вершине ≈ 565)."""
        found = _arc_from(x_end, radius, sign)
        if found is None:
            found = _arc_from(x_end - sign * int(radius), radius, sign, lead=2.0)
        return found

    def _arc_from(x_end: int, radius: float, sign: int, lead: float = 1.0) -> int | None:
        """Крайний столбец дуги конца паза: основные линии в полосах выше и
        ниже оси (осевая по самой оси не считается) без разрыва от конца
        прямых и не дальше своей ступени."""
        # Полосы — от осевой почти до самих прямых: дуга выходит из них
        # сразу за концом прямых, без разрыва.
        # Осевая — тонкая, в основные линии не входит: полоса — почти от оси.
        reach = max(2.0, radius - 0.8 * line)
        rows = [
            (int(axis_y - reach), int(axis_y - 1)),
            (int(axis_y + 1), int(axis_y + reach)),
        ]
        level = float(np.interp(x_end, xs, rs))
        previous = level
        last = None
        gap = 0
        for step in range(int((0.8 + lead) * radius) + 1):
            x = x_end + sign * step
            if not 0 <= x < width:
                break
            # У уступа паз кончается (shaft-4: подпись «Ø25» поверх паза
            # склеивала дугу с двойной линией уступа). Уступ в профиле —
            # скачок в один столбец; плавный подъём — погрешность профиля у
            # канавки при уступе (shaft-6: 97→99,6 мм), дугу он не обрывает.
            here = float(np.interp(x, xs, rs))
            if abs(here - previous) > 0.5 * line or abs(here - level) > max(line, radius):
                break
            previous = here
            # Дуга — основная линия: тонкие выносные размеров пересекают
            # полосу у концов паза и продлевали его.
            hit = any(thick[max(0, a) : max(0, b) + 1, x].any() for a, b in rows)
            if hit:
                last, gap = x, 0
            else:
                gap += 1
                # До первой дуги — до радиуса: у узкого паза дуга входит в
                # полосу поиска не сразу за концом прямых (shaft-6, паз 6 мм).
                if gap > (line if last is not None else lead * radius):
                    break
        return last

    found = []
    for a, b, radius in runs:
        # Прямые не короче ширины паза; концы закрыты дугами.
        if b - a < 2 * radius or radius < 1.0 * line:
            continue
        # Паз — в одной ступени: наружный контур вдоль него постоянен
        # (расточка разреза тянется через все ступени).
        along = [float(np.interp(x, xs, rs)) for x in range(int(a), int(b) + 1)]
        if max(along) - min(along) > 1.5 * line:
            continue
        left, right = arc_end(a, radius, -1), arc_end(b, radius, 1)
        if left is None or right is None:
            continue
        # Между прямыми — пусто (не расточка со штриховкой): доля чернил
        # по середине полосы мала.
        # Столбцы, где прямые закрыты подписью или стрелками, в счёт не идут;
        # у дуг — тоже (маска основных линий продлевает прямые до вершины
        # дуги, а дуга проходит через середину полосы).
        seen_columns = [
            x
            for x in range(max(int(a), int(left + radius)), min(int(b), int(right - radius)) + 1)
            if 0 <= x - start < len(per_column)
            and any(abs(r - radius) <= line for r in per_column[x - start])
        ]
        middle = ink[int(axis_y - radius / 2) : int(axis_y + radius / 2) + 1, seen_columns]
        # Штриховка ложится на все столбцы поровну, подпись Ø внутри паза —
        # кучно (shaft-4: «Ø25» в пазу — среднее 0,26, медиана 0,19).
        if middle.size and middle.mean() > 0.25 and float(np.median(middle.mean(axis=0))) > 0.25:
            continue
        # Концы — по середине линии дуги; паз длиннее полутора ширин.
        left, right = int(left + line / 2), int(right - line / 2)
        if right - left < 3 * radius:
            continue
        found.append((left, right, float(radius)))
    return found
