"""Этап C2: замеры → номиналы надписей, как при перечерчивании инженером.

Тело, построенное по замерам, несёт погрешность листа (89,75 вместо 90,
отверстие в 79,99; 89,02 вместо 80; 90). Инженер перечерчивает деталь в
номиналах: координата по одной оси — это цепочка размеров от базы. Решатель
идёт так же: известны база (0) и габарит; неизвестная координата, которую
объясняет «известная ± надпись» в пределах допуска листа, получает номинал и
сама становится известной; и так, пока что-то меняется. Координата, которую
не объясняет ни одна надпись, остаётся замером — номинал не выдумывается.
"""

from __future__ import annotations

import math


def snap_axis(
    values: list[float],
    labels: list[float],
    *,
    tolerance: float,
    overall: float | None = None,
    chain: bool = False,
) -> dict[float, float]:
    """{замер: номинал} для координат одной оси (от базы 0).

    ``overall`` — габарит по этой оси, если его объясняет надпись: крайняя
    координата получает его первой.

    ``chain`` — координаты образуют цепочку (станции вала): звено соединяет
    СОСЕДНИЕ станции. Сначала — от ближайших известных слева и справа, и
    первым фиксируется самое короткое звено; только когда так ничего не
    объясняется — от любой известной (размер от базы). Без этого звено
    перескакивало станцию: «18» от 115 давало 97 вместо уступа 100, и от
    неверной 97 − 18 = 79 съезжала вся цепочка (shaft-6).
    """
    # Звено цепочки — размер заметно больше допуска: мелкие надписи (фаски,
    # канавки 1,5…3) притягивали станции к любым своим суммам.
    positive = sorted({v for v in labels if v >= 3 * tolerance})
    pending = sorted({round(v, 6) for v in values})
    snapped: dict[float, float] = {}
    known: list[float] = [0.0]
    for value in pending:
        if abs(value) <= tolerance:
            snapped[value] = 0.0
    if overall is not None and pending:
        far = max(pending, key=abs)
        if abs(abs(far) - overall) <= tolerance:
            snapped[far] = overall if far > 0 else -overall
            known.append(snapped[far])
    changed = True
    while chain and changed:
        changed = False
        best_pair: tuple[float, float, float, float] | None = None
        for value in pending:
            if value in snapped:
                continue
            lower = [k for k in known if k <= value]
            upper = [k for k in known if k >= value]
            bases = ([max(lower)] if lower else []) + ([min(upper)] if upper else [])
            for base in bases:
                # Вторая точка того же уступа (край канавки у уступа, наклон
                # замера) — та же станция.
                if abs(value - base) <= tolerance:
                    pair = (abs(value - base), 0.0, value, base)
                    if best_pair is None or pair < best_pair:
                        best_pair = pair
                for label in positive:
                    for candidate in (base + label, base - label):
                        gap = abs(candidate - value)
                        if gap <= tolerance:
                            pair = (abs(value - base), gap, value, candidate)
                            if best_pair is None or pair < best_pair:
                                best_pair = pair
        if best_pair is not None:
            _reach, _gap, value, candidate = best_pair
            snapped[value] = round(candidate, 4)
            known.append(snapped[value])
            changed = True
    changed = True
    while changed:
        changed = False
        for value in pending:
            if value in snapped:
                continue
            best: tuple[float, float] | None = None
            for base in known:
                for label in positive:
                    for candidate in (base + label, base - label):
                        gap = abs(candidate - value)
                        if gap <= tolerance and (best is None or gap < best[0]):
                            best = (gap, candidate)
            if best is not None:
                snapped[value] = round(best[1], 4)
                known.append(snapped[value])
                changed = True
    return snapped


def snap_diameter(value: float, diameters: list[float], share: float = 0.03) -> float:
    """Ø площадки или отверстия — номинал надписи в пределах доли.

    Из нескольких надписей в допуске целый номинал важнее дробного: Ø дна
    канавки с выносного элемента (Ø49,5, Ø24,5, Ø21,7 — d − 2t) ближе к
    замеру, чем номинал ступени Ø50, и ступени вала (ГОСТ 6636 — целые
    миллиметры) привязывались к канавкам (реальные p007, z4-r4)."""
    near = [d for d in diameters if abs(d - value) <= share * d]
    if not near:
        return value
    whole = [d for d in near if d >= 10.0 and abs(d - round(d)) <= 1e-6]
    if whole and len(whole) < len(near):
        return min(whole, key=lambda d: abs(d - value))
    return min(near, key=lambda d: abs(d - value))


def nominal_sketch(
    segments: list[dict],
    holes: list[dict],
    linear: list[float],
    *,
    tolerance: float,
) -> tuple[list[dict], list[dict], int]:
    """Эскиз выдавливания и центры отверстий — в номиналах (число правок).

    В номиналы приводятся только координаты вершин прямых участков вдоль
    осей и центры отверстий: точки ломаной дуги к надписям не относятся, и
    цепочка мелких надписей (2, 4, 10) притягивала их куда угодно (дуга R25
    планки становилась ступеньками).
    """
    segments = _square_corners(segments, tolerance)
    points = [(0.0, 0.0)] + [(float(s["to"][0]), float(s["to"][1])) for s in segments]
    snap_x: set[int] = set()
    snap_y: set[int] = set()
    for index, (a, b) in enumerate(zip(points, points[1:])):
        if segments[index].get("kind") != "line":
            continue
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        # Вдоль оси — наклон меньше 2° (замер кромки листа не идеален).
        if dx <= 0.035 * dy and dy > tolerance:
            snap_x.update({index, index + 1})
        elif dy <= 0.035 * dx and dx > tolerance:
            snap_y.update({index, index + 1})
    xs = [points[i][0] for i in sorted(snap_x)] + [float(h["center_x_mm"]) for h in holes]
    ys = [points[i][1] for i in sorted(snap_y)] + [float(h["center_y_mm"]) for h in holes]
    all_x = [p[0] for p in points]
    all_y = [p[1] for p in points]
    width = max(all_x) - min(all_x) if all_x else 0.0
    height = max(all_y) - min(all_y) if all_y else 0.0
    labels = sorted(set(linear))

    def overall(extent: float) -> float | None:
        near = min(labels, key=lambda v: abs(v - extent), default=None)
        return near if near is not None and abs(near - extent) <= tolerance else None

    total_x, total_y = overall(width), overall(height)
    map_x = snap_axis(xs, labels, tolerance=tolerance, overall=total_x)
    map_y = snap_axis(ys, labels, tolerance=tolerance, overall=total_y)
    changed = 0
    low_x, high_x = min(all_x), max(all_x)
    low_y, high_y = min(all_y), max(all_y)

    def extreme(value: float, low: float, high: float, total: float | None) -> float | None:
        # Деталь не выходит за габарит: вершина у его крайней линии лежит на ней.
        if total is None:
            return None
        if abs(value - high) <= tolerance and abs(high - low - total) <= tolerance:
            return total if abs(low) <= tolerance else high
        return None

    def fix(value: float, mapping: dict[float, float], allowed: bool) -> float:
        nonlocal changed
        if not allowed:
            return value
        new = mapping.get(round(value, 6), value)
        if abs(new - value) > 1e-6:
            changed += 1
        return new

    out_segments = []
    for index, segment in enumerate(segments, start=1):
        item = dict(segment)
        x, y = segment["to"]
        new_x = fix(float(x), map_x, index in snap_x)
        new_y = fix(float(y), map_y, index in snap_y)
        edge_x = extreme(new_x, low_x, high_x, total_x)
        edge_y = extreme(new_y, low_y, high_y, total_y)
        if edge_x is not None and abs(edge_x - new_x) > 1e-6:
            new_x, changed = edge_x, changed + 1
        if edge_y is not None and abs(edge_y - new_y) > 1e-6:
            new_y, changed = edge_y, changed + 1
        item["to"] = [new_x, new_y]
        out_segments.append(item)
    out_holes = []
    for hole in holes:
        item = dict(hole)
        item["center_x_mm"] = fix(float(hole["center_x_mm"]), map_x, True)
        item["center_y_mm"] = fix(float(hole["center_y_mm"]), map_y, True)
        out_holes.append(item)
    return out_segments, out_holes, changed


def _square_corners(segments: list[dict], tolerance: float) -> list[dict]:
    """Короткое ребро между двумя длинными сторонами вдоль разных осей —
    скруглённый размыканием прямой угол: вершины заменяются пересечением."""
    points = [(0.0, 0.0)] + [(float(s["to"][0]), float(s["to"][1])) for s in segments]
    closed = points[:-1] if points[-1] == points[0] else points
    count = len(closed)
    if count < 5:
        return segments

    def axis(a: tuple[float, float], b: tuple[float, float]) -> str | None:
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        if dy <= 0.035 * dx and dx > 3 * tolerance:
            return "h"
        if dx <= 0.035 * dy and dy > 3 * tolerance:
            return "v"
        return None

    merged: dict[int, tuple[float, float]] = {}
    drop: set[int] = set()
    for i in range(count):
        a, b = closed[i], closed[(i + 1) % count]
        if abs(b[0] - a[0]) + abs(b[1] - a[1]) > 2.5 * tolerance:
            continue
        before = axis(closed[(i - 1) % count], a)
        after = axis(b, closed[(i + 2) % count])
        if {before, after} != {"h", "v"}:
            continue
        corner = (a[0], b[1]) if before == "v" else (b[0], a[1])
        if (i + 1) % count == 0:
            continue  # начало эскиза (0, 0) не сдвигается
        merged[i] = corner
        drop.add((i + 1) % count)
    if not merged:
        return segments
    out_points = []
    for i, point in enumerate(closed):
        if i in drop:
            continue
        out_points.append(merged.get(i, point))
    if out_points[0] != (0.0, 0.0):
        return segments
    rebuilt = [{"kind": "line", "to": [round(x, 4), round(y, 4)]} for x, y in out_points[1:]]
    rebuilt.append({"kind": "line", "to": [0.0, 0.0]})
    return rebuilt


def nominal_round_holes(
    diameter: float, holes: list[dict], circles: list[float], *, tolerance: float
) -> tuple[list[dict], int]:
    """Круглая деталь (эскиз от самой левой точки): центральное отверстие —
    в центр, остальные — на окружность центров из надписи; N отверстий на
    одной окружности — равномерный массив (шаг 360/N, фаза — к 15°)."""
    import math

    cx, cy = diameter / 2.0, 0.0
    changed = 0
    out = [dict(h) for h in holes]
    on_circle: dict[float, list[int]] = {}
    for index, hole in enumerate(out):
        dx, dy = float(hole["center_x_mm"]) - cx, float(hole["center_y_mm"]) - cy
        radius = math.hypot(dx, dy)
        if radius <= tolerance:
            hole["center_x_mm"], hole["center_y_mm"] = cx, cy
            changed += 1
            continue
        near = min(circles, key=lambda c: abs(c / 2.0 - radius), default=None)
        if near is not None and abs(near / 2.0 - radius) <= tolerance:
            on_circle.setdefault(near, []).append(index)
    for circle, members in on_circle.items():
        count = len(members)
        step = 360.0 / count
        angles = [
            math.degrees(
                math.atan2(float(out[i]["center_y_mm"]) - cy, float(out[i]["center_x_mm"]) - cx)
            )
            for i in members
        ]
        # Фаза массива — круговое среднее остатков по шагу.
        folded = [math.radians((a % step) * 360.0 / step) for a in angles]
        phase = math.degrees(
            math.atan2(sum(math.sin(f) for f in folded), sum(math.cos(f) for f in folded))
        )
        phase = (phase % 360.0) * step / 360.0
        rounded = round(phase / 15.0) * 15.0
        phase = rounded if abs(rounded - phase) <= 3.0 else round(phase)
        for i, angle in zip(members, angles):
            k = round((angle - phase) / step)
            nominal = math.radians(phase + k * step)
            out[i]["center_x_mm"] = round(cx + circle / 2.0 * math.cos(nominal), 4)
            out[i]["center_y_mm"] = round(cy + circle / 2.0 * math.sin(nominal), 4)
            changed += 1
    return out, changed


def _explained(
    mapping: dict[float, float], labels: list[float], tolerance: float
) -> tuple[int, int]:
    """Сколько надписей-звеньев совпадает с расстоянием между какими-либо двумя
    станциями и — при равенстве — между соседними (цепочка)."""
    points = sorted(set(mapping.values()))
    gaps = {round(b - a, 4) for i, a in enumerate(points) for b in points[i + 1 :]}
    links = {round(b - a, 4) for a, b in zip(points, points[1:])}
    wanted = {round(v, 4) for v in labels if v >= 3 * tolerance}
    return len(wanted & gaps), len(wanted & links)


def nominal_revolve(
    outer: list[dict],
    bore: list[dict],
    linear: list[float],
    outer_diameters: list[float],
    bore_diameters: list[float],
    *,
    tolerance: float,
    bore_share: float = 0.03,
    diameter_tolerance_mm: float = 0.0,
    measured: dict[float, float] | None = None,
    prefer_measured: bool = False,
    station_map: dict[float, float] | None = None,
) -> tuple[list[dict], list[dict], int]:
    """Профиль тела вращения — в номиналах: станции вдоль оси и Ø площадок.

    ``bore_share`` — допуск привязки Ø расточки: у надписей с полем допуска
    отверстия (H) шире — это заведомо отверстия, а внутренний контур по
    штриховке систематически меньше («Опора»: 8,07 при Ø8,5H10)."""
    # Концы расточки выведены за торцы на 0,05 мм (иначе ядро оставляет
    # плёнку) — их станции не номинализуются.
    stations = [p["z"] for p in outer] + [p["z"] for p in bore[1:-1]]
    length = max((p["z"] for p in outer), default=0.0)
    labels = sorted(set(linear))
    near = min(labels, key=lambda v: abs(v - length), default=None)
    total = near if near is not None and abs(near - length) <= tolerance else None
    # Цепочка (звено между соседними станциями) и размеры от базы дают разные
    # привязки; берётся та, где больше надписей совпадает с расстоянием между
    # станциями, при равенстве — от базы (реальные листы: z4-r4, p007, p018).
    plain = snap_axis(stations, labels, tolerance=tolerance, overall=total)
    chained = snap_axis(stations, labels, tolerance=tolerance, overall=total, chain=True)
    mapping = (
        chained
        if _explained(chained, labels, tolerance) > _explained(plain, labels, tolerance)
        else plain
    )
    if measured:
        # Станции по размерным линиям листа (`dimension_lines`): надпись
        # привязана к своей паре станций местом линии, а не близостью длины.
        # На листе в масштабе — только если объясняет больше надписей; не в
        # масштабе — и при равенстве (длины там не различают звенья).
        ours = _explained(measured, labels, tolerance)
        theirs = _explained(mapping, labels, tolerance)
        if ours > theirs or (prefer_measured and ours >= theirs):
            mapping = {**{k: v for k, v in mapping.items() if k not in measured}, **measured}
    if station_map is not None:
        # Привязка станций — наружу: элементы, найденные в замере (пазы),
        # переводятся в номиналы той же привязкой.
        station_map.update(mapping)
    changed = 0

    def fix_points(
        points: list[dict], diameters: list[float], ends: bool, share: float = 0.03
    ) -> list[dict]:
        nonlocal changed
        out = [dict(p) for p in points]
        # Площадка — две соседние точки с одним радиусом: Ø по надписи.
        span = max((p["z"] for p in out), default=0.0) - min((p["z"] for p in out), default=0.0)
        for a, b in zip(out, out[1:]):
            # Длинный участок с разницей Ø до 6 % — площадка, снятая с
            # перекошенного скана (p121: 28,3 → 27,1 на 34 мм), а не конус:
            # конусы на валах короткие (фаски, переходы) или надписаны.
            long_flat = b["z"] - a["z"] >= 0.1 * span and abs(a["r"] - b["r"]) <= 0.06 * max(
                a["r"], b["r"], 1.0
            )
            if (abs(a["r"] - b["r"]) <= 0.02 * max(a["r"], b["r"], 1.0) or long_flat) and b[
                "z"
            ] - a["z"] > 0.3:
                measured = a["r"] + b["r"]
                nominal = snap_diameter(measured, diameters, share) / 2.0
                if abs(2 * nominal - measured) <= 1e-9 and diameter_tolerance_mm > 0:
                    # Толщина линии в замере: мелкий вид с толстыми линиями
                    # даёт Ø на линию шире (14,7 при Ø14) — номинал в пределах
                    # толщины линии, если он единственный.
                    near = sorted(
                        (d for d in diameters if abs(d - measured) <= diameter_tolerance_mm),
                        key=lambda d: abs(d - measured),
                    )
                    # Два номинала в допуске — ближайший, если он явно ближе
                    # (shaft-6: 14,7 между Ø14 и Ø16 оставался замером).
                    if len(near) == 1 or (
                        len(near) > 1 and abs(near[0] - measured) <= 0.6 * abs(near[1] - measured)
                    ):
                        nominal = near[0] / 2.0
                if abs(nominal - a["r"]) > 1e-6 or abs(nominal - b["r"]) > 1e-6:
                    a["r"] = b["r"] = round(nominal, 4)
                    changed += 1
        # Станция — номинал, только если порядок вдоль оси с соседями не
        # ломается (иначе остаётся замер этой станции, а не откат профиля).
        # Точки одной станции (уступ — две точки) сдвигаются вместе.
        index = 0
        while index < len(out):
            end = index
            while end + 1 < len(out) and abs(out[end + 1]["z"] - out[index]["z"]) <= 1e-6:
                end += 1
            group = range(index, end + 1)
            movable = [i for i in group if ends or i not in (0, len(out) - 1)]
            new = mapping.get(round(out[index]["z"], 6))
            if movable and new is not None and abs(new - out[index]["z"]) > 1e-6:
                before = out[index - 1]["z"] if index > 0 else -math.inf
                after = out[end + 1]["z"] if end + 1 < len(out) else math.inf
                if before - 1e-6 <= new <= after + 1e-6:
                    for i in movable:
                        out[i]["z"] = new
                    changed += 1
            index = end + 1
        return out

    new_outer = _flatten_spikes(fix_points(outer, outer_diameters, True), outer_diameters)
    new_bore = fix_points(bore, bore_diameters, False, bore_share)
    # Станция расточки, сведённая номиналом на станцию уступа снаружи, —
    # стенка нулевой толщины и тело из двух частей («Опора»: уступ Ø7,6→Ø11,5
    # и расточка Ø9,8 на одной станции 13,9). Такая станция — замер.
    steps = {
        round(a["z"], 6)
        for a, b in zip(new_outer, new_outer[1:])
        if abs(a["z"] - b["z"]) <= 1e-6 and abs(a["r"] - b["r"]) > 1e-6
    }
    for index, (old_point, point) in enumerate(zip(bore, new_bore)):
        if (
            round(point["z"], 6) in steps
            and abs(old_point["z"] - point["z"]) > 1e-6
            and 0 < index < len(new_bore) - 1
        ):
            point["z"] = old_point["z"]
    if any(q["z"] < p["z"] - 1e-6 for p, q in zip(new_bore, new_bore[1:])):
        new_bore = [dict(p) for p in bore]

    # Стенка не тоньше 0,05 мм строго внутри каждого участка расточки: радиус
    # контура — на тех же z (у уступа — с той стороны, где лежит участок).
    def outer_at(z: float) -> float | None:
        for a, b in zip(new_outer, new_outer[1:]):
            if a["z"] < z < b["z"]:
                t = (z - a["z"]) / (b["z"] - a["z"])
                return a["r"] + t * (b["r"] - a["r"])
        return None

    for a, b in zip(new_bore, new_bore[1:]):
        if b["z"] - a["z"] <= 1e-6:
            continue
        for t in (0.02, 0.5, 0.98):
            z = a["z"] + t * (b["z"] - a["z"])
            limit = outer_at(z)
            radius = a["r"] + t * (b["r"] - a["r"])
            if limit is not None and radius > limit - 0.05:
                # Нарушение — расточка этого участка по контуру минус стенка.
                a["r"] = round(max(0.0, min(a["r"], limit - 0.05)), 4)
                b["r"] = round(max(0.0, min(b["r"], limit - 0.05)), 4)
    return new_outer, new_bore, changed


def _flatten_spikes(points: list[dict], diameters: list[float]) -> list[dict]:
    """Выброс над площадкой — не ступень: вершины между двумя точками одного
    номинального радиуса, отклонённые до 8 % и не объяснённые надписью,
    ставятся на этот радиус (z4-r4: Ø25 → 26,2 → Ø25 на 6 мм — след контура
    паза; ступень не засчитывалась)."""
    out = [dict(p) for p in points]
    labelled = {round(d / 2.0, 4) for d in diameters}
    changed = True
    while changed:
        changed = False
        for i in range(1, len(out) - 1):
            r = out[i]["r"]
            if round(r, 4) in labelled:
                continue
            # Соседи по обе стороны с другим z (две точки уступа — одна станция).
            left = next((out[j] for j in range(i - 1, -1, -1) if abs(out[j]["r"] - r) > 1e-6), None)
            right = next(
                (out[j] for j in range(i + 1, len(out)) if abs(out[j]["r"] - r) > 1e-6), None
            )
            if left is None or right is None or abs(left["r"] - right["r"]) > 1e-6:
                continue
            base = left["r"]
            if round(base, 4) not in labelled or abs(r - base) > 0.08 * base:
                continue
            for j in range(len(out)):
                if (
                    abs(out[j]["r"] - r) <= 1e-6
                    and out[j]["z"] >= left["z"]
                    and out[j]["z"] <= right["z"]
                ):
                    out[j]["r"] = base
            changed = True
            break
    # Подряд идущие точки одного радиуса на одном z — лишние.
    cleaned: list[dict] = []
    for p in out:
        if (
            cleaned
            and abs(cleaned[-1]["z"] - p["z"]) <= 1e-6
            and abs(cleaned[-1]["r"] - p["r"]) <= 1e-6
        ):
            continue
        cleaned.append(p)
    return cleaned


def _replace_zone(points: list[dict], z0: float, z1: float) -> list[dict] | None:
    """Точки внутри [z0; z1] — прямой цилиндр по соседям, если соседи равны
    (в 3 %); иначе — прямая между соседними значениями. None — зона не
    внутри контура."""
    left = [p for p in points if p["z"] < z0]
    right = [p for p in points if p["z"] > z1]
    if not left or not right:
        return None
    r_left, r_right = left[-1]["r"], right[0]["r"]
    if abs(r_left - r_right) <= 0.03 * max(r_left, r_right, 1e-6):
        radius = max(r_left, r_right)
        return [*left, {"r": radius, "z": z0}, {"r": radius, "z": z1}, *right]
    # Соседи разные — уступ на краю зоны, а не конус через неё (конус в
    # расточке под отверстием — выдумка).
    return [
        *left,
        {"r": r_left, "z": z0},
        {"r": r_left, "z": z1},
        {"r": r_right, "z": z1},
        *right,
    ]


def bridge_cross_holes(
    outer: list[dict], bore: list[dict], holes: list[dict]
) -> tuple[list[dict], list[dict], int]:
    """В зоне поперечного отверстия профиль — цилиндр по соседним участкам.

    В разрезе поперечное отверстие видно дугами — линиями его пересечения с
    цилиндром и расточкой — и незаштрихованной полосой. Профиль шёл по дугам,
    и тело вращения получало фасонную «талию» («Опора»: Ø11,5 → Ø10,8 → Ø11,5
    на зоне Ø5). Отверстие — отдельный элемент по второму виду; поверхность
    под ним продолжается как у соседей.
    """
    bridged = 0
    for hole in holes:
        diameter = hole.get("diameter_mm")
        origin = hole.get("origin_mm")
        axis = hole.get("axis") or [0.0, 0.0, 1.0]
        if not diameter or not origin or abs(axis[2]) > 0.5:
            continue  # только поперечные (ось поперёк оси детали)
        z = float(origin[2])
        margin = 0.3 + 0.1 * float(diameter)
        z0, z1 = z - diameter / 2.0 - margin, z + diameter / 2.0 + margin
        new_outer = _replace_zone(outer, z0, z1)
        if new_outer is not None:
            outer = new_outer
            bridged += 1
        if bore:
            new_bore = _replace_zone(bore, z0, z1)
            if new_bore is not None:
                bore = new_bore
    return outer, bore, bridged


def chamfer_threaded_end(
    outer: list[dict], chamfers: list[float], threads: list[float]
) -> tuple[list[dict], str | None]:
    """Фаска «c×45°» — у торца, где начинается резьба (вход резьбы по ГОСТ
    всегда с фаской): «Опора» — 0,5×45° у M10×0,5. Без резьбы у торца фаска
    не ставится наугад."""
    if not chamfers or not threads or len(outer) < 2:
        return outer, None
    size = chamfers[0]
    for side in ("left", "right"):
        points = outer if side == "left" else list(reversed(outer))
        end, nxt = points[0], points[1]
        diameter = 2.0 * end["r"]
        if not any(abs(diameter - t) <= 0.12 * t for t in threads):
            continue
        length = abs(nxt["z"] - end["z"])
        if abs(nxt["r"] - end["r"]) > 0.02 * end["r"] or length <= 1.5 * size or end["r"] <= size:
            continue
        step = size if side == "left" else -size
        chamfered = [
            {"r": round(end["r"] - size, 4), "z": end["z"]},
            {"r": end["r"], "z": round(end["z"] + step, 4)},
            *points[1:],
        ]
        result = chamfered if side == "left" else list(reversed(chamfered))
        return (
            result,
            f"фаска {size:g}×45° у {'левого' if side == 'left' else 'правого'} торца (вход резьбы)",
        )
    return outer, None


__all__ = [
    "bridge_cross_holes",
    "chamfer_threaded_end",
    "nominal_revolve",
    "nominal_round_holes",
    "nominal_sketch",
    "snap_axis",
    "snap_diameter",
]


def remap_station(z: float, station_map: dict[float, float]) -> float:
    """Координата замера → номинал: кусочно-линейно между соседними
    привязанными станциями (паз на листе не в масштабе сдвинут вместе с
    уступами: z4-r4 — 69,2 при 71 от надписей)."""
    pairs = sorted(station_map.items())
    if len(pairs) < 2:
        return z
    below = [p for p in pairs if p[0] <= z]
    above = [p for p in pairs if p[0] >= z]
    if not below or not above:
        return z
    (a, na), (b, nb) = below[-1], above[0]
    if b - a <= 1e-9:
        return na + (z - a)
    return na + (nb - na) * (z - a) / (b - a)
