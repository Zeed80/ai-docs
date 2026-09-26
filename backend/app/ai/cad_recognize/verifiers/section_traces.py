"""Следы секущих плоскостей на главном виде вала (ГОСТ 2.305).

Вынесенное сечение привязано к следу: короткому толстому штриху над видом и
такому же под ним на одной станции (со стрелкой и буквой). Элемент,
прочитанный по сечению, обязан стоять на станции следа — иначе ридер взял
его не с сечения (живой turned_multiaxis-1: осевое «Ø5 гл.10,7» с вида с
торца выдано радиальным отверстием на 43,7 мм, а след на листе один — на
53,5 у лыски).
"""

from __future__ import annotations

from typing import Any

# Штрих следа — основная линия (толщина от этой доли), длиннее цифр надписей
# (не короче стольких толщин линии), пара над и под видом симметрична оси.
_STROKE_SHARE = 0.85
_MIN_LENGTH_LINES = 8.0
_PAIR_LINES = 2.0


def locate_section_traces(gray: Any, frame: Any, profile: Any) -> list[float]:
    """Станции следов (мм от левого торца) по главному виду; пусто — следов нет."""
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _ink, _lines, _stroke

    gray = np.asarray(gray)
    ink = _ink(gray)
    line = float(getattr(profile, "line_px", 0.0) or 0.0) or 4.0
    half = np.asarray(profile.half_px, dtype=float)
    extent = float(np.nanmax(half))
    axis_y = float(profile.axis_y)
    min_length = max(6, int(round(2.0 * line)))
    top_edge, bottom_edge = axis_y - extent, axis_y + extent
    above, below = [], []
    for segment in _lines(ink, min_length, axis=1):
        if not profile.x0 - 2 * line <= segment.position <= profile.x1 + 2 * line:
            continue
        length = float(segment.end - segment.start)
        if length < _MIN_LENGTH_LINES * line or length > 2.0 * extent:
            continue
        if _stroke(gray, ink, segment, (segment.start, segment.end), axis=1) < _STROKE_SHARE * line:
            continue
        # Вне силуэта; расстояние ближнего конца до оси — для симметрии пары.
        if segment.end <= top_edge + line:
            above.append((float(segment.position), axis_y - segment.end, length))
        elif segment.start >= bottom_edge - line:
            below.append((float(segment.position), segment.start - axis_y, length))

    def thick_at(x: float, top: float, bottom: float) -> bool:
        """Толстый штрих в столбце ``x`` на строках ``top…bottom`` — по самим
        чернилам: штрих следа сливается в один отрезок с тонкой выносной на
        том же столбце (размер станции), и толщина отрезка выходила тонкой."""
        rows = range(max(0, int(top)), min(ink.shape[0], int(bottom)))
        reach = int(2 * line)
        thick = 0
        for y in rows:
            lo, hi = max(0, int(x) - reach), min(ink.shape[1], int(x) + reach + 1)
            columns = np.nonzero(ink[y, lo:hi])[0]
            if columns.size:
                runs = np.split(columns, np.nonzero(np.diff(columns) > 1)[0] + 1)
                if max(len(run) for run in runs) >= _STROKE_SHARE * line:
                    thick += 1
        return bool(rows) and thick >= 0.7 * len(rows)

    stations = []
    for x, gap, length, side in [
        *(a + ("above",) for a in above),
        *(b + ("below",) for b in below),
    ]:
        if side == "above":
            mirror = (axis_y + gap, axis_y + gap + length)
        else:
            mirror = (axis_y - gap - length, axis_y - gap)
        if thick_at(x, *mirror):
            station = round((x - profile.x0) * frame.mm_per_px, 2)
            if not any(abs(station - known) < 1.0 for known in stations):
                stations.append(station)
    return sorted(stations)
