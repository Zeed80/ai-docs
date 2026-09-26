"""Система координат главного вида тела вращения и его наружный профиль — по листу.

Осевая линия на перечерченном листе не обязательна, но профиль вала
симметричен: верхняя и нижняя кромка каждой ступени — пара горизонталей на
равном расстоянии от оси. Кандидаты в ось — середины, у которых таких пар
больше всего (вес пары — общая длина, умноженная на толщину более тонкой
линии: пара размерных линий тонкая и ось не перетянет). Кандидат принимается,
только если ось пересекают ОСНОВНЫЕ вертикали торцов на обоих концах профиля:
у полого вала два вида одного профиля стоят один под другим, и середина
промежутка между ними «симметрична» не хуже настоящей оси, но торцы через неё
не проходят (а вертикальные размерные линии Ø через ось проходят — они тонкие).
Толщина вертикали мерится у самой оси: торец сливается в одну линию с
выносной, которая от него начинается, и медиана по всей линии — тонкая.

Главная ось — первая по голосам из прошедших. Второй вид того же вала (торцы
на тех же столбцах) заменяет её, если его профиль больше по площади: в
разрезе полого вала паз опускает верхнюю кромку, симметричной пары там нет, и
профиль падал до расточки, а вид снизу показывает ту же ступень целиком.
(Площадь среди ВСЕХ прошедших — нельзя: нелепая пара далёких линий выигрывала.)

Профиль — для каждого столбца самая внешняя симметричная пара основных линий
(контур паза и расточка — внутри, размерные — тонкие), одним участком: вид с
торца стоит на той же оси, но отдельно. Масштаб — прочитанная общая длина на
расстояние между торцами.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.ai.cad_recognize.verifiers.view_frame import ViewFrame

# Допуск симметрии пары, пиксели листа — не меньше трети толщины основной
# линии: асимметрия кромок растёт с разрешением (скан 600 dpi, лист после
# увеличения), и при ×2 последние ступени теряли пару — вид обрывался на
# 18 % раньше торца. При 300 dpi (линия 6 px) допуск тот же — 2 px.
_SYMMETRY_PX = 2.0
_SYMMETRY_LINE_SHARE = 1.0 / 3.0
# …но только на заметно увеличенном листе (линия от 9 px ≈ 450 dpi): живой
# z4-r4 (фото, линия 8,2 px) при допуске 2,7 px складывал лишнюю пару кромок —
# «паз», которого нет (гейт реальных листов).
_SYMMETRY_FROM_LINE_PX = 9.0
# Основная линия — не тоньше этой доли эталонной толщины (тонкая — вдвое тоньше).
_MAIN_SHARE = 0.6
# Торцы и грани — строже: по длине линий толщины листа лежат кластерами
# 0,48 (тонкие), 0,60–0,67 (штрихи надписей, стрелки — и куски контура),
# провал 0,68–0,79, 0,96–1,0 (основные). На 0,6 уточнение торца цеплялось за
# повёрнутую надпись у торца, когда лист увеличен в 1,25 раза; профилю же
# куски среднего кластера нужны (0,7 для всех линий — 4 регрессии храповика).
_FACE_SHARE = 0.7
# Внутренние разрывы профиля заполняются парами с таким кратным допуском.
_LOOSE_SYMMETRY_SHARE = 2.5
# Лист годен для замера — основная линия не тоньше (`shaft_profile._MIN_LINE_PX`).
_MEASURABLE_LINE_PX = 4.5
# Разрыв профиля, который ещё считается тем же видом, — доля всей протяжённости
# найденных столбцов. Внутри вида разрывы до 3,5 % (короткая ступень без пары,
# канавка: shaft-3 — 22 px, shaft-20 — 47 px), вид с торца отделён на 20–33 %.
# От доли текущего куска профиль рвался, и торец находился на середине вала.
_GAP_TOTAL_SHARE = 0.05
# Сколько лучших кандидатов оси проверять на торцы (part_02: у вала Ø6 на
# мелком листе ось была седьмой — после рамки и штампа).
_AXIS_CANDIDATES = 12
# Второй вид того же вала: торцы на тех же столбцах в пределах этой доли длины.
_SAME_VIEW_SHARE = 0.02
# Концевая ступень с лыской: пара кромок несимметрична (одна опущена лыской),
# меньшая — не ближе к оси, чем эта доля большей (лыски корпуса — до 25 % R).
_END_FLAT_SHARE = 0.5


@dataclass(frozen=True)
class ShaftProfile:
    """Наружный профиль: полувысота в пикселях для каждого столбца ``x0..x1``."""

    x0: int
    x1: int
    axis_y: float
    half_px: Any  # numpy: полувысота или nan, длина x1 - x0 + 1
    # Столбцы основных вертикалей внутри вида — грани уступов (центр линии).
    # (x, начало, конец по вертикали): у канавки у уступа тоже есть грань,
    # отличить уступ можно только по тому, какой скачок радиуса она покрывает.
    faces_px: tuple[tuple[float, float, float], ...] = ()
    # Эталонная толщина основной линии (масса поперёк, px) — мера разрешения.
    line_px: float = 0.0

    def half_at(self, x: float) -> float | None:
        import math

        index = int(round(x)) - self.x0
        if index < 0 or index >= len(self.half_px):
            return None
        value = float(self.half_px[index])
        return None if math.isnan(value) else value


@dataclass(frozen=True)
class _Sheet:
    gray: Any
    ink: Any
    main_weight: float
    # Порог торцов — строже (`_FACE_SHARE`).
    end_weight: float = 0.0


def locate_shaft_frame(sheet: Any, total_length_mm: float) -> tuple[ViewFrame, ShaftProfile] | None:
    """Главный вид вала → (система координат с началом на левом торце на оси, профиль)."""
    views = locate_shaft_views(sheet, total_length_mm)
    return views[0] if views else None


def locate_shaft_views(
    sheet: Any, total_length_mm: float, diameters_mm: list[float] | None = None
) -> list[tuple[ViewFrame, ShaftProfile]]:
    """Все виды одного вала на листе — самый полный первым.

    У полого вала опорный вид — разрез, а паз лицом виден на виде `bottom`
    под ним (Ф3.0a): проверяльщику паза нужен второй вид, а не только первый
    (shaft-8, shaft-12: паз на разрезе «не измерим»).
    """
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _ink, _lines, _stroke

    if not total_length_mm or total_length_mm <= 0:
        return []
    gray = np.asarray(sheet)
    ink = _ink(gray)
    min_length = max(6, int(round(0.006 * min(gray.shape))))
    lines = _segments(ink, min_length)
    if len(lines) < 2:
        return []
    # Нижний квартиль толщины: у короткой размерной линии залитые стрелки
    # занимают почти половину длины, и по медиане она выходила основной
    # (shaft-4: пара размерных «30» и «16», симметричных оси, — «Ø72»).
    weight = {
        id(line): _stroke(gray, ink, line, (line.start, line.end), axis=0, quantile=0.25)
        for line in lines
    }
    # Эталон толщины — 90-й процентиль толщин ДЛИННЫХ линий: короткие
    # горизонтали — это ещё и основания залитых стрелок размеров Ø (масса
    # 17–18 против 6 у кромки), и по всем линиям эталон отсекал сам контур.
    long_lines = [line for line in lines if line.end - line.start >= 4 * min_length]
    main_ref = float(np.percentile([weight[id(line)] for line in (long_lines or lines)], 90))
    main = [line for line in lines if weight[id(line)] >= _MAIN_SHARE * main_ref]
    # Запасной набор — только уверенно основные: штрихи надписи у торца
    # (0,60–0,67 эталона) продлевали профиль наружу, и торец там не находился.
    strict = [line for line in lines if weight[id(line)] >= _FACE_SHARE * main_ref]
    vertical = _lines(ink, min_length, axis=1)
    context = _Sheet(
        gray=gray,
        ink=ink,
        main_weight=_MAIN_SHARE * main_ref,
        end_weight=_FACE_SHARE * main_ref,
    )

    symmetry = (
        max(_SYMMETRY_PX, _SYMMETRY_LINE_SHARE * main_ref)
        if main_ref >= _SYMMETRY_FROM_LINE_PX
        else _SYMMETRY_PX
    )
    raw: dict[int, tuple[list[Any], tuple[int, int, Any]]] = {}

    def collect(extend: bool) -> list[tuple]:
        found_views = []
        for axis_y in _axis_candidates(main, weight, min_length, symmetry):
            faces = None
            for candidates in (main, strict):
                found = _profile(candidates, axis_y, gray.shape[1], min_length, main_ref, symmetry)
                if found is None:
                    continue
                if extend:
                    found = _extend_flat_ends(
                        candidates, axis_y, found, min_length, main_ref, symmetry
                    )
                x0, x1, half = found
                faces = _end_faces(vertical, axis_y, x0, x1, half, context)
                if faces is not None:
                    break
            if faces is None:
                continue
            x0, x1 = faces
            if x1 - x0 < 4 * min_length:
                continue
            segment = _slice(half, x0, x1)
            item = (axis_y, x0, x1, segment, float(np.nansum(segment)))
            raw[id(item)] = (candidates, found)
            found_views.append(item)
        return found_views

    passing = collect(extend=False)
    if not passing:
        # Ни одного вида — может быть, обе концевые ступени без пары кромок
        # (лыска на всю ступень, живой turned_multiaxis-28).
        passing = collect(extend=True)
    if not passing:
        return []
    # Главный вид — тот, чьи ступени совпадают с диаметрами листа (прочитанные
    # Ø и надписи Ø), затем самый длинный, затем первый по голосам. Живые
    # part_02 и part_01: длинные линии рамки и штампа набирают больше голосов,
    # а проверку торцов проходят и рамка листа («Ø132»), и штамп, и таблица
    # зацепления — по длине выигрывала рамка, по голосам — штамп.
    primary = max(
        passing,
        key=lambda item: (
            _diameter_matches(item, total_length_mm, diameters_mm),
            item[2] - item[1],
        ),
    )
    # Концевая ступень под лыской — продлевается только выбранный вид и только
    # до торца: продление всех осей давало смещённым осям полную длину, и
    # такая набирала больше совпадений Ø, чем настоящая (turned_multiaxis-19).
    lines_used, found = raw[id(primary)]
    extended = _extend_flat_ends(lines_used, primary[0], found, min_length, main_ref, symmetry)
    if extended is not found:
        faces = _end_faces(vertical, primary[0], *extended, context)
        if faces is not None and faces[1] - faces[0] > primary[2] - primary[1]:
            segment = _slice(extended[2], *faces)
            index = passing.index(primary)
            primary = (primary[0], faces[0], faces[1], segment, float(np.nansum(segment)))
            passing[index] = primary
    reach = _SAME_VIEW_SHARE * (primary[2] - primary[1])
    same_shaft = [
        item
        for item in passing
        if abs(item[1] - primary[1]) <= reach and abs(item[2] - primary[2]) <= reach
    ]
    # Устойчивая сортировка: при равной площади первым остаётся тот же вид,
    # что выбирал прежний `max`.
    same_shaft.sort(key=lambda item: -item[4])
    return [
        _view(gray, ink, vertical, context, main_ref, total_length_mm, min_length, item)
        for item in same_shaft
    ]


def _diameter_matches(item: tuple, total_length_mm: float, diameters_mm: list[float] | None) -> int:
    """Сколько площадок вида совпадает (±6 %) с диаметрами листа при его масштабе."""
    from types import SimpleNamespace

    from app.ai.cad_recognize.verifiers.shaft_profile import _plateaus

    if not diameters_mm:
        return 0
    _axis_y, x0, x1, segment, _area = item
    scale = float(total_length_mm) / float(x1 - x0)
    shortest = 0.03 * float(x1 - x0)
    matches = 0
    for start, end, level in _plateaus(SimpleNamespace(half_px=segment)):
        if end - start + 1 < shortest:
            continue
        diameter = 2.0 * level * scale
        if any(abs(diameter - d) <= 0.06 * d for d in diameters_mm if d > 0):
            matches += 1
    return matches


def _view(
    gray: Any,
    ink: Any,
    vertical: list[Any],
    context: Any,
    main_ref: float,
    total_length_mm: float,
    min_length: int,
    item: tuple,
) -> tuple[ViewFrame, ShaftProfile]:
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _stroke

    axis_y, x0, x1, segment, _area = item
    extent = float(np.nanmax(segment))
    # Грани уступов: основные вертикали внутри вида. Толщина — на отрезке
    # внутри профиля: грань тоже сливается с выносной цепочки размеров.
    inside = (axis_y - extent - 2.0, axis_y + extent + 2.0)
    faces = sorted(
        (_face_x(ink, line, inside), line.start, line.end)
        for line in vertical
        if x0 - 3 <= line.position <= x1 + 3
        and min(line.end, inside[1]) - max(line.start, inside[0]) >= min_length
        and _stroke(gray, ink, _at_rows(ink, line, inside), inside, axis=1) >= context.main_weight
    )
    profile = ShaftProfile(
        x0=x0, x1=x1, axis_y=axis_y, half_px=segment, faces_px=tuple(faces), line_px=main_ref
    )
    frame = ViewFrame(
        bbox_px=(x0 - 3, axis_y - extent - 3, x1 + 3, axis_y + extent + 3),
        mm_per_px=float(total_length_mm) / float(x1 - x0),
        origin_px=(float(x0), axis_y),
    )
    return frame, profile


def _segments(ink: Any, min_length: int) -> list[Any]:
    """Горизонтали вида: центр линии — по каждому столбцу, со скачком — разрез.

    Общий `_lines` утолщает маску по вертикали и берёт центр тяжести всей
    компоненты. Кромки соседних ступеней с близкими радиусами при этом
    сливаются (150 dpi: низ Ø30 и Ø28 в 4 px; 1:2 — Ø22 и Ø20 линиями в 6 px
    на 5,9 px), центр ложится между ними — пара пропадает, профиль рвётся или
    две ступени сливаются в одну «Ø21,4». Здесь без утолщения, а компонента
    режется там, где центр строки прыгает больше чем на пиксель.
    """
    import cv2
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _Line

    # Ядро размыкания (0,6 % меньшей стороны листа) на листах A-формата при
    # любом dpi примерно в 2,5 раза толще основной линии, и грани уступов им
    # стираются. На изображении, где ядро не толще линии, грань переживает
    # размыкание и сшивает верх и низ ступени — вычитать вертикали нельзя
    # (выносные режут кромки: корпус v7, чистый лист 26 → 12 из 28).
    opened = cv2.morphologyEx(
        ink.astype(np.uint8), cv2.MORPH_OPEN, np.ones((1, min_length), np.uint8)
    )
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(opened, connectivity=8)
    result = []
    for index in range(1, count):
        x, y, w, h, _area = stats[index]
        if w < min_length:
            continue
        block = labels[y : y + h, x : x + w] == index
        # В столбце может быть несколько прогонов одной компоненты: подпись
        # «Ø14» поверх паза сшивает его верхнюю и нижнюю прямую (shaft-3),
        # грань толщиной с ядро — верх и низ ступени. Один центр на столбец
        # усреднял их и попадал на ось. Каждый прогон — отдельно; прогоны
        # соседних столбцов сцепляются в цепочки по близости центра.
        chains: list[list[float]] = []  # [начало, конец, сумма центров, число]
        active: list[int] = []
        for column in range(w):
            rows = np.nonzero(block[:, column])[0]
            if rows.size == 0:
                active = []
                continue
            runs = np.split(rows, np.nonzero(np.diff(rows) > 1)[0] + 1)
            next_active = []
            for run in runs:
                centre = y + (run[0] + run[-1]) / 2.0
                match = None
                for chain_index in active:
                    chain = chains[chain_index]
                    last = chain[4]
                    if abs(centre - last) <= 1.0 and chain_index not in next_active:
                        match = chain_index
                        break
                if match is None:
                    chains.append([column, column, centre, 1, centre])
                    match = len(chains) - 1
                else:
                    chain = chains[match]
                    chain[1] = column
                    chain[2] += centre
                    chain[3] += 1
                    chain[4] = centre
                next_active.append(match)
            active = next_active
        for first, last, total, number, _last_centre in chains:
            if last - first + 1 >= min_length:
                result.append(_Line(float(total / number), float(x + first), float(x + last)))
    return sorted(result, key=lambda line: line.position)


def _axis_candidates(
    lines: list[Any], weight: dict[int, float], min_length: int, symmetry: float = _SYMMETRY_PX
) -> list[float]:
    """Середины симметричных пар с наибольшим весом — лучшие первыми."""
    votes: dict[int, float] = {}
    for i, top in enumerate(lines):
        for bottom in lines[i + 1 :]:
            if bottom.position - top.position < 2 * symmetry:
                continue
            overlap = top.overlap(bottom.start, bottom.end)
            if overlap < min_length:
                continue
            middle = int(round((top.position + bottom.position) / 2.0))
            votes[middle] = votes.get(middle, 0.0) + overlap * min(
                weight[id(top)], weight[id(bottom)]
            )
    smoothed = {y: sum(votes.get(y + d, 0.0) for d in (-1, 0, 1)) for y in votes}
    peaks = [
        y
        for y in smoothed
        if smoothed[y] >= smoothed.get(y - 1, 0.0) and smoothed[y] > smoothed.get(y + 1, 0.0)
    ]
    peaks.sort(key=lambda y: smoothed[y], reverse=True)
    result = []
    for y in peaks[:_AXIS_CANDIDATES]:
        near = [value for value in range(y - 1, y + 2) if value in votes]
        result.append(sum(value * votes[value] for value in near) / sum(votes[v] for v in near))
    return result


def _profile(
    lines: list[Any],
    axis_y: float,
    width: int,
    min_length: int,
    main_ref: float = 0.0,
    symmetry: float = _SYMMETRY_PX,
):
    """Самая внешняя симметричная пара на каждом столбце, самый длинный участок."""
    import numpy as np

    half = np.full(width, np.nan)
    above = [line for line in lines if line.position < axis_y - symmetry]
    below = [line for line in lines if line.position > axis_y + symmetry]
    for top in above:
        mirror = 2.0 * axis_y - top.position
        for bottom in below:
            if abs(bottom.position - mirror) > symmetry:
                continue
            start, end = max(top.start, bottom.start), min(top.end, bottom.end)
            if end - start < min_length:
                continue
            value = (bottom.position - top.position) / 2.0
            segment = half[int(start) : int(end) + 1]
            half[int(start) : int(end) + 1] = np.where(
                np.isnan(segment), value, np.maximum(segment, value)
            )
    columns = np.nonzero(~np.isnan(half))[0]
    if columns.size == 0:
        return None
    # Внутренние разрывы — парами с допуском шире: на грубом исходнике кромка
    # ступени и опущенная кромка лыски в 1,9 px друг от друга сливаются в одну
    # линию посередине (увеличение SeedVR2 честно рисует одну), пара на всей
    # ступени несимметрична на 3 px, и вид рвался надвое. Концы профиля не
    # трогаются — там подписи и выноски у торцов. Только на листе, годном для
    # замера (основная линия от 4,5 px): грубее его продукт сначала увеличивает,
    # а на грубом исходнике широкий допуск находил вид, где пазы опровергались
    # ложно (150 dpi).
    inner = np.zeros(width, dtype=bool)
    inner[int(columns[0]) : int(columns[-1]) + 1] = True
    inner &= np.isnan(half)
    if inner.any() and main_ref >= _MEASURABLE_LINE_PX:
        loose = _LOOSE_SYMMETRY_SHARE * symmetry
        for top in above:
            mirror = 2.0 * axis_y - top.position
            for bottom in below:
                if abs(bottom.position - mirror) > loose:
                    continue
                start, end = max(top.start, bottom.start), min(top.end, bottom.end)
                if end - start < min_length:
                    continue
                # Несимметричная пара — одна кромка опущена элементом (лыска
                # срезает силуэт с одной стороны): ступень задаёт целая кромка.
                # По среднему выходила ложная ступень Ø = 2R − глубина лыски
                # (turned_multiaxis-0: Ø23,7 посреди Ø25), и профиль по листу
                # отказывал — уступы не объяснялись надписями.
                value = max(axis_y - top.position, bottom.position - axis_y)
                span = slice(int(start), int(end) + 1)
                segment = half[span]
                half[span] = np.where(
                    inner[span] & np.isnan(segment),
                    value,
                    np.where(inner[span], np.fmax(segment, value), segment),
                )
    gap = max(3.0, _GAP_TOTAL_SHARE * float(columns[-1] - columns[0]))
    columns = np.nonzero(~np.isnan(half))[0]
    runs: list[list[int]] = [[int(columns[0]), int(columns[0])]]
    for x in columns[1:]:
        x = int(x)
        if x - runs[-1][1] <= gap:
            runs[-1][1] = x
        else:
            runs.append([x, x])
    x0, x1 = max(runs, key=lambda run: run[1] - run[0])
    return x0, x1, half


def _extend_flat_ends(
    lines: list[Any],
    axis_y: float,
    found: tuple[int, int, Any],
    min_length: int,
    main_ref: float,
    symmetry: float,
) -> tuple[int, int, Any]:
    """Профиль, продлённый за конец концевой ступенью под лыской.

    Лыска на концевой ступени опускает одну кромку на всю её длину (живой
    turned_multiaxis-5: Ø28 × 25 с лыской 3,5 справа): симметричной пары там
    нет, вид кончался на уступе Ø35, и масштаб вида выходил в 1,4 раза мельче.
    Продолжение — пара основных линий по обе стороны оси, меньшая не ближе
    половины большей, примыкающая к концу; ступень задаёт целая кромка. Торец
    у нового конца проверяет вызывающий — без него остаётся прежний профиль.
    """
    import numpy as np

    x0, x1, half = found
    if main_ref < _MEASURABLE_LINE_PX:
        return found
    pairs = []
    for top in (line for line in lines if line.position < axis_y - symmetry):
        for bottom in (line for line in lines if line.position > axis_y + symmetry):
            near, far = sorted((axis_y - top.position, bottom.position - axis_y))
            if near < _END_FLAT_SHARE * far:
                continue
            start, end = max(top.start, bottom.start), min(top.end, bottom.end)
            if end - start >= min_length:
                pairs.append((int(start), int(end), far))
    if not pairs:
        return found
    x0_before, x1_before = x0, x1
    half = half.copy()
    gap = max(3.0, _GAP_TOTAL_SHARE * float(x1 - x0))
    grown = True
    while grown:
        grown = False
        for start, end, value in pairs:
            if x0 <= start <= x1 + gap and end > x1 + min_length:
                span = slice(x1 + 1, end + 1)
                half[span] = np.where(np.isnan(half[span]), value, half[span])
                x1, grown = end, True
            if x0 - gap <= end <= x1 and start < x0 - min_length:
                span = slice(start, x0)
                half[span] = np.where(np.isnan(half[span]), value, half[span])
                x0, grown = start, True
    if (x0, x1) == (x0_before, x1_before):
        return found
    return x0, x1, half


def _at_rows(ink: Any, line: Any, rows: tuple[float, float]) -> Any:
    """Та же вертикаль, но на своём столбце в строках ``rows``.

    `_lines` склеивает торец или грань с выносной, которая её продолжает
    (размер фаски под видом, ширина канавки): центр тяжести компоненты
    уезжает к тонкой выносной, и толщина, измеренная в этом столбце у оси,
    выходила нулевой — торец отбрасывался (корпус v8, shaft-5: вид вала не
    найден), грань уступа тоже (длины у канавок мимо на её ширину).
    """
    from app.ai.cad_recognize.verifiers.plate_frame import _Line

    return _Line(_face_x(ink, line, rows), line.start, line.end)


def _end_faces(
    vertical: list[Any], axis_y: float, x0: int, x1: int, half: Any, sheet: _Sheet
) -> tuple[int, int] | None:
    """Торцы — основные вертикали через ось у обоих концов профиля; нет обоих — не ось.

    Вертикаль должна перекрывать большую часть высоты концевой ступени, а
    положение уточняется по самому внешнему прогону у оси (`_outer_x`).
    """
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _stroke

    reach = max(3.0, 0.03 * (x1 - x0))
    extent = float(np.nanmax(half[x0 : x1 + 1]))
    near_axis = (axis_y - 0.25 * extent, axis_y + 0.25 * extent)
    edge = max(3, int(0.05 * (x1 - x0)))

    def end_height(lo: int, hi: int) -> float:
        values = half[max(0, lo) : max(0, hi) + 1]
        values = values[~np.isnan(values)]
        return float(np.median(values)) if values.size else extent

    left_height = end_height(x0, x0 + edge)
    right_height = end_height(x1 - edge, x1)

    def is_face(line: Any, height: float) -> bool:
        if not line.start <= axis_y <= line.end:
            return False
        # Торец идёт через большую часть высоты концевой ступени: короткий
        # обрывок через ось (метка, конец выноски) стал торцом у shaft-2 на
        # 150 dpi. Фаска укорачивает торец на свой размер — это меньше 40 %.
        span = min(line.end, axis_y + height) - max(line.start, axis_y - height)
        if span < 0.6 * 2.0 * height:
            return False
        weight = _stroke(
            sheet.gray, sheet.ink, _at_rows(sheet.ink, line, near_axis), near_axis, axis=1
        )
        return weight >= sheet.main_weight

    left = [
        line.position
        for line in vertical
        if x0 - reach <= line.position <= x0 + reach and is_face(line, left_height)
    ]
    right = [
        line.position
        for line in vertical
        if x1 - reach <= line.position <= x1 + reach and is_face(line, right_height)
    ]
    if not left or not right:
        return None
    band = 0.2 * min(left_height, right_height)
    rows = (axis_y - band, axis_y + band)
    x_left = _outer_x(
        sheet.ink, min(left), rows, reach, side=-1, min_width=sheet.end_weight or sheet.main_weight
    )
    x_right = _outer_x(
        sheet.ink, max(right), rows, reach, side=1, min_width=sheet.end_weight or sheet.main_weight
    )
    return int(round(x_left)), int(round(x_right))


def _outer_x(
    ink: Any,
    x: float,
    rows: tuple[float, float],
    reach: float,
    *,
    side: int,
    min_width: float = 0.0,
) -> float:
    """Самый внешний прогон чернил у торца — по строкам у оси, медиана середин.

    По ЕСКД фаска на торце вала — ещё одна вертикаль через всю высоту на
    расстоянии фаски от торца; на 150 dpi она в 3 px от торца, линии сливаются
    в одну компоненту, и её центр уезжал внутрь (shaft-25: 858,9 вместо 862,2,
    shaft-7: 1077,5 вместо 1083,7 — масштаб вида мимо на 1,5 %). Окно — от
    найденной вертикали наружу на ширину окна торца.
    """
    import numpy as np

    width = ink.shape[1]
    if side > 0:
        lo, hi = max(0, int(x) - 2), min(width, int(x + reach) + 1)
    else:
        lo, hi = max(0, int(x - reach)), min(width, int(x) + 3)
    # Середины прогонов по строкам полосы у оси — и ширина каждого прогона.
    samples: list[tuple[int, float]] = []
    widths: dict[tuple[int, float], float] = {}
    inked_rows = 0
    for y in range(max(0, int(rows[0])), min(ink.shape[0], int(rows[1]) + 1)):
        columns = np.nonzero(ink[y, lo:hi])[0]
        if columns.size == 0:
            continue
        inked_rows += 1
        for run in np.split(columns, np.nonzero(np.diff(columns) > 1)[0] + 1):
            sample = (y, (run[0] + run[-1]) / 2.0 + lo)
            samples.append(sample)
            widths[sample] = float(run[-1] - run[0] + 1)
    if not samples:
        return float(x)
    # Край — вертикаль: один столбец почти во всех строках. Метку у торца
    # (shaft-2: окружность отверстия в 7 px от торца) выдаёт форма — у кривой
    # столбец меняется от строки к строке; правило «только вплотную» не
    # переносилось между разрешениями (фаска на 150 dpi — те же 4–7 px).
    samples.sort(key=lambda item: item[1])
    groups: list[list[tuple[int, float]]] = [[samples[0]]]
    for item in samples[1:]:
        if item[1] - groups[-1][-1][1] <= 1.5:
            groups[-1].append(item)
        else:
            groups.append([item])
    steady = [group for group in groups if len({row for row, _ in group}) >= 0.8 * inked_rows]
    # Край торца — толстый штрих (торец, слитый с фаской), не тонкая линия:
    # на увеличенном фото z4-r4 размерная линия резьбы M18 стояла в 50 px
    # снаружи торца, ровная через ось, и «краем» становилась она — масштаб
    # вида съезжал, фаска мерилась 4,2 мм вместо 1,6.
    if min_width > 0.0:
        steady = [
            group
            for group in steady
            if float(np.median([widths[item] for item in group])) >= min_width
        ]
    if not steady:
        return float(x)
    edge = steady[-1] if side > 0 else steady[0]
    return float(np.median([column for _, column in edge]))


def _face_x(ink: Any, line: Any, inside: tuple[float, float]) -> float:
    """Столбец грани — середина линии в строках ВНУТРИ профиля, медиана по строкам.

    Вертикаль грани сливается в одну компоненту с выносной, стенкой канавки
    или размерной линией отверстия, и центр тяжести компоненты уезжал: грань
    на 250,0 выходила 251,3 (shaft-11), на 50 — 50,36 (shaft-16), хотя на
    листе она стоит ровно на станции. Внутри профиля грань стоит одна.
    """
    import numpy as np

    top = int(max(line.start, inside[0]))
    bottom = int(min(line.end, inside[1]))
    centre = int(round(line.position))
    reach = 6
    lo = max(0, centre - reach)
    hi = min(ink.shape[1], centre + reach + 1)
    middles = []
    for y in range(top, bottom + 1):
        cut = ink[y, lo:hi]
        columns = np.nonzero(cut)[0]
        if columns.size == 0:
            continue
        # Прогон чернил, ближайший к положению линии: соседняя вертикаль в
        # пределах окна не должна тянуть середину к себе.
        runs = np.split(columns, np.nonzero(np.diff(columns) > 1)[0] + 1)
        run = min(runs, key=lambda item: abs((item[0] + item[-1]) / 2.0 + lo - line.position))
        middles.append((run[0] + run[-1]) / 2.0 + lo)
    return float(np.median(middles)) if middles else float(line.position)


def _slice(half: Any, x0: int, x1: int) -> Any:
    import numpy as np

    out = np.full(x1 - x0 + 1, np.nan)
    lo, hi = max(0, x0), min(len(half) - 1, x1)
    out[lo - x0 : hi - x0 + 1] = half[lo : hi + 1]
    return out
