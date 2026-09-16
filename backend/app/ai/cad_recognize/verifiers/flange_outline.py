"""Контур фланца тела вращения по виду с торца: круг с лысками.

Втулка part_03: фланец — круг Ø29, срезанный тремя лысками; надпись «24» —
от лыски до противоположной точки круга. Трассировать контур нельзя: на
увеличенном листе выносные размеров становятся толстыми и прилипают к кромке.
Поэтому контур не восстанавливается, а ПРОВЕРЯЕТСЯ: надписи дают гипотезы
(Ø круга, расстояние лыски, число лысок), лист выбирает ту, чья линия покрыта
основными чернилами почти целиком и заметно лучше соседних.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.ai.cad_recognize.verifiers.plate_contour import plate_labels

# Доля линии гипотезы, покрытая основными чернилами, чтобы её принять.
_MIN_COVERAGE = 0.9
# Насколько принятая гипотеза обязана быть лучше другой формы (число или
# положение лысок): на втулке соседние гипотезы давали ≤ 0,69 при 1,00.
_MIN_MARGIN = 0.15
_FLAT_COUNTS = (2, 3, 4, 6)
_SAMPLES = 1440
_REFINE_TOP = 5
_MIN_RADIUS_LINES = 15.0
# Масштаб вида принимается, если на нём ≥ 2 надписи Ø легли на окружности вида.
_MIN_SCALE_SUPPORT = 2
_SCALES_KEPT = 3
# Линия гипотезы против той же формы, сдвинутой на 4 толщины линии.
_MIN_CONTRAST = 0.5
_CENTRE_GROUP_PX = 40


@dataclass
class FlangeOutline:
    diameter_mm: float
    flats: int
    flat_distance_mm: float | None
    phase_deg: float
    centre_px: tuple[float, float]
    px_per_mm: float
    coverage: float
    margin: float
    contrast: float = 0.0

    def sketch(self) -> tuple[list[dict[str, Any]], tuple[float, float]]:
        """Эскиз от оси (x вправо, y вверх): сегменты и начало эскиза от оси."""
        return outline_sketch(
            self.diameter_mm / 2.0, self.flats, self.flat_distance_mm, self.phase_deg
        )


def outline_sketch(
    radius: float, flats: int, distance: float | None, phase_deg: float
) -> tuple[list[dict[str, Any]], tuple[float, float]]:
    """Круг радиуса ``radius`` с ``flats`` лысками на ``distance`` от оси.

    Возвращает сегменты эскиза (начало (0, 0) — первая вершина) и положение
    этой вершины от оси. Без лысок — круг из двух дуг.
    """
    if not flats or distance is None:
        start = (radius, 0.0)
        segments = [
            {"kind": "arc", "to": (-2 * radius, 0.0), "center": (-radius, 0.0), "clockwise": False},
            {"kind": "arc", "to": (0.0, 0.0), "center": (-radius, 0.0), "clockwise": False},
        ]
        return segments, start
    half = math.acos(max(-1.0, min(1.0, distance / radius)))
    step = 2 * math.pi / flats
    normals = [math.radians(phase_deg) + j * step for j in range(flats)]
    points: list[tuple[str, tuple[float, float]]] = []
    if half >= step / 2:
        # Лыски пересекаются раньше круга — многоугольник без дуг.
        for j, normal in enumerate(normals):
            corner = normal + step / 2
            reach = distance / math.cos(step / 2)
            points.append(("line", (reach * math.cos(corner), reach * math.sin(corner))))
    else:
        for normal in normals:
            points.append(("arc", _polar(radius, normal - half)))
            points.append(("line", _polar(radius, normal + half)))
    origin = points[-1][1]
    segments = []
    for kind, (x, y) in points:
        to = (round(x - origin[0], 6), round(y - origin[1], 6))
        if kind == "arc":
            segments.append(
                {
                    "kind": "arc",
                    "to": to,
                    "center": (round(-origin[0], 6), round(-origin[1], 6)),
                    "clockwise": False,
                }
            )
        else:
            segments.append({"kind": "line", "to": to})
    segments[-1]["to"] = (0.0, 0.0)
    return segments, (round(origin[0], 6), round(origin[1], 6))


def _polar(radius: float, angle: float) -> tuple[float, float]:
    return radius * math.cos(angle), radius * math.sin(angle)


def propose_flange_outline(
    gray: Any, spec: dict[str, Any], *, centre_px: tuple[float, float] | None = None
) -> tuple[FlangeOutline | None, str]:
    """Контур фланца по виду с торца или ``None`` с причиной."""
    import numpy as np

    from app.ai.cad_recognize.sheet_upscale import main_line_px
    from app.ai.cad_recognize.verifiers.shaft_profile import _MIN_LINE_PX

    gray = np.asarray(gray)
    labels = plate_labels(spec)
    diameters = sorted({d for d in labels["diameters"] if d > 0}, reverse=True)
    if not diameters:
        return None, "на листе нет надписей Ø"
    line_px = main_line_px(gray)
    if line_px < _MIN_LINE_PX:
        return None, f"лист слишком грубый: основная линия {line_px:.1f} px"
    thick = _main_lines(gray, line_px)
    covered = _covered_mask(thick, line_px)
    offset_px = 4.0 * line_px
    centres = [centre_px] if centre_px else _concentric_centres(thick, line_px)
    if not centres:
        return None, "концентрических окружностей (оси вида с торца) нет"
    centres = [_refine_centre(thick > 0, centre, line_px) for centre in centres]
    axial = sorted({v for v in labels["axial"] if v > 0})
    coarse: list[tuple[float, tuple[float, float], float, float, int, float | None, float]] = []
    for centre in centres:
        for diameter, scale in _scale_candidates(covered, centre, diameters):
            # Лыска отличима от круга, только если контур много крупнее допуска
            # положения: на втулке окружность отверстия r 59 px «вмещала» Ø29 с
            # любыми лысками при 4,2 px/мм — всё покрыто штрихом.
            if diameter / 2.0 * scale < _MIN_RADIUS_LINES * line_px:
                continue
            coarse.extend(
                _coarse_shapes(
                    covered,
                    centre,
                    scale,
                    diameter,
                    axial,
                    offset_px,
                    _MIN_RADIUS_LINES * line_px,
                )
            )
    if not coarse:
        return None, "ни одна гипотеза контура не легла на лист"
    # Ранжирование — по контрасту; из вариантов одной окружности (центр, Ø) —
    # лучший, иначе пятёрка уходит на одну и ту же фигуру.
    coarse.sort(key=lambda item: item[0], reverse=True)
    distinct: dict[tuple[int, int, float], tuple] = {}
    for item in coarse:
        key = (round(item[1][0] / 20), round(item[1][1] / 20), item[3])
        distinct.setdefault(key, item)
    top = list(distinct.values())[:_REFINE_TOP]
    found = [_refined(covered, item, axial, offset_px) for item in top]
    accepted = [
        o
        for o in found
        if o.coverage >= _MIN_COVERAGE and o.margin >= _MIN_MARGIN and o.contrast >= _MIN_CONTRAST
    ]
    if not accepted:
        best = max(found, key=lambda o: o.coverage)
        if best.coverage < _MIN_COVERAGE:
            return None, f"лучшая гипотеза контура покрыта на {best.coverage:.2f}"
        if best.contrast < _MIN_CONTRAST:
            return (
                None,
                f"гипотеза контура лежит в сплошных чернилах (контраст {best.contrast:.2f})",
            )
        return None, (
            f"контур неоднозначен: соседняя форма покрыта почти так же "
            f"({best.coverage:.2f}, запас {best.margin:.2f})"
        )
    # Фланец — наружный контур вида: окружности ступицы и расточки тоже
    # покрыты целиком, но лежат внутри.
    return max(accepted, key=lambda o: (o.diameter_mm, o.coverage)), ""


def _main_lines(gray: Any, line_px: float) -> Any:
    """Основные линии: чернила без тонких (размерных, осевых, выносных)."""
    import cv2
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _ink

    ink = _ink(gray).astype(np.uint8)
    kernel = max(3, int(round(0.7 * line_px)))
    return cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((kernel, kernel), np.uint8))


def _covered_mask(thick: Any, line_px: float) -> Any:
    """Основные линии, расширенные на допуск положения."""
    import cv2
    import numpy as np

    grow = max(3, int(round(1.3 * line_px)) | 1)
    return cv2.dilate(thick, np.ones((grow, grow), np.uint8)) > 0


def _refine_centre(thick: Any, centre: tuple[float, float], line_px: float) -> tuple[float, float]:
    """Центр, в котором концентрические окружности вида резче всего: сумма пяти
    лучших долей покрытия по радиусам на НЕрасширенных основных линиях (на
    расширенных доля насыщается и пик размыт). Втулка: грубый центр в 26 px от
    оси → 5 px."""
    import numpy as np

    height, width = thick.shape
    angles = np.linspace(0, 2 * np.pi, 720, endpoint=False)
    cos, sin = np.cos(angles), np.sin(angles)

    def sharpness(c: tuple[float, float]) -> float:
        reach = int(min(c[0], c[1], width - 1 - c[0], height - 1 - c[1]))
        radii = np.arange(int(5 * line_px), max(int(5 * line_px) + 1, reach))
        xs = (c[0] + np.outer(radii, cos)).round().astype(int)
        ys = (c[1] - np.outer(radii, sin)).round().astype(int)
        share = thick[ys, xs].mean(axis=1)
        return float(np.sort(share)[-5:].sum())

    for step, span in ((4, 32), (1, 4)):
        centre = max(
            (
                (centre[0] + dx, centre[1] + dy)
                for dx in range(-span, span + 1, step)
                for dy in range(-span, span + 1, step)
            ),
            key=sharpness,
        )
    return centre


def _concentric_centres(thick: Any, line_px: float) -> list[tuple[float, float]]:
    """Грубые оси видов с торца: замкнутые контуры основных линий, ложащиеся на
    окружность, — у одного центра ≥ 2 разных радиусов.

    Хаф по листу не годится: на увеличенном листе он даёт десятки тысяч
    окружностей шума. Контуры, к которым прилипли выноски, смещают центр на
    десятки пикселей — это исправляет широкое уточнение гипотезы.
    """
    import cv2
    import numpy as np

    contours, _ = cv2.findContours(thick, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    fits = []
    for contour in contours:
        if len(contour) < 120:
            continue
        points = contour[:, 0, :].astype(float)
        system = np.c_[2 * points[:, 0], 2 * points[:, 1], np.ones(len(points))]
        (cx, cy, c), *_ = np.linalg.lstsq(system, (points**2).sum(1), rcond=None)
        radius = math.sqrt(max(c + cx * cx + cy * cy, 0.0))
        if radius < 5 * line_px:
            continue
        residual = np.abs(np.hypot(points[:, 0] - cx, points[:, 1] - cy) - radius)
        if np.percentile(residual, 90) <= max(1.5 * line_px, 0.02 * radius):
            fits.append((cx, cy, radius))
    groups: list[list[tuple[float, float, float]]] = []
    for fit in fits:
        for group in groups:
            if math.hypot(fit[0] - group[0][0], fit[1] - group[0][1]) <= _CENTRE_GROUP_PX:
                group.append(fit)
                break
        else:
            groups.append([fit])
    centres = []
    for group in sorted(groups, key=lambda g: max(f[2] for f in g), reverse=True):
        if len({round(f[2] / line_px) for f in group}) >= 2:
            centres.append(
                (float(np.mean([f[0] for f in group])), float(np.mean([f[1] for f in group])))
            )
    return centres[:4]


def _scale_candidates(
    covered: Any, centre: tuple[float, float], diameters: list[float]
) -> list[tuple[float, float]]:
    """(Ø, px/мм) на масштабах, которые объясняют НЕСКОЛЬКО надписей Ø сразу.

    Одна окружность «совпадает» с любой надписью при своём масштабе (втулка:
    окружность ступицы r 294 px была и Ø29, и Ø25, и Ø22). Верный масштаб кладёт
    на пики радиального профиля несколько надписей: Ø15, Ø13, Ø11 → 343, 297,
    252 px при 45,75 px/мм.
    """
    import numpy as np

    height, width = covered.shape
    reach = int(min(centre[0], centre[1], width - 1 - centre[0], height - 1 - centre[1]))
    if reach < 20:
        return []
    angles = np.linspace(0, 2 * np.pi, 720, endpoint=False)
    radii = np.arange(5, reach)
    xs = (centre[0] + np.outer(radii, np.cos(angles))).round().astype(int)
    ys = (centre[1] - np.outer(radii, np.sin(angles))).round().astype(int)
    share = covered[ys, xs].mean(axis=1)
    # Окружность на расширенной маске — плато, а не пик: радиус — середина
    # участка не ниже 0,8 максимума (конец плато уводил радиус на 4 px).
    peaks: list[float] = []
    i = 1
    while i < len(share) - 1:
        if share[i] >= 0.12 and share[i] >= share[i - 1] and share[i] >= share[i + 1]:
            level = 0.8 * share[i]
            left, right = i, i
            while left > 0 and share[left - 1] >= level:
                left -= 1
            while right < len(share) - 1 and share[right + 1] >= level:
                right += 1
            peaks.append(float(radii[left] + radii[right]) / 2.0)
            i = right + 1
            continue
        i += 1
    if not peaks:
        return []
    peak_array = np.array(peaks, dtype=float)

    def support(scale: float) -> int:
        used: set[int] = set()
        count = 0
        for diameter in diameters:
            expected = diameter / 2.0 * scale
            if expected > reach:
                continue
            gaps = np.abs(peak_array - expected)
            index = int(np.argmin(gaps))
            if index not in used and gaps[index] <= max(3.0, 0.015 * expected):
                used.add(index)
                count += 1
        return count

    scored = sorted(
        (
            (support(peak / (diameter / 2.0)), peak / (diameter / 2.0))
            for peak in peaks
            for diameter in diameters
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    ranked: list[tuple[int, float]] = []
    for count, scale in scored:
        if count < _MIN_SCALE_SUPPORT:
            break
        if any(abs(scale / other - 1.0) < 0.01 for _c, other in ranked):
            continue
        ranked.append((count, scale))
    return [(diameter, scale) for _count, scale in ranked[:_SCALES_KEPT] for diameter in diameters]


def _coverage(
    covered: Any, centre, scale, radius, flats, distance, phase_deg, samples: int = _SAMPLES
) -> float:
    import numpy as np

    theta = np.linspace(0, 2 * np.pi, samples, endpoint=False)
    reach = np.full_like(theta, radius)
    if flats and distance is not None:
        for j in range(flats):
            cos = np.cos(theta - math.radians(phase_deg + 360.0 * j / flats))
            mask = cos > 1e-9
            reach[mask] = np.minimum(reach[mask], distance / cos[mask])
    xs = (centre[0] + reach * scale * np.cos(theta)).round().astype(int)
    ys = (centre[1] - reach * scale * np.sin(theta)).round().astype(int)
    height, width = covered.shape
    inside = (xs >= 0) & (ys >= 0) & (xs < width) & (ys < height)
    if not inside.all():
        return 0.0
    return float(covered[ys, xs].mean())


def _contrast(covered, centre, scale, radius, flats, distance, phase, samples, offset_px) -> float:
    """Покрытие линии гипотезы минус покрытие той же формы, сдвинутой внутрь и
    наружу: в штриховке разреза покрыто всё — линии там нет (втулка: шестигранник
    в штриховке с покрытием 1,00)."""
    own = _coverage(covered, centre, scale, radius, flats, distance, phase, samples)
    shift = offset_px / (radius * scale)
    around = max(
        _coverage(
            covered, centre, scale * (1 + sign * shift), radius, flats, distance, phase, samples
        )
        for sign in (-1.0, 1.0)
    )
    return own - around


def _coarse_shapes(
    covered: Any,
    centre: tuple[float, float],
    scale: float,
    diameter: float,
    axial: list[float],
    offset_px: float,
    min_reach_px: float = 0.0,
) -> list[tuple[float, tuple[float, float], float, float, int, float | None, float]]:
    """Грубо — все формы при данном Ø и масштабе: круг и лыски из надписей.
    Первый элемент — контраст линии гипотезы (см. ``_contrast``)."""
    radius = diameter / 2.0
    circle = _coverage(covered, centre, scale, radius, 0, None, 0.0, 360)
    if circle < 0.1:
        return []  # на этом радиусе нет и дуги контура
    shapes = [
        (
            _contrast(covered, centre, scale, radius, 0, None, 0.0, 360, offset_px),
            centre,
            scale,
            diameter,
            0,
            None,
            0.0,
        )
    ]
    for flats in _FLAT_COUNTS:
        for distance in _flat_distances(radius, axial):
            phase, score = max(
                (
                    (p, _coverage(covered, centre, scale, radius, flats, distance, p, 360))
                    for p in range(0, 360 // flats, 2)
                ),
                key=lambda item: item[1],
            )
            reach = min(radius, distance / math.cos(math.pi / flats))
            if score < 0.5 or reach * scale < min_reach_px:
                continue
            contrast = _contrast(
                covered, centre, scale, radius, flats, distance, phase, 360, offset_px
            )
            shapes.append((contrast, centre, scale, diameter, flats, distance, float(phase)))
    return shapes


def _flat_distances(radius: float, axial: list[float]) -> list[float]:
    """Расстояние лыски от оси по надписи: L − R (лыска → круг), L/2 (между
    лысками), L (от оси)."""
    return sorted(
        {
            round(value, 3)
            for label in axial
            for value in (label - radius, label / 2.0, label)
            if 0.3 * radius < value < radius - 1e-6
        }
    )


def _refined(covered: Any, item, axial: list[float], offset_px: float) -> FlangeOutline:
    """Широкое, затем точное уточнение центра, масштаба и фазы; запас над
    лучшей другой формой того же Ø в уточнённой системе."""
    _score, centre, scale, diameter, flats, distance, phase = item
    radius = diameter / 2.0

    def search(centre, scale, phase, step_px, reach_px, scales, phases, samples):
        best = (-1.0, centre, scale, phase)
        for dx in range(-reach_px, reach_px + 1, step_px):
            for dy in range(-reach_px, reach_px + 1, step_px):
                c = (centre[0] + dx, centre[1] + dy)
                for ds in scales:
                    for dp in phases if flats else (0.0,):
                        value = _coverage(
                            covered,
                            c,
                            scale * (1 + ds),
                            radius,
                            flats,
                            distance,
                            phase + dp,
                            samples,
                        )
                        if value > best[0]:
                            best = (value, c, scale * (1 + ds), phase + dp)
        return best

    _v, centre, scale, phase = search(
        centre, scale, phase, 4, 32, (-0.03, -0.015, 0.0, 0.015, 0.03), (-3.0, 0.0, 3.0), 360
    )
    score, centre, scale, phase = search(
        centre, scale, phase, 1, 4, (-0.005, 0.0, 0.005), (-1.0, -0.5, 0.0, 0.5, 1.0), _SAMPLES
    )
    rival = 0.0
    for flats_other in (0, *_FLAT_COUNTS):
        for distance_other in (None,) if not flats_other else _flat_distances(radius, axial):
            if (flats_other, distance_other) == (flats, distance):
                continue
            phases = range(0, 360 // flats_other, 2) if flats_other else (0,)
            rival = max(
                rival,
                max(
                    _coverage(covered, centre, scale, radius, flats_other, distance_other, p, 720)
                    for p in phases
                ),
            )
    return FlangeOutline(
        diameter_mm=diameter,
        flats=flats,
        flat_distance_mm=distance,
        phase_deg=round(phase % (360.0 / flats if flats else 360.0), 2),
        centre_px=(float(centre[0]), float(centre[1])),
        px_per_mm=float(scale),
        coverage=round(score, 3),
        margin=round(score - rival, 3),
        contrast=round(
            _contrast(covered, centre, scale, radius, flats, distance, phase, _SAMPLES, offset_px),
            3,
        ),
    )
