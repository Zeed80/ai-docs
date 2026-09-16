"""Вид-разрез полой детали вращения (втулки): наружный профиль, расточка, фланец.

`shaft_frame` ищет торцы как основные вертикали ЧЕРЕЗ ось — у разреза втулки
через ось не идёт ничего: торец — два коротких отрезка стенки. Второе отличие
реального листа: контур и выносная размера, которая его продолжает за торец,
сливаются в одну горизонталь, и по всей её длине толщина — как у тонкой
(втулка part_03: Ø13 расточки у торца 3,2 px при основной 5,4). Поэтому
горизонтали режутся по толщине в каждом столбце, а вид признаётся втулкой,
только если расточка есть у обоих торцов.

Правка `shaft_frame` ради реального листа уже ломала синтетический корпус
(8 регрессий) — этот поиск отдельный и включается, когда вал не найден.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.ai.cad_recognize.verifiers.shaft_frame import ShaftProfile
from app.ai.cad_recognize.verifiers.view_frame import ViewFrame

# Доля основной толщины линии, с которой столбец горизонтали считается контуром.
_MAIN_MASS_SHARE = 0.75
# Разрыв профиля, который его не рвёт: фланец в разрезе несимметричен (сверху
# лыска, снизу круг) — пары там нет, втулка: 2 мм из 24.
_GAP_SHARE = 0.12
# Расточка обязана быть у обоих торцов в пределах этой доли длины.
_END_SHARE = 0.04
_AXIS_CANDIDATES = 30
# Несимметрия пары относительно оси в долях радиуса: ось кандидата сама
# округлена до 0,5 px, и 1,5 % убивали расточку Ø13 втулки при оси 1294,5.
_PAIR_SHARE = 0.025
# Горизонталь длиннее этой доли ширины листа — рамка, а не деталь.
_MAX_RUN_SHARE = 0.4
_SECTIONS_KEPT = 40
_FACES_KEPT = 16


@dataclass(frozen=True)
class SleeveSection:
    outer: ShaftProfile
    bore: ShaftProfile
    # Столбцы граней фланца (левая, правая) — вертикали, уходящие за наружный
    # радиус; ``None`` — фланца на разрезе нет.
    flange_px: tuple[float, float] | None
    # Как далеко от оси уходят грани фланца вверх и вниз, px.
    flange_reach_px: tuple[float, float] | None

    def frame(self, total_length_mm: float) -> ViewFrame:
        import numpy as np

        extent = float(np.nanmax(self.outer.half_px))
        return ViewFrame(
            bbox_px=(
                self.outer.x0 - 3,
                self.outer.axis_y - extent - 3,
                self.outer.x1 + 3,
                self.outer.axis_y + extent + 3,
            ),
            mm_per_px=float(total_length_mm) / float(self.outer.x1 - self.outer.x0),
            origin_px=(float(self.outer.x0), self.outer.axis_y),
        )


def locate_sleeve_section(
    sheet: Any, diameters_mm: list[float] | tuple[float, ...] = ()
) -> SleeveSection | None:
    """Лучший кандидат разреза втулки (см. `locate_sleeve_sections`) или ``None``."""
    found = locate_sleeve_sections(sheet, diameters_mm)
    return found[0] if found else None


def locate_sleeve_sections(
    sheet: Any, diameters_mm: list[float] | tuple[float, ...] = ()
) -> list[SleeveSection]:
    """Кандидаты разреза втулки: ось × пара торцов-кандидатов.

    Порядок — больше площадок наружного профиля и расточки совпадает с
    надписями Ø при одном масштабе, затем длиннее: горизонтали штампа тоже
    дают «стенку у обоих торцов». Какой кандидат настоящий, решают надписи
    (`propose_sleeve`): на увеличенном листе размерные линии толщиной с
    основные и тоже «торцы» (колесо part_06: Ø46 и Ø38 левее детали).
    """
    import numpy as np

    from app.ai.cad_recognize.verifiers import shaft_frame as sf
    from app.ai.cad_recognize.verifiers.plate_frame import _ink, _lines, _stroke

    gray = np.asarray(sheet)
    ink = _ink(gray)
    min_length = max(6, int(round(0.006 * min(gray.shape))))
    lines = sf._segments(ink, min_length)
    if len(lines) < 4:
        return []
    long_lines = [line for line in lines if line.end - line.start >= 4 * min_length]
    weights = [
        _stroke(gray, ink, line, (line.start, line.end), axis=0, quantile=0.25)
        for line in (long_lines or lines)
    ]
    main_ref = float(np.percentile(weights, 90))
    dark = (255.0 - gray.astype(np.float32)) / 255.0
    runs = [run for line in lines for run in _main_runs(dark, line, main_ref, min_length)]
    # Линии рамки и графы штампа — не деталь: у портретного листа колеса они
    # вытесняли ось детали из кандидатов.
    from app.ai.cad_recognize.verifiers.sheet_scale import locate_title_block

    block = locate_title_block(gray)
    width = gray.shape[1]

    def of_part(run: Any) -> bool:
        if run.end - run.start > _MAX_RUN_SHARE * width:
            return False
        if block is not None:
            left, top, _right, bottom = block.bbox_px
            if top - 5 <= run.position <= bottom + 5 and run.start >= left - 5:
                return False
        return True

    runs = [run for run in runs if of_part(run)]
    if len(runs) < 4:
        return []
    vertical = _lines(ink, min_length, axis=1)
    found: list[SleeveSection] = []
    for axis_y in _axis_candidates(runs, min_length, main_ref):
        found.extend(_sections(runs, axis_y, gray.shape[1], main_ref, dark, vertical))
    found.sort(
        key=lambda s: (_diameter_support(s, diameters_mm), s.outer.x1 - s.outer.x0), reverse=True
    )
    return found[:_SECTIONS_KEPT]


def _diameter_support(section: SleeveSection, diameters_mm) -> int:
    """Сколько площадок (наружных и расточки) ложится на надписи Ø при лучшем
    общем масштабе (±2 %)."""
    from app.ai.cad_recognize.verifiers.shaft_profile import _plateaus

    diameters = [float(d) for d in diameters_mm if d and d > 0]
    if not diameters:
        return 0
    length = section.outer.x1 - section.outer.x0
    levels = [
        2.0 * level
        for profile in (section.outer, section.bore)
        for start, end, level in _plateaus(profile)
        if end - start >= 0.08 * length
    ]
    best = 0
    for level in levels:
        for diameter in diameters:
            scale = level / diameter
            count = sum(
                1 for other in levels if any(abs(other / scale - d) <= 0.02 * d for d in diameters)
            )
            best = max(best, count)
    return best


def _main_runs(dark: Any, line: Any, main_ref: float, min_length: int) -> list[Any]:
    """Куски горизонтали, где поперечная масса — как у основной линии."""
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _Line

    y = int(round(line.position))
    half = int(round(2 * main_ref))
    x0, x1 = int(line.start), int(line.end)
    if y - half < 0 or y + half >= dark.shape[0] or x1 - x0 < min_length:
        return []
    mass = dark[y - half : y + half + 1, x0 : x1 + 1].sum(axis=0)
    window = 9
    if mass.size >= window:
        padded = np.pad(mass, window // 2, mode="edge")
        mass = np.median(np.lib.stride_tricks.sliding_window_view(padded, window), axis=1)
    main = mass >= _MAIN_MASS_SHARE * main_ref
    result = []
    start = None
    for index, value in enumerate(list(main) + [False]):
        if value and start is None:
            start = index
        elif not value and start is not None:
            if index - 1 - start >= min_length:
                result.append(_Line(line.position, x0 + start, x0 + index - 1))
            start = None
    return result


def _axis_candidates(runs: list[Any], min_length: int, main_ref: float) -> list[float]:
    votes: dict[int, float] = {}
    for i, top in enumerate(runs):
        for bottom in runs[i + 1 :]:
            low, high = sorted((top, bottom), key=lambda line: line.position)
            if high.position - low.position < 4 * main_ref:
                continue
            overlap = low.overlap(high.start, high.end)
            if overlap < min_length:
                continue
            middle = int(round((low.position + high.position) / 2.0))
            votes[middle] = votes.get(middle, 0.0) + overlap
    smoothed = {y: sum(votes.get(y + d, 0.0) for d in (-1, 0, 1)) for y in votes}
    peaks = sorted(
        (
            y
            for y in smoothed
            if smoothed[y] >= smoothed.get(y - 1, 0.0) and smoothed[y] > smoothed.get(y + 1, 0.0)
        ),
        key=lambda y: smoothed[y],
        reverse=True,
    )
    result = []
    for y in peaks[:_AXIS_CANDIDATES]:
        near = [value for value in range(y - 1, y + 2) if value in votes]
        result.append(sum(v * votes[v] for v in near) / sum(votes[v] for v in near))
    return result


def _sections(
    runs: list[Any], axis_y: float, width: int, main_ref: float, dark: Any, vertical: list[Any]
) -> list[SleeveSection]:
    import numpy as np

    tolerance = max(2.0, 0.5 * main_ref)
    pairs: list[tuple[float, int, int]] = []
    outer = np.full(width, np.nan)
    inner = np.full(width, np.nan)
    above = [run for run in runs if run.position < axis_y - 2 * main_ref]
    below = [run for run in runs if run.position > axis_y + 2 * main_ref]
    for top in above:
        mirror = 2.0 * axis_y - top.position
        # Допуск растёт с радиусом: увеличенный лист дрейфует на единицы px
        # (втулка: расточка Ø13 — 999 и 1595 при оси 1295, на 4 px мимо).
        pair_tolerance = max(tolerance, _PAIR_SHARE * (axis_y - top.position))
        for bottom in below:
            if abs(bottom.position - mirror) > pair_tolerance:
                continue
            start, end = int(max(top.start, bottom.start)), int(min(top.end, bottom.end))
            if end - start < 3 * main_ref:
                continue
            level = (bottom.position - top.position) / 2.0
            pairs.append((level, start, end))
            window = slice(start, end + 1)
            outer[window] = np.where(
                np.isnan(outer[window]), level, np.maximum(outer[window], level)
            )
            inner[window] = np.where(
                np.isnan(inner[window]), level, np.minimum(inner[window], level)
            )
    # Одна пара в столбце — стенки нет (сплошное сечение или вал).
    inner[inner >= outer - 2 * main_ref] = np.nan
    columns = np.nonzero(~np.isnan(outer))[0]
    if columns.size < 2:
        return []
    faces = _end_face_candidates(
        vertical, axis_y, int(columns[0]), int(columns[-1]), outer, inner, main_ref
    )
    result = []
    for i, left in enumerate(faces):
        for right in faces[i + 1 :]:
            section = _window(
                pairs, width, axis_y, int(round(left)), int(round(right)), main_ref, dark, vertical
            )
            if section is not None:
                result.append(section)
    return result


def _window(pairs, width, axis_y, x0, x1, main_ref, dark, vertical) -> SleeveSection | None:
    """Вид между двумя торцами-кандидатами; ``None`` — не втулка или разрыв."""
    import numpy as np

    length = x1 - x0
    if length < 20 * main_ref:
        return None
    outer = np.full(width, np.nan)
    inner = np.full(width, np.nan)
    for level, start, end in pairs:
        if end < x0 or start > x1:
            continue
        window = slice(max(start, x0), min(end, x1) + 1)
        outer[window] = np.where(np.isnan(outer[window]), level, np.maximum(outer[window], level))
        inner[window] = np.where(np.isnan(inner[window]), level, np.minimum(inner[window], level))
    inner[inner >= outer - 2 * main_ref] = np.nan
    outer_half = outer[x0 : x1 + 1].copy()
    # Профиль без больших разрывов: торцы разных изображений не соединяются.
    defined = np.nonzero(~np.isnan(outer_half))[0]
    if (
        defined.size < 2
        or defined[0] > _END_SHARE * length
        or len(outer_half) - 1 - defined[-1] > _END_SHARE * length
    ):
        return None
    if np.max(np.diff(defined)) > _GAP_SHARE * length:
        return None
    end = max(3, int(_END_SHARE * length))
    if np.all(np.isnan(inner[x0 : x0 + end + 1])) or np.all(np.isnan(inner[x1 - end : x1 + 1])):
        return None  # не втулка: у торца нет расточки
    bore_half = inner[x0 : x1 + 1].copy()
    extent = float(np.nanmax(outer_half))
    faces = _faces(dark, vertical, axis_y, x0, x1, extent, main_ref)
    flange, reach = _flange(dark, vertical, axis_y, x0, x1, outer_half, main_ref)
    if flange is not None:
        # Между гранями фланца наружного профиля нет: ступень под фланцем
        # продолжается, а пара «лыска — что-то снизу» — не ступень.
        lo = max(0, int(round(flange[0] - x0 - main_ref)))
        hi = min(len(outer_half), int(round(flange[1] - x0 + main_ref)) + 1)
        outer_half[lo:hi] = np.nan
    return SleeveSection(
        outer=ShaftProfile(
            x0=x0, x1=x1, axis_y=axis_y, half_px=outer_half, faces_px=faces, line_px=main_ref
        ),
        bore=ShaftProfile(
            x0=x0, x1=x1, axis_y=axis_y, half_px=bore_half, faces_px=faces, line_px=main_ref
        ),
        flange_px=flange,
        flange_reach_px=reach,
    )


def _end_face_candidates(vertical, axis_y, x0, x1, outer, inner, main_ref) -> list[float]:
    """Торцы-кандидаты: вертикаль стенки между расточкой и наружной линией над
    осью и под ней. Какие из них настоящие торцы, решают надписи."""
    import numpy as np

    def wall_at(x: float) -> bool:
        column = int(round(x))
        window = slice(max(0, column - 12), column + 13)
        outer_near = outer[window]
        inner_near = inner[window]
        if np.all(np.isnan(outer_near)) or np.all(np.isnan(inner_near)):
            return False
        top = (axis_y - float(np.nanmax(outer_near)), axis_y - float(np.nanmin(inner_near)))
        bottom = (axis_y + float(np.nanmin(inner_near)), axis_y + float(np.nanmax(outer_near)))
        need = 0.6 * (top[1] - top[0])
        above = any(
            abs(v.position - x) <= 2 * main_ref and v.overlap(*top) >= need for v in vertical
        )
        below = any(
            abs(v.position - x) <= 2 * main_ref and v.overlap(*bottom) >= need for v in vertical
        )
        return above and below

    faces: list[float] = []
    for position in sorted(
        v.position
        for v in vertical
        if x0 - 3 * main_ref <= v.position <= x1 + 3 * main_ref and wall_at(v.position)
    ):
        if not faces or position - faces[-1] > 2 * main_ref:
            faces.append(position)
    return faces[:_FACES_KEPT]


def _vertical_mass(dark: Any, line: Any, rows: tuple[float, float], main_ref: float) -> float:
    """Поперечная масса вертикали в строках ``rows`` (медиана по строкам)."""
    import numpy as np

    x = int(round(line.position))
    half = int(round(2 * main_ref))
    top, bottom = int(max(line.start, rows[0])), int(min(line.end, rows[1]))
    if bottom - top < 3 or x - half < 0 or x + half >= dark.shape[1]:
        return 0.0
    return float(np.median(dark[top : bottom + 1, x - half : x + half + 1].sum(axis=1)))


def _faces(
    dark, vertical, axis_y, x0, x1, extent, main_ref
) -> tuple[tuple[float, float, float], ...]:
    rows = (axis_y - extent - 2.0, axis_y + extent + 2.0)
    return tuple(
        sorted(
            (float(line.position), float(line.start), float(line.end))
            for line in vertical
            if x0 - 3 <= line.position <= x1 + 3
            and min(line.end, rows[1]) - max(line.start, rows[0]) >= 3 * main_ref
            and _vertical_mass(dark, line, rows, main_ref) >= _MAIN_MASS_SHARE * main_ref
        )
    )


def _flange(dark, vertical, axis_y, x0, x1, outer_half, main_ref):
    """Грани фланца: пара основных вертикалей внутри вида, уходящих за наружный
    радиус не меньше чем на его четверть, ближе друг к другу, чем ¼ длины.

    Отсчёт — от медианы наружного профиля, а не от профиля рядом: у фланца
    лыска сверху и что-то снизу дают ложную «пару» (втулка: Ø19,3 на 16…18).
    Отрезки над и под осью на одном столбце — одна грань.
    """
    import numpy as np

    level = float(np.nanmedian(outer_half))
    beyond = max(2.0 * main_ref, 0.25 * level)
    pieces = []
    for line in vertical:
        if not x0 + 3 * main_ref <= line.position <= x1 - 3 * main_ref:
            continue
        up = axis_y - line.start
        down = line.end - axis_y
        if max(up, down) < level + beyond:
            continue
        # Грань фланца растёт из тела: отрезок пересекает полосу у наружного
        # контура со своей стороны (втулка: вертикаль рамки допуска над видом
        # на 21,75 мм уходила «за радиус», но тела не касалась).
        top_band = (axis_y - 1.3 * level, axis_y - 0.7 * level)
        bottom_band = (axis_y + 0.7 * level, axis_y + 1.3 * level)
        if not (
            line.overlap(*top_band) > 0
            and up >= level + beyond
            or line.overlap(*bottom_band) > 0
            and down >= level + beyond
        ):
            continue
        if (
            _vertical_mass(dark, line, (line.start, line.end), main_ref)
            < _MAIN_MASS_SHARE * main_ref
        ):
            continue
        pieces.append((float(line.position), up, down))
    edges: list[list[float]] = []
    for x, up, down in sorted(pieces):
        if edges and x - edges[-1][0] <= 2 * main_ref:
            edge = edges[-1]
            edge[0] = (edge[0] + x) / 2.0
            edge[1], edge[2] = max(edge[1], up), max(edge[2], down)
        else:
            edges.append([x, up, down])
    best = None
    for a, b in zip(edges, edges[1:]):
        if b[0] - a[0] < 2 * main_ref or b[0] - a[0] > 0.25 * (x1 - x0):
            continue
        reach = min(max(a[1], a[2]), max(b[1], b[2]))
        if best is None or reach > best[2]:
            best = (a, b, reach)
    if best is None:
        return None, None
    a, b, _reach = best
    return (a[0], b[0]), (max(a[1], b[1]), max(a[2], b[2]))


# Станция и толщина фланца — надписью, если грань разреза в пределах этой доли
# габарита (не меньше 0,4 мм).
_FLANGE_STATION_SHARE = 0.02
# Масштаб вида с торца обязан совпасть с масштабом разреза: это один лист.
_SCALE_AGREEMENT = 0.03


@dataclass(frozen=True)
class SleeveProposal:
    outer: tuple[dict[str, Any], ...]
    bore: tuple[dict[str, Any], ...]
    total_mm: float
    flange: dict[str, Any] | None
    notes: tuple[str, ...] = ()
    frame: ViewFrame | None = None
    # Рамка вида с торца, где найден контур фланца.
    end_view_bbox_px: tuple[float, float, float, float] | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "outer": [dict(step) for step in self.outer],
            "bore": [dict(step) for step in self.bore],
            "total_mm": self.total_mm,
            "flange": self.flange,
            "notes": list(self.notes),
        }


def propose_sleeve(gray: Any, spec: dict[str, Any]) -> tuple[SleeveProposal | None, str]:
    """Втулка по листу: разрез (профиль, расточка, грани фланца) + вид с торца
    (контур фланца и отверстия) + надписи."""
    import numpy as np

    from app.ai.cad_recognize.verifiers.flange_outline import (
        outline_sketch,
        propose_flange_outline,
        propose_hole_pattern,
    )
    from app.ai.cad_recognize.verifiers.sheet_profile import propose_profile, sheet_labels

    gray = np.asarray(gray)
    labels = sheet_labels(spec)
    candidates = locate_sleeve_sections(gray, labels.diameters)
    if not candidates:
        return None, "разреза втулки (расточка у обоих торцов) на листе нет"
    # Первый кандидат, у которого и наружный профиль, и расточка строго
    # объясняются надписями; иначе — причина лучшего кандидата.
    first_reason = None
    chosen = None
    for section in candidates:
        outer, why = propose_profile(section.outer, labels, None, allow_threads=False)
        if outer is None:
            first_reason = first_reason or f"наружный профиль разреза: {why}"
            continue
        bore, why = propose_profile(section.bore, labels, outer.total_mm, allow_threads=False)
        if bore is None:
            first_reason = first_reason or f"расточка разреза: {why}"
            continue
        if abs(bore.total_mm - outer.total_mm) > 1e-6:
            first_reason = first_reason or "расточка и наружный профиль дали разные габариты"
            continue
        chosen = section
        break
    if chosen is None:
        return None, first_reason or "разрез втулки надписями не объясняется"
    section = chosen
    notes: list[str] = []
    flange = None
    end_view = None
    if section.flange_px is not None:
        flange, why = _flange_on_section(section, outer, labels)
        if flange is None:
            notes.append(f"фланец на разрезе: {why}")
        else:
            outline, why = propose_flange_outline(gray, spec)
            scale = (section.outer.x1 - section.outer.x0) / outer.total_mm
            biggest = max(step["diameter_mm"] for step in outer.steps)
            if outline is None:
                notes.append(f"контур фланца с торца: {why}")
                flange = None
            elif abs(outline.px_per_mm / scale - 1.0) > _SCALE_AGREEMENT:
                notes.append(
                    f"контур фланца с торца в другом масштабе ({outline.px_per_mm:.2f} px/мм "
                    f"против {scale:.2f} у разреза)"
                )
                flange = None
            elif outline.diameter_mm <= biggest:
                notes.append(f"контур с торца Ø{outline.diameter_mm:g} не больше тела")
                flange = None
            else:
                reach = outline.diameter_mm / 2.0 * outline.px_per_mm
                end_view = (
                    round(outline.centre_px[0] - reach, 1),
                    round(outline.centre_px[1] - reach, 1),
                    round(outline.centre_px[0] + reach, 1),
                    round(outline.centre_px[1] + reach, 1),
                )
                sketch, origin = outline_sketch(
                    outline.diameter_mm / 2.0,
                    outline.flats,
                    outline.flat_distance_mm,
                    outline.phase_deg,
                )
                profile: dict[str, Any] = {"shape": "sketch", "sketch": sketch, "holes": []}
                pattern, why = propose_hole_pattern(gray, spec, outline)
                if pattern is None:
                    notes.append(f"отверстия фланца: {why}")
                else:
                    profile["hole_patterns"] = [pattern]
                flange = {
                    **flange,
                    "sketch_origin_mm": [round(origin[0], 6), round(origin[1], 6)],
                    "profile": profile,
                    "outline": {
                        "diameter_mm": outline.diameter_mm,
                        "flats": outline.flats,
                        "flat_distance_mm": outline.flat_distance_mm,
                        "phase_deg": outline.phase_deg,
                        "coverage": outline.coverage,
                    },
                }
    return (
        SleeveProposal(
            outer=tuple(outer.steps),
            bore=tuple(bore.steps),
            total_mm=outer.total_mm,
            flange=flange,
            notes=tuple(notes),
            frame=section.frame(outer.total_mm),
            end_view_bbox_px=end_view if flange else None,
        ),
        "",
    )


def _flange_on_section(section: SleeveSection, outer: Any, labels: Any):
    """Станция и толщина фланца: грани разреза, привязанные к надписям цепочки
    (от уступа или торца ± надпись; толщина — надпись)."""
    total = outer.total_mm
    scale = total / float(section.outer.x1 - section.outer.x0)
    left = (section.flange_px[0] - section.outer.x0) * scale
    right = (section.flange_px[1] - section.outer.x0) * scale
    # Допуски формы («0,01») — тоже голые числа; цепочку они не образуют.
    values = sorted({value for value, _column in labels.axial if value >= 0.5})
    tolerance = max(0.4, _FLANGE_STATION_SHARE * total)
    stations = [0.0]
    for step in outer.steps:
        stations.append(stations[-1] + float(step["length_mm"]))
    anchors = sorted(set(round(s, 6) for s in stations))

    def snap(position: float) -> float | None:
        options = {a + v for a in anchors for v in values} | {
            a - v for a in anchors for v in values
        }
        options = {round(o, 6) for o in options if 0 < o < total}
        ranked = sorted(options, key=lambda o: abs(o - position))
        if not ranked or abs(ranked[0] - position) > tolerance:
            return None
        return ranked[0]

    start, end = snap(left), snap(right)
    if start is None or end is None or end <= start:
        return None, f"грани {left:.2f}…{right:.2f} мм не объясняются надписями"
    thickness = round(end - start, 6)
    if not any(abs(thickness - v) <= 1e-6 for v in values):
        return None, f"толщина {thickness:g} мм не надписана"
    return {"axial_start_mm": start, "thickness_mm": thickness}, ""
