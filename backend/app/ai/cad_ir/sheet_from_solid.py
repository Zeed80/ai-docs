"""The sheet, drawn from the solid the sheet was read into.

This is the second half of the 3D-first redraw. The first half compiles what
was read into a real part; here that part is projected back onto paper — views,
sections, hatching and dimensions all measured off the model by TechDraw.

Why it matters that the drawing comes from the SOLID and not from the numbers:

* Two views cannot disagree. A left view is the same body seen from another
  direction, so the diameter it shows is the diameter the front view shows, by
  arithmetic rather than by a drafter keeping them in step.
* A dimension cannot disagree with what it labels. Its value is measured off
  the model, not restated from the reading, so a mismatch between the drawn
  geometry and its callout is not a class of bug that can exist here.
* A section is a real cut. The hatched region is the material the plane passes
  through, holes already excluded, instead of a shape assembled from the same
  stepped profile that drew the outline.

The reading still supplies everything that is NOT geometry — the stamp, the
technical requirements, and the exact text of a callout (``Ø80js6``, not "80").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from app.ai.cad_ir.schema import CadIR, SourceInfo

logger = structlog.get_logger(__name__)

# Paper-space resolution of the drafted sheet canvas, px per sheet millimetre.
PAPER_PX_PER_MM = 4.0
# Sheet formats to try, smallest first: a part should get the smallest sheet it
# reads well on, not the biggest one it fits on.
_FORMAT_LADDER = ("A4", "A3", "A2", "A1", "A0")
# Below this the drawing is too small to read even if it technically fits.
_MIN_USEFUL_RATIO = 1 / 5
_SEMANTIC_ANNOTATION_KINDS = frozenset({"roughness", "tolerance", "datum", "thread", "weld"})
_ANNOTATION_ROW_MM = 6.0


@dataclass
class SheetPlan:
    """What to ask the kernel for, and on what paper to put the answer."""

    part_class: str
    views: list[dict[str, Any]]
    sheet_format: str
    landscape: bool
    ratio: float
    scale_label: str
    layout_w_mm: float
    layout_h_mm: float
    # Views the kernel needed but the sheet must not show. A section has to cut
    # a base view, so on a hollow part the plain front view is requested and
    # then dropped: it is the same part its own section already draws.
    scaffold_views: set[int] = field(default_factory=set)
    # Views that stand to the RIGHT of the main view whatever their kind (the
    # thickness view of a plate is the kernel's `top`, which would go below).
    right_views: set[int] = field(default_factory=set)
    geometry_only: bool = True
    view_reasons: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SheetResult:
    ir: CadIR
    plan: SheetPlan
    drawing: dict[str, Any]
    verification: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _semantic_annotations(spec: dict) -> list[dict[str, Any]]:
    """Manufacturing symbols that belong on a geometry-only detail sheet."""
    return [
        item
        for item in (spec.get("annotations") or [])
        if isinstance(item, dict)
        and item.get("kind") in _SEMANTIC_ANNOTATION_KINDS
        and str(item.get("text") or "").strip()
    ]


def _semantic_annotations_height_mm(spec: dict) -> float:
    annotations = _semantic_annotations(spec)
    return len(annotations) * _ANNOTATION_ROW_MM + (4.0 if annotations else 0.0)


def classify_part(spec: dict, report: dict) -> str:
    """What kind of part this is, decided by DATA rather than by a label.

    The reader's ``type`` string is unreliable — a flange read perfectly as
    {circle, Ø560, thickness 20} has come back labelled "тело вращения" — so the
    class follows from what was actually read and what the solid measures.
    """
    from app.ai.cad_recognize.spec_vectorize import (
        _prismatic_profiles,
        _rotation_parts,
    )

    parts = _rotation_parts(spec)
    if parts:
        return "hollow_rotation" if parts[0].get("bore") else "solid_rotation"
    profiles = _prismatic_profiles(spec)
    if profiles:
        shape = str((profiles[0] or {}).get("shape") or "")
        return "flange" if shape == "circle" else "plate"
    bounds = report.get("bounds_mm") or {}
    length = float(bounds.get("z") or 0.0)
    diameter = max(float(bounds.get("x") or 0.0), float(bounds.get("y") or 0.0))
    if diameter > 0 and length > 0 and length / diameter < 0.5:
        return "flange"
    return "other"


def plan_views(part_class: str, spec: dict) -> list[dict[str, Any]]:
    """The views to ask for, in the order the sheet will carry them.

    A hollow turned part is SHOWN in section — that is its main view, not an
    extra. ``/drawing`` needs a base view before a section, so a front view is
    always requested first even when the sheet will not carry it.
    """
    source_views = [view for view in (spec.get("views") or []) if isinstance(view, dict)]
    source_sections = [view for view in source_views if view.get("kind") == "section"]
    views: list[dict[str, Any]] = [{"kind": "front"}]
    if part_class == "hollow_rotation":
        source = source_sections[0] if source_sections else {}
        section = {
            "kind": "section",
            "label": source.get("label") or "А-А",
            "section_symbol": (source.get("label") or "А").split("-")[0],
        }
        if source.get("section_origin_mm") is not None:
            section["section_origin_mm"] = source["section_origin_mm"]
        if source.get("section_path_mm"):
            section["section_path_mm"] = source["section_path_mm"]
        views.append(section)
    elif part_class in ("flange", "plate"):
        # Вид ВДОЛЬ оси выдавливания (`side`, вдоль −Z) — это деталь в плане:
        # контур, отверстия, окружность болтов, прорези. Он и есть главный вид
        # (ГОСТ 2.305: наиболее полное представление о форме). `front` ядра
        # смотрит на ребро и кладёт ширину ВЕРТИКАЛЬНО (u — толщина, v —
        # ширина): лист вёл пластину вертикальной полоской 25×100, и модель
        # чтения честно прочитала ширину 25 (базовая линия M5). `front` остаётся
        # основой для ядра, на лист он не идёт (см. `plan_sheet`).
        if part_class == "flange":
            # Разрез по оси — толщина и центральное отверстие; делит с планом
            # вертикальную ось (u — толщина, v — диаметр), встаёт справа.
            views.append({"kind": "section", "label": "А-А", "section_symbol": "А"})
            views.append({"kind": "side"})
        else:
            # Разрез пластины режет по направлению `front` — тот же перекос
            # осей — и по середине, мимо отверстий: штрихованная полоска без
            # сведений. Толщину показывает `top` (u — толщина, v — высота
            # плана): он делит ось с планом и встаёт справа.
            views.append({"kind": "side"})
            views.append({"kind": "top"})

    requested = {str(view.get("kind")) for view in source_views}
    # A view the reader saw on the source sheet is reproduced. "top" used to be
    # read, validated and then silently never drawn.
    for kind in ("side", "top", "section"):
        if kind in requested and not any(v["kind"] == kind for v in views):
            source = next(view for view in source_views if view.get("kind") == kind)
            planned = {"kind": kind}
            for field in ("label", "section_origin_mm", "section_path_mm"):
                if source.get(field) not in (None, []):
                    planned[field] = source[field]
            views.append(planned)
    for source in source_views:
        if source.get("kind") != "removed_section":
            continue
        planned = {
            "kind": "section",
            "presentation_kind": "removed_section",
            "label": source.get("label"),
            "section_symbol": (source.get("label") or "").split("-")[0] or None,
        }
        for field in ("section_origin_mm", "section_path_mm"):
            if source.get(field) not in (None, []):
                planned[field] = source[field]
        views.append(planned)
    for source in source_views:
        if source.get("kind") != "detail":
            continue
        centre = source.get("detail_center_mm")
        radius = source.get("detail_radius_mm")
        # A label without a model-space crop cannot be reconstructed from the
        # solid. Keep it absent so coverage stays red instead of magnifying an
        # arbitrary part of the projection.
        if not centre or not radius:
            continue
        views.append(
            {
                "kind": "detail",
                "label": source.get("label"),
                "detail_center_mm": centre,
                "detail_radius_mm": radius,
                "detail_scale_factor": source.get("detail_scale_factor") or 2.0,
            }
        )
    if part_class in ("solid_rotation", "hollow_rotation") and not any(
        v["kind"] == "side" for v in views
    ):
        # A turned part with cross features needs the end view to show them.
        body = spec.get("main_view") or {}
        if body.get("keyways") or body.get("cross_holes") or body.get("axial_holes"):
            views.append({"kind": "side"})
    if part_class == "solid_rotation":
        body = spec.get("main_view") or {}
        if body.get("keyways") or body.get("cross_holes"):
            # Главный вид — лицом к пазу (ГОСТ 2.305: наиболее полное
            # представление). С `front` паз под углом 0 — полоска по верхней
            # кромке, поперечные отверстия — щели в силуэте; положение и длину
            # паза лист не проставлял, и ридер читал пазы наугад (базовая
            # линия v2: 0/2). `bottom` смотрит на паз, отверстия — окружностями.
            # `front` остаётся основой для ядра и на лист не идёт.
            views.insert(1, {"kind": "bottom"})
    return views


def _view_reasons(views: list[dict[str, Any]], part_class: str, spec: dict) -> list[dict[str, Any]]:
    body = spec.get("main_view") or {}
    source_kinds = {
        str(view.get("kind")) for view in (spec.get("views") or []) if isinstance(view, dict)
    }
    reasons: list[dict[str, Any]] = []
    for index, view in enumerate(views):
        kind = view.get("presentation_kind") or view["kind"]
        reason = "основная проекция детали"
        if kind == "section":
            reason = (
                "показ внутреннего профиля"
                if body.get("bore")
                else "разрез прочитан на исходном листе"
            )
        elif kind == "side" and (
            body.get("keyways") or body.get("cross_holes") or body.get("axial_holes")
        ):
            reason = "показ радиальных, осевых отверстий и пазов"
        elif kind == "detail":
            reason = "увеличенный местный вид, прочитанный на исходном листе"
        elif kind in source_kinds:
            reason = "проекция присутствует на исходном листе"
        reasons.append(
            {
                "view_index": index,
                "kind": kind,
                "visible": True,
                "reason": reason,
            }
        )
    if part_class == "hollow_rotation":
        for item in reasons:
            if item["kind"] == "front":
                item["visible"] = False
                item["reason"] = "техническая основа для построения продольного разреза"
    elif part_class in ("flange", "plate"):
        for item in reasons:
            if item["kind"] == "front":
                item["visible"] = False
                item["reason"] = "техническая основа: главный вид плоской детали — план"
    elif part_class == "solid_rotation" and any(view["kind"] == "bottom" for view in views):
        for item in reasons:
            if item["kind"] == "front":
                item["visible"] = False
                item["reason"] = "техническая основа: главный вид вала — лицом к пазу"
            elif item["kind"] == "bottom":
                item["reason"] = "главный вид: паз и поперечные отверстия лицом к наблюдателю"
    return reasons


def verify_view_coverage(plan: SheetPlan, spec: dict) -> dict[str, Any]:
    """Do the planned visible views expose every modeled feature family?"""
    visible = [
        view.get("presentation_kind") or view["kind"]
        for index, view in enumerate(plan.views)
        if index not in plan.scaffold_views
    ]
    body = spec.get("main_view") or {}
    required: list[dict[str, str]] = []
    if body.get("bore"):
        required.append({"feature": "bore", "view": "section"})
    if body.get("keyways") or body.get("cross_holes") or body.get("axial_holes"):
        required.append({"feature": "radial_features", "view": "side"})
    for source_view in spec.get("views") or []:
        if not isinstance(source_view, dict):
            continue
        kind = str(source_view.get("kind") or "")
        if kind in {"side", "top", "section", "detail", "removed_section"}:
            required.append({"feature": f"source_view:{kind}", "view": kind})
    missing = [item for item in required if item["view"] not in visible]
    return {
        "ok": not missing,
        "visible_views": visible,
        "required": required,
        "missing": missing,
        "view_reasons": plan.view_reasons,
    }


def _estimate_layout_mm(part_class: str, report: dict, views: list[dict]) -> tuple[float, float]:
    """Roughly how much room the views need, before anything is drawn.

    The scale has to be known BEFORE ``/drawing`` is called — it returns geometry
    already multiplied by it — so the extent is estimated from the solid's own
    bounding box rather than measured from the views.
    """
    from app.ai.cad_projection import VIEW_GAP_MM

    bounds = report.get("bounds_mm") or {}
    length = float(bounds.get("z") or 0.0)
    diameter = max(float(bounds.get("x") or 0.0), float(bounds.get("y") or 0.0))
    kinds = [view.get("presentation_kind") or view["kind"] for view in views]

    if part_class in ("flange", "plate"):
        width, height = diameter, diameter
        # Справа от плана — разрез фланца или вид на толщину пластины (`top`).
        if any(kind in kinds for kind in ("section", "removed_section", "top")):
            width += VIEW_GAP_MM + max(length, 1.0)
    else:
        width, height = length, diameter
        if "side" in kinds:
            width += VIEW_GAP_MM + diameter
        if "removed_section" in kinds:
            width += VIEW_GAP_MM + length
        if "top" in kinds:
            height += VIEW_GAP_MM + diameter
    for view in views:
        if (view.get("presentation_kind") or view.get("kind")) != "detail":
            continue
        radius = float(view.get("detail_radius_mm") or 0.0)
        factor = float(view.get("detail_scale_factor") or 2.0)
        if radius > 0:
            width += VIEW_GAP_MM + 2.0 * radius * factor
    # Dimensions and their witness lines stand off the part; give them room, or
    # the sheet fits the geometry and clips everything that describes it.
    return width + 40.0, height + 40.0


def plan_sheet(
    spec: dict,
    report: dict,
    *,
    sheet_format: str | None = None,
    landscape: bool = True,
    geometry_only: bool = True,
) -> SheetPlan:
    """Pick the views, the paper and the ГОСТ 2.302 scale — in that order."""
    from app.ai.cad_recognize.spec_vectorize import (
        _read_scale_ratio,
        choose_standard_scale,
        technical_requirements_height_mm,
    )

    part_class = classify_part(spec, report)
    views = plan_views(part_class, spec)
    layout_w, layout_h = _estimate_layout_mm(part_class, report, views)
    notes_mm = (
        _semantic_annotations_height_mm(spec)
        if geometry_only
        else technical_requirements_height_mm(spec)
    )

    formats = [sheet_format.upper()] if sheet_format else list(_FORMAT_LADDER)
    chosen_format, ratio, label = formats[-1], 1.0, "1:1"
    for candidate in formats:
        ratio, label = choose_standard_scale(
            layout_w,
            layout_h,
            candidate,
            landscape=landscape,
            reserve_title_block=not geometry_only,
            reserve_notes_mm=notes_mm,
        )
        chosen_format = candidate
        # Stop at the first sheet the part reads well on rather than the first
        # it merely fits on: 1:10 on A4 is a technically valid, useless drawing.
        if ratio >= _MIN_USEFUL_RATIO:
            break

    # Reproducing a sheet means reproducing its scale where that is possible.
    read_scale = _read_scale_ratio(spec)
    if read_scale is not None:
        read_ratio, read_label = read_scale
        if read_ratio <= ratio * 1.0001:
            ratio, label = read_ratio, read_label
    # A longitudinal section IS the main view of a hollow turned part (ГОСТ
    # 2.305): showing the plain outline beside it draws the same body twice.
    scaffold: set[int] = set()
    right: set[int] = set()
    if part_class == "hollow_rotation" and any(v["kind"] == "section" for v in views):
        scaffold = {index for index, view in enumerate(views) if view["kind"] == "front"}
    elif part_class == "solid_rotation" and any(v["kind"] == "bottom" for v in views):
        # Вал с пазом: главный вид — `bottom`, лицом к пазу.
        scaffold = {index for index, view in enumerate(views) if view["kind"] == "front"}
    elif part_class in ("flange", "plate"):
        # Главный вид плоской детали — план; вид на ребро `front` нужен ядру
        # как основа, а на листе стоял бы с перекошенными осями.
        scaffold = {index for index, view in enumerate(views) if view["kind"] == "front"}
        # `top` пластины делит с планом вертикальную ось — его место справа.
        right = {index for index, view in enumerate(views) if view["kind"] == "top"}
    return SheetPlan(
        part_class=part_class,
        views=views,
        sheet_format=chosen_format,
        landscape=landscape,
        ratio=ratio,
        scale_label=label,
        layout_w_mm=layout_w,
        layout_h_mm=layout_h,
        scaffold_views=scaffold,
        right_views=right,
        geometry_only=geometry_only,
        view_reasons=_view_reasons(views, part_class, spec),
    )


def _dimension_requests(drawing: dict, spec: dict, plan: SheetPlan) -> list[dict[str, Any]]:
    """Which edges to dimension, chosen by matching the reading to the views.

    A dimension is placed only where a READ value lines up with an edge the
    model actually has: the point of taking dimensions from the kernel is that
    they measure the part, and the point of taking the SET of them from the
    reading is that the sheet says which sizes matter.

    The chain is deliberately left open (ГОСТ 2.307 forbids closing it): the
    longest step goes undimensioned and the overall length carries it.
    """
    from app.ai.cad_recognize.spec_vectorize import _rotation_parts

    requests: list[dict[str, Any]] = []
    views = drawing.get("views") or []
    if not views:
        return requests

    parts = _rotation_parts(spec)
    if not parts:
        return _prismatic_dimension_requests(views, spec, plan)
    outer = parts[0].get("outer") or []
    if not outer:
        return requests
    ratio = plan.ratio or 1.0

    # Diameters: a circular edge of the right radius, on whichever view shows
    # circles. On the front view of a shaft the same step reads as a vertical
    # line, and that is the other half of this. The BORE counts too — on a
    # hollow part it is the dimension the machinist works to, and leaving it off
    # made the section a picture of a cavity with no size.
    bore = parts[0].get("bore") or []
    wanted_diameters = sorted(
        {float(s["d"]) for s in outer if s.get("d")} | {float(s["d"]) for s in bore if s.get("d")}
    )
    wanted_lengths = _chain_lengths(outer)
    total_length = sum(float(s.get("l") or 0.0) for s in outer)

    for view_index, view in enumerate(views):
        # A dimension on a view the sheet does not carry is a dimension nobody
        # sees: the scaffold front view of a hollow part is dropped, and every
        # dimension placed on it went with it.
        if view_index in plan.scaffold_views:
            continue
        for item in view.get("visible") or []:
            index = item.get("edge_index")
            if index is None:
                continue
            if item.get("type") == "circle" and wanted_diameters:
                measured = 2.0 * float(item.get("radius") or 0.0) / ratio
                match = _closest(measured, wanted_diameters)
                if match is not None:
                    wanted_diameters.remove(match)
                    requests.append(
                        {
                            "view_index": view_index,
                            "edge_index": int(index),
                            "kind": "Diameter",
                            "label": "",
                            "_nominal_mm": match,
                            "_is_diameter": True,
                        }
                    )
        requests.extend(_step_length_requests(view, view_index, outer, wanted_lengths, ratio))
        # Запасной путь — длина ребра, как было. Он нужен виду, где ядро не дало
        # рёбер уступов; когда даёт, уступы находят длину надёжнее, а этот
        # подбирает только то, что осталось.
        requests.extend(_edge_length_requests(view, view_index, wanted_lengths, ratio))
        requests.extend(_diameter_requests(view, view_index, wanted_diameters, ratio))
        if total_length > 0 and not any(request.get("_is_overall") for request in requests):
            overall = _overall_length_request(view, view_index, total_length, ratio)
            if overall is not None:
                requests.append(overall)
    return requests


# Углы диаметральных линий для концентрических окружностей, по очереди.
_CONCENTRIC_ANGLES = (45.0, 135.0, 20.0, 160.0, 70.0, 110.0)


def _diameters_from_circles(drawing: dict, requests: list[dict], plan: SheetPlan) -> None:
    """Диаметр окружности, который TechDraw без GUI меряет как ноль.

    Размер типа ``Diameter`` на окружности вида headless-сборка TechDraw
    возвращает со значением 0 — и лист справедливо отбрасывал его как
    неизмеренный. У вала это не проявлялось: там диаметры меряются парой
    образующих. А у фланца пропадали ВСЕ диаметры — наружный, центрального
    отверстия, отверстий под болты.

    Значение от этого не становится выдуманным: окружность ядро уже измерило,
    проецируя вид, её радиус лежит в геометрии вида. Размер строится по ней —
    диаметральная линия через центр под 45°, значение 2r / масштаб листа.
    """
    import math

    dimensions = drawing.get("dimensions") or []
    ratio = plan.ratio or 1.0
    wanted = [
        request
        for request in requests
        if request.get("kind") == "Diameter" and request.get("_circle")
    ]
    for request in wanted:
        circle = request["_circle"]
        radius = float(circle.get("radius") or 0.0)
        if radius <= 0:
            continue
        # Тот же размер мог и измериться — тогда он уже стоит.
        present = any(
            item.get("view_index") == request["view_index"]
            and item.get("kind") == "Diameter"
            and isinstance(item.get("value_mm"), (int, float))
            and abs(float(item["value_mm"]) - 2.0 * radius / ratio) <= 0.05
            for item in dimensions
        )
        if present:
            continue
        cu, cv = (float(value) for value in circle.get("center") or (0.0, 0.0))
        # Концентрические окружности — под РАЗНЫМИ углами: под одним 45° их
        # подписи ложились в центр друг на друга («Ø2560» из Ø250 и Ø66).
        concentric = sum(
            1
            for item in dimensions
            if item.get("measured_by") == "view_circle"
            and item.get("view_index") == request["view_index"]
            and item.get("_centre") == [round(cu, 3), round(cv, 3)]
        )
        angle = math.radians(_CONCENTRIC_ANGLES[concentric % len(_CONCENTRIC_ANGLES)])
        dx, dy = radius * math.cos(angle), radius * math.sin(angle)
        # Убрать ноль, который вернул TechDraw за этот же запрос: нарисованный,
        # он был бы выносной линией с «0».
        for item in list(dimensions):
            if (
                item.get("view_index") == request["view_index"]
                and item.get("kind") == "Diameter"
                and not item.get("value_mm")
            ):
                dimensions.remove(item)
                break
        dimensions.append(
            {
                "view_index": request["view_index"],
                "kind": "Diameter",
                "label": "",
                "anchors_mm": [[cu - dx, cv - dy], [cu + dx, cv + dy]],
                "value_mm": round(2.0 * radius / ratio, 3),
                "measured_by": "view_circle",
                "_centre": [round(cu, 3), round(cv, 3)],
            }
        )
    drawing["dimensions"] = dimensions


def _prismatic_dimension_requests(
    views: list[dict], spec: dict, plan: SheetPlan
) -> list[dict[str, Any]]:
    """Размеры пластины и фланца: контур, толщина, диаметры отверстий.

    Раньше их не было НИ ОДНОГО: простановка выходила сразу, если деталь не
    тело вращения, и перечерченная пластина уходила пользователю картинкой без
    размеров. Здесь то, что меряется по рёбрам вида: диаметры — по окружностям,
    ширина и высота — между крайними рёбрами вида в плане, толщина — между
    крайними рёбрами вида ребром. Координаты отверстий и окружность болтов —
    не рёбра (центр отверстия и PCD ядро как ребро не назовёт), это следующий шаг.
    """
    from app.ai.cad_recognize.spec_vectorize import _prismatic_profiles

    profiles = _prismatic_profiles(spec)
    if not profiles:
        return []
    profile = profiles[0]
    ratio = plan.ratio or 1.0

    wanted_diameters: list[float] = []
    if profile.get("shape") == "circle" and profile.get("diameter_mm"):
        wanted_diameters.append(float(profile["diameter_mm"]))
    for hole in profile.get("holes") or []:
        if hole.get("diameter_mm"):
            wanted_diameters.append(float(hole["diameter_mm"]))
    for pattern in profile.get("hole_patterns") or []:
        if pattern.get("hole_diameter_mm"):
            wanted_diameters.append(float(pattern["hole_diameter_mm"]))
    wanted_diameters = sorted(set(wanted_diameters))

    # Один размер — один раз на ЛИСТ. Словарь заводился заново для каждого
    # вида, и толщина 16 вставала дважды, ширина 60 — трижды.
    extent = {
        "width": profile.get("width_mm") if profile.get("shape") == "rectangle" else None,
        "height": profile.get("height_mm") if profile.get("shape") == "rectangle" else None,
        "thickness": profile.get("thickness_mm"),
    }
    requests: list[dict[str, Any]] = []
    for view_index, view in enumerate(views):
        if view_index in plan.scaffold_views:
            continue
        for item in view.get("visible") or []:
            index = item.get("edge_index")
            if index is None or item.get("type") != "circle" or not wanted_diameters:
                continue
            match = _closest(2.0 * float(item.get("radius") or 0.0) / ratio, wanted_diameters)
            if match is None:
                continue
            wanted_diameters.remove(match)
            requests.append(
                {
                    "view_index": view_index,
                    "edge_index": int(index),
                    "kind": "Diameter",
                    "label": "",
                    "_nominal_mm": match,
                    "_is_diameter": True,
                    # Окружность уже измерена ядром при проекции вида — если
                    # TechDraw не измерит сам размер, строим его по ней.
                    "_circle": {
                        "center": list(item.get("center") or [0.0, 0.0]),
                        "radius": float(item.get("radius") or 0.0),
                    },
                }
            )
        spans = _extreme_edge_pairs(view)
        for axis, (span_mm, first, second) in spans.items():
            size = span_mm / ratio
            for name, value in extent.items():
                if not value or abs(size - float(value)) > max(0.05, float(value) * 0.01):
                    continue
                extent[name] = None  # один размер — один раз на лист
                requests.append(
                    {
                        "view_index": view_index,
                        "edge_index": first,
                        "second_edge_index": second,
                        "kind": "DistanceX" if axis == "u" else "DistanceY",
                        "label": "",
                        "_nominal_mm": float(value),
                        "_is_diameter": False,
                    }
                )
                break
    return requests


def _chain_lengths(outer: list[dict]) -> list[float]:
    """Step lengths the sheet must state: every link but ONE — the longest.

    ГОСТ 2.307 leaves exactly one link of a chain open under an overall size.
    This was a SET of the lengths minus every length equal to the longest, so
    repeats collapsed and ties were all dropped: a shaft with steps
    12/30/35/80/15/80/15 got 12, 30, 35 and one 15 — three links open instead
    of one. Measured on the verifier corpus with the metric corrected the same
    way: 13 of 30 shaft sheets complete, not the 30 the set-based metric said.
    """
    lengths = sorted(float(s["l"]) for s in outer if s.get("l"))
    return lengths[:-1]


def _extreme_edge_pairs(view: dict) -> dict[str, tuple[float, int, int]]:
    """Самые удалённые друг от друга рёбра вида по u и по v: габарит вида."""
    verticals: list[tuple[float, int]] = []
    horizontals: list[tuple[float, int]] = []
    for item in view.get("visible") or []:
        index = item.get("edge_index")
        points = item.get("points") or []
        if index is None or item.get("type") != "line" or len(points) != 2:
            continue
        (u1, v1), (u2, v2) = points
        if abs(u2 - u1) <= 1e-6:
            verticals.append((float(u1), int(index)))
        elif abs(v2 - v1) <= 1e-6:
            horizontals.append((float(v1), int(index)))
    pairs: dict[str, tuple[float, int, int]] = {}
    if len(verticals) >= 2:
        verticals.sort()
        pairs["u"] = (verticals[-1][0] - verticals[0][0], verticals[0][1], verticals[-1][1])
    if len(horizontals) >= 2:
        horizontals.sort()
        pairs["v"] = (horizontals[-1][0] - horizontals[0][0], horizontals[0][1], horizontals[-1][1])
    return pairs


def _step_length_requests(
    view: dict,
    view_index: int,
    outer: list[dict],
    wanted: list[float],
    ratio: float,
) -> list[dict[str, Any]]:
    """Each step's length as the distance between the SHOULDERS that bound it.

    Before this the length was found by matching a horizontal edge's length to
    the reading, 1 % tolerance. It fails on any real part: a chamfer shortens
    the end step's generatrix (15 mm reads 14.x), a groove splits it, a keyway
    cuts it — on a synthetic three-step shaft two of the three lengths went
    undimensioned. A length is a distance along the axis between two faces
    across it, so it is measured between the vertical edges standing at the
    step's two stations. Stations come from the reading, anchored at the left
    end face; an edge is accepted only within a hair of its station, so a
    groove wall next to the shoulder cannot stand in for it.
    """
    verticals: list[tuple[float, int]] = []
    for item in view.get("visible") or []:
        index = item.get("edge_index")
        points = item.get("points") or []
        if index is None or item.get("type") != "line" or len(points) != 2:
            continue
        (u1, _v1), (u2, _v2) = points
        if abs(u2 - u1) > 1e-6:
            continue
        verticals.append((float(u1), int(index)))
    if len(verticals) < 2 or not wanted:
        return []
    verticals.sort()
    left = verticals[0][0]
    tolerance = 0.2 * ratio  # 0.2 mm of the PART, whatever the sheet scale

    def edge_at(station_u: float) -> int | None:
        best = min(verticals, key=lambda item: abs(item[0] - station_u))
        return best[1] if abs(best[0] - station_u) <= tolerance else None

    requests: list[dict[str, Any]] = []
    position = 0.0
    for section in outer:
        length = float(section.get("l") or 0.0)
        start, end = position, position + length
        position = end
        match = _closest(length, wanted) if length > 0 else None
        if match is None:
            continue
        first = edge_at(left + start * ratio)
        second = edge_at(left + end * ratio)
        if first is None or second is None or first == second:
            continue
        wanted.remove(match)
        requests.append(
            {
                "view_index": view_index,
                "edge_index": first,
                "second_edge_index": second,
                "kind": "DistanceX",
                "label": "",
                "_nominal_mm": match,
                "_is_diameter": False,
            }
        )
    return requests


def _edge_length_requests(
    view: dict, view_index: int, wanted: list[float], ratio: float
) -> list[dict[str, Any]]:
    """Длина ступени как длина горизонтального ребра — прежний способ, запасной."""
    import math

    requests: list[dict[str, Any]] = []
    for item in view.get("visible") or []:
        index = item.get("edge_index")
        points = item.get("points") or []
        if index is None or item.get("type") != "line" or len(points) != 2 or not wanted:
            continue
        (u1, v1), (u2, v2) = points
        if abs(v2 - v1) >= abs(u2 - u1):
            continue  # вертикальное ребро — уступ, не длина
        match = _closest(math.hypot(u2 - u1, v2 - v1) / ratio, wanted)
        if match is None:
            continue
        wanted.remove(match)
        requests.append(
            {
                "view_index": view_index,
                "edge_index": int(index),
                "kind": "DistanceX",
                "label": "",
                "_nominal_mm": match,
                "_is_diameter": False,
            }
        )
    return requests


def _overall_length_request(
    view: dict, view_index: int, total_length: float, ratio: float
) -> dict[str, Any] | None:
    """The overall length, measured between the two end faces.

    No single edge is the part's length: the silhouette is broken into steps, so
    asking for a 364 mm edge finds nothing and the sheet comes out with every
    step dimensioned and no overall size — the one dimension a shop reads first.
    It is the distance between the END FACES, which are the extreme edges
    perpendicular to the axis.
    """
    verticals: list[tuple[float, int]] = []
    for item in view.get("visible") or []:
        index = item.get("edge_index")
        if index is None or item.get("type") != "line":
            continue
        points = item.get("points") or []
        if len(points) != 2:
            continue
        (u1, v1), (u2, v2) = points
        if abs(u2 - u1) > 1e-6:  # not perpendicular to the axis
            continue
        verticals.append((u1, int(index)))
    if len(verticals) < 2:
        return None
    verticals.sort()
    left, right = verticals[0], verticals[-1]
    if abs((right[0] - left[0]) / ratio - total_length) > max(0.05, total_length * 0.01):
        # The extreme faces do not span the length the reading states — say
        # nothing rather than label a span with a number it is not.
        return None
    return {
        "view_index": view_index,
        "edge_index": left[1],
        "second_edge_index": right[1],
        "kind": "DistanceX",
        "label": "",
        "_nominal_mm": total_length,
        "_is_diameter": False,
        "_is_overall": True,
    }


def _diameter_requests(
    view: dict, view_index: int, wanted: list[float], ratio: float
) -> list[dict[str, Any]]:
    """Diameters measured BETWEEN the two generatrices, as ГОСТ 2.307 draws them.

    On a longitudinal view the upper contour line of a step is a LENGTH, not a
    diameter — the diameter is the distance from it to its mirror image below
    the axis. One edge cannot say that, which is why these come in pairs. On a
    view seen down the axis the same step is a full circle and needs no pair.
    """
    lines: list[tuple[float, float, float, int]] = []  # (v, u_min, u_max, edge_index)
    for item in view.get("visible") or []:
        index = item.get("edge_index")
        if index is None or item.get("type") != "line":
            continue
        points = item.get("points") or []
        if len(points) != 2:
            continue
        (u1, v1), (u2, v2) = points
        if abs(v2 - v1) > 1e-6:  # not parallel to the axis
            continue
        lines.append((v1, min(u1, u2), max(u1, u2), int(index)))

    # Every line ABOVE the axis against every line below it — searching only
    # the tail of the list found a pair solely when the upper generatrix
    # happened to come first, which on a section it usually does not: two of a
    # shaft's three diameters went undimensioned for that reason alone.
    #
    # Mirrored about the axis and OVERLAPPING along it: the two generatrices of
    # one step. Requiring them to span the identical stretch left a step
    # undimensioned whenever something cut one side only — a keyway splits the
    # upper generatrix of its step in two, and Ø28 of a synthetic shaft went
    # missing for exactly that. The diameter is the distance between the lines;
    # how long each line happens to be is not part of it.
    candidates: list[tuple[float, float, int, int, float, float]] = []
    for v_a, u0_a, u1_a, edge_a in lines:
        if v_a <= 0:
            continue
        for v_b, u0_b, u1_b, edge_b in lines:
            if edge_b == edge_a or abs(v_a + v_b) > 1e-6:
                continue
            lo, hi = max(u0_a, u0_b), min(u1_a, u1_b)
            if hi - lo <= 1e-6:
                continue
            candidates.append((hi - lo, (v_a - v_b) / ratio, edge_a, edge_b, lo, hi))

    # Longest shared stretch first, over ALL pairs at once. Choosing per upper
    # edge let a short leftover piece next to the keyway claim the diameter
    # simply by coming first in the list, and the dimension went across the
    # keyway.
    requests: list[dict[str, Any]] = []
    used: set[int] = set()
    for _shared, diameter_mm, edge_a, edge_b, lo, hi in sorted(candidates, key=lambda c: -c[0]):
        if edge_a in used or edge_b in used:
            continue
        match = _closest(diameter_mm, wanted)
        if match is None:
            continue
        wanted.remove(match)
        used.update({edge_a, edge_b})
        requests.append(
            {
                "view_index": view_index,
                "edge_index": edge_a,
                "second_edge_index": edge_b,
                "kind": "DistanceY",
                "label": "",
                "_nominal_mm": match,
                "_is_diameter": True,
                # Где рисовать: середина участка, на котором ОБЕ образующие
                # есть. Привязки TechDraw — начальные вершины рёбер, и у
                # соседних ступеней это одна и та же точка на уступе: Ø25 и
                # Ø28 синтетического вала легли друг на друга ровно там.
                "_place_u": (lo + hi) / 2.0,
            }
        )
    return requests


def _closest(value: float, pool: list[float], *, tolerance: float = 0.01) -> float | None:
    """The pool entry this measurement is, or None if it is none of them."""
    best: float | None = None
    best_error = tolerance
    for candidate in pool:
        if candidate <= 0:
            continue
        error = abs(value - candidate) / candidate
        if error <= best_error:
            best, best_error = candidate, error
    return best


def _hole_dimensions(drawing: dict, plan: SheetPlan) -> None:
    """Где стоят отверстия плоской детали — координаты и окружность центров (X1).

    На листе пластины и фланца стояли только диаметры отверстий: где они,
    лист не говорил. Их центры и PCD — не рёбра, ядро их размером не назовёт,
    но окружности отверстий оно уже измерило, проецируя план: центр и радиус
    лежат в геометрии вида. Размеры строятся по ним, как диаметры в
    `_diameters_from_circles`:

    * три и больше одинаковых отверстий на одном радиусе от центра плана —
      окружность центров: её Ø (PCD), сама окружность штрихпунктиром;
    * остальные — координаты центра от левой и нижней кромки плана
      (ГОСТ 2.307, от баз), одинаковые значения — один раз;
    * к диаметру повторяющихся отверстий — «N отв.».

    Базовая линия M5: фланцы корпуса несли Ø отверстий, но не PCD и не их
    число, и прочитать массив с листа было нельзя.

    Фаза окружности болтов (угол первого отверстия) пока не проставляется —
    угловых размеров отрисовка не умеет.
    """
    import math

    if plan.part_class not in ("plate", "flange"):
        return
    dimensions = drawing.setdefault("dimensions", [])
    ratio = plan.ratio or 1.0
    near = 0.05 * ratio  # 0.05 mm of the part, on the sheet
    for index, view in enumerate(drawing.get("views") or []):
        if view.get("kind") != "side" or index in plan.scaffold_views:
            continue
        bounds = view.get("bounds_mm") or {}
        if not bounds:
            continue
        u_min, v_min = float(bounds["u_min"]), float(bounds["v_min"])
        v_max = float(bounds["v_max"])
        cu = (float(bounds["u_min"]) + float(bounds["u_max"])) / 2.0
        cv = (v_min + v_max) / 2.0
        circles = [
            (float(item["center"][0]), float(item["center"][1]), float(item["radius"]))
            for item in view.get("visible") or []
            if item.get("type") == "circle" and item.get("center") and item.get("radius")
        ]
        # Наружный контур фланца и центральное отверстие стоят в центре плана —
        # у них нет координат, только диаметр.
        if plan.part_class == "plate":
            _corner_radii(view, index, dimensions, bounds, ratio)
        holes = [c for c in circles if math.hypot(c[0] - cu, c[1] - cv) > near]
        if plan.part_class == "plate":
            # Центр прорези получает координаты как отверстие — радиус 0 его
            # отличает: ни «N отв.», ни окружности болтов у него нет.
            holes += _slots(view, index, dimensions, bounds, ratio)
        if not holes:
            continue

        groups: dict[tuple[float, float], list[tuple[float, float, float]]] = {}
        for hole in holes:
            if hole[2] <= 0:
                continue
            key = (round(math.hypot(hole[0] - cu, hole[1] - cv) / near), round(hole[2] / near))
            groups.setdefault(key, []).append(hole)
        on_pitch: list[tuple[float, float, float]] = []
        concentric = sum(
            1
            for item in dimensions
            if item.get("view_index") == index and item.get("measured_by") == "view_circle"
        )
        for members in groups.values():
            if not _is_bolt_circle(members, cu, cv, plan.part_class):
                continue
            on_pitch.extend(members)
            radius = math.hypot(members[0][0] - cu, members[0][1] - cv)
            angle = math.radians(_CONCENTRIC_ANGLES[concentric % len(_CONCENTRIC_ANGLES)])
            concentric += 1
            du, dv = radius * math.cos(angle), radius * math.sin(angle)
            value = round(2.0 * radius / ratio, 3)
            dimensions.append(
                {
                    "view_index": index,
                    "kind": "Diameter",
                    "label": f"Ø{value:g}",
                    "anchors_mm": [[cu - du, cv - dv], [cu + du, cv + dv]],
                    "value_mm": value,
                    "measured_by": "pitch_circle",
                    "pitch_circle": True,
                    "ir_kind": "diameter",
                    "_centre": [round(cu, 3), round(cv, 3)],
                }
            )

        xs: list[float] = []
        ys: list[float] = []
        for hole in holes:
            if hole in on_pitch:
                continue
            x = round((hole[0] - u_min) / ratio, 3)
            y = round((hole[1] - v_min) / ratio, 3)
            if not any(abs(x - other) <= 0.05 for other in xs):
                xs.append(x)
                dimensions.append(
                    {
                        "view_index": index,
                        "kind": "DistanceX",
                        "label": f"{x:g}",
                        "anchors_mm": [[u_min, v_max], [hole[0], hole[1]]],
                        "value_mm": x,
                        "measured_by": "hole_centre",
                        "ir_kind": "linear",
                    }
                )
            if not any(abs(y - other) <= 0.05 for other in ys):
                ys.append(y)
                dimensions.append(
                    {
                        "view_index": index,
                        "kind": "DistanceY",
                        "label": f"{y:g}",
                        "anchors_mm": [[u_min, v_min], [hole[0], hole[1]]],
                        "value_mm": y,
                        "measured_by": "hole_centre",
                        "ir_kind": "linear",
                        "place_u": u_min,
                        "outside": True,
                    }
                )
        if xs or ys:
            # Высота плана встаёт в те же ряды слева — самым внешним.
            for item in dimensions:
                if (
                    item.get("view_index") == index
                    and item.get("kind") == "DistanceY"
                    and not isinstance(item.get("place_u"), (int, float))
                ):
                    item["place_u"] = u_min
                    item["outside"] = True

        counts: dict[float, int] = {}
        for hole in holes:
            if hole[2] <= 0:
                continue
            diameter = round(2.0 * hole[2] / ratio, 3)
            counts[diameter] = counts.get(diameter, 0) + 1
        for item in dimensions:
            if item.get("view_index") != index or item.get("kind") != "Diameter":
                continue
            if item.get("pitch_circle"):
                continue
            value = item.get("value_mm")
            count = next(
                (
                    n
                    for d, n in counts.items()
                    if isinstance(value, (int, float)) and abs(d - value) <= 0.05
                ),
                0,
            )
            label = str(item.get("label") or f"Ø{value:g}")
            if count >= 2 and "отв." not in label:
                item["label"] = f"{count} отв. {label}"


def _slots(
    view: dict, index: int, dimensions: list[dict], bounds: dict, ratio: float
) -> list[tuple[float, float, float]]:
    """Прорезь пластины: межцентровое расстояние, «R» конца — и её центр.

    Прорезь стояла на листе без единого размера (корпус v4, plate-3). На плане
    ядро отдаёт её двумя отрезками и дугами концов — полуокружностью или
    двумя четвертями с общим центром. Два конца одного радиуса на одной
    горизонтали или вертикали — одна прорезь. Возвращаются центры прорезей:
    их координаты ставит общий путь отверстий.
    """
    import math

    u_min, u_max = float(bounds["u_min"]), float(bounds["u_max"])
    v_min, v_max = float(bounds["v_min"]), float(bounds["v_max"])
    ends: list[tuple[float, float, float]] = []
    for item in view.get("visible") or []:
        if item.get("type") != "arc" or not item.get("center") or not item.get("radius"):
            continue
        au, av = (float(value) for value in item["center"])
        radius = float(item["radius"])
        tolerance = 0.02 * max(radius, 1.0)
        at_corner = (
            min(abs(au - u_min), abs(au - u_max)) - radius <= tolerance
            and min(abs(av - v_min), abs(av - v_max)) - radius <= tolerance
        )
        if at_corner:
            continue  # скругление угла, см. `_corner_radii`
        if not any(
            math.hypot(au - e[0], av - e[1]) <= tolerance and abs(radius - e[2]) <= tolerance
            for e in ends
        ):
            ends.append((au, av, radius))

    centres: list[tuple[float, float, float]] = []
    used: set[int] = set()
    for i, first in enumerate(ends):
        if i in used:
            continue
        partners = []
        for j, second in enumerate(ends):
            if j <= i or j in used or abs(first[2] - second[2]) > 0.02 * max(first[2], 1.0):
                continue
            span = math.hypot(second[0] - first[0], second[1] - first[1])
            if span <= 0:
                continue
            if abs(first[1] - second[1]) <= 1e-3 * span or abs(first[0] - second[0]) <= 1e-3 * span:
                partners.append((span, j))
        if not partners:
            continue
        span, j = min(partners)
        used |= {i, j}
        low, high = sorted((first, ends[j]))
        radius = first[2]
        horizontal = abs(low[1] - high[1]) <= 1e-3 * span
        value = round(span / ratio, 3)
        distance = {
            "view_index": index,
            "kind": "DistanceX" if horizontal else "DistanceY",
            "label": f"{value:g}",
            "anchors_mm": [[low[0], low[1]], [high[0], high[1]]],
            "value_mm": value,
            "measured_by": "slot",
            "ir_kind": "linear",
        }
        if not horizontal:
            distance.update({"place_u": u_min, "outside": True})
        dimensions.append(distance)
        tip = (high[0] + radius, high[1]) if horizontal else (high[0], high[1] + radius)
        end_radius = round(radius / ratio, 3)
        dimensions.append(
            {
                "view_index": index,
                "kind": "Radius",
                "label": f"R{end_radius:g}",
                "anchors_mm": [[high[0], high[1]], [tip[0], tip[1]]],
                "value_mm": end_radius,
                "measured_by": "slot",
                "ir_kind": "radial",
            }
        )
        centres.append(((low[0] + high[0]) / 2.0, (low[1] + high[1]) / 2.0, 0.0))
    return centres


def _corner_radii(
    view: dict, index: int, dimensions: list[dict], bounds: dict, ratio: float
) -> None:
    """Радиус скругления углов пластины — одним размером «R…» на каждый радиус.

    Базовая линия M5 v2: ридер честно отвечал «радиус скругления углов не
    указан на чертеже» — лист его и не проставлял. Скругление угла — дуга в
    четверть окружности у угла плана; концы прорези — полуокружности внутри
    контура, их здесь не берём. Размер — от центра дуги в сторону угла.

    Угол — правый нижний: координаты отверстий идут от левой и нижней кромок,
    и в левом нижнем углу подпись «R5» терялась среди их выносных.
    """
    import math

    u_min, u_max = float(bounds["u_min"]), float(bounds["u_max"])
    v_min, v_max = float(bounds["v_min"]), float(bounds["v_max"])
    fillets: dict[float, tuple[float, float, float]] = {}
    for item in view.get("visible") or []:
        if item.get("type") != "arc" or not item.get("center") or not item.get("radius"):
            continue
        au, av = (float(value) for value in item["center"])
        radius = float(item["radius"])
        points = item.get("points") or []
        if len(points) < 2:
            continue
        sweep = abs(
            (
                math.degrees(math.atan2(points[1][1] - av, points[1][0] - au))
                - math.degrees(math.atan2(points[0][1] - av, points[0][0] - au))
                + 180.0
            )
            % 360.0
            - 180.0
        )
        tolerance = 0.02 * max(radius, 1.0)
        at_corner = (
            min(abs(au - u_min), abs(au - u_max)) - radius <= tolerance
            and min(abs(av - v_min), abs(av - v_max)) - radius <= tolerance
        )
        if abs(sweep - 90.0) > 5.0 or not at_corner:
            continue
        value = round(radius / ratio, 3)
        key = next((other for other in fillets if abs(other - value) <= 0.05), value)
        best = fillets.get(key)
        # Правее и ниже — лучше.
        if best is None or (au - av) > (best[0] - best[1]):
            fillets[key] = (au, av, radius)
    for value, (au, av, radius) in sorted(fillets.items()):
        du = math.copysign(1.0, au - (u_min + u_max) / 2.0)
        dv = math.copysign(1.0, av - (v_min + v_max) / 2.0)
        tip = (au + du * radius / math.sqrt(2.0), av + dv * radius / math.sqrt(2.0))
        dimensions.append(
            {
                "view_index": index,
                "kind": "Radius",
                "label": f"R{value:g}",
                "anchors_mm": [[au, av], [tip[0], tip[1]]],
                "value_mm": value,
                "measured_by": "view_arc",
                "ir_kind": "radial",
            }
        )


def _is_bolt_circle(
    members: list[tuple[float, float, float]], cu: float, cv: float, part_class: str
) -> bool:
    """Одинаковые отверстия на одном радиусе — окружность болтов, только у фланца.

    Прямоугольный массив по углам пластины тоже лежит на одном радиусе от
    центра, и лист ставил через него фиктивную окружность центров Ø97.529
    вместо координат (корпус v4: 11 пластин из 30 без координат угловых
    отверстий). Окружность болтов — это круглая деталь и равный угловой шаг.
    """
    import math

    if part_class != "flange" or len(members) < 3:
        return False
    angles = sorted(math.degrees(math.atan2(v - cv, u - cu)) % 360.0 for u, v, _r in members)
    step = 360.0 / len(angles)
    gaps = [(b - a) for a, b in zip(angles, angles[1:])] + [angles[0] + 360.0 - angles[-1]]
    return all(abs(gap - step) <= 0.5 for gap in gaps)


def _label_dimensions(dimensions: list[dict], requests: list[dict], spec: dict) -> None:
    """Give each measured dimension the text the sheet actually carries.

    ``Ø80js6`` and ``80`` are different instructions to the shop. The VALUE
    stays the kernel's measurement; only the text comes from the reading.

    Matching is by MEASUREMENT, never by position in the list. The kernel drops
    a dimension it could not place, so the answers are not parallel to the
    requests — pairing them by index would slide every label one place along
    and put a fit on the wrong feature, which is exactly the bug this pipeline
    already paid for once.
    """
    from app.ai.cad_recognize.spec_vectorize import _dimension_text, _read_dimension_index

    index = _read_dimension_index(spec)
    unclaimed = list(requests)
    for dimension in dimensions:
        measured = dimension.get("value_mm")
        if not isinstance(measured, (int, float)) or measured <= 0:
            continue
        match = None
        for request in unclaimed:
            nominal = request.get("_nominal_mm")
            if not nominal:
                continue
            if abs(float(measured) - float(nominal)) <= max(0.05, nominal * 0.005):
                match = request
                break
        if match is None:
            # The kernel measured something the reading does not claim. Its own
            # number stands; nothing is invented to label it.
            continue
        unclaimed.remove(match)
        is_diameter = bool(match.get("_is_diameter"))
        dimension["label"] = _dimension_text(
            index, float(match["_nominal_mm"]), diameter=is_diameter
        )
        # A diameter on a longitudinal view is MEASURED as a DistanceY between
        # the two generatrices, so only the request knows it is a diameter. Say
        # so, or it reaches the IR (and the DXF) as a plain distance and the
        # part appears to have no diameters at all.
        dimension["ir_kind"] = "diameter" if is_diameter else "linear"
        if isinstance(match.get("_place_u"), (int, float)):
            dimension["place_u"] = float(match["_place_u"])


async def build_sheet_from_solid(
    candidate: Any,
    spec: dict,
    report: dict,
    *,
    sheet_format: str | None = None,
    landscape: bool = True,
    geometry_only: bool = True,
) -> SheetResult | None:
    """Compile the sheet: views from the kernel, everything else from the read.

    Returns ``None`` when the kernel cannot draw the part (an older image, an
    unprojectable shape) so the caller can say so plainly rather than hand back
    a drawing of something else.
    """
    from app.ai.cad_projection import (
        verify_views_against_solid,
    )
    from app.services.cad_kernel import draw_candidate_sheet

    plan = plan_sheet(
        spec,
        report,
        sheet_format=sheet_format,
        landscape=landscape,
        geometry_only=geometry_only,
    )
    drawing = await draw_candidate_sheet(
        candidate, views=plan.views, scale=plan.ratio, hidden_lines=True
    )
    if not drawing or not (drawing.get("views") or []):
        return None

    warnings = list(drawing.get("warnings") or [])
    requests = _dimension_requests(drawing, spec, plan)
    if requests:
        # A second pass, because an edge can only be named once the view exists.
        dimensioned = await draw_candidate_sheet(
            candidate,
            views=plan.views,
            scale=plan.ratio,
            hidden_lines=True,
            dimensions=[
                {k: v for k, v in request.items() if not k.startswith("_")} for request in requests
            ],
        )
        if dimensioned and dimensioned.get("views"):
            drawing = dimensioned
            warnings = list(drawing.get("warnings") or [])
    _diameters_from_circles(drawing, requests, plan)
    # A dimension TechDraw could not measure comes back reading zero. Drawn, it
    # is a stray witness line with "0" on it — worse than the dimension being
    # absent, because a reader has to work out that it means nothing.
    measured = [
        item
        for item in (drawing.get("dimensions") or [])
        if isinstance(item.get("value_mm"), (int, float)) and item["value_mm"] > 0
    ]
    if len(measured) != len(drawing.get("dimensions") or []):
        warnings.append(
            f"размеров отброшено как неизмеренные: "
            f"{len(drawing.get('dimensions') or []) - len(measured)}"
        )
    drawing["dimensions"] = measured
    _label_dimensions(measured, requests, spec)
    _hole_dimensions(drawing, plan)

    ir, extent = _assemble(drawing, spec, plan)
    geometry_verification = verify_views_against_solid(
        {
            str(view.get("kind")): view
            for view in (drawing.get("views") or [])
            if view.get("bounds_mm")
        },
        report,
        part_class="flange" if plan.part_class in ("flange", "plate") else "rotation",
        # The views came back already multiplied by the sheet scale; the solid
        # is measured in real millimetres.
        scale=plan.ratio,
    )
    view_coverage = verify_view_coverage(plan, spec)
    verification = {
        **geometry_verification,
        "geometry_ok": bool(geometry_verification.get("ok")),
        "view_coverage": view_coverage,
        "ok": bool(geometry_verification.get("ok") and view_coverage["ok"]),
    }
    logger.info(
        "cad_sheet_from_solid",
        part_class=plan.part_class,
        views=[view["kind"] for view in plan.views],
        scale=plan.scale_label,
        sheet=plan.sheet_format,
        dimensions=len(drawing.get("dimensions") or []),
        extent_mm=[round(value, 1) for value in extent],
        verified=verification.get("ok"),
    )
    return SheetResult(
        ir=ir, plan=plan, drawing=drawing, verification=verification, warnings=warnings
    )


def _assemble(drawing: dict, spec: dict, plan: SheetPlan) -> tuple[CadIR, tuple[float, float]]:
    """Views and dimensions, with sheet furniture only when explicitly asked."""
    from app.ai.cad_projection import (
        dimensions_from_kernel,
        place_sheet_views,
        sheet_extent_mm,
    )
    from app.ai.cad_recognize.spec_vectorize import (
        _drawing_area_mm,
        _sheet_frame_entities,
        _sheet_info,
        technical_requirements_height_mm,
    )

    views = drawing.get("views") or []
    notes_mm = (
        _semantic_annotations_height_mm(spec)
        if plan.geometry_only
        else technical_requirements_height_mm(spec)
    )
    paper_w, paper_h, area_x0, area_y0, area_w, area_h = _drawing_area_mm(
        plan.sheet_format,
        plan.landscape,
        reserve_title_block=not plan.geometry_only,
        reserve_notes_mm=notes_mm,
    )

    # Lay the views out at the origin first, measure them, then centre.
    entities, placements = place_sheet_views(
        views, px_per_mm=PAPER_PX_PER_MM, skip=plan.scaffold_views, right=plan.right_views
    )
    extent_w, extent_h = sheet_extent_mm(views, placements)
    offset_u = area_x0 + max((area_w - extent_w) / 2.0, 0.0)
    offset_v = area_y0 + max((area_h - extent_h) / 2.0, 0.0)
    entities, placements = place_sheet_views(
        views,
        px_per_mm=PAPER_PX_PER_MM,
        origin_u_mm=offset_u,
        origin_v_mm=offset_v,
        skip=plan.scaffold_views,
        right=plan.right_views,
    )
    entities += dimensions_from_kernel(
        drawing.get("dimensions") or [],
        {
            # Границы вида едут вместе с размещением: без них длину некуда
            # вынести за контур, и она ложится внутрь детали.
            index: {**placement, "bounds_mm": (views[index] or {}).get("bounds_mm") or {}}
            for index, placement in enumerate(placements)
            if placement
        },
        list(range(len(views))),
        px_per_mm=PAPER_PX_PER_MM,
    )
    if plan.geometry_only:
        entities += _annotation_entities(
            spec,
            x_mm=area_x0 + 2.0,
            y_mm=area_y0 + area_h + 5.0,
        )
    if not plan.geometry_only:
        entities += _sheet_frame_entities(paper_w, paper_h, PAPER_PX_PER_MM, spec, plan.scale_label)

    ir = CadIR(
        source=SourceInfo(
            image_width=int(paper_w * PAPER_PX_PER_MM),
            image_height=int(paper_h * PAPER_PX_PER_MM),
            kind="spec",
        ),
        # Paper millimetres per pixel, times the scale the views were drawn at:
        # a pixel on this canvas is 1/4 mm of PAPER, which is ratio/4 mm of PART.
        scale=1.0 / (PAPER_PX_PER_MM * plan.ratio),
        scale_source="sheet_format",
        entities=entities,
        recognizer_used="spec-solid-sheet",
        digitization_status="review_required",
    )
    ir.sheet = _sheet_info(plan.sheet_format, spec, plan.scale_label)
    if plan.geometry_only:
        ir.sheet.frame = False
        ir.sheet.title_block = {}
    return ir, (extent_w, extent_h)


def _annotation_entities(spec: dict, *, x_mm: float, y_mm: float) -> list[Any]:
    """Place exact structured manufacturing symbols in their reserved band."""
    from app.ai.cad_ir.schema import AnnotationEntity, Point

    entities: list[Any] = []
    for index, item in enumerate(_semantic_annotations(spec)):
        text = str(item.get("text") or "").strip()
        kind = str(item["kind"])
        value = item.get("value")
        symbol = item.get("symbol")
        if value is None and kind in {"roughness", "thread", "weld"}:
            value = text
        if symbol is None and kind == "datum":
            symbol = text
        entities.append(
            AnnotationEntity(
                kind=kind,
                position=Point(
                    x=x_mm * PAPER_PX_PER_MM,
                    y=(y_mm + index * _ANNOTATION_ROW_MM) * PAPER_PX_PER_MM,
                ),
                text=text,
                value=str(value) if value is not None else None,
                symbol=str(symbol) if symbol is not None else None,
                datum_refs=[str(ref) for ref in (item.get("datum_refs") or [])],
                height=3.5 * PAPER_PX_PER_MM,
                evidence=[f"spec_annotation:{index}"],
                origin="spec",
                assurance="constraint_validated",
            )
        )
    return entities
