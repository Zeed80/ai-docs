"""Проверяльщик наружного профиля тела вращения: Ø и длины ступеней по виду.

Гипотеза ридера — ступени ``(Ø, L)`` слева направо. Профиль вида
(`shaft_frame.locate_shaft_frame`) даёт полувысоту на каждом столбце:

* Ø ступени — медиана полувысоты в средней части ступени (края — фаски,
  канавки у уступа), ×2 и в масштабе;
* границы ступеней — уступы профиля (скачок полувысоты); прочитанная граница
  подтверждается ближайшим уступом в допуске, длина — между подтверждёнными.

Вердикт один на весь профиль, по ступеням — в ``measured["steps"]``: стадия
раскладывает его по утверждениям.
"""

from __future__ import annotations

from typing import Any

from app.ai.cad_recognize.verifiers.contract import Hypothesis, Verdict
from app.ai.cad_recognize.verifiers.registry import register
from app.ai.cad_recognize.verifiers.view_frame import ViewFrame

# Средняя часть ступени, по которой меряется Ø.
_CORE = (0.2, 0.8)
# Толщина основной линии (масса, px), ниже которой лист слишком груб для
# профиля: на корпусе v7 при 300 dpi — 6 px и 7 % ложных опровержений верного
# чтения, при 150 dpi — 3 px и 18–25 %. Грубее — «не измеримо», а не догадка.
_MIN_LINE_PX = 4.5


# Запас вокруг вертикалей поперечного отверстия, px (толщина линии).
_HOLE_MARGIN_PX = 1.0


def shaft_tolerances(mm_per_px: float) -> tuple[float, float]:
    """Допуски длины и Ø, мм: не меньше пары пикселей листа."""
    return max(0.5, 2.0 * mm_per_px), max(0.3, 1.5 * mm_per_px)


@register("shaft_profile", min_feature_px=4.0)
def verify_shaft_profile(hypothesis: Hypothesis, frame: ViewFrame | None, sheet: Any) -> Verdict:
    """``expected["steps"]``: ``[{"diameter_mm", "length_mm"}, ...]``; ``sheet`` — ``ShaftProfile``."""
    import numpy as np

    steps = hypothesis.expected.get("steps") or []
    if frame is None or sheet is None:
        return Verdict(status="unmeasurable", reason="главный вид вала на листе не найден")
    if not steps:
        return Verdict(status="unmeasurable", reason="нет прочитанных ступеней")
    profile = sheet
    line_px = float(getattr(profile, "line_px", 0.0) or 0.0)
    if 0.0 < line_px < _MIN_LINE_PX:
        return Verdict(
            status="unmeasurable",
            evidence_bbox_px=frame.bbox_px,
            reason=(
                f"лист слишком грубый для проверки профиля: основная линия "
                f"{line_px:.1f} px (нужно от {_MIN_LINE_PX:g})"
            ),
        )
    # Пролёты пазов из прочитанного: под пазом ступень на виде лишена пары
    # кромок (shaft-20) или меряется по контуру паза (shaft-12: Ø 19,56 вместо
    # 20) — её Ø и пропавший уступ не проверяются, а не опровергаются.
    keyways = [
        (float(item["axial_start_mm"]), float(item["axial_start_mm"]) + float(item["length_mm"]))
        for item in hypothesis.expected.get("keyways") or []
        if isinstance(item, dict)
        and isinstance(item.get("axial_start_mm"), (int, float))
        and isinstance(item.get("length_mm"), (int, float))
    ]
    frame = chain_frame(frame, profile, [step.get("length_mm") for step in steps])
    scale_u, scale_v = frame.mm_per_px, frame.scale_v
    length_tol, diameter_tol = shaft_tolerances(frame.scale_mean)
    # Сквозное поперечное отверстие на виде сбоку — две вертикали через весь
    # диаметр: они покрывают скачок радиуса и шли уступом (holdout shaft-24:
    # Ø6 на 47,8 у уступа 55 — «длина 11,5 при 15»). Отверстия известны из
    # прочитанного — их вертикали уступом не считаются.
    hole_spans = [
        (
            frame.origin_px[0]
            + (float(item["axial_position_mm"]) - float(item["diameter_mm"]) / 2.0) / scale_u
            - _HOLE_MARGIN_PX,
            frame.origin_px[0]
            + (float(item["axial_position_mm"]) + float(item["diameter_mm"]) / 2.0) / scale_u
            + _HOLE_MARGIN_PX,
        )
        for item in hypothesis.expected.get("cross_holes") or []
        if isinstance(item, dict)
        and isinstance(item.get("axial_position_mm"), (int, float))
        and isinstance(item.get("diameter_mm"), (int, float))
    ]
    shoulders = _shoulders(
        profile,
        jump_px=max(2.0, 0.5 / scale_v),
        min_plateau_px=max(4.0, 1.5 / scale_u),
        excluded_px=hole_spans,
    )
    measured_steps = []
    problems = []
    bad_steps: set[int] = set()
    keyed_steps: set[int] = set()
    station = 0.0
    boundaries = [0.0]
    for step in steps:
        station += float(step.get("length_mm") or 0.0)
        boundaries.append(station)
    matched = [0.0]
    # Конец вида — измеренный торец, а не прочитанная сумма: при неверном
    # звене цепочки сумма мимо (shaft-1: 285 вместо 267).
    end_mm = (float(profile.x1) - frame.origin_px[0]) * scale_u
    for index, boundary in enumerate(boundaries[1:-1]):
        x = frame.origin_px[0] + boundary / scale_u
        # И от правого торца: неверное звено сдвигает все станции за ним, а
        # от конца граница за ним стоит на месте.
        predictions = [x]
        from_right = end_mm - (boundaries[-1] - boundary)
        if abs(from_right - boundary) > length_tol:
            predictions.append(frame.origin_px[0] + from_right / scale_u)
        # Окно — не ±1 мм, а до 40 % более короткой соседней ступени: граница,
        # прочитанная на 2 мм мимо, давала «уступ не найден» без замера, и
        # оператор не видел, где уступ на самом деле (харнесс: 0/28 случаев
        # «сдвинутая граница» с верным замером). Направление скачка — из
        # прочитанных Ø; из подходящих — ближайший к прочитанной станции.
        neighbours = [float(steps[index].get("length_mm") or 0.0)]
        neighbours.append(float(steps[index + 1].get("length_mm") or 0.0))
        reach_mm = max(2.0 * length_tol, 0.4 * min(neighbours))
        d_left = float(steps[index].get("diameter_mm") or 0.0)
        d_right = float(steps[index + 1].get("diameter_mm") or 0.0)
        sign = 0.0 if d_right == d_left else (1.0 if d_right > d_left else -1.0)

        def gap(s: float, predictions: list[float] = predictions) -> float:
            return min(abs(s - p) for p in predictions)

        window = [
            (s, jump)
            for s, jump in shoulders
            if gap(s) * scale_u <= reach_mm and (sign == 0.0 or jump * sign > 0)
        ]
        if window:
            best = min(window, key=lambda item: gap(item[0]))[0]
            matched.append((best - frame.origin_px[0]) * scale_u)
        else:
            matched.append(None)
    matched.append(end_mm)
    for index, step in enumerate(steps):
        start, end = boundaries[index], boundaries[index + 1]
        # Ø — в середине ступени, найденной на листе: за неверным звеном
        # прочитанные границы сдвинуты, и окно ложилось на соседнюю ступень
        # (харнесс «неверное звено»: Ø мимо у ступени сразу за ним).
        core_start, core_end = start, end
        if (
            matched[index] is not None
            and matched[index + 1] is not None
            and matched[index + 1] > matched[index]
        ):
            core_start, core_end = matched[index], matched[index + 1]
        a = frame.origin_px[0] + (core_start + _CORE[0] * (core_end - core_start)) / scale_u
        b = frame.origin_px[0] + (core_start + _CORE[1] * (core_end - core_start)) / scale_u
        halves = [profile.half_at(x) for x in np.arange(a, b + 1.0)]
        halves = [h for h in halves if h is not None]
        under_key = any(k0 < end and k1 > start for k0, k1 in keyways)
        if under_key:
            keyed_steps.add(index)
        diameter = (
            round(2.0 * float(np.median(halves)) * scale_v, 3) if halves and not under_key else None
        )
        left, right = matched[index], matched[index + 1]
        length = round(right - left, 3) if left is not None and right is not None else None
        measured_steps.append({"diameter_mm": diameter, "length_mm": length})
        read_d, read_l = step.get("diameter_mm"), step.get("length_mm")
        if diameter is not None and read_d and abs(diameter - float(read_d)) > diameter_tol:
            problems.append(f"ступень {index + 1}: Ø {diameter:g}, прочитано {read_d:g}")
            bad_steps.add(index)
        if length is not None and read_l and abs(length - float(read_l)) > length_tol:
            problems.append(f"ступень {index + 1}: длина {length:g}, прочитано {read_l:g}")
            bad_steps.add(index)
        if (left is None or right is None) and not under_key:
            problems.append(f"ступень {index + 1}: уступ на прочитанной станции не найден")
            bad_steps.add(index)
    # Настоящая ошибка ридера — одно-два значения. Разошлась большая часть —
    # значит, неверен вид или система координат: честнее «не измеримо», чем
    # опровергнуть всё прочитанное (фото, размытие, не тот вид). Считаются
    # ошибки, а не ступени: сдвинутая граница портит длины ДВУХ соседних
    # ступеней, но это одна ошибка (3 ступени — иначе сразу «больше половины»).
    diameter_bad = {
        index
        for index, (got, step) in enumerate(zip(measured_steps, steps))
        if got["diameter_mm"] is not None
        and step.get("diameter_mm")
        and abs(got["diameter_mm"] - float(step["diameter_mm"])) > diameter_tol
    }
    boundary_bad = sum(
        1
        for index, boundary in enumerate(boundaries[1:-1])
        if (matched[index + 1] is None and not {index, index + 1} & keyed_steps)
        or (matched[index + 1] is not None and abs(matched[index + 1] - boundary) > length_tol)
    )
    checked = len(steps) + max(0, len(steps) - 1)
    errors = len(diameter_bad) + boundary_bad
    if errors > checked / 2.0:
        return Verdict(
            status="unmeasurable",
            evidence_bbox_px=frame.bbox_px,
            reason=(
                f"профиль вида не сходится с прочитанным почти целиком "
                f"({errors} расхождений из {checked} величин) — вероятно, не тот вид"
            ),
        )
    unmeasured = all(
        item["diameter_mm"] is None and item["length_mm"] is None for item in measured_steps
    )
    return Verdict(
        status="unmeasurable" if unmeasured else ("refuted" if problems else "confirmed"),
        measured={"steps": measured_steps},
        evidence_bbox_px=frame.bbox_px,
        reason="; ".join(problems),
    )


def chain_frame(frame: ViewFrame | None, profile: Any, lengths_mm: list[Any]) -> ViewFrame | None:
    """Масштаб вида по прочитанной цепочке, а не по её сумме.

    Система координат вала делит прочитанную сумму длин на длину вида в px.
    Одно неверное звено (shaft-1: 98 вместо 80) уводит масштаб на 6,7 %, и
    расходится всё — предохранитель «не тот вид» прятал настоящую ошибку
    ридера. Масштаб — тот, при котором больше прочитанных станций цепочки
    ложится на уступы листа (торец — тоже уступ). Верное чтение базовый
    масштаб объясняет целиком — он и остаётся.
    """
    from itertools import accumulate

    if frame is None or profile is None:
        return frame
    lengths = [float(v) for v in lengths_mm if isinstance(v, (int, float)) and v > 0]
    if len(lengths) < 2 or len(lengths) != len(lengths_mm):
        return frame
    x0, base = frame.origin_px[0], frame.mm_per_px
    marks = [
        s
        for s, _jump in _shoulders(
            profile, jump_px=max(2.0, 0.5 / frame.scale_v), min_plateau_px=max(4.0, 1.5 / base)
        )
    ]
    x1 = float(profile.x1)
    marks.append(x1)
    stations = list(accumulate(lengths))
    total = stations[-1]

    def inliers(scale: float) -> int:
        # Станция совпадает от левого торца или от правого: звенья за
        # неверным сдвинуты от левого, но стоят на месте от правого.
        # Сама сумма от правого торца совпадает всегда — её не считаем.
        tolerance_px = shaft_tolerances(scale)[0] / scale
        hits = 0
        for c in stations:
            places = [x0 + c / scale]
            if c < total:
                places.append(x1 - (total - c) / scale)
            if min(abs(p - m) for p in places for m in marks) <= tolerance_px:
                hits += 1
        return hits

    candidates = [c / (m - x0) for c in stations for m in marks if m - x0 > 4.0]
    candidates += [(total - c) / (x1 - m) for c in stations[:-1] for m in marks if x1 - m > 4.0]
    candidates = [s for s in candidates if 0.5 * base <= s <= 2.0 * base]
    if not candidates:
        return frame
    best = max(candidates, key=lambda s: (inliers(s), -abs(s - base)))
    if inliers(best) < 2 or inliers(best) <= inliers(base):
        return frame
    return ViewFrame(bbox_px=frame.bbox_px, mm_per_px=best, origin_px=frame.origin_px)


def _shoulders(
    profile: Any,
    *,
    jump_px: float,
    min_plateau_px: float = 1.0,
    excluded_px: list[tuple[float, float]] | None = None,
) -> list[tuple[float, float]]:
    """Уступы профиля: ``(столбец грани уступа, скачок полувысоты в px со знаком)``.

    Знак: ``+`` — подъём (следующая ступень больше), ``−`` — спуск.

    Профиль делится на площадки постоянного уровня. На фаске у уступа
    горизонтальной пары нет — между площадками пропуск, и первая версия
    ставила уступ на дальний край фаски (длины мимо на 0,5–1 мм). Грань
    уступа — там, где кончается или начинается МЕНЬШАЯ ступень: её кромка
    доходит до самой грани.
    """
    # Короткие площадки — не ступени: пара дуг поперечного отверстия даёт
    # «ступень» в 2,5 мм и ложные уступы (shaft-20), канавка у уступа —
    # площадку, из-за которой переход считался не между ступенями.
    plateaus = [item for item in _plateaus(profile) if item[1] - item[0] + 1 >= min_plateau_px]
    result = []
    for (a_start, a_end, a_level), (b_start, b_end, b_level) in zip(plateaus, plateaus[1:]):
        jump = abs(b_level - a_level)
        if jump < jump_px:
            continue
        # Максимум по парам переключается на большую ступень там, где
        # начинается её кромка — у внешнего края толстой линии грани: уступ
        # уезжал на полтолщины в сторону большей ступени (подъём — раньше,
        # спуск — позже, до 0,8 мм при 1:2). Точная грань — центр вертикали
        # в окне перехода, и не любой: рядом бывает грань канавки (shaft-1:
        # 157,2 — уступ, 158,1 — канавка, ближе к середине оказалась она).
        # Грань уступа покрывает полосу скачка радиуса — от меньшей ступени
        # до большей; нет такой — середина промежутка.
        middle = profile.x0 + (a_end + b_start) / 2.0
        window = max(8.0, (b_start - a_end) + 6.0)
        # Из покрывающих скачок — ближайшая к границе БОЛЬШЕЙ ступени, а не к
        # середине: канавка режется в меньшую ступень у самого уступа, и её
        # стенка (слитая с выносной, тоже «покрывает») ближе к середине
        # перехода (shaft-6: 96,4 вместо 99,9). Фаска стоит на кромке большей
        # ступени, но грань и тогда в пределах размера фаски от этой границы.
        anchor = profile.x0 + (b_start if a_level < b_level else a_end)
        small, big = min(a_level, b_level), max(a_level, b_level)
        bands = (
            (profile.axis_y - big, profile.axis_y - small),
            (profile.axis_y + small, profile.axis_y + big),
        )

        def cover(face: tuple[float, float, float]) -> float:
            _x, top, bottom = face
            return max(
                max(0.0, min(bottom, high) - max(top, low)) / (big - small) for low, high in bands
            )

        nearby = [
            face
            for face in profile.faces_px
            if abs(face[0] - middle) <= window and cover(face) >= 0.5
        ]

        def on_hole(face: tuple[float, float, float]) -> bool:
            return any(low <= face[0] <= high for low, high in excluded_px or [])

        if nearby:
            # Вертикаль отверстия проигрывает любой другой грани, но не
            # исключается: отверстие вплотную к уступу (shaft-17: Ø8 до 119 при
            # уступе 120) — их вертикали в 2 px друг от друга.
            best = max(
                nearby,
                key=lambda face: (
                    not on_hole(face),
                    round(cover(face), 1),
                    -abs(face[0] - anchor),
                ),
            )
            result.append((best[0], b_level - a_level))
        else:
            result.append((middle, b_level - a_level))
    return result


def _plateaus(profile: Any) -> list[tuple[int, int, float]]:
    """Площадки профиля: ``(начало, конец, уровень)`` в столбцах от ``x0``."""
    import math

    plateaus: list[tuple[int, int, float]] = []
    start = level = None
    for index, value in enumerate(list(profile.half_px) + [float("nan")]):
        value = float(value)
        if start is not None and (math.isnan(value) or abs(value - level) > 1.0):
            plateaus.append((start, index - 1, level))
            start = level = None
        if not math.isnan(value) and start is None:
            start, level = index, value
    return plateaus
