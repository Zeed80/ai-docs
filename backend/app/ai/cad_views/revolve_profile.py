"""Этап D1: полупрофиль тела вращения по изображению — как читает инженер.

На виде или разрезе тела вращения каждая основная линия контура имеет
зеркальную пару относительно оси. Идя вдоль оси, в каждом столбце берутся
симметричные пары горизонтальных штрихов: самая дальняя — наружный контур,
следующая за ней — стенка расточки (на разрезе). Штриховка под 45° и
вертикальные размерные линии снимаются горизонтальным размыканием; класс
детали не нужен — годится для вала, втулки, полой детали с конусом и дугой
(«Опора пружин»: 5 ступеней, шейка по дуге, конус 18°, трубка).

Выход в пикселях изображения; в миллиметры его переводит этап C (надписи Ø).

Постолбцовый разбор по линиям (первый прототип) путал штрихи штриховки с
кромкой тонкой стенки; профиль строится по маске материала разреза
(`section_material`): верх участка материала — наружный контур, низ — расточка.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HalfProfile:
    axis_y: float
    line_px: float
    x0: int
    x1: int
    outer: list[tuple[float, float]] = field(default_factory=list)  # (x, r) ломаная
    inner: list[tuple[float, float]] = field(default_factory=list)  # пусто — сплошное


def _despike(values: list[float | None], width: int) -> list[float | None]:
    """Уровни короче ``width`` столбцов между двумя одинаковыми соседями —
    стыки граней и пересечения со штриховкой, а не ступени: заменяются соседом."""
    out = list(values)
    runs: list[list] = []  # [start, end, level]
    for x, v in enumerate(out):
        if runs and v is not None and runs[-1][2] is not None and abs(v - runs[-1][2]) <= 1.5:
            runs[-1][1] = x
        elif runs and v is None and runs[-1][2] is None:
            runs[-1][1] = x
        else:
            runs.append([x, x, v])
    for i in range(1, len(runs) - 1):
        start, end, _level = runs[i]
        before, after = runs[i - 1][2], runs[i + 1][2]
        if end - start + 1 < width and before is not None and after is not None:
            # Выброс между соседями: одинаковыми — их уровень, разными —
            # плавный переход (стрелка размера у конуса, «Опора пружин»:
            # расточка закрывалась до оси на 0,6 мм — перемычка, которой нет).
            span = end - start + 2
            for x in range(start, end + 1):
                t = (x - start + 1) / span
                out[x] = before if abs(before - after) <= 2.0 else before + (after - before) * t
    return out


def _median(values: list[float | None], window: int) -> list[float | None]:
    """Скользящая медиана по присутствующим значениям: выброс уже половины окна
    (стрелка размерной линии у грани) уходит, уступ остаётся уступом."""
    import numpy as np

    half = max(1, window // 2)
    out: list[float | None] = []
    for x, v in enumerate(values):
        if v is None:
            out.append(None)
            continue
        near = [u for u in values[max(0, x - half) : x + half + 1] if u is not None]
        out.append(float(np.median(near)))
    return out


def drop_fins(values: list[float | None], width: int, rise: float = 0.15) -> list[float | None]:
    """Узкий выступ (короче ``width`` столбцов, выше обоих соседей на долю
    ``rise``) — линия, симметричная оси (выноска, рамка допуска), а не бурт:
    на валу из методички такие «диски» вырастали в теле."""
    out = list(values)
    x = 0
    n = len(out)
    while x < n:
        if out[x] is None:
            x += 1
            continue
        left = out[x - 1] if x > 0 else None
        end = x
        while (
            end + 1 < n
            and out[end + 1] is not None
            and left is not None
            and out[end + 1] > left * (1 + rise)
        ):
            end += 1
        right = out[end + 1] if end + 1 < n else None
        if (
            left is not None
            and right is not None
            and out[x] > left * (1 + rise)
            and out[x] > right * (1 + rise)
            and end - x + 1 < width
        ):
            level = max(left, right)
            for k in range(x, end + 1):
                out[k] = level
            x = end + 1
            continue
        x += 1
    return out


def _simplify(points: list[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
    import cv2
    import numpy as np

    if len(points) < 3:
        return points
    curve = np.array(points, dtype=np.float32).reshape(-1, 1, 2)
    return [(float(p[0][0]), float(p[0][1])) for p in cv2.approxPolyDP(curve, tolerance, False)]


def _end_face(
    ink: Any, axis_y: int, x: int, r_out: float, r_in: float, line: float, side: int
) -> int:
    """Торец за краем материала: самая дальняя вертикаль, перекрывающая
    стенку по высоте, не дальше 6 толщин линии (край кольца с фаской в маску
    материала не попадает — «Опора пружин»: 30 px, длина 26,8 вместо 29)."""

    top = int(axis_y - r_out + 0.3 * line)
    bottom = int(axis_y - max(r_in, 0.0) - 0.3 * line)
    if bottom - top < 2:
        return x
    best = x
    reach = int(6 * line)
    for dx in range(1, reach + 1):
        col = x + side * dx
        if not 0 <= col < ink.shape[1]:
            break
        window = ink[top:bottom, max(0, col - 1) : col + 2].any(axis=1)
        if window.mean() >= 0.8:
            best = col
    return best


def profile_from_material(
    material: Any, axis_y: int, line: float, ink: Any = None
) -> HalfProfile | None:
    """Полупрофиль по маске материала разреза (`section_material`).

    В каждом столбце над осью — участок материала, самый удалённый от оси:
    его верх — наружный контур, низ — стенка расточки (у сплошной детали
    участок доходит до оси — расточки нет). Граница материала идёт по краю
    линии, сама линия — на полтолщины дальше. Короткие разрывы вдоль оси
    (линии поперечного отверстия, стыки граней) перекрываются.
    """
    import numpy as np

    width = material.shape[1]

    def half(mask: Any, axis: int) -> tuple[list[float | None], list[float | None]]:
        outer_h: list[float | None] = [None] * width
        inner_h: list[float | None] = [None] * width
        columns = []
        for x in range(width):
            column = mask[:axis, x]
            edges = np.diff(np.concatenate([[0], column, [0]]))
            columns.append((np.where(edges == 1)[0], np.where(edges == -1)[0]))
        # Полая деталь (у большинства столбцов материал не доходит до оси) —
        # материала у оси нет: такой участок — надписи внутри расточки
        # («Ø7,6» со штрихами цифр), а не сплошное сечение.
        reaching = [len(e) and e[-1] >= axis - line for _s, e in columns if len(e)]
        hollow_part = bool(reaching) and sum(reaching) < 0.3 * len(reaching)
        for x in range(width):
            starts, ends = columns[x]
            if hollow_part:
                keep = [i for i in range(len(ends)) if ends[i] < axis - line]
                starts, ends = starts[keep], ends[keep]
            if not len(starts):
                continue
            top, bottom = int(starts[0]), int(ends[0])
            outer_h[x] = axis - top + line / 2.0
            inner_h[x] = max(0.0, axis - bottom - line / 2.0) if bottom < axis - line else 0.0
        return outer_h, inner_h

    # Обе половины разреза: у тела вращения наружный контур — наибольший из
    # двух, расточка — наименьшая; шпоночный паз, лыска — местные элементы с
    # одной стороны (колесо p009: паз в расточке сверху давал Ø36 вместо
    # Ø30). Незаштрихованная половина (половина вида + половина разреза) —
    # берётся та, что есть.
    upper = half(material, axis_y)
    lower = half(material[::-1], material.shape[0] - axis_y)
    outer = [None] * width
    inner = [None] * width
    for x in range(width):
        pairs = [(o, i) for o, i in ((upper[0][x], upper[1][x]), (lower[0][x], lower[1][x])) if o]
        if not pairs:
            continue
        outer[x] = max(o for o, _i in pairs)
        inner[x] = min(i or 0.0 for _o, i in pairs)
    present = [x for x, v in enumerate(outer) if v is not None]
    # Разрыв материала — отверстие поперёк оси (стенки не заштрихованы) или
    # стык граней; до четверти длины детали — та же деталь.
    span = (present[-1] - present[0]) if present else 0
    gap = max(int(10 * line), int(0.45 * span))
    for a, b in zip(present, present[1:]):
        if 1 < b - a <= gap:
            for x in range(a + 1, b):
                outer[x] = max(outer[a], outer[b])
                inner[x] = min(inner[a], inner[b])
    outer = drop_fins(_median(_despike(outer, int(2 * line)), int(2 * line) + 1), int(2.5 * line))
    inner = _median(_despike(inner, int(4 * line)), int(2 * line) + 1)
    runs: list[list[int]] = []
    for x, r in enumerate(outer):
        if r is None:
            continue
        if runs and x - runs[-1][1] <= gap:
            runs[-1][1] = x
        else:
            runs.append([x, x])
    if not runs:
        return None
    x0, x1 = max(runs, key=lambda run: run[1] - run[0])
    if ink is not None:
        new0 = _end_face(ink, axis_y, x0, outer[x0], inner[x0] or 0.0, line, -1)
        new1 = _end_face(ink, axis_y, x1, outer[x1], inner[x1] or 0.0, line, 1)
        for x in range(new0, x0):
            outer[x], inner[x] = outer[x0], inner[x0]
        for x in range(x1 + 1, new1 + 1):
            outer[x], inner[x] = outer[x1], inner[x1]
        x0, x1 = new0, new1
    tolerance = max(1.0, 0.5 * line)
    outer_pts = [(float(x), float(outer[x])) for x in range(x0, x1 + 1) if outer[x] is not None]
    inner_pts = [
        (float(x), float(inner[x] or 0.0)) for x in range(x0, x1 + 1) if outer[x] is not None
    ]
    hollow = any(r > line for _x, r in inner_pts)
    return HalfProfile(
        axis_y=float(axis_y),
        line_px=line,
        x0=int(x0),
        x1=int(x1),
        outer=_simplify(outer_pts, tolerance),
        inner=_simplify(inner_pts, tolerance) if hollow else [],
    )


def plateaus(
    points: list[tuple[float, float]], min_length: float
) -> list[tuple[float, float, float]]:
    """Участки постоянного радиуса ломаной: (x начала, x конца, r)."""
    out = []
    for (xa, ra), (xb, rb) in zip(points, points[1:]):
        if xb - xa >= min_length and abs(rb - ra) <= 0.02 * max(ra, rb, 1.0):
            out.append((xa, xb, (ra + rb) / 2.0))
    return out


def fit_scale(
    profile: HalfProfile,
    outer_labels: list[float],
    inner_labels: list[float],
    *,
    near: float | None = None,
    spread: float = 0.06,
) -> tuple[float | None, int]:
    """мм/px, при котором больше всего площадок профиля объяснены надписями Ø.

    Надпись с квалитетом отверстия (H) — внутренняя поверхность, прочие —
    наружная или любая (`labels.parse_label`). Совпадение — в пределах 2 %.
    Возвращает (масштаб, число различных надписей, объяснивших площадки);
    без надписей — (None, 0).
    """
    import numpy as np

    min_length = 3 * profile.line_px
    outer_d = [2 * r for _a, _b, r in plateaus(profile.outer, min_length)]
    inner_d = [2 * r for _a, _b, r in plateaus(profile.inner, min_length) if r > 0]
    labels_any = sorted(set(outer_labels) | set(inner_labels))
    if not labels_any or not (outer_d or inner_d):
        return None, 0
    candidates = [lab / d for lab in labels_any for d in outer_d + inner_d if d > 0]
    if near is not None:
        # Масштаб рядом с заданным (габарит вдоль оси; фото анизотропно на
        # несколько процентов).
        candidates = [c for c in candidates if abs(c - near) <= spread * near]
    best: tuple[int, float, float] | None = None
    for scale in candidates:
        # Счёт — различные надписи, объяснившие площадки: две шейки Ø25 —
        # одно подтверждение масштаба, а не два (иначе одна надпись на листе
        # без чисел «объясняла» весь вал).
        matched: set[float] = set()
        residual = 0.0
        # Расточка по штриховке систематически меньше надписи (~0,4 мм:
        # «Опора», 8,07 при Ø8,5H10); надписи с полем допуска отверстия (H)
        # сопоставляются с ней в 5 %, прочие — в 2 %.
        for diameters, pool, share in (
            (outer_d, outer_labels or labels_any, 0.02),
            (inner_d, inner_labels or labels_any, 0.05 if inner_labels else 0.02),
        ):
            for d in diameters:
                mm = d * scale
                nearest = min(pool, key=lambda v: abs(v - mm))
                if abs(nearest - mm) <= share * nearest:
                    matched.add(nearest)
                    residual += abs(nearest - mm) / nearest
        hits = len(matched)
        key = (hits, -residual)
        if best is None or key > (best[0], -best[1]):
            best = (hits, residual, scale)
    return (float(np.round(best[2], 6)), best[0]) if best else (None, 0)


def fit_axial_scale(
    profile: HalfProfile, linear_labels: list[float], near: float | None = None
) -> tuple[float | None, int]:
    """мм/px вдоль оси: линейные надписи — расстояния между изломами профиля.

    Вдоль оси масштаб свой: выпрямленное фото анизотропно до нескольких
    процентов («Опора пружин»: 0,0278 вдоль при 0,0266 поперёк), и длины по
    радиальному масштабу уезжали на 4 %. ``near`` — радиальный масштаб:
    кандидаты дальше ±15 % от него не рассматриваются.
    """
    xs = sorted(
        {round(x) for x, _r in profile.outer}
        | {round(x) for x, _r in profile.inner}
        | {profile.x0, profile.x1}
    )
    distances = [
        b - a for i, a in enumerate(xs) for b in xs[i + 1 :] if b - a > 2 * profile.line_px
    ]
    labels = [v for v in linear_labels if v > 0]
    if not labels or not distances:
        return None, 0
    # Габарит: наибольший линейный размер — длина детали (ГОСТ 2.307 требует
    # габаритный размер). По изломам профиля подбор неразличим — их десятки,
    # и любой масштаб «объясняет» почти все надписи.
    # Габарит — наибольшая надпись, согласная с длиной профиля: лишняя
    # «180» (угол без знака градуса) при габарите 158 уводила масштаб к
    # подбору по мелким надписям — длина 155,7 (многоосевой вал 2).
    span = max(1, profile.x1 - profile.x0)
    fitting = [v for v in labels if near is None or 0.9 * near <= v / span <= 1.1 * near]
    overall = max(fitting or labels) / span
    if near is None or 0.9 * near <= overall <= 1.1 * near:
        hits = sum(
            1
            for other in labels
            if min(abs(dd * overall - other) for dd in distances)
            <= max(0.015 * other, 0.6 * profile.line_px * overall)
        )
        return overall, hits
    best: tuple[float, float, float, int] | None = None
    for label in labels:
        for d in distances:
            scale = label / d
            if near and not 0.85 * near <= scale <= 1.15 * near:
                continue
            # Мелкая надпись (фаска 0,5, канавка 2) совпадёт со случайным
            # расстоянием при любом масштабе — вес совпадения по величине.
            weight, residual, hits = 0.0, 0.0, 0
            for other in labels:
                gap = min(abs(dd * scale - other) for dd in distances)
                if gap <= max(0.015 * other, 0.6 * profile.line_px * scale):
                    hits += 1
                    weight += other
                    residual += gap / other
            if best is None or (weight, -residual) > (best[0], -best[1]):
                best = (weight, residual, scale, hits)
    return (best[2], best[3]) if best else (None, 0)
