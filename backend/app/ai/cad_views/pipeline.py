"""Сквозной конвейер метода `views`: лист → тело вращения (первая стратегия D).

1. Роли областей листа (этап A, `sheet_reading`).
2. Надписи — одним вопросом модели, смысл — разбором ЕСКД (`labels`).
3. Главное изображение: увеличение до рабочей толщины линии; ось симметрии
   (горизонтальная или вертикальная — вертикальная поворачивается).
4. Профиль: по материалу разреза (`section_material`) или по силуэту вида.
5. Масштабы: по радиусу — надписи Ø, вдоль оси — габарит.
6. Элементы по видам той же детали в проекционной связи (`view_features`).
7. Тело — операциями ядра (`revolve_body` + элементы по размещению).

Другие стратегии D (выдавливание плана, корпуса) — следующие шаги; деталь,
которая не тело вращения, честно возвращается с причиной.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

LABELS_PROMPT = (
    "Выпиши с чертежа ВСЕ размерные надписи и обозначения ровно так, как они "
    "написаны (с Ø, R, M, допусками, «гл.», «N отв.», фасками «1×45°», углами). "
    "Не пересчитывай и не придумывай. Только надписи у изображений детали: "
    "содержимое таблиц (параметры зубчатого венца, спецификация), основной "
    "надписи (штампа) и технических требований НЕ выписывай — это не размеры. "
    'Ответ — ОДНОЙ строкой JSON: {"labels": ["...", "..."]}'
)
LABELS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"labels": {"type": "array", "items": {"type": "string"}}},
    "required": ["labels"],
}
# Рабочая толщина основной линии для замеров (px).
_WORK_LINE_PX = 7.0


@dataclass
class ViewsResult:
    ok: bool
    reason: str = ""
    candidate: dict[str, Any] | None = None
    profile: dict[str, Any] = field(default_factory=dict)
    features: list[dict[str, Any]] = field(default_factory=list)
    scales: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # E1: надписи листа, найденные / не найденные на построенном теле.
    coverage: dict[str, Any] = field(default_factory=dict)


def _line_px(gray: Any) -> float:
    import numpy as np

    from app.ai.cad_views.section_material import ink_mask

    ink = ink_mask(gray, 3.0)
    runs: list[int] = []
    for x in range(0, ink.shape[1], 2):
        edges = np.diff(np.concatenate([[0], ink[:, x], [0]]))
        runs.extend(
            int(r) for r in np.where(edges == -1)[0] - np.where(edges == 1)[0] if 1 <= r < 30
        )
    if not runs:
        return 3.0
    # Основная линия — верхняя часть распределения толщин (тонкие вдвое тоньше).
    return float(np.percentile(runs, 70))


def prepare(
    gray: Any, box: tuple[int, int, int, int], margin: float = 0.03
) -> tuple[Any, float, tuple[int, int]]:
    """Вырез области с полем, увеличенный до рабочей толщины линии.

    Возвращает (изображение, коэффициент увеличения, начало выреза)."""
    import cv2
    import numpy as np

    g = np.asarray(gray)
    x0, y0, x1, y1 = box
    pad = int(margin * max(x1 - x0, y1 - y0))
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(g.shape[1], x1 + pad), min(g.shape[0], y1 + pad)
    crop = g[y0:y1, x0:x1]
    line = _line_px(crop)
    factor = max(1.0, min(4.0, _WORK_LINE_PX / max(line, 1.0)))
    if factor > 1.05:
        crop = cv2.resize(crop, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
    return crop, factor, (x0, y0)


def _symmetric_orientation(gray: Any, line: float) -> tuple[bool, float]:
    """(вертикальная ли ось, сила симметрии) — по горизонтальным штрихам.

    Пара линий, зеркальных относительно оси, на скане и после увеличения
    расходится на 1–2 px: попиксельное совпадение давало ей оценку не выше
    случайной, и колесо p009 (ось горизонтальна) поворачивалось набок.
    Совпадение — с допуском в толщину линии поперёк.
    """
    import cv2
    import numpy as np

    from app.ai.cad_views.section_material import ink_mask

    def strength(image: Any) -> float:
        ink = ink_mask(image, line)
        horizontal = cv2.morphologyEx(
            ink, cv2.MORPH_OPEN, np.ones((1, max(3, int(3 * line))), np.uint8)
        )[::2, ::2]
        kernel = max(3, int(line)) | 1
        wide = cv2.dilate(horizontal, np.ones((kernel, 1), np.uint8))
        height = horizontal.shape[0]
        best = 0.0
        for y in range(height // 5, 4 * height // 5):
            half = min(y, height - y)
            above, below = horizontal[y - half : y], horizontal[y : y + half][::-1]
            wide_above, wide_below = wide[y - half : y], wide[y : y + half][::-1]
            matched = float((above & wide_below).sum()) + float((below & wide_above).sum())
            best = max(best, matched / (float(above.sum() + below.sum()) + 1.0))
        return best

    straight = strength(gray)
    turned = strength(np.ascontiguousarray(np.rot90(gray)))
    return (turned > 1.05 * straight), max(straight, turned)


def lifted_rim(gray: Any, profile: Any, line: float) -> Any:
    """Площадка материала, поднятая до основной линии над незаштрихованной
    полосой (зубья в разрезе не штрихуют, ГОСТ 2.402: штриховка кончается у
    впадин, вершины — основная линия выше). None — такой линии нет."""
    import cv2
    import numpy as np

    from app.ai.cad_views.extrude_body import main_line_mask
    from app.ai.cad_views.revolve_profile import HalfProfile, plateaus

    # Только основные линии: тонкие (выносная знака шероховатости, делительная
    # окружность штрихпунктиром) лежат в той же полосе над штриховкой.
    _ink, thick, _line = main_line_mask(gray)
    horizontal = cv2.morphologyEx(
        thick, cv2.MORPH_OPEN, np.ones((1, max(3, int(3 * line))), np.uint8)
    )
    axis = int(round(profile.axis_y))
    lifted = list(profile.outer)
    changed = False
    for xa, xb, radius in plateaus(profile.outer, 3 * line):
        found = []
        columns = range(int(xa) + int(line), int(xb) - int(line))
        for x in columns:
            top = int(axis - radius - 1.5 * line)
            limit = int(axis - 1.3 * radius)
            hit = None
            for y in range(top, max(limit, 0), -1):
                if horizontal[y, x]:
                    hit = axis - y
                    break
            if hit is not None:
                found.append(hit)
        if len(columns) < 3 or len(found) < 0.8 * len(columns):
            continue
        level = float(np.median(found))
        if np.std(found) > line or level - radius < 2 * line:
            continue
        lifted = [(x, level if xa <= x <= xb and abs(r - radius) < line else r) for x, r in lifted]
        changed = True
    if not changed:
        return None
    return HalfProfile(
        axis_y=profile.axis_y,
        line_px=profile.line_px,
        x0=profile.x0,
        x1=profile.x1,
        outer=lifted,
        inner=profile.inner,
    )


def tolerant_axis(mask: Any, line: float) -> int:
    """Ось симметрии горизонталей с допуском в толщину линии.

    Попиксельное совпадение зеркальных линий ломается о разницу в толщину:
    у втулки p015 (половина вида и половина разреза) верная ось проигрывала
    ложной, относительно которой симметричны образующая и линия расточки."""
    import cv2
    import numpy as np

    small = mask[::2, ::2]
    kernel = max(3, int(line)) | 1
    wide = cv2.dilate(small, np.ones((kernel, 1), np.uint8))
    height = small.shape[0]
    best = (-1.0, height // 2)
    for y in range(height // 5, 4 * height // 5):
        half = min(y, height - y)
        above, below = small[y - half : y], small[y : y + half][::-1]
        wide_above, wide_below = wide[y - half : y], wide[y : y + half][::-1]
        score = float((above & wide_below).sum()) + float((below & wide_above).sum())
        if score > best[0]:
            best = (score, y)
    # Уточнение: точное совпадение в окне ± толщина линии вокруг грубой оси.
    coarse = 2 * best[1]
    full_height = mask.shape[0]
    refined = (-1.0, coarse)
    for y in range(max(1, coarse - int(line) - 1), min(full_height - 1, coarse + int(line) + 2)):
        half = min(y, full_height - y)
        above = mask[y - half : y].astype(np.float32)
        below = mask[y : y + half][::-1].astype(np.float32)
        score = float((above * below).sum()) / (float(above.sum() + below.sum()) + 1.0)
        if score > refined[0]:
            refined = (score, y)
    return refined[1]


def _drop_section_traces(half: list[Any], gray: Any, axis: int, line: float) -> list[Any]:
    """Следы секущих плоскостей (ГОСТ 2.305) — не бурты.

    След — короткий толстый штрих над и под видом со стрелкой и буквой;
    выносные связывают его с контуром, и силуэт «вырастал» буртом Ø52 и
    Ø99 у вала Ø50 (многоосевой вал 0). У бурта две боковые кромки от
    соседней ступени до его образующей, у следа — один штрих."""

    from app.ai.cad_views.extrude_body import main_line_mask

    values = [h for h in half if h is not None]
    if len(values) < 10:
        return half
    _ink, thick, _line = main_line_mask(gray)
    height, width = thick.shape[:2]
    out = list(half)
    # Площадки — участки почти одного уровня (скачок больше полутора линий).
    plateaus: list[list[int]] = []
    for x, h in enumerate(out):
        if h is None:
            continue
        if plateaus and x - plateaus[-1][1] <= 2 and abs(h - out[plateaus[-1][1]]) <= 1.5 * line:
            plateaus[-1][1] = x
        else:
            plateaus.append([x, x])
    for index, (x, end) in enumerate(plateaus):
        if index == 0 or index == len(plateaus) - 1:
            continue
        level = max(out[k] for k in range(x, end + 1))
        left = out[plateaus[index - 1][1]]
        right = out[plateaus[index + 1][0]]
        base = max(left, right)
        if (end - x) > 0.15 * len(values) or level <= 1.3 * base:
            continue
        # Боковые кромки: вертикали основной линии от base до level над осью.
        top, bottom = int(axis - level + line), int(axis - base - line)
        sides = 0
        if 0 <= top < bottom <= height:
            columns = [
                c
                for c in range(max(0, x - int(line)), min(width, end + int(line) + 1))
                if thick[top:bottom, c].mean() >= 0.7
            ]
            groups: list[list[int]] = []
            for c in columns:
                if groups and c - groups[-1][-1] <= 2:
                    groups[-1].append(c)
                else:
                    groups.append([c])
            sides = len(groups)
        if sides < 2:
            for k in range(x, end + 1):
                out[k] = base
    return out


def silhouette_profile(gray: Any, line: float, axis: int | None = None) -> Any:
    """Профиль тела вращения по неразрезанному виду: силуэт, без расточки."""
    import cv2
    import numpy as np

    from app.ai.cad_views.revolve_profile import (
        HalfProfile,
        _despike,
        _median,
        _simplify,
        drop_fins,
    )
    from app.ai.cad_views.section_material import ink_mask
    from app.ai.cad_views.view_features import _silhouette

    ink = ink_mask(gray, line)
    # Контур детали связан выносными в одну крупную фигуру; буквы
    # обозначений сечений («А», «Б») и подписи — отдельные мелкие фигуры, а
    # их горизонтальные штрихи симметричны оси и давали ложные бурты (p121:
    # Ø65 при наибольшем Ø50).
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    if count > 2:
        areas = stats[1:, cv2.CC_STAT_AREA]
        keep = np.zeros(count, bool)
        keep[1:] = areas >= 0.2 * areas.max()
        ink = (keep[labels] & (ink > 0)).astype(ink.dtype)
    horizontal = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN, np.ones((1, max(3, int(3 * line))), np.uint8)
    )
    if axis is None:
        axis = tolerant_axis(horizontal, line)
    half = drop_fins(
        _median(_despike(_silhouette(ink, axis, line), int(2 * line)), int(2 * line) + 1),
        int(2.5 * line),
    )
    half = _drop_section_traces(half, gray, axis, line)
    present = [x for x, h in enumerate(half) if h is not None]
    if not present:
        return None
    # Разрыв силуэта (паз, подпись поверх кромки) до четверти длины — та же деталь.
    gap = max(4 * line, 0.25 * (present[-1] - present[0]))
    runs: list[list[int]] = []
    for x in present:
        if runs and x - runs[-1][1] <= gap:
            runs[-1][1] = x
        else:
            runs.append([x, x])
    x0, x1 = max(runs, key=lambda r: r[1] - r[0])
    known = [x for x in range(x0, x1 + 1) if half[x] is not None]
    levels = [half[k] for k in known]
    points = [
        (float(x), float(np.interp(x, known, levels)) + line / 2.0) for x in range(x0, x1 + 1)
    ]
    return HalfProfile(
        axis_y=float(axis),
        line_px=line,
        x0=x0,
        x1=x1,
        outer=_simplify(points, max(1.0, 0.5 * line)),
    )


def _same_feature(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Тот же элемент: вид, ось, начало ближе 0,5 мм, размеры в 3 %."""
    import math

    if a.get("kind") != b.get("kind") or a.get("axis") != b.get("axis"):
        return False
    if math.dist(a["origin_mm"], b["origin_mm"]) > 0.5:
        return False
    for key in ("diameter_mm", "width_mm", "height_mm", "depth_mm"):
        va, vb = a.get(key), b.get(key)
        if (va is None) != (vb is None):
            return False
        if va is not None and abs(va - vb) > 0.03 * max(abs(va), abs(vb), 1e-6):
            return False
    return True


def section_features(
    gray: Any,
    main: Any,
    profile: Any,
    factor: float,
    origin: tuple[int, int],
    axial: float,
    outer: list[dict[str, float]],
    label_texts: list[str],
    region_labels: dict[int, list[str]] | None,
) -> list[dict[str, Any]]:
    """Элементы на вынесенных сечениях вала — проверенный путь спека (У6).

    Профиль и масштаб — в пикселях выреза; следы и сечения ищутся по листу,
    поэтому всё переводится в пиксели листа."""
    from types import SimpleNamespace

    import numpy as np

    from app.ai.cad_recognize.verifiers.reconcile import placed_additions
    from app.ai.cad_recognize.verifiers.section_outline import propose_placed
    from app.ai.cad_recognize.verifiers.section_traces import locate_section_traces

    # Ступени — площадки профиля (конусы и фаски не ступени).
    steps: list[dict[str, float]] = []
    for a, b in zip(outer, outer[1:]):
        length = float(b["z"]) - float(a["z"])
        if length <= 0 or abs(float(a["r"]) - float(b["r"])) > 1e-6:
            if steps and length > 0:
                steps[-1]["length_mm"] += length
            continue
        steps.append({"diameter_mm": round(2 * float(a["r"]), 3), "length_mm": length})
    if not steps:
        return []
    sheet_mm_per_px = axial * factor  # вырез увеличен в factor раз
    x0 = origin[0] + profile.x0 / factor
    x1 = origin[0] + profile.x1 / factor
    xs = [x for x, _r in profile.outer]
    rs = [r for _x, r in profile.outer]
    columns = np.arange(int(round(x0)), int(round(x1)) + 1)
    half = np.interp((columns - origin[0]) * factor, xs, rs) / factor
    sheet_profile = SimpleNamespace(
        x0=float(columns[0]),
        x1=float(columns[-1]),
        axis_y=origin[1] + profile.axis_y / factor,
        line_px=profile.line_px / factor,
        half_px=half,
    )
    frame = SimpleNamespace(mm_per_px=sheet_mm_per_px)
    try:
        traces = locate_section_traces(gray, frame, sheet_profile)
        if not traces:
            return []
        body = {"outer": steps, "placed_features": []}
        proposals = propose_placed(
            gray, tuple(main.box), sheet_mm_per_px, body, traces, sheet_profile
        )
    except Exception:  # noqa: BLE001 — элементы по сечениям необязательны
        return []
    if not proposals:
        return []
    texts = [*label_texts, *sum((region_labels or {}).values(), [])]
    spec = {"main_view": body, "dimensions": [{"value": t} for t in texts]}
    notes: list[str] = []
    found = []
    for addition in placed_additions(spec, {"placed_proposals": proposals}, notes):
        item = dict(addition["feature"])
        item.pop("sheet_station", None)
        item["note"] = addition.get("reason") or "элемент по сечению"
        found.append(item)
    return found


def build_revolve(
    gray: Any,
    reading: Any,
    label_texts: list[str],
    *,
    part: str | None = None,
    region_labels: dict[int, list[str]] | None = None,
) -> ViewsResult:
    """Тело вращения по главному изображению и связанным видам листа."""
    import numpy as np

    from app.ai.cad_views.labels import parse_label
    from app.ai.cad_views.revolve_body import revolve_candidate, revolve_points
    from app.ai.cad_views.revolve_profile import (
        fit_axial_scale,
        fit_scale,
        plateaus,
        profile_from_material,
    )
    from app.ai.cad_views.section_material import ink_mask, section_material
    from app.ai.cad_views.view_features import side_view_features

    # Линейка бумаги — основная надпись ЕСКД (нет её — масштаб только по
    # надписям).
    try:
        from app.ai.cad_recognize.verifiers.sheet_scale import locate_title_block

        block = locate_title_block(gray)
    except Exception:  # noqa: BLE001 — лист без штампа или необычный
        block = None
    paper = block.paper_px_per_mm if block is not None else None
    pictures = [r for r in reading.regions if r.role in ("view", "section")]
    if part:
        pictures = [r for r in pictures if (r.part or "") == part] or pictures
    if not pictures:
        return ViewsResult(False, "на листе не найдено изображения детали")

    def label_sets(texts: list[str]) -> tuple[list[float], list[float], list[float], list[float]]:
        parsed = [parse_label(t) for t in texts]
        diameters = [
            lab.value for lab in parsed if lab.kind in ("diameter", "thread") and lab.value
        ]
        holes = [
            lab.value
            for lab in parsed
            if lab.kind == "diameter" and lab.surface == "hole" and lab.value
        ]
        shafts = [v for v in diameters if v not in holes]
        linear = [lab.value for lab in parsed if lab.kind == "linear" and lab.value]
        return diameters, holes, shafts, linear

    sheet_sets = label_sets(label_texts)

    def sets_for(region: Any) -> tuple[list[float], list[float], list[float], list[float]]:
        # Надписи своего изображения (вырез с полями под размеры) читаются
        # точнее: на плотном листе, ужатом целиком, числа пропадают. Диаметры
        # — объединением с листом (в вырез попадают не все Ø отверстий),
        # длины — свои, если они есть: габарит соседней детали деталировки
        # не должен становиться длиной этой.
        own = (region_labels or {}).get(region.n) or []
        if not own:
            return sheet_sets
        own_sets = label_sets(own)
        both = label_sets(own + list(label_texts))
        linear = own_sets[3] if len(set(own_sets[3])) >= 2 else both[3]
        return both[0], both[1], both[2], linear

    # Изображение для профиля: названное моделью главным — первым, затем
    # разрезы и виды по площади; берётся то, чей профиль надписи Ø объясняют
    # лучше всего. Живой /cad: модель назвала главным вид с торца, и тело
    # вышло по нему; порог — две объяснённые площадки (одна «Ø0,05» знака
    # биения давала деталь размером 0,05 мм).
    ordered = sorted(
        pictures,
        key=lambda r: (
            r.n != reading.main,
            r.role != "section",
            -(r.box[2] - r.box[0]) * (r.box[3] - r.box[1]),
        ),
    )
    notes: list[str] = []
    best = None
    tried: list[str] = []
    for region in ordered[:6]:
        crop, factor, origin = prepare(gray, region.box)
        line = _line_px(crop)
        vertical, symmetry = _symmetric_orientation(crop, line)
        if symmetry < 0.4:
            tried.append(f"{region.name or region.n}: нет оси симметрии")
            continue
        if vertical:
            crop = np.ascontiguousarray(np.rot90(crop))
        # Контур упирается в край выреза — рамка области обрезала деталь
        # (колесо part_06: вершины зубьев Ø46 выше рамки). Инженер смотрит
        # шире: тот же вид с запасом побольше. Всем подряд запас не
        # расширяется — в вырез тогда попадают соседние размеры (part_02).
        probe = silhouette_profile(crop, line)
        if probe is not None:
            reach = max((r for _x, r in probe.outer), default=0.0)
            if min(probe.axis_y, crop.shape[0] - probe.axis_y) - reach <= 2 * line:
                crop, factor, origin = prepare(gray, region.box, margin=0.12)
                line = _line_px(crop)
                if vertical:
                    crop = np.ascontiguousarray(np.rot90(crop))
                probe = silhouette_profile(crop, line)
        # Разрез узнаётся по штриховке, а не по роли: роль от прогона к
        # прогону плавает («Опора»: главный вид в разрезе назван видом, вид
        # с торца — разрезом). Пробуются оба профиля, берётся лучше
        # объяснённый надписями.
        variants = []
        # Ось — по всему изображению (силуэт основных линий): у половины
        # разреза штриховка с одной стороны, и ось по ней находится неверно.
        probe_axis = int(round(probe.axis_y)) if probe is not None else None
        material, axis = section_material(crop, line, axis=probe_axis)
        if material.sum() > 0:
            by_material = profile_from_material(material, axis, line, ink=ink_mask(crop, line))
            if by_material is not None:
                variants.append((by_material, True))
        if variants:
            rim = lifted_rim(crop, variants[0][0], line)
            if rim is not None:
                variants.append((rim, True))
        # Силуэт только по основным линиям: тонкие выноски, прошедшие через
        # контур, «достраивали» ступень конусом (многоосевой вал 5: выноска
        # «Ø6 120°» дала Ø60 и скос до Ø28). Вариант сверяется с надписями
        # наравне с остальными.
        import cv2

        from app.ai.cad_views.extrude_body import main_line_mask

        ink_all, thick_only, _ = main_line_mask(crop)
        thin_only = ink_all & (1 - cv2.dilate(thick_only, np.ones((3, 3), np.uint8)))
        if thin_only.any():
            main_only = crop.copy()
            main_only[thin_only > 0] = 255
            by_main_lines = silhouette_profile(main_only, line)
            if by_main_lines is not None:
                variants.append((by_main_lines, False))
            # Контур детали — одна связная фигура основных линий; следы
            # секущих (штрих со стрелкой над и под видом), буквы и стрелки
            # размеров — отдельные: силуэт по ним «вырастал» буртами (вал
            # с четырьмя сечениями).
            count, labels_cc, stats, _ = cv2.connectedComponentsWithStats(thick_only, 8)
            axis_row = probe_axis if probe_axis is not None else None
            if count > 2 and axis_row is not None:
                # Контур может рваться (паз, надпись поверх кромки) — берутся
                # все фигуры, пересекающие ось: торцы и уступы её пересекают,
                # следы, буквы и стрелки лежат над и под видом.
                straddle = [
                    index
                    for index in range(1, count)
                    if stats[index][cv2.CC_STAT_TOP] < axis_row - line
                    and stats[index][cv2.CC_STAT_TOP] + stats[index][cv2.CC_STAT_HEIGHT]
                    > axis_row + line
                ]
                outline_only = np.full_like(crop, 255)
                keep = cv2.dilate(
                    np.isin(labels_cc, straddle).astype(np.uint8), np.ones((3, 3), np.uint8)
                )
                outline_only[keep > 0] = crop[keep > 0]
                by_outline = silhouette_profile(outline_only, line)
                if by_outline is not None:
                    variants.append((by_outline, False))
        by_silhouette = silhouette_profile(crop, line)
        if by_silhouette is not None:
            variants.append((by_silhouette, False))
            # Зубья в осевом разрезе не штрихуют (ГОСТ 2.402): штриховка
            # кончается у впадин, наружный контур — основные линии силуэта,
            # расточка — по материалу (колесо p009: Ø78 по зубьям).
            if (
                variants[0][1]
                and variants[0][0].inner
                and (abs(variants[0][0].axis_y - by_silhouette.axis_y) <= 2 * line)
            ):
                from app.ai.cad_views.revolve_profile import HalfProfile

                hatched = variants[0][0]
                # Границы детали — где есть и контур, и материал: за торец
                # уходит то материал (скосы стрелок Ø у торца вала-шестерни
                # p018), то силуэт (выносные у колеса part_06).
                x0 = max(by_silhouette.x0, hatched.x0)
                x1 = min(by_silhouette.x1, hatched.x1)
                if x1 - x0 > 4 * line:

                    def clip(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
                        kept = [(x, r) for x, r in points if x0 <= x <= x1]
                        if not kept:
                            return kept
                        xs = [x for x, _r in points]
                        rs = [r for _x, r in points]
                        head = (float(x0), float(np.interp(x0, xs, rs)))
                        tail = (float(x1), float(np.interp(x1, xs, rs)))
                        return [head, *[p for p in kept if x0 < p[0] < x1], tail]

                    inner = clip(hatched.inner)
                    outer = clip(by_silhouette.outer)
                    if inner and outer:
                        variants.append(
                            (
                                HalfProfile(
                                    axis_y=by_silhouette.axis_y,
                                    line_px=line,
                                    x0=x0,
                                    x1=x1,
                                    outer=outer,
                                    inner=inner,
                                ),
                                True,
                            )
                        )
        if not variants:
            tried.append(f"{region.name or region.n}: профиль не найден")
            continue
        diameters, holes, shafts, linear = sets_for(region)
        for profile, hatched in variants:
            radial, hits = fit_scale(profile, shafts, holes)
            # Габарит — самая надёжная надпись: масштаб, при котором длина
            # профиля с ним не сходится, объясняет Ø случайно («Опора»: 5 из
            # 11 Ø при длине 21 вместо 29). Габарит засчитывается как ещё одна
            # объяснённая надпись.
            overall = max(linear) / max(1, profile.x1 - profile.x0) if linear else None
            if overall is not None:
                along, along_hits = fit_scale(profile, shafts, holes, near=overall)
                if along is not None and along_hits + 1 > hits and along_hits >= 1:
                    radial, hits = along, along_hits + 1
            if paper is not None and radial is not None:
                # Масштаб по штампу: основная надпись 185 × 55 мм — линейка
                # бумаги, масштаб изображения — из ряда ГОСТ 2.302. Масштаб
                # вне ряда — случайное совпадение надписей Ø; из масштабов
                # ряда берётся лучший по надписям, если объясняет хоть две.
                from app.ai.cad_recognize.verifiers.sheet_scale import _GOST_SCALES

                allowed = [
                    1.0 / (paper * factor * model / sheet_paper)
                    for model, sheet_paper in _GOST_SCALES
                ]
                if not any(abs(radial / g - 1.0) <= 0.04 for g in allowed):
                    options = []
                    for g in allowed:
                        found, found_hits = fit_scale(profile, shafts, holes, near=g, spread=0.04)
                        if found is not None and found_hits >= 2:
                            options.append((found_hits, found))
                    if options:
                        hits, radial = max(options)
            source = "разрез" if hatched else "силуэт"
            tried.append(f"{region.name or region.n} ({source}): объяснено надписей {hits}")
            if radial is None or hits < 2:
                continue
            # По ЕСКД каждый диаметр образмерен: площадка намного больше
            # наибольшей надписи Ø — чужие линии в профиле (размерные,
            # выносные, соседний вид), а не деталь (p007: Ø192 при Ø56).
            widest = 2 * max((r for _x, r in profile.outer), default=0.0) * radial
            if diameters and widest > 1.15 * max(diameters):
                tried[-1] += f", но профиль шире наибольшего Ø ({widest:.1f} > {max(diameters):g})"
                continue
            # Вдоль оси то же: длиннее габарита деталь быть не может (p018:
            # профиль 220 при габарите 146 — захвачены линии за торцом).
            # Граница — наибольший размер всего листа: в вырез вида габарит
            # может не попасть («Опора», вид сверху: 12,5 при габарите 29).
            longest = (profile.x1 - profile.x0) * radial
            bound = max(linear + sheet_sets[3], default=0.0)
            # Вдвое длиннее наибольшей надписи — не захват линий за торцом, а
            # непрочитанный габарит (втулка p015: 18 при наибольшей «9»).
            if bound and 1.15 * bound < longest <= 1.6 * bound:
                tried[-1] += f", но профиль длиннее габарита ({longest:.1f} > {bound:g})"
                continue
            # Равное число объяснённых площадок — выигрывает профиль, у
            # которого они составляют большую долю: случайный масштаб
            # объясняет две площадки из многих (колесо p009 — 2 из 4 при
            # диаметре 92 вместо 78), верный — почти все.
            count = len(plateaus(profile.outer, 3 * profile.line_px)) + len(
                [p for p in plateaus(profile.inner, 3 * profile.line_px) if p[2] > 0]
            )
            # Наибольшая надпись Ø — габарит по диаметру: при равном счёте
            # выигрывает профиль, чья наибольшая площадка её объясняет
            # (колесо part_06: вершины Ø46, а не впадины, совпавшие с Ø38).
            overall_d = max(shafts or diameters, default=0.0)
            fits_overall = bool(overall_d) and abs(widest - overall_d) <= 0.03 * overall_d
            key = (hits, fits_overall, round(hits / max(1, count), 2), hatched)
            if best is None or key > best[0]:
                best = (key, region, crop, factor, origin, line, vertical, profile, radial, hits)
                chosen_sets = (diameters, holes, shafts, linear)
    if best is None:
        return ViewsResult(
            False,
            "ни одно изображение детали не объяснено надписями Ø (нужно от двух разных, габарит считается): "
            + "; ".join(tried),
            notes=notes,
        )
    _key, main, crop, factor, origin, line, vertical, profile, radial, hits = best
    diameters, holes, shafts, linear = chosen_sets
    notes.append("выбор изображения: " + "; ".join(tried))
    axial, _ = fit_axial_scale(profile, linear, near=radial)
    axial = axial or radial
    outer, bore = revolve_points(profile, axial, radial)
    # C2: станции и Ø площадок — номиналы надписей (перечерчивание инженером).
    from app.ai.cad_views.nominals import nominal_revolve

    length = max((p["z"] for p in outer), default=0.0)
    outer, bore, snapped = nominal_revolve(
        outer,
        bore,
        linear,
        shafts or diameters,
        holes or diameters,
        tolerance=max(1.2 * line * axial, 0.006 * length),
        bore_share=0.07 if holes else 0.03,
        diameter_tolerance_mm=1.2 * line * radial,
    )
    if snapped:
        notes.append(f"номиналы надписей: исправлено {snapped} значений замера")
    candidate = revolve_candidate(outer, bore, part or main.part or "деталь")
    features: list[dict[str, Any]] = []
    # Виды той же детали в проекционной связи: перекрываются с главным по
    # столбцам (или строкам, если ось вертикальная).
    for region in pictures:
        # Элементы — по видам: сечение и разрез показывают деталь в другой
        # плоскости (z4-r4: сечения А-А и Б-Б дали ложные «лыски»).
        if region is main or region.role != "view":
            continue
        a0, a1 = (main.box[1], main.box[3]) if vertical else (main.box[0], main.box[2])
        b0, b1 = (region.box[1], region.box[3]) if vertical else (region.box[0], region.box[2])
        overlap = min(a1, b1) - max(a0, b0)
        if overlap < 0.6 * min(a1 - a0, b1 - b0):
            continue
        # Вырез вида — в тех же столбцах (строках), что главное изображение:
        # проекционная связь, тот же масштаб и то же увеличение.
        import cv2

        g = np.asarray(gray)
        if vertical:
            y0, y1 = origin[1], origin[1] + int(round(crop.shape[1] / factor))
            x0, x1 = region.box[0], region.box[2]
            pad = int(0.03 * (x1 - x0))
            piece = g[y0:y1, max(0, x0 - pad) : x1 + pad]
        else:
            x0, x1 = origin[0], origin[0] + int(round(crop.shape[1] / factor))
            y0, y1 = region.box[1], region.box[3]
            pad = int(0.03 * (y1 - y0))
            piece = g[max(0, y0 - pad) : y1 + pad, x0:x1]
        view_crop = (
            cv2.resize(piece, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
            if factor > 1.05
            else piece
        )
        if vertical:
            view_crop = np.ascontiguousarray(np.rot90(view_crop))
        found = side_view_features(view_crop, profile, axial, radial, diameters, linear)
        for item in found:
            item["view"] = region.name or f"рамка {region.n}"
            # Один и тот же элемент с двух перекрывающихся изображений
            # («вид сверху» дважды в разметке) — одна лыска, а не две: второй
            # карман «не касается материала», и ядро отвергало всё тело.
            if any(_same_feature(item, other) for other in features):
                continue
            features.append(item)
    # Лыски и радиальные отверстия — по вынесенным сечениям (У6): станция —
    # след секущей на главном виде, угол и размер — сечение, числа —
    # надписи листа. Многоосевые валы собирались без единого элемента.
    if not vertical:
        for item in section_features(
            gray, main, profile, factor, origin, axial, outer, label_texts, region_labels
        ):
            if not any(_same_feature(item, other) for other in features):
                features.append(item)
                notes.append(item.pop("note", "элемент по сечению"))
    # Поверхность под поперечным отверстием — цилиндр по соседям, а не дуги
    # пересечения из разреза; фаска «c×45°» — у входа резьбы.
    from app.ai.cad_views.nominals import bridge_cross_holes, chamfer_threaded_end

    outer, bore, bridged = bridge_cross_holes(
        outer, bore, [f for f in features if f.get("kind") == "hole"]
    )
    if bridged:
        notes.append(f"зона поперечного отверстия — цилиндр по соседям: {bridged}")
    every = [parse_label(t) for t in [*label_texts, *sum((region_labels or {}).values(), [])]]
    chamfer_sizes = [
        lab.value
        for lab in every
        if lab.kind == "chamfer" and lab.value and abs((lab.angle or 45.0) - 45.0) < 1.0
    ]
    thread_sizes = [lab.value for lab in every if lab.kind == "thread" and lab.value]
    outer, chamfer_note = chamfer_threaded_end(outer, chamfer_sizes, thread_sizes)
    if chamfer_note:
        notes.append(chamfer_note)
    if bridged or chamfer_note:
        candidate = revolve_candidate(outer, bore, part or main.part or "деталь")
    for item in features:
        params: dict[str, Any] = {
            "placement": {"origin": item["origin_mm"], "axis": item["axis"], "ref": item["ref"]}
        }
        for key in ("diameter_mm", "through", "profile", "width_mm", "height_mm", "depth_mm"):
            if key in item:
                params[key] = item[key]
        candidate["candidate"]["features"].append(
            {"kind": item["kind"], "params": params, "confidence": 0.6}
        )
    return ViewsResult(
        True,
        candidate=candidate,
        profile={
            "outer": outer,
            "bore": bore,
            "main_view": main.name or main.n,
            "role": main.role,
            "source_box": list(main.box),
        },
        features=features,
        scales={
            # Мм на пиксель ЛИСТА: вырез увеличен в factor раз.
            "radial_mm_per_px": radial * factor,
            "axial_mm_per_px": axial * factor,
            "diameters_explained": hits,
        },
        notes=notes,
    )


async def read_labels(gray: Any, *, router: Any = None, confidential: bool = True) -> list[str]:
    """Надписи листа одним вопросом модели (без схемы спека)."""
    from PIL import Image

    from app.ai.cad_recognize.spec_fragments import _ask

    if router is None:
        from app.ai.router import ai_router

        router = ai_router
    image = Image.fromarray(gray)
    image.thumbnail((2400, 2400))
    answer = await _ask(
        LABELS_PROMPT,
        image,
        router=router,
        confidential=confidential,
        num_predict=3000,
        schema=LABELS_SCHEMA,
        timeout_seconds=150.0,
    )
    return [str(t) for t in (answer or {}).get("labels") or [] if str(t).strip()]


LABELS_VIEW_PROMPT = (
    "Это вырез технического чертежа: одно изображение детали с размерами вокруг. "
    "Выпиши ВСЕ размерные надписи на этом вырезе ровно так, как они написаны "
    "(с Ø, R, M, допусками, «гл.», «N отв.», фасками «1×45°», углами). Числа "
    "переписывай цифра в цифру; обрезанную краем надпись не выписывай; "
    "содержимое таблиц, штампа и технических требований не выписывай. Ответ — "
    'ОДНОЙ строкой JSON: {"labels": ["...", "..."]}'
)


async def read_region_labels(
    gray: Any,
    regions: list[Any],
    *,
    router: Any = None,
    confidential: bool = True,
    limit: int = 4,
) -> dict[int, list[str]]:
    """Надписи каждого изображения детали — по его вырезу с полями под размеры."""
    from PIL import Image

    from app.ai.cad_recognize.spec_fragments import _ask

    if router is None:
        from app.ai.router import ai_router

        router = ai_router
    height, width = gray.shape[:2]
    out: dict[int, list[str]] = {}
    ordered = sorted(regions, key=lambda r: -(r.box[2] - r.box[0]) * (r.box[3] - r.box[1]))
    for region in ordered[:limit]:
        x0, y0, x1, y1 = region.box
        pad = int(0.3 * max(x1 - x0, y1 - y0))
        crop = gray[
            max(0, y0 - pad) : min(height, y1 + pad), max(0, x0 - pad) : min(width, x1 + pad)
        ]
        image = Image.fromarray(crop)
        longest = max(image.size)
        if longest < 1400:
            factor = min(3.0, 1400 / max(1, longest))
            image = image.resize(
                (int(image.size[0] * factor), int(image.size[1] * factor)), Image.LANCZOS
            )
        image.thumbnail((2000, 2000))
        answer = await _ask(
            LABELS_VIEW_PROMPT,
            image,
            router=router,
            confidential=confidential,
            num_predict=2000,
            schema=LABELS_SCHEMA,
            timeout_seconds=120.0,
        )
        out[region.n] = [str(t) for t in (answer or {}).get("labels") or [] if str(t).strip()]
    return out


# Ниже этой доли надписей на теле основа не принимается: у заведомо неверных
# тел реальных листов (литой корпус как тело вращения, «вал» 6 × 14) на теле
# 22–38 % надписей, у верных — от 44 % (сплошной вал-шестерня) до 100 %.
_MIN_COVERAGE = 0.40
_COVERAGE_MIN_LABELS = 5


def choose_body(
    gray: Any,
    reading: Any,
    labels: list[str],
    region_labels: dict[int, list[str]],
    merged: list[str],
) -> ViewsResult:
    """Обе основы (вращение, выдавливание) — берётся та, на которой больше
    надписей листа (E1); если и на лучшей их мало — честный отказ."""
    from app.ai.cad_views.checks import label_coverage
    from app.ai.cad_views.extrude_body import build_extrude
    from app.ai.cad_views.prismatic import build_prismatic

    revolved = build_revolve(gray, reading, labels, region_labels=region_labels)
    extruded = build_extrude(gray, reading, labels, region_labels=region_labels)
    prismatic = build_prismatic(gray, reading, labels, region_labels=region_labels)
    built = [r for r in (revolved, extruded, prismatic) if r.ok]
    if not built:
        return ViewsResult(
            False,
            f"тело вращения: {revolved.reason}; выдавливание: {extruded.reason}; "
            f"по трём видам: {prismatic.reason}",
            notes=revolved.notes + extruded.notes + prismatic.notes,
        )
    for candidate in built:
        candidate.coverage = label_coverage(candidate, merged)
    # Наибольшая линейная надпись — габарит детали: гипотеза, на теле которой
    # его нет, проигрывает при любой доле (корпус живьём: «пластина» вдвое
    # меньшего масштаба объясняла 86 % надписей половинками, но не «80»).
    from app.ai.cad_views.labels import parse_label

    lengths = [
        lab.value for lab in (parse_label(t) for t in merged) if lab.kind == "linear" and lab.value
    ]
    largest = max(lengths) if lengths else None

    def rank(result: ViewsResult) -> tuple[bool, bool, float]:
        explained = {
            lab.value
            for lab in (parse_label(t) for t in result.coverage.get("explained") or [])
            if lab.kind == "linear"
        }
        share = result.coverage.get("share") or 0.0
        # Только среди правдоподобных: габарит вне надписей бывает и у
        # верного тела (793539cc_p012: 0,78 у вала против 0,35 у «призмы»).
        return (share >= _MIN_COVERAGE, largest is None or largest in explained, share)

    best = max(built, key=rank)
    names = {
        id(revolved): "тело вращения",
        id(extruded): "выдавливание",
        id(prismatic): "по трём видам",
    }
    for other in (revolved, extruded, prismatic):
        if other is not best:
            share = other.coverage.get("share") if other.ok else None
            best.notes.append(
                names[id(other)]
                + (
                    f": надписей на теле {share:.0%}"
                    if share is not None
                    else f": {other.reason[:160]}"
                )
            )
    coverage = best.coverage
    total = len(coverage.get("explained") or []) + len(coverage.get("missing") or [])
    if (
        total >= _COVERAGE_MIN_LABELS
        and coverage.get("share") is not None
        and coverage["share"] < _MIN_COVERAGE
    ):
        return ViewsResult(
            False,
            f"тело не согласуется с надписями листа: на нём {len(coverage['explained'])} из "
            f"{total} (нет: {', '.join(coverage['missing'][:8])})",
            notes=best.notes,
            coverage=coverage,
        )
    return best


async def digitize_revolve(gray: Any, *, router: Any = None) -> tuple[ViewsResult, Any, list[str]]:
    """Лист → (результат, прочтение ролей, надписи)."""
    from app.ai.cad_views.sheet_reading import read_sheet

    reading = await read_sheet(gray, router=router)
    labels = await read_labels(gray, router=router)
    pictures = [r for r in reading.regions if r.role in ("view", "section")]
    region_labels = await read_region_labels(gray, pictures, router=router) if pictures else {}
    seen = set(labels)
    merged = list(labels)
    for texts in region_labels.values():
        for text in texts:
            if text not in seen:
                seen.add(text)
                merged.append(text)
    result = choose_body(gray, reading, labels, region_labels, merged)
    return result, reading, merged


# Лист → тело любой поддержанной основы (вращение, выдавливание).
digitize = digitize_revolve


__all__ = [
    "ViewsResult",
    "build_revolve",
    "digitize",
    "digitize_revolve",
    "read_labels",
    "read_region_labels",
]
