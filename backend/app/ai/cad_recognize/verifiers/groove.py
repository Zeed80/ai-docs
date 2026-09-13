"""Проверяльщик канавки вала: положение, ширина и глубина по главному виду.

Канавка на продольном виде — две основные вертикали (стенки) и провал
кромки между ними. Стенки уже найдены системой координат вала
(`ShaftProfile.faces_px`: у канавки у уступа одна стенка — сама грань
уступа), полувысота по столбцам — `ShaftProfile.half_at`. Прочитанная
станция выбирает пару стенок, между которыми кромка ниже своей ступени;
положение — середина пары, ширина — расстояние между стенками (центрами
линий, как ставит размер лист), глубина — своя ступень минус дно. Своя
ступень — меньшая из соседних: канавка выхода инструмента режет меньшую
ступень у уступа.
"""

from __future__ import annotations

from typing import Any

from app.ai.cad_recognize.verifiers.contract import Hypothesis, Verdict
from app.ai.cad_recognize.verifiers.registry import register
from app.ai.cad_recognize.verifiers.view_frame import ViewFrame


def _ink_root(gray: Any, axis: float, lo: float, hi: float, own: float) -> float | None:
    """Дно канавки по чернилам: в каждом столбце между стенками — самая
    внешняя кромка внутри своей ступени, сверху и снизу от оси; медиана.

    Выше своей ступени — размерные линии и подписи, их отсекает радиус
    ``own``; ниже дна — штриховка разреза и расточка, но они внутри, а берётся
    самая внешняя кромка.
    """
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _ink

    if hi - lo < 1.0:
        return None
    top = max(0, int(axis - own) - 3)
    bottom = min(gray.shape[0], int(axis + own) + 4)
    # Область шире самой канавки: порог чернил берётся от местного фона.
    left = max(0, int(lo) - 30)
    right = min(gray.shape[1], int(hi) + 31)
    ink = _ink(np.ascontiguousarray(gray[top:bottom, left:right]))
    radii = []
    for x in range(int(np.ceil(lo)), int(hi) + 1):
        column = ink[:, x - left] if 0 <= x - left < ink.shape[1] else None
        if column is None:
            continue
        rows = np.nonzero(column)[0]
        if rows.size == 0:
            continue
        best = {-1: None, 1: None}
        for run in np.split(rows, np.nonzero(np.diff(rows) > 1)[0] + 1):
            centre = (run[0] + run[-1]) / 2.0 + top
            side = -1 if centre < axis else 1
            radius = abs(centre - axis)
            if 0.4 * own <= radius <= own - 1.0 and (best[side] is None or radius > best[side]):
                best[side] = radius
        radii += [radius for radius in best.values() if radius is not None]
    return float(np.median(radii)) if len(radii) >= 2 else None


def groove_tolerances(mm_per_px: float) -> tuple[float, float, float]:
    """Допуски положения, ширины и глубины, мм: соседние по ряду ширины
    канавок (1,6/2/3) и глубины (0,3/0,5/1) отличаются на 0,2–0,4 мм; не
    меньше пары пикселей листа."""
    return max(0.3, 2.0 * mm_per_px), max(0.25, 1.5 * mm_per_px), max(0.15, 1.0 * mm_per_px)


@register("groove", min_feature_px=3.0)
def verify_groove(hypothesis: Hypothesis, frame: ViewFrame | None, sheet: Any) -> Verdict:
    """``expected``: ``axial_position_mm`` (середина), ``width_mm``, ``depth_mm``;
    ``sheet`` — ``(серый лист, ShaftProfile)`` того же вида."""
    import numpy as np

    from app.ai.cad_recognize.verifiers.shaft_profile import _MIN_LINE_PX

    if frame is None or not sheet or sheet[1] is None:
        return Verdict(status="unmeasurable", reason="главный вид вала на листе не найден")
    profile = sheet[1]
    centre = hypothesis.expected.get("axial_position_mm")
    width = hypothesis.expected.get("width_mm")
    depth = hypothesis.expected.get("depth_mm")
    if not isinstance(centre, (int, float)) or not isinstance(width, (int, float)):
        return Verdict(status="unmeasurable", reason="нет прочитанного положения или ширины")
    line_px = float(getattr(profile, "line_px", 0.0) or 0.0)
    if 0.0 < line_px < _MIN_LINE_PX:
        return Verdict(
            status="unmeasurable",
            reason=f"лист слишком грубый: основная линия {line_px:.1f} px (нужно от {_MIN_LINE_PX:g})",
        )
    scale_u, scale_v = frame.mm_per_px, frame.scale_v
    x0 = frame.origin_px[0]
    cx = x0 + float(centre) / scale_u
    reach = (float(width) / 2.0 + max(1.5, float(width))) / scale_u
    walls = sorted(face[0] for face in profile.faces_px if abs(face[0] - cx) <= reach)
    # Полоса у стенки, где кромка ещё не своя: полтолщины самой вертикали от её
    # центра. Две толщины (первая версия) съедали всю канавку 2 мм на листе 1:2
    # — 12 px между стенками (shaft-1, shaft-4: «канавки нет»).
    gap = max(2.0, 0.5 * line_px + 1.0)
    probe = max(3.0, 0.5 / scale_u)

    def level(lo: float, hi: float) -> float | None:
        values = [profile.half_at(x) for x in np.arange(lo, hi + 1.0)]
        values = [v for v in values if v is not None]
        return float(np.median(values)) if len(values) >= 2 else None

    best = None
    for i, a in enumerate(walls):
        for b in walls[i + 1 :]:
            span = (b - a) * scale_u
            if span < 0.5 or span > 3.0 * float(width) + 1.0:
                continue
            inner = (b - a) - 2.0 * gap
            if inner < 2.0:
                continue
            sides = [level(a - gap - probe, a - gap), level(b + gap, b + gap + probe)]
            sides = [side for side in sides if side is not None]
            if not sides:
                continue
            own = min(sides)
            root = level(a + gap + 0.2 * inner, b - gap - 0.2 * inner)
            if root is None:
                # Дно короче порога отрезков профиля: канавка 1,6–3 мм на листе
                # 1:2 — 9–18 px, в профиле пусто (корпус v9: 22 из 26 канавок).
                root = _ink_root(sheet[0], profile.axis_y, a + gap, b - gap, own)
            if root is None:
                continue
            # Провал, а не ступень: дно ниже своей ступени хотя бы на 0,1 мм.
            if own - root < max(1.0, 0.1 / scale_v):
                continue
            key = abs((a + b) / 2.0 - cx) * scale_u + abs(span - float(width))
            if best is None or key < best[0]:
                best = (key, a, b, root, own)
    if best is None:
        return Verdict(
            status="unmeasurable",
            evidence_bbox_px=(cx - reach, profile.axis_y - 1, cx + reach, profile.axis_y + 1),
            reason="канавки (две стенки и провал кромки) у прочитанного положения нет",
        )
    _key, a, b, root, own = best
    measured = {
        "axial_position_mm": round(((a + b) / 2.0 - x0) * scale_u, 3),
        "width_mm": round((b - a) * scale_u, 3),
        "depth_mm": round((own - root) * scale_v, 3),
        "root_diameter_mm": round(2.0 * root * scale_v, 3),
    }
    position_tol, width_tol, depth_tol = groove_tolerances(frame.scale_mean)
    problems = []
    if abs(measured["axial_position_mm"] - float(centre)) > position_tol:
        problems.append(
            f"положение {measured['axial_position_mm']:g} мм, прочитано {float(centre):g}"
        )
    if abs(measured["width_mm"] - float(width)) > width_tol:
        problems.append(f"ширина {measured['width_mm']:g} мм, прочитано {float(width):g}")
    if isinstance(depth, (int, float)) and abs(measured["depth_mm"] - float(depth)) > depth_tol:
        problems.append(f"глубина {measured['depth_mm']:g} мм, прочитано {float(depth):g}")
    # Как у фаски: канавку выхода инструмента допускается изображать не в
    # масштабе (ГОСТ 2.305; размеры — на выносном элементе), поэтому замер
    # надпись не опровергает — совпало — подтверждение, нет — «не измеримо»
    # с замером для справки.
    return Verdict(
        status="unmeasurable" if problems else "confirmed",
        measured=measured,
        evidence_bbox_px=(a, profile.axis_y - own, b, profile.axis_y + own),
        reason=(
            "; ".join(problems) + " — мелкий элемент допускается изображать не в масштабе "
            "(ГОСТ 2.305), замер надпись не опровергает"
            if problems
            else ""
        ),
    )
