"""Проверяльщик шпоночного паза: начало, длина и ширина по главному виду вала.

С пазов начался план: ридер ставил их «не на те поверхности». Главный вид
вала с пазом показывает его лицом (Ф3.0a): закрытый паз по ГОСТ 23360 —
капсула: две прямые, симметричные оси на половине ширины, и дуги скругления
радиусом в полширины. Паз ищется как капсула целиком, а не по чернилам на
оси: на оси паза стоят подпись Ø ступени и её размерная линия (shaft-3:
«Ø14» перекрывает левую половину паза — глазами паз казался на 6 мм короче),
и они же рвут прямые паза пополам (shaft-6, shaft-28: бралась одна половина).

Концы — по шаблону дуги скругления, а не «конец прямой плюс радиус»: цепочка
прямой заходит на пологую часть дуги, и радиус считался дважды. Вертикальная
размерная линия Ø, проходящая через вершину, покрывает лишь четверть дуги.
Ширина — медиана середин прямых в средней части паза: среднее по цепочке
тянули к оси те же пологие концы дуг (shaft-26: 11,28 вместо 12). Система
координат — `shaft_frame.locate_shaft_frame` (начало на левом торце, на оси).
"""

from __future__ import annotations

from typing import Any

from app.ai.cad_recognize.verifiers.contract import Hypothesis, Verdict
from app.ai.cad_recognize.verifiers.registry import register
from app.ai.cad_recognize.verifiers.view_frame import ViewFrame

# Допуск симметрии пары, px; во сколько раз настоящая ширина может отличаться.
_SYMMETRY_PX = 2.0
_WIDTH_SPAN = (0.4, 1.8)
# Шаблон дуги — ±75° от вершины: у касания дуга сливается с прямой и
# «покрыта» при любом положении центра.
_ARC_SPAN_DEG = 75.0
# Доля дуги под чернилами, чтобы считать её концом паза; доля прямой — её
# рвут подпись и размерная линия Ø ступени.
_ARC_FLOOR = 0.8
_STRAIGHT_FLOOR = 0.6
# Штриховка за прямыми (`_hatch_score`): доля столбцов с совпадением по 45°.
_HATCH_FLOOR = 0.04
# «Паза на прочитанном месте нет» — на измеримом листе это свидетельство, а не
# отказ: стадия по нему не исключает Ø ступени под прочитанным пазом.
NOT_FOUND_REASON = "контура паза (прямые и скругления) на прочитанном месте не найдено"


@register("keyway", min_feature_px=6.0)
def verify_keyway(hypothesis: Hypothesis, frame: ViewFrame | None, sheet: Any) -> Verdict:
    """``expected``: ``axial_start_mm``, ``length_mm``, ``width_mm``; ``sheet`` — серый лист."""
    import cv2
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _ink, _stroke
    from app.ai.cad_recognize.verifiers.shaft_frame import _segments
    from app.ai.cad_recognize.verifiers.shaft_profile import _MIN_LINE_PX, shaft_tolerances

    if frame is None:
        return Verdict(status="unmeasurable", reason="главный вид вала на листе не найден")
    expected = hypothesis.expected
    start, length, width = (
        expected.get("axial_start_mm"),
        expected.get("length_mm"),
        expected.get("width_mm"),
    )
    if not all(isinstance(value, (int, float)) for value in (start, length, width)):
        return Verdict(
            status="unmeasurable", reason="нет прочитанных начала, длины или ширины паза"
        )
    gray = np.asarray(sheet)
    scale = frame.scale_mean
    x0, axis = frame.origin_px
    half_px = float(width) / 2.0 / scale
    # Область: прочитанный паз с запасом в половину его длины и ширину сверху-снизу.
    margin = 0.5 * float(length) / frame.mm_per_px + 2.0 * half_px
    left = max(0, int(x0 + float(start) / frame.mm_per_px - margin))
    right = min(gray.shape[1], int(x0 + (float(start) + float(length)) / frame.mm_per_px + margin))
    # По вертикали — с полосами проверки штриховки за самой широкой парой:
    # иначе у широкой капсулы полоса выходила за область и штриховка
    # «не находилась» (150 dpi, shaft-11: расточка разреза вместо паза).
    reach_v = 1.5 * _WIDTH_SPAN[1] * half_px
    top = max(0, int(axis - reach_v - 4))
    bottom = min(gray.shape[0], int(axis + reach_v + 5))
    if right - left < 8 or bottom - top < 8:
        return Verdict(status="unmeasurable", reason="область паза вне вида")
    roi = np.ascontiguousarray(gray[top:bottom, left:right])
    ink = _ink(roi)
    min_length = max(6, int(round(0.3 * half_px)))
    lines = _segments(ink, min_length)
    axis_local = axis - top
    weights = {id(line): _stroke(roi, ink, line, (line.start, line.end), axis=0) for line in lines}
    # Эталон толщины — по ДЛИННЫМ линиям: самые тяжёлые в области паза —
    # основания залитых стрелок размера Ø (масса 24 против 6 у кромки), и от
    # них прямые паза выходили «тонкими» (shaft-1: паз не найден).
    long_lines = [line for line in lines if line.end - line.start >= 4 * min_length]
    reference = (
        float(np.percentile([weights[id(line)] for line in long_lines], 90)) if long_lines else 0.0
    )
    # Грубый лист — как у ступеней вала (`shaft_profile._MIN_LINE_PX`): на
    # 150 dpi основная линия в 3 px, дуги скругления и прямые паза сливаются
    # с подписью Ø и выносными, замер уезжает на 1–2 мм (shaft-4, shaft-10).
    # Мера — та же, что у системы координат вала (нижний квартиль толщины):
    # по медиане 200 dpi проходил и давал ложные опровержения (shaft-10).
    line_px = (
        float(
            np.percentile(
                [
                    _stroke(roi, ink, line, (line.start, line.end), axis=0, quantile=0.25)
                    for line in long_lines
                ],
                90,
            )
        )
        if long_lines
        else 0.0
    )
    if 0.0 < line_px < _MIN_LINE_PX:
        return Verdict(
            status="unmeasurable",
            evidence_bbox_px=(left, top, right, bottom),
            reason=(
                f"лист слишком грубый: основная линия {line_px:.1f} px (нужно от {_MIN_LINE_PX:g})"
            ),
        )
    # И сверху: основания залитых стрелок размера Ø тоже симметричны оси и
    # давали капсулу шире паза (shaft-17: масса 21 против 7, ширина 8,6 вместо 6).
    main = [
        line
        for line in lines
        if reference and 0.6 * reference <= weights[id(line)] <= 1.8 * reference
    ]
    # Кандидаты в прямые паза: пары основных линий, симметричные оси.
    pairs: dict[tuple[float, float], None] = {}
    for upper in (line for line in main if line.position < axis_local):
        for lower in (line for line in main if line.position > axis_local):
            centre = (upper.position + lower.position) / 2.0
            h = (lower.position - upper.position) / 2.0
            if abs(centre - axis_local) > _SYMMETRY_PX:
                continue
            if not _WIDTH_SPAN[0] * half_px <= h <= _WIDTH_SPAN[1] * half_px:
                continue
            if min(upper.end, lower.end) - max(upper.start, lower.start) < min_length:
                continue
            pairs[(round(centre * 2) / 2, round(h * 2) / 2)] = None
    mask = cv2.dilate(ink.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
    read_from = x0 + float(start) / frame.mm_per_px - left
    read_to = read_from + float(length) / frame.mm_per_px
    best = None
    for centre, h in pairs:
        for cl, cr, support in _capsules(mask, centre, h):
            lo, hi = cl - h, cr + h
            overlap = min(hi, read_to) - max(lo, read_from)
            if overlap <= 0:
                continue
            # Расточка на разрезе полого вала — те же прямые с дугами у
            # торцов, но за ними штриховка: паз лицом на таком виде не виден.
            if _hatch_score(ink, centre, h, cl, cr) >= _HATCH_FLOOR:
                continue
            iou = overlap / (max(hi, read_to) - min(lo, read_from))
            # Главное — полнота контура, а не совпадение с прочитанным: иначе
            # замер подгонялся под ошибку ридера (длина +5 → капсула до чужой
            # дуги с дырявой прямой, shaft-10, shaft-19). Прочитанное решает
            # только между равными — соседние пазы на одной ступени.
            key = (-round(support, 1), round(abs(h - half_px) / half_px, 1), -round(iou, 2))
            if best is None or key < best[0]:
                best = (key, centre, h, cl, cr)
    if best is None:
        return Verdict(
            status="unmeasurable",
            evidence_bbox_px=(left, top, right, bottom),
            reason=NOT_FOUND_REASON,
        )
    _key, centre, h, cl, cr = best
    h = _refined_half(ink, centre, h, cl, cr)
    # Плато шаблона растягивают выносные размера длины — они выходят из
    # вершин дуг (shaft-26: конец +1 мм); центр — подгонкой по строкам.
    cl = _arc_centre(ink, centre, h, cl, side=-1)
    cr = _arc_centre(ink, centre, h, cr, side=1)
    measured_start = (left + cl - h - x0) * frame.mm_per_px
    measured_end = (left + cr + h - x0) * frame.mm_per_px
    measured = {
        "axial_start_mm": round(measured_start, 3),
        "length_mm": round(measured_end - measured_start, 3),
        "width_mm": round(2.0 * h * frame.scale_v, 3),
    }
    length_tol, width_tol = shaft_tolerances(scale)
    problems = []
    if abs(measured["axial_start_mm"] - float(start)) > length_tol:
        problems.append(f"начало {measured['axial_start_mm']:g} мм, прочитано {float(start):g}")
    if abs(measured["length_mm"] - float(length)) > length_tol:
        problems.append(f"длина {measured['length_mm']:g} мм, прочитано {float(length):g}")
    if abs(measured["width_mm"] - float(width)) > width_tol:
        problems.append(f"ширина {measured['width_mm']:g} мм, прочитано {float(width):g}")
    return Verdict(
        status="refuted" if problems else "confirmed",
        measured=measured,
        evidence_bbox_px=(left + cl - h, top + centre - h, left + cr + h, top + centre + h),
        reason="; ".join(problems),
    )


def _arc_scores(mask: Any, centre_y: float, h: float, side: int) -> Any:
    """Доля дуги скругления под чернилами — для каждого столбца её центра.

    ``side=-1`` — левый конец (дуга выпукла влево), ``+1`` — правый.
    """
    import numpy as np

    height, width = mask.shape
    count = max(9, int(np.pi * h * _ARC_SPAN_DEG / 90.0))
    hits = np.zeros(width, dtype=float)
    used = 0
    for angle in np.radians(np.linspace(-_ARC_SPAN_DEG, _ARC_SPAN_DEG, count)):
        row = int(round(centre_y + h * np.sin(angle)))
        if not 0 <= row < height:
            continue
        used += 1
        dx = int(round(side * h * np.cos(angle)))
        line = mask[row]
        shifted = np.zeros(width, dtype=bool)
        # Значение для центра cx — чернила в столбце cx + dx (без заворота края).
        if dx >= 0:
            shifted[: width - dx] = line[dx:]
        else:
            shifted[-dx:] = line[: width + dx]
        hits += shifted
    return hits / max(1, used)


def _peaks(score: Any) -> list[float]:
    """Центры дуг: середина плато каждого прогона столбцов выше порога.

    Шаблон ложится на линию толщиной в несколько пикселей при любом центре в
    её пределах — доля выходит плато. Первый столбец плато сдвигал начало
    паза наружу на полтолщины линии, а конец — внутрь (или наоборот).
    """
    import numpy as np

    columns = np.nonzero(score >= _ARC_FLOOR)[0]
    if columns.size == 0:
        return []
    peaks: list[float] = []
    for run in np.split(columns, np.nonzero(np.diff(columns) > 1)[0] + 1):
        values = score[run]
        top = run[values >= values.max() - 0.05]
        peaks.append(float(top.mean()))
    return peaks


def _capsules(mask: Any, centre: float, h: float) -> list[tuple[float, float, float]]:
    """Капсулы с прямыми на ``centre ± h``: (центр левой дуги, правой, опора)."""
    import numpy as np

    height, width = mask.shape

    def band(y: float) -> Any:
        row = int(round(y))
        if not 0 <= row < height:
            return np.zeros(width, dtype=bool)
        return mask[max(0, row - 1) : row + 2].any(axis=0)

    straight = band(centre - h) & band(centre + h)
    cumulative = np.concatenate(([0], np.cumsum(straight)))
    left_score = _arc_scores(mask, centre, h, side=-1)
    right_score = _arc_scores(mask, centre, h, side=1)
    found = []
    for cl in _peaks(left_score):
        for cr in _peaks(right_score):
            # Прямая часть паза не короче полширины — иначе это окружность
            # (поперечное отверстие того же Ø на ступени).
            if cr - cl < 0.5 * h:
                continue
            a, b = int(round(cl)), int(round(cr))
            share = (cumulative[b + 1] - cumulative[a]) / float(b - a + 1)
            if share < _STRAIGHT_FLOOR:
                continue
            found.append((cl, cr, float(left_score[a] + right_score[b] + share)))
    return found


def _arc_centre(ink: Any, centre: float, h: float, cx: float, *, side: int) -> float:
    """Центр дуги скругления: по строкам — середина штриха минус √(h² − dy²), медиана."""
    import numpy as np

    height, width = ink.shape
    reach = max(2.0, 0.15 * h)
    for _round in range(2):
        estimates = []
        for y in range(int(round(centre - 0.8 * h)), int(round(centre + 0.8 * h)) + 1):
            if not 0 <= y < height:
                continue
            offset = float(np.sqrt(max(0.0, h * h - (y - centre) ** 2)))
            expected = cx + side * offset
            lo, hi = max(0, int(expected - reach) - 3), min(width, int(expected + reach) + 4)
            columns = np.nonzero(ink[y, lo:hi])[0]
            if columns.size == 0:
                continue
            middles = [
                (run[0] + run[-1]) / 2.0 + lo
                for run in np.split(columns, np.nonzero(np.diff(columns) > 1)[0] + 1)
            ]
            nearest = min(middles, key=lambda middle: abs(middle - expected))
            if abs(nearest - expected) <= reach:
                estimates.append(nearest - side * offset)
        if len(estimates) < 3:
            return cx
        cx = float(np.median(estimates))
    return cx


def _outside_share(ink: Any, centre: float, h: float, cl: float, cr: float) -> float:
    """Доля столбцов с чернилами в полосах сразу за прямыми паза.

    Паз лицом — на гладкой поверхности ступени: за его прямыми пусто (кроме
    подписи и размерной линии Ø). Расточка на разрезе полого вала даёт те
    же две прямые с дугами у торцов, но за ними — штриховка (shaft-8).
    """
    import numpy as np

    gap = max(3.0, 0.3 * h)
    lo, hi = int(round(cl)), int(round(cr)) + 1
    if hi <= lo:
        return 0.0
    shares = []
    for y in (centre - h - gap, centre + h + gap):
        row = int(round(y))
        if not 0 <= row < ink.shape[0]:
            continue
        shares.append(float(np.mean(ink[row, lo:hi])))
    return max(shares) if shares else 0.0


def _hatch_score(ink: Any, centre: float, h: float, cl: float, cr: float) -> float:
    """Штриховка за прямыми паза: совпадение строк y и y+d со сдвигом ±d сверх сдвига 0.

    Штриховка разреза — линии под 45° (ГОСТ 2.306): чернила строки через d
    строк повторяются со сдвигом на d столбцов. Вертикали (грани, выносные,
    размерная линия Ø) совпадают без сдвига, горизонталь — при любом сдвиге,
    подпись — случайно. Доля столбцов тут не годится: у паза у торца полоса
    за прямой ложится на линию фаски (shaft-4: 1,0 у верного паза).
    """
    import numpy as np

    height, width = ink.shape
    gap = max(3.0, 0.3 * h)
    step = max(2, int(round(0.15 * h)))
    lo, hi = max(step, int(round(cl - h))), min(width - step, int(round(cr + h)) + 1)
    if hi - lo < 4 * step:
        return 0.0
    score = 0.0
    for sign in (-1, 1):
        y = int(round(centre + sign * (h + gap)))
        y2 = y + sign * step
        if not (0 <= y < height and 0 <= y2 < height):
            continue
        base = ink[y, lo:hi]
        straight = float(np.mean(base & ink[y2, lo:hi]))
        diagonal = max(
            float(np.mean(base & ink[y2, lo + shift : hi + shift])) for shift in (-step, step)
        )
        score = max(score, diagonal - straight)
    return score


def _refined_half(ink: Any, centre: float, h: float, cl: float, cr: float) -> float:
    """Полуширина по медиане середин прямых в средней части паза."""
    import numpy as np

    lo, hi = int(cl + 0.2 * (cr - cl)), int(cr - 0.2 * (cr - cl)) + 1
    reach = max(2.0, 0.15 * h)
    edges: dict[int, list[float]] = {-1: [], 1: []}
    for x in range(max(0, lo), min(ink.shape[1], hi)):
        rows = np.nonzero(ink[:, x])[0]
        if rows.size == 0:
            continue
        for run in np.split(rows, np.nonzero(np.diff(rows) > 1)[0] + 1):
            middle = (run[0] + run[-1]) / 2.0
            for side in (-1, 1):
                if abs(middle - (centre + side * h)) <= reach:
                    edges[side].append(middle)
    if not edges[-1] or not edges[1]:
        return h
    return (float(np.median(edges[1])) - float(np.median(edges[-1]))) / 2.0
