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
    scale_u, scale_v = frame.mm_per_px, frame.scale_v
    length_tol, diameter_tol = shaft_tolerances(frame.scale_mean)
    shoulders = _shoulders(profile, jump_px=max(2.0, 0.5 / scale_v))
    measured_steps = []
    problems = []
    station = 0.0
    boundaries = [0.0]
    for step in steps:
        station += float(step.get("length_mm") or 0.0)
        boundaries.append(station)
    matched = [0.0]
    for boundary in boundaries[1:-1]:
        x = frame.origin_px[0] + boundary / scale_u
        # В окне станции — уступ с наибольшим скачком: край канавки у уступа
        # ближе, но мельче (shaft-0: канавка 0,3 мм у границы 57 давала
        # длины 14,5 и 25,5 вместо 15 и 25).
        window = [(s, jump) for s, jump in shoulders if abs(s - x) * scale_u <= 2.0 * length_tol]
        if window:
            best = max(window, key=lambda item: item[1])[0]
            matched.append((best - frame.origin_px[0]) * scale_u)
        else:
            matched.append(None)
    matched.append(boundaries[-1])
    for index, step in enumerate(steps):
        start, end = boundaries[index], boundaries[index + 1]
        a = frame.origin_px[0] + (start + _CORE[0] * (end - start)) / scale_u
        b = frame.origin_px[0] + (start + _CORE[1] * (end - start)) / scale_u
        halves = [profile.half_at(x) for x in np.arange(a, b + 1.0)]
        halves = [h for h in halves if h is not None]
        diameter = round(2.0 * float(np.median(halves)) * scale_v, 3) if halves else None
        left, right = matched[index], matched[index + 1]
        length = round(right - left, 3) if left is not None and right is not None else None
        measured_steps.append({"diameter_mm": diameter, "length_mm": length})
        read_d, read_l = step.get("diameter_mm"), step.get("length_mm")
        if diameter is not None and read_d and abs(diameter - float(read_d)) > diameter_tol:
            problems.append(f"ступень {index + 1}: Ø {diameter:g}, прочитано {read_d:g}")
        if length is not None and read_l and abs(length - float(read_l)) > length_tol:
            problems.append(f"ступень {index + 1}: длина {length:g}, прочитано {read_l:g}")
        if left is None or right is None:
            problems.append(f"ступень {index + 1}: уступ на прочитанной станции не найден")
    unmeasured = all(item["diameter_mm"] is None for item in measured_steps)
    return Verdict(
        status="unmeasurable" if unmeasured else ("refuted" if problems else "confirmed"),
        measured={"steps": measured_steps},
        evidence_bbox_px=frame.bbox_px,
        reason="; ".join(problems),
    )


def _shoulders(profile: Any, *, jump_px: float) -> list[tuple[float, float]]:
    """Уступы профиля: ``(столбец грани уступа, величина скачка в px)``.

    Профиль делится на площадки постоянного уровня. На фаске у уступа
    горизонтальной пары нет — между площадками пропуск, и первая версия
    ставила уступ на дальний край фаски (длины мимо на 0,5–1 мм). Грань
    уступа — там, где кончается или начинается МЕНЬШАЯ ступень: её кромка
    доходит до самой грани.
    """
    plateaus = _plateaus(profile)
    result = []
    for (a_start, a_end, a_level), (b_start, b_end, b_level) in zip(plateaus, plateaus[1:]):
        jump = abs(b_level - a_level)
        if jump < jump_px:
            continue
        # Максимум по парам переключается на большую ступень там, где
        # начинается её кромка — у внешнего края толстой линии грани: уступ
        # уезжал на полтолщины в сторону большей ступени (подъём — раньше,
        # спуск — позже, до 0,8 мм при 1:2). Точная грань — центр вертикали
        # в окне перехода; нет её — середина промежутка.
        middle = profile.x0 + (a_end + b_start) / 2.0
        window = max(8.0, (b_start - a_end) + 6.0)
        nearby = [x for x in profile.faces_px if abs(x - middle) <= window]
        face = min(nearby, key=lambda x: abs(x - middle)) if nearby else middle
        result.append((face, jump))
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
