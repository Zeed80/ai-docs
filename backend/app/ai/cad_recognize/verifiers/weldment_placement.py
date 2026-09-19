"""Размещение деталей сварного узла — по плану на листе (X3).

Живой узел: ребро прочитано у ближнего края основания (y = 10 — толщина
основания), а на листе оно у дальнего (размер 115), и узел собрался молча
не тем: сверка тела проверяет габарит, а ребро в любом месте основания
габарита не меняет.

На плане узла основание — прямоугольник W × H (`locate_plate_frame`), а
приваренная деталь — её след: у ребра, стоящего на основании, — полоса
толщиной s на всю длину. Проверка: лежат ли на листе кромки следа там, куда
его ставит прочитанное размещение. Не лежат — след ищется сдвигом вдоль
тонкой оси по всему основанию; найден ровно один — это замер.
"""

from __future__ import annotations

from typing import Any

# Доля длины кромки, покрытая чернилами, чтобы кромка считалась на листе.
_EDGE_COVERAGE = 0.8
# Шаг поиска следа вдоль тонкой оси, мм.
_STEP_MM = 0.5
# Полоса вокруг кромки, px (толщина линии и её сдвиг при растеризации).
_BAND_PX = 2
# Допуск совпадения линий, мм.
_SNAP_MM = 0.75
# Больше равноправных мест — не замер, а неразличимость.
_MAX_OPTIONS = 3


def _full_lines(frame, origin, lo, hi, thin, base_box, ink) -> list[float]:
    """Положения (по тонкой оси) сплошных линий на всю длину следа внутри основания."""
    hits = []
    position = base_box[0][thin]
    while position <= base_box[1][thin] + 1e-6:
        a, b = list(lo), list(hi)
        a[thin] = b[thin] = position
        long_axis = 1 - thin
        start = [0.0, 0.0]
        end = [0.0, 0.0]
        start[long_axis], end[long_axis] = lo[long_axis], hi[long_axis]
        start[thin] = end[thin] = position
        if (
            _coverage(
                ink,
                frame.to_px(start[0] - origin[0], start[1] - origin[1]),
                frame.to_px(end[0] - origin[0], end[1] - origin[1]),
            )
            >= _EDGE_COVERAGE
        ):
            hits.append(position)
        position += _STEP_MM
    groups: list[list[float]] = []
    for value in hits:
        if groups and value - groups[-1][-1] <= _STEP_MM + 1e-6:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(g) / len(g) for g in groups]


def _coverage(ink: Any, start: tuple[float, float], end: tuple[float, float]) -> float:
    import numpy as np

    (x0, y0), (x1, y1) = start, end
    count = int(max(abs(x1 - x0), abs(y1 - y0)))
    if count < 3:
        return 0.0
    hits = 0
    for t in np.linspace(0.1, 0.9, count):
        x, y = int(round(x0 + t * (x1 - x0))), int(round(y0 + t * (y1 - y0)))
        hits += bool(
            ink[
                max(0, y - _BAND_PX) : y + _BAND_PX + 1, max(0, x - _BAND_PX) : x + _BAND_PX + 1
            ].any()
        )
    return hits / count


def _long_edges(frame: Any, origin: list[float], lo: list[float], hi: list[float], ink: Any):
    """Покрытие двух длинных кромок следа (в плоскости плана x–y) и индекс
    ТОНКОЙ оси следа (поперёк длинных кромок)."""
    along_x = (hi[0] - lo[0]) >= (hi[1] - lo[1])

    def px(x: float, y: float) -> tuple[float, float]:
        return frame.to_px(x - origin[0], y - origin[1])

    if along_x:
        pairs = (((lo[0], lo[1]), (hi[0], lo[1])), ((lo[0], hi[1]), (hi[0], hi[1])))
    else:
        pairs = (((lo[0], lo[1]), (lo[0], hi[1])), ((hi[0], lo[1]), (hi[0], hi[1])))
    return [_coverage(ink, px(*a), px(*b)) for a, b in pairs], (1 if along_x else 0)


def verify_weldment_placement(gray: Any, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Вердикт размещению каждой приваренной прямоугольной детали."""
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import locate_plate_frame
    from app.ai.cad_solid import _body_box

    parts = [p for p in spec.get("parts") or [] if isinstance(p, dict)]
    if len(parts) < 2:
        return []
    base = parts[0]
    base_box = _body_box(base)
    profile = base.get("profile") or {}
    width, height = profile.get("width_mm"), profile.get("height_mm")
    items: list[dict[str, Any]] = []
    gray = np.asarray(gray)
    frame = (
        locate_plate_frame(gray, float(width), float(height))
        if base_box is not None and width and height
        else None
    )
    ink = gray < 160
    for index, part in enumerate(parts[1:], start=1):
        box = _body_box(part)
        placement = part.get("placement") or {}
        item = {
            "kind": "weldment_placement",
            "path": f"parts[{index}]",
            "read": {"position_mm": list(placement.get("position_mm") or [])},
            "measured": {},
        }
        if box is None or frame is None or base_box is None:
            items.append(
                {
                    **item,
                    "status": "unmeasurable",
                    "reason": "план основания на листе не найден"
                    if frame is None
                    else "деталь не прямоугольная",
                }
            )
            continue
        origin = base_box[0]
        lo, hi = list(box[0]), list(box[1])
        edges, thin = _long_edges(frame, origin, lo, hi, ink)
        if min(edges) >= _EDGE_COVERAGE:
            items.append({**item, "status": "confirmed", "reason": "след детали на плане на месте"})
            continue
        # Поиск следа вдоль тонкой оси по всему основанию. Сплошные линии вдоль
        # детали — её кромки и кромки валиков швов (на катет от граней): при
        # катете, равном толщине ребра, «валик + ребро» — тоже полоса толщиной s.
        # Кандидат — пара линий через s; выбирается тот, чью окрестность целиком
        # объясняют ребро и валики известных швов.
        span = hi[thin] - lo[thin]
        legs = [
            float(weld.get("leg_mm"))
            for weld in spec.get("welds") or []
            if index in (weld.get("bodies") or []) and weld.get("leg_mm")
        ]
        lines = _full_lines(frame, origin, lo, hi, thin, base_box, ink)
        candidates = [a for a in lines if any(abs(b - (a + span)) <= _SNAP_MM for b in lines)]
        scored = []
        for a in candidates:
            expected = [a, a + span] + [a - leg for leg in legs] + [a + span + leg for leg in legs]
            reach = (a - max(legs, default=0.0) - 1.0, a + span + max(legs, default=0.0) + 1.0)
            near = [y for y in lines if reach[0] <= y <= reach[1]]
            unexplained = sum(1 for y in near if not any(abs(y - e) <= _SNAP_MM for e in expected))
            scored.append((unexplained, a))
        best = [a for u, a in scored if u == min((u for u, _a in scored), default=0)]
        if not best or len(best) > _MAX_OPTIONS:
            items.append(
                {
                    **item,
                    "status": "unmeasurable",
                    "reason": "след детали на плане не найден"
                    if not best
                    else "на плане много полос такой толщины",
                }
            )
            continue
        # Несколько равноправных мест (односторонний шов: «ребро у края + валик»
        # и «ребро дальше + валик у края» объясняют те же линии) — все
        # варианты; выбирает надпись положения на листе (согласование).
        options = [
            {
                "axis": "xyz"[thin],
                "shift_mm": round(a - lo[thin], 2),
                "from_edge_mm": round(a - base_box[0][thin], 2),
                "to_far_edge_mm": round(base_box[1][thin] - (a + span), 2),
            }
            for a in best
        ]
        items.append(
            {
                **item,
                "status": "refuted",
                "measured": {**options[0], "options": options},
                "reason": (
                    f"на плане деталь сдвинута по {'xyz'[thin]} на "
                    + " или ".join(f"{o['shift_mm']:+.1f}" for o in options)
                    + " мм"
                ),
            }
        )
        continue
    return items
