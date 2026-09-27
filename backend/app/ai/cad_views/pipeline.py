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
    """(вертикальная ли ось, сила симметрии) — по горизонтальным штрихам."""
    import cv2
    import numpy as np

    from app.ai.cad_views.section_material import ink_mask, symmetry_axis

    def strength(image: Any) -> float:
        ink = ink_mask(image, line)
        horizontal = cv2.morphologyEx(
            ink, cv2.MORPH_OPEN, np.ones((1, max(3, int(3 * line))), np.uint8)
        )
        axis = symmetry_axis(horizontal)
        half = min(axis, horizontal.shape[0] - axis)
        above = horizontal[axis - half : axis].astype(float)
        below = horizontal[axis : axis + half][::-1].astype(float)
        return float((above * below).sum()) / (float(above.sum() + below.sum()) + 1.0)

    straight = strength(gray)
    turned = strength(np.rot90(gray))
    return (turned > 1.2 * straight), max(straight, turned)


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
) -> ViewsResult:
    """Тело вращения по главному изображению и связанным видам листа."""
    import numpy as np

    from app.ai.cad_views.labels import parse_label
    from app.ai.cad_views.revolve_body import revolve_candidate, revolve_points
    from app.ai.cad_views.revolve_profile import fit_axial_scale, fit_scale, profile_from_material
    from app.ai.cad_views.section_material import ink_mask, section_material
    from app.ai.cad_views.view_features import side_view_features

    pictures = [r for r in reading.regions if r.role in ("view", "section")]
    if part:
        pictures = [r for r in pictures if (r.part or "") == part] or pictures
    main = next((r for r in pictures if r.n == reading.main), None) or (
        max(pictures, key=lambda r: (r.box[2] - r.box[0]) * (r.box[3] - r.box[1]))
        if pictures
        else None
    )
    if main is None:
        return ViewsResult(False, "на листе не найдено изображения детали")
    crop, factor, origin = prepare(gray, main.box)
    line = _line_px(crop)
    vertical, symmetry = _symmetric_orientation(crop, line)
    if symmetry < 0.2:
        return ViewsResult(
            False,
            f"главное изображение не симметрично оси (сила {symmetry:.2f}) — не тело вращения",
        )
    if vertical:
        crop = np.ascontiguousarray(np.rot90(crop))
    labels = [parse_label(t) for t in label_texts]
    diameters = [lab.value for lab in labels if lab.kind in ("diameter", "thread") and lab.value]
    holes = [
        lab.value
        for lab in labels
        if lab.kind == "diameter" and lab.surface == "hole" and lab.value
    ]
    shafts = [v for v in diameters if v not in holes]
    linear = [lab.value for lab in labels if lab.kind == "linear" and lab.value]
    notes: list[str] = []
    profile = None
    if main.role == "section":
        material, axis = section_material(crop, line)
        if material.sum() > 0:
            profile = profile_from_material(material, axis, line, ink=ink_mask(crop, line))
        else:
            notes.append("разрез без штриховки — профиль по силуэту")
    if profile is None:
        profile = silhouette_profile(crop, line)
    if profile is None:
        return ViewsResult(False, "профиль главного изображения не найден", notes=notes)
    radial, hits = fit_scale(profile, shafts, holes)
    if radial is None:
        return ViewsResult(False, "ни одна надпись Ø не объясняет площадки профиля", notes=notes)
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


async def digitize_revolve(gray: Any, *, router: Any = None) -> tuple[ViewsResult, Any, list[str]]:
    """Лист → (результат, прочтение ролей, надписи)."""
    from app.ai.cad_views.sheet_reading import read_sheet

    reading = await read_sheet(gray, router=router)
    labels = await read_labels(gray, router=router)
    return build_revolve(gray, reading, labels), reading, labels


__all__ = ["ViewsResult", "build_revolve", "digitize_revolve", "read_labels"]
