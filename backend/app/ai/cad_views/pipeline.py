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
    "Не пересчитывай и не придумывай. Ответ — ОДНОЙ строкой JSON: "
    '{"labels": ["...", "..."]}'
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


def silhouette_profile(gray: Any, line: float) -> Any:
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
    from app.ai.cad_views.section_material import ink_mask, symmetry_axis
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
    axis = symmetry_axis(horizontal)
    half = drop_fins(
        _median(_despike(_silhouette(ink, axis, line), int(2 * line)), int(2 * line) + 1),
        int(2.5 * line),
    )
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
        # Разрез узнаётся по штриховке, а не по роли: роль от прогона к
        # прогону плавает («Опора»: главный вид в разрезе назван видом, вид
        # с торца — разрезом). Пробуются оба профиля, берётся лучше
        # объяснённый надписями.
        variants = []
        material, axis = section_material(crop, line)
        if material.sum() > 0:
            by_material = profile_from_material(material, axis, line, ink=ink_mask(crop, line))
            if by_material is not None:
                variants.append((by_material, True))
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
                variants.append(
                    (
                        HalfProfile(
                            axis_y=by_silhouette.axis_y,
                            line_px=line,
                            x0=min(hatched.x0, by_silhouette.x0),
                            x1=max(hatched.x1, by_silhouette.x1),
                            outer=by_silhouette.outer,
                            inner=hatched.inner,
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
            if bound and longest > 1.15 * bound:
                tried[-1] += f", но профиль длиннее габарита ({longest:.1f} > {bound:g})"
                continue
            # Равное число объяснённых площадок — выигрывает профиль, у
            # которого они составляют большую долю: случайный масштаб
            # объясняет две площадки из многих (колесо p009 — 2 из 4 при
            # диаметре 92 вместо 78), верный — почти все.
            count = len(plateaus(profile.outer, 3 * profile.line_px)) + len(
                [p for p in plateaus(profile.inner, 3 * profile.line_px) if p[2] > 0]
            )
            key = (hits, round(hits / max(1, count), 2), hatched)
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
        features.extend(found)
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
        profile={"outer": outer, "bore": bore, "main_view": main.name or main.n, "role": main.role},
        features=features,
        scales={
            "radial_mm_per_px": radial / factor,
            "axial_mm_per_px": axial / factor,
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
    "переписывай цифра в цифру; обрезанную краем надпись не выписывай. Ответ — "
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


async def digitize_revolve(gray: Any, *, router: Any = None) -> tuple[ViewsResult, Any, list[str]]:
    """Лист → (результат, прочтение ролей, надписи)."""
    from app.ai.cad_views.sheet_reading import read_sheet

    reading = await read_sheet(gray, router=router)
    labels = await read_labels(gray, router=router)
    pictures = [r for r in reading.regions if r.role in ("view", "section")]
    region_labels = await read_region_labels(gray, pictures, router=router) if pictures else {}
    result = build_revolve(gray, reading, labels, region_labels=region_labels)
    seen = set(labels)
    merged = list(labels)
    for texts in region_labels.values():
        for text in texts:
            if text not in seen:
                seen.add(text)
                merged.append(text)
    return result, reading, merged


__all__ = [
    "ViewsResult",
    "build_revolve",
    "digitize_revolve",
    "read_labels",
    "read_region_labels",
]
