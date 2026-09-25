"""Глубина отверстия пластины — по виду на толщину (X1, Ф2 `line_style`).

На плане глухое и сквозное отверстие — одна и та же окружность, а надпись
«гл.15» ридер живьём не читает: прочитанное «сквозное» проходило молча. На
виде на толщину отверстие видно невидимым контуром (штриховые линии по
ГОСТ 2.303) на строках y ± r: у сквозного — на всю толщину, у глухого — от
открытой грани до дна. Длина этого штрихового участка и есть глубина.

Строки вида общие с планом (проекционная связь, ГОСТ 2.305), вид находит
`locate_housing_views` — тот же, по которому мерится толщина пластины.
"""

from __future__ import annotations

from typing import Any

# Строки двух кромок отверстия согласны о глубине в пределах доли толщины.
_ROWS_AGREE = 0.08
# Пропуски штриховки сливаются, пока не длиннее стольких типичных пропусков.
_GAP_FACTOR = 2.2
# Доля полутонов среди тёмного на виде: выше — лист размыт (размытие 150 dpi
# 0,88…0,94, 75 dpi не выше 0,83). На размытом дно «находилось» в слившихся
# штрихах: 14 неверных из 62 против 1…4 на остальных ступенях.
_BLUR_SHARE = 0.86


def verify_hole_depths(
    gray: Any, profile: dict[str, Any], measured: dict[int, dict[str, float]] | None = None
) -> list[dict[str, Any]]:
    """Вердикт «сквозное / глухое на глубину» каждому отверстию пластины.

    ``measured`` — центр и Ø отверстия, измеренные по плану (`plate_hole`):
    строки вида берутся от них. Живая пластина: y резьбового M5 прочитан с
    ошибкой 6 мм, и по прочитанному строки отверстия уходили мимо.
    """
    import numpy as np

    from app.ai.cad_recognize.verifiers.housing_views import locate_housing_views
    from app.ai.cad_recognize.verifiers.plate_frame import _ink
    from app.ai.cad_recognize.verifiers.stage import _drawn_hole_diameter, _is_number

    holes = [
        (index, hole)
        for index, hole in enumerate(profile.get("holes") or [])
        if isinstance(hole, dict)
        and all(_is_number(hole.get(key)) for key in ("center_x_mm", "center_y_mm", "diameter_mm"))
    ]
    width, height = profile.get("width_mm"), profile.get("height_mm")
    thickness = profile.get("thickness_mm")
    if not holes or not all(_is_number(v) for v in (width, height, thickness)):
        return []

    def item(index: int, hole: dict, status: str, measured: dict, reason: str) -> dict:
        depth = hole.get("depth_mm")
        return {
            "kind": "hole_depth",
            "path": f"main_view.profile.holes[{index}]",
            "read": {
                "through": not _is_number(depth),
                **({"depth_mm": float(depth)} if _is_number(depth) else {}),
            },
            "measured": measured,
            "status": status,
            "reason": reason,
        }

    gray = np.asarray(gray)
    views = locate_housing_views(gray, float(width), float(height))
    if views is None or views.side_bbox_px is None:
        reason = "вид на толщину на листе не найден"
        return [item(index, hole, "unmeasurable", {}, reason) for index, hole in holes]
    sx0, _sy0, sx1, _sy1 = views.side_bbox_px
    span_px = sx1 - sx0
    mm_per_px = float(thickness) / span_px if span_px > 0 else 0.0
    if mm_per_px <= 0:
        return [item(i, h, "unmeasurable", {}, "вид на толщину пуст") for i, h in holes]
    if _blurred(gray, views.side_bbox_px):
        reason = "лист размыт: штрихи невидимого контура сливаются, глубину по ним не мерить"
        return [item(index, hole, "unmeasurable", {}, reason) for index, hole in holes]
    ink = _ink(gray)
    plan = views.plan
    half_h = float(height) / 2.0
    rows_of = {}
    for index, hole in holes:
        known = (measured or {}).get(index) or {}
        radius = float(known.get("diameter_mm") or _drawn_hole_diameter(hole)) / 2.0
        centre = float(known.get("center_y_mm", hole["center_y_mm"])) + half_h
        rows_of[index] = (plan.to_px(0.0, centre + radius)[1], plan.to_px(0.0, centre - radius)[1])
    gap = _typical_gap(ink, [row for pair in rows_of.values() for row in pair], sx0, sx1)
    results = []
    for index, hole in holes:
        # Кромка другого отверстия на той же строке (массив в ряд по y)
        # накладывает свои штрихи — такая строка не мерит; мерит другая.
        others = [row for other, pair in rows_of.items() if other != index for row in pair]
        rows = [row for row in rows_of[index] if all(abs(row - o) > 4.0 for o in others)]
        if not rows:
            results.append(
                item(
                    index,
                    hole,
                    "unmeasurable",
                    {},
                    "на виде на толщину его закрывает другое отверстие",
                )
            )
            continue
        extents = [_extent(ink, _best_row(ink, row, sx0, sx1), sx0, sx1, gap) for row in rows]
        if any(extent is None for extent in extents):
            results.append(
                item(index, hole, "unmeasurable", {}, "невидимого контура отверстия на виде нет")
            )
            continue
        sides = {side for side, _length in extents}
        lengths = [length for _side, length in extents]
        if len(sides) > 1 or max(lengths) - min(lengths) > _ROWS_AGREE * span_px:
            results.append(
                item(index, hole, "unmeasurable", {}, "кромки отверстия на виде расходятся")
            )
            continue
        length_px = sum(lengths) / len(lengths)
        # Дно глухого отверстия — вертикаль между его строками на глубине;
        # у сквозного её нет. По краю вида судить нельзя: в тонкой пластине
        # глухому до дальней грани 3…4 мм, и запас на поля у кромок вида
        # делал его «сквозным» (6 из 63 на 150 dpi).
        side = next(iter(sides))
        top_row, bottom_row = rows_of[index]
        # Первая вертикаль между строками отверстия от открытой грани — дно;
        # строка может тянуться дальше чужой линией (резьба, соседний элемент).
        first_bottom = None
        start = max(4.0, 0.04 * span_px, _face_width(ink, sx0, sx1, _sy0, _sy1, side) + 2.0)
        for offset in range(int(start), int(length_px) + 3):
            if offset >= span_px - max(6.0, 0.08 * span_px):
                break
            column = sx0 + offset if side == "left" else sx1 - offset
            if _vertical_line(
                ink, column, top_row, bottom_row, window=0, reach_gap=_GAP_FACTOR * gap + 3.0
            ):
                first_bottom = float(offset)
                break
        if first_bottom is not None:
            through, length_px = False, first_bottom
        elif length_px >= 0.6 * span_px:
            through = True
        else:
            results.append(
                item(index, hole, "unmeasurable", {}, "дна глухого отверстия на виде не найдено")
            )
            continue
        depth = length_px * mm_per_px
        measured = {"through": through, **({} if through else {"depth_mm": round(depth, 2)})}
        read_depth = hole.get("depth_mm")
        tolerance = max(0.5, 3.0 * mm_per_px)
        if not _is_number(read_depth):
            ok = through
        else:
            ok = not through and abs(depth - float(read_depth)) <= tolerance
        reason = (
            "невидимый контур на всю толщину — сквозное"
            if through
            else f"невидимый контур до глубины {depth:.1f} мм"
        )
        results.append(item(index, hole, "confirmed" if ok else "refuted", measured, reason))
    return results


def _typical_gap(ink: Any, rows: list[float], x0: float, x1: float) -> float:
    """Типичный пропуск штриховой линии на этих строках, px."""
    gaps = []
    for row in rows:
        runs = _runs(ink, _best_row(ink, row, x0, x1), x0, x1)
        gaps += [b[0] - a[1] for a, b in zip(runs, runs[1:], strict=False)]
    short = sorted(g for g in gaps if 1 < g < 0.25 * (x1 - x0))
    return float(short[len(short) // 2]) if short else 4.0


def _best_row(ink: Any, row: float, x0: float, x1: float) -> int:
    """Строка с наибольшим числом чернил рядом с ожидаемой (±2 px)."""
    candidates = range(int(round(row)) - 2, int(round(row)) + 3)
    return max(
        (r for r in candidates if 0 <= r < ink.shape[0]),
        key=lambda r: int(ink[r, int(x0) + 3 : int(x1) - 2].sum()),
        default=int(round(row)),
    )


def _runs(ink: Any, row: int, x0: float, x1: float) -> list[tuple[int, int]]:
    """Участки чернил строки внутри вида (кромки вида не в счёт)."""
    if not 0 <= row < ink.shape[0]:
        return []
    line = ink[max(0, row - 1) : row + 2, int(x0) + 3 : int(x1) - 2].any(axis=0)
    runs, start = [], None
    for position, value in enumerate(line):
        if value and start is None:
            start = position
        elif not value and start is not None:
            runs.append((start + int(x0) + 3, position + int(x0) + 3))
            start = None
    if start is not None:
        runs.append((start + int(x0) + 3, len(line) + int(x0) + 3))
    return runs


def _extent(ink: Any, row: int, x0: float, x1: float, gap: float) -> tuple[str, float] | None:
    """(открытая грань, длина штрихового участка от неё) на строке, px."""
    runs = _runs(ink, row, x0, x1)
    if not runs:
        return None
    limit = _GAP_FACTOR * gap + 1.0
    best = None
    for side in ("left", "right"):
        ordered = (
            runs if side == "left" else [(x1 - b + x0, x1 - a + x0) for a, b in reversed(runs)]
        )
        # Открытая грань — та, которой участок касается вплотную (строка
        # обрезана у кромок вида на 3 px): допуск в пропуск штриховки на узком
        # виде 150 dpi засчитывал штрихи от противоположной грани.
        if ordered[0][0] - x0 > 5:
            continue
        reach = ordered[0][1]
        for a, b in ordered[1:]:
            if a - reach > limit:
                break
            reach = b
        length = reach - x0
        if best is None or length > best[1]:
            best = (side, float(length))
    return best


def _vertical_line(
    ink: Any, column: float, top: float, bottom: float, *, window: int = 2, reach_gap: float = 6.0
) -> bool:
    """Линия дна: чернила между строками кромок на столбце глубины (±2 px).

    Дно — тоже невидимый контур (штрихи), отсюда порог половины.
    """
    low, high = int(round(min(top, bottom))) + 2, int(round(max(top, bottom))) - 1
    if high - low < 3:
        return False
    for x in range(int(round(column)) - window, int(round(column)) + window + 1):
        if not 0 <= x < ink.shape[1]:
            continue
        strip = ink[:, max(0, x - 1) : x + 2].any(axis=1)
        if float(strip[low:high].mean()) < 0.5:
            continue
        # Дно замыкает ИМЕННО эти строки: за ними вертикаль не продолжается.
        # Дно соседнего глухого отверстия проходило через полосу сквозного и
        # делало его «глухим на 14,8» (7 из 64 на чистом листе).
        top_row, bottom_row = int(round(min(top, bottom))), int(round(max(top, bottom)))
        inside = [row for row in range(low, high) if strip[row]]
        # И начинается у одной строки, и доходит до другой (конец штриховой
        # может прийтись на пропуск): дно соседнего отверстия, проходящее
        # через полосу на 65 %, начиналось далеко от строки кромки.
        if not inside or inside[0] - low > 3 or high - inside[-1] > reach_gap:
            continue
        above = strip[max(0, top_row - 9) : max(0, top_row - 3)]
        below = strip[bottom_row + 4 : bottom_row + 10]
        if (above.size and above.mean() > 0.5) or (below.size and below.mean() > 0.5):
            continue
        return True
    return False


def _face_width(ink: Any, x0: float, x1: float, y0: float, y1: float, side: str) -> float:
    """Толщина линии грани вида у открытой стороны: столбцы, залитые на всю
    высоту вида. Поиск дна внутри неё принимал саму грань за дно (43 из 64)."""
    low, high = int(y0) + 5, int(y1) - 5
    if high <= low:
        return 0.0
    width = 0
    for step in range(int(0.3 * (x1 - x0))):
        x = int(round(x0 + step)) if side == "left" else int(round(x1 - step))
        if not 0 <= x < ink.shape[1]:
            break
        if ink[low:high, x].mean() >= 0.8:
            width = step + 1
        elif width:
            break
    return float(width)


def _blurred(gray: Any, bbox: tuple[float, float, float, float]) -> bool:
    """Размыт ли вид: у линий нет тёмной сердцевины, почти всё — полутон."""
    x0, y0, x1, y1 = (int(value) for value in bbox)
    region = gray[max(0, y0) : y1, max(0, x0 - 5) : x1 + 5].astype(int)
    dark = int((region < 200).sum())
    if dark == 0:
        return False
    return ((region > 60) & (region < 200)).sum() / dark > _BLUR_SHARE
