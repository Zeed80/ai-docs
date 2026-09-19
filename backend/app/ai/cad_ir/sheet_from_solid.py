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
from app.ai.cad_ir.weldment_sheet import (
    weldment_dimensions,
    weldment_entities,
    weldment_extra_height_mm,
)

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
    # Виды ПОД главным, когда по направлению они ушли бы вправо: вид спереди
    # корпуса стоит под планом в проекционной связи по ширине.
    below_views: set[int] = field(default_factory=set)
    # The view the others are laid out around, when the kind alone does not
    # say it: a hollow shaft's main view is its SECTION, and «first non-section
    # view» picked its end view (or the keyway view) instead.
    anchor_view: int | None = None
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

    if isinstance((spec.get("main_view") or {}).get("sheet_metal"), dict):
        return "sheet_metal"
    if (
        spec.get("welds")
        or len(
            [
                body
                for body in spec.get("parts") or []
                if isinstance(body, dict) and body.get("profile")
            ]
        )
        > 1
    ):
        return "weldment"
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


# Видов в одном запросе /drawing ядро принимает не больше.
_MAX_KERNEL_VIEWS = 8


def _wall_features(spec: dict) -> list[dict]:
    profile = ((spec.get("main_view") or {}).get("profile")) or {}
    return [item for item in (profile.get("wall_features") or []) if isinstance(item, dict)]


def _has_wall_features(spec: dict) -> bool:
    """Есть ли у детали карманы или приливы на гранях (корпус)."""
    return bool(_wall_features(spec))


def _has_cavity(spec: dict) -> bool:
    """Есть ли полость — карман на верхней или нижней грани."""
    return any(
        item.get("kind") == "pocket" and item.get("on_plane") in ("top", "bottom")
        for item in _wall_features(spec)
    )


def plan_views(part_class: str, spec: dict) -> list[dict[str, Any]]:
    """The views to ask for, in the order the sheet will carry them.

    A hollow turned part is SHOWN in section — that is its main view, not an
    extra. ``/drawing`` needs a base view before a section, so a front view is
    always requested first even when the sheet will not carry it.
    """
    source_views = [view for view in (spec.get("views") or []) if isinstance(view, dict)]
    source_sections = [view for view in source_views if view.get("kind") == "section"]
    views: list[dict[str, Any]] = [{"kind": "front"}]
    # Корпус (X2): у детали есть элементы на передних стенках — тогда вид
    # спереди идёт на лист, и ширина на нём должна лежать горизонтально
    # (по умолчанию ядро кладёт её вертикально — это годится только валу).
    if _has_wall_features(spec):
        # Корпус (X2/Ф5): вид спереди идёт на лист — на нём элементы передних
        # стенок; ширина на нём должна лежать горизонтально (по умолчанию ядро
        # кладёт её вертикально — это годится только валу). Полость же видна
        # только РАЗРЕЗОМ (ГОСТ 2.305): штриховыми линиями её глубину не
        # измерить ни человеку, ни проверке.
        views[0] = {"kind": "front", "x_direction": [1.0, 0.0, 0.0]}
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
    elif part_class in ("flange", "plate") and _has_cavity(spec):
        # Корпус с полостью: под планом — РАЗРЕЗ через середину детали
        # (ГОСТ 2.305), а не вид спереди: глубину полости показывает он.
        # План — главный вид; под ним разрез через середину, справа — вид
        # на толщину.
        views.append({"kind": "side"})
        views.append(
            {
                "kind": "section",
                "label": "А-А",
                "section_symbol": "А",
                # Ширина детали — горизонтально, как на её видах.
                "x_direction": [1.0, 0.0, 0.0],
            }
        )
        views.append({"kind": "top"})
        if any(item.get("on_plane") == "front" for item in _wall_features(spec)):
            # Разрез снимает переднюю стенку: её приливы и карманы без вида
            # спереди остаются без размеров (полнота корпусов 0,99 → 0,89) и
            # их нечем проверить. Вид спереди — под разрезом, по оси.
            views.append({"kind": "front", "x_direction": [1.0, 0.0, 0.0], "role": "front_wall"})
    elif part_class == "weldment":
        # Сварной узел (X3): главный вид спереди (ширина горизонтально), под
        # ним план, справа — вид слева: на нём профиль узла и швы.
        # Оси подобраны пробой ядра: вид спереди — u = −x, v = z (основание
        # внизу; с x_direction +X ядро кладёт узел вверх ногами), план —
        # наблюдатель на +Z с той же u, вид слева — u = y, v = z.
        views[0] = {"kind": "front", "x_direction": [-1.0, 0.0, 0.0]}
        views.append({"kind": "plan", "x_direction": [-1.0, 0.0, 0.0]})
        views.append({"kind": "top", "x_direction": [0.0, 1.0, 0.0]})
    elif part_class == "sheet_metal":
        # Гнутая деталь (X4): главный вид — вдоль ширины (`side`), на нём
        # сечение с полками и гибами; справа `top` — ширина. `front` — основа
        # для ядра, на лист не идёт.
        views.append({"kind": "side"})
        views.append({"kind": "top"})
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
    keyed = part_class in ("solid_rotation", "hollow_rotation") and bool(
        [k for k in (spec.get("main_view") or {}).get("keyways") or [] if isinstance(k, dict)]
    )
    for source in source_views:
        if source.get("kind") != "removed_section" or keyed:
            # У вала с пазами вынесенные сечения строятся поперёк оси по самим
            # пазам (ниже): прочитанное сечение резалось вдоль вида — не та
            # геометрия, выреза паза в нём нет.
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
    if part_class == "hollow_rotation":
        body = spec.get("main_view") or {}
        if (body.get("keyways") or body.get("cross_holes")) and not any(
            v["kind"] == "bottom" for v in views
        ):
            # Главный вид полого вала — разрез; паз и поперечные отверстия
            # лицом — отдельным видом `bottom` под ним (общая ось). Без него
            # элементы полого вала оставались без размеров (корпус: shaft-2,
            # shaft-7, shaft-10).
            views.append({"kind": "bottom"})
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
    if keyed:
        # X1b: ширина b и глубина t1 паза ставятся на вынесенном сечении через
        # паз (ГОСТ 2.307, ГОСТ 23360) — сечение поперёк оси на середине паза.
        used = {str(v.get("label") or "") for v in views}
        letters = [c for c in "БВГДЕЖИК" if f"{c}-{c}" not in used]
        body = spec.get("main_view") or {}
        for keyway, letter in zip(body.get("keyways") or [], letters, strict=False):
            if len(views) >= _MAX_KERNEL_VIEWS or not isinstance(keyway, dict):
                break
            start, length = keyway.get("axial_start_mm"), keyway.get("length_mm")
            if not isinstance(start, (int, float)) or not isinstance(length, (int, float)):
                continue
            views.append(
                {
                    "kind": "section",
                    "presentation_kind": "removed_section",
                    "section_normal": "axis",
                    "section_station_mm": _keyway_section_station(keyway),
                    "label": f"{letter}-{letter}",
                    "section_symbol": letter,
                }
            )
    return views


def _keyway_section_station(keyway: dict) -> float:
    """Станция сечения через паз — на трети его длины, а не на середине.

    На середине паза обычно и середина ступени, где стоит размерная линия Ø: след
    секущей плоскости ложился на неё (shaft-4, Ø25). Сечение обязано пройти по
    полной ширине паза — не ближе b/2 к скруглённому концу; короткий паз — по
    середине.
    """
    start, length = float(keyway["axial_start_mm"]), float(keyway["length_mm"])
    width = float(keyway.get("width_mm") or 0.0)
    offset = max(length / 3.0, width / 2.0 + 0.5)
    return round(start + min(offset, length / 2.0), 3)


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

    if part_class == "weldment":
        # Вид спереди (x × z), под ним план (x × y), справа вид слева (y × z),
        # сверху — полки швов, снизу — перечень позиций.
        size_x, size_y = float(bounds.get("x") or 0.0), float(bounds.get("y") or 0.0)
        size_z = length
        return (
            size_x + VIEW_GAP_MM + size_y + 40.0,
            size_z + VIEW_GAP_MM + size_y + 40.0 + 40.0,
        )
    if part_class == "sheet_metal":
        # Сечение (x × y) и справа вид на ширину (z × y).
        section_w, section_h = float(bounds.get("x") or 0.0), float(bounds.get("y") or 0.0)
        width = max(section_w + VIEW_GAP_MM + length, _flat_length_mm(report, bounds))
        # Под сечением — развёртка (длина × ширина) с надписью и размером.
        height = section_h + VIEW_GAP_MM + length + _FLAT_TITLE_MM
    elif part_class in ("flange", "plate"):
        width, height = diameter, diameter
        # Справа от плана — разрез фланца или вид на толщину пластины (`top`).
        if any(kind in kinds for kind in ("section", "removed_section", "top")):
            width += VIEW_GAP_MM + max(length, 1.0)
        # Под планом — вид спереди корпуса и/или его разрез (ширина × толщина),
        # каждый своей высотой: второй вид под разрезом не учитывался, и лист
        # корпуса выходил за формат — верхние размеры плана обрезались.
        stacked = sum(
            1 for view in views if view.get("role") == "front_wall" or view.get("kind") == "section"
        ) or int(any(view.get("kind") == "front" and view.get("x_direction") for view in views))
        height += stacked * (VIEW_GAP_MM + max(length, 1.0))
    else:
        width, height = length, diameter
        if "side" in kinds:
            width += VIEW_GAP_MM + diameter
        if "removed_section" in kinds:
            width += VIEW_GAP_MM + length
        if "top" in kinds:
            height += VIEW_GAP_MM + diameter
        if "bottom" in kinds and part_class == "hollow_rotation":
            # у полого вала `bottom` стоит под разрезом, у сплошного он сам главный
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
    below: set[int] = set()
    anchor: int | None = None
    if part_class == "hollow_rotation" and any(v["kind"] == "section" for v in views):
        scaffold = {index for index, view in enumerate(views) if view["kind"] == "front"}
        anchor = next(index for index, view in enumerate(views) if view["kind"] == "section")
    elif part_class == "solid_rotation" and any(v["kind"] == "bottom" for v in views):
        # Вал с пазом: главный вид — `bottom`, лицом к пазу.
        scaffold = {index for index, view in enumerate(views) if view["kind"] == "front"}
    elif part_class == "weldment":
        anchor = 0
        below = {index for index, view in enumerate(views) if view["kind"] == "plan"}
        right = {index for index, view in enumerate(views) if view["kind"] == "top"}
    elif part_class == "sheet_metal":
        scaffold = {index for index, view in enumerate(views) if view["kind"] == "front"}
        right = {index for index, view in enumerate(views) if view["kind"] == "top"}
    elif part_class in ("flange", "plate"):
        # Главный вид плоской детали — план; вид на ребро `front` нужен ядру
        # как основа, а на листе стоял бы с перекошенными осями — кроме
        # корпуса, где на передних стенках есть элементы и вид спереди
        # запрошен с горизонтальной шириной.
        scaffold = {
            index
            for index, view in enumerate(views)
            if view["kind"] == "front" and not view.get("x_direction")
        }
        # Корпус: план — главный вид, вид спереди или разрез — под ним.
        below = {
            index
            for index, view in enumerate(views)
            if (view["kind"] == "front" and view.get("x_direction"))
            or (view["kind"] == "section" and _has_cavity(spec))
        }
        if below:
            anchor = next(
                (index for index, view in enumerate(views) if view["kind"] == "side"), None
            )
            # Разрез строится от вида спереди — сам он на лист не идёт; вид
            # передней стенки (`role`) — идёт, под разрезом.
            scaffold |= (
                {
                    index
                    for index, view in enumerate(views)
                    if view["kind"] == "front" and view.get("role") != "front_wall"
                }
                if any(view["kind"] == "section" for view in views)
                else set()
            )
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
        below_views=below,
        anchor_view=anchor,
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

    if plan.part_class == "weldment":
        # Размеры узла ставит `weldment_dimensions` по всем телам; простановка
        # пластины мерила бы одно первое тело и дублировала ширину.
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
    placed: list[float] = []
    level_of = {edge: v for v, _u0, _u1, edge in lines}

    def v_a_of(edge: int) -> float:
        return level_of.get(edge, 0.0)

    for _shared, diameter_mm, edge_a, edge_b, lo, hi in sorted(candidates, key=lambda c: -c[0]):
        if edge_a in used or edge_b in used:
            continue
        match = _closest(diameter_mm, wanted)
        if match is None:
            continue
        wanted.remove(match)
        used.update({edge_a, edge_b})
        place_u = _separated_place(
            lo, hi, placed, blocked=_interior_spans(view, lo, hi, v_top=max(v_a_of(edge_a), 0.0))
        )
        placed.append(place_u)
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
                "_place_u": place_u,
            }
        )
    return requests


def _interior_spans(view: dict, lo: float, hi: float, *, v_top: float) -> list[tuple[float, float]]:
    """Участки ступени, занятые элементами внутри контура: паз лицом, отверстие.

    На главном виде `bottom` паз смотрит на наблюдателя и образующие ступени
    не разрывает — общий участок был всей ступенью, и линия Ø шла прямо через
    контур паза (план, Ф3). Занятое — u-проекции линий, дуг и окружностей,
    лежащих строго внутри ступени по высоте.
    """
    spans: list[tuple[float, float]] = []
    inner = 0.95 * v_top
    for item in (view.get("visible") or []) + (view.get("hidden") or []):
        kind = item.get("type")
        if kind in ("circle", "arc") and item.get("center") and item.get("radius"):
            cu, cv = (float(value) for value in item["center"])
            radius = float(item["radius"])
            if abs(cv) + radius < inner or abs(cv) < inner:
                u0, u1 = cu - radius, cu + radius
            else:
                continue
        elif kind == "line" and len(item.get("points") or []) == 2:
            (u0, v0), (u1, v1) = item["points"]
            if abs(v0 - v1) > 1e-6 or abs(v0) >= inner:
                continue
            u0, u1 = min(u0, u1), max(u0, u1)
            # Осевая и линии через весь вал — не элемент ступени.
            if u0 <= lo + 1e-6 and u1 >= hi - 1e-6:
                continue
        else:
            continue
        if u1 > lo and u0 < hi:
            spans.append((max(u0, lo), min(u1, hi)))
    return spans


def _separated_place(
    lo: float, hi: float, placed: list[float], blocked: list[tuple[float, float]] | None = None
) -> float:
    """Где встать диаметру на своём участке, не поверх соседнего диаметра.

    Середина общего участка образующих — хорошее место, пока участок у
    диаметра свой. У наружного диаметра и расточки одной ступени участок
    общий, и подписи «Ø35» и «Ø15» полого вала ложились одна на другую
    (корпус, shaft-2). Занятая середина — сдвиг на четверть участка туда, где
    дальше от соседей; с участка диаметр не уходит.
    """
    from app.ai.cad_projection import DIM_TEXT_MM

    gap = 2.5 * DIM_TEXT_MM
    options = [(lo + hi) / 2.0, lo + (hi - lo) * 0.25, lo + (hi - lo) * 0.75]
    spans = blocked or []
    if spans:
        # Свободные промежутки между занятыми участками — середины их, от
        # самого широкого; занятая середина ступени — не место для Ø.
        edges = sorted(spans)
        free: list[tuple[float, float]] = []
        cursor = lo
        for u0, u1 in edges:
            if u0 > cursor:
                free.append((cursor, u0))
            cursor = max(cursor, u1)
        if cursor < hi:
            free.append((cursor, hi))
        # Подпись Ø повёрнута и стоит СЛЕВА от своей линии на высоту текста
        # (ГОСТ 2.307): промежуток должен вместить и её. Считая подпись
        # симметричной, Ø первой ступени ставился в 2 мм от торца — «Ø20» ложился
        # на сам торец, и проверка профиля принимала глифы за торец: весь вид
        # уезжал (корпус v10, shaft-28: уступы 85,2 и 166,7 при 80 и 160).
        left = 1.2 * DIM_TEXT_MM
        right = 0.3 * DIM_TEXT_MM
        roomy = [(a, b) for a, b in free if b - a >= left + right]
        if roomy:
            options = [
                min(max((a + b) / 2.0, a + left), b - right)
                for a, b in sorted(roomy, key=lambda f: -(f[1] - f[0]))
            ]
    for option in options:
        if all(abs(option - other) >= gap for other in placed):
            return option
    return max(
        options,
        key=lambda option: min((abs(option - other) for other in placed), default=0.0),
    )


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


def _body_rect_mm(spec: dict, ratio: float) -> tuple[float, float] | None:
    """Прямоугольник тела в плане (в мм листа) — когда у детали есть элементы
    стенок: их выступы уводят крайние линии вида за габарит детали."""
    profile = ((spec.get("main_view") or {}).get("profile")) or {}
    if not profile.get("wall_features") or profile.get("shape") != "rectangle":
        return None
    width = float(profile.get("width_mm") or 0.0)
    height = float(profile.get("height_mm") or 0.0)
    if not (width and height):
        return None
    return width * ratio, height * ratio


def _wall_feature_dimensions(drawing: dict, spec: dict, plan: SheetPlan) -> None:
    """Карманы и приливы на гранях корпуса: размер, положение и глубина (X2).

    Лист корпуса нёс только габарит и крепёж: полость, приливы и карманы стенок
    стояли без единого размера (полнота 0,25). Числа из спека сюда класть
    нельзя — у видов ядра на каждой грани свой порядок осей и свой знак;
    поэтому элемент ищется в НАРИСОВАННОЙ геометрии своего вида (круг нужного
    радиуса, прямоугольник нужного размера), а координаты меряются от кромок
    тела на том же виде, как у отверстий плана.

    Глубина и вылет — на виде, где грань видна с ребра: прилив выходит за
    кромку тела, карман уходит внутрь штриховой линией.
    """
    profiles_source = (spec.get("main_view") or {}).get("profile") or {}
    walls = [
        item for item in (profiles_source.get("wall_features") or []) if isinstance(item, dict)
    ]
    if not walls or plan.part_class != "plate":
        return
    width = float(profiles_source.get("width_mm") or 0.0)
    height = float(profiles_source.get("height_mm") or 0.0)
    thickness = float(profiles_source.get("thickness_mm") or 0.0)
    if not (width and height and thickness):
        return
    ratio = plan.ratio or 1.0
    views = drawing.get("views") or []

    def view_index(kind: str, *, front_on_sheet: bool = False) -> int | None:
        for index, view in enumerate(views):
            if index in plan.scaffold_views or view.get("kind") != kind:
                continue
            if kind == "front" and front_on_sheet and index in plan.scaffold_views:
                continue
            return index
        return None

    plan_view = view_index("side")
    # У корпуса с полостью вид спереди заменён разрезом (ГОСТ 2.305): полость
    # и заднюю стенку показывает он; переднюю стенку разрез снимает — её
    # элементы на отдельном виде спереди под разрезом, если он есть.
    front_view = (
        view_index("section")
        if view_index("section") is not None
        else view_index("front", front_on_sheet=True)
    )
    front_wall_view = next(
        (
            index
            for index, view in enumerate(getattr(plan, "views", None) or [])
            if view.get("role") == "front_wall" and index not in plan.scaffold_views
        ),
        front_view,
    )
    side_view = view_index("top")
    # Грань → (вид лицом, размеры тела на нём, вид с ребра, ось глубины на нём).
    faces = {
        "top": (plan_view, (width, height), front_view, "v", (width, thickness)),
        "bottom": (plan_view, (width, height), front_view, "v", (width, thickness)),
        "front": (front_wall_view, (width, thickness), plan_view, "v", (width, height)),
        "back": (front_view, (width, thickness), plan_view, "v", (width, height)),
        "left": (side_view, (thickness, height), plan_view, "u", (width, height)),
        "right": (side_view, (thickness, height), plan_view, "u", (width, height)),
    }
    dimensions = drawing.setdefault("dimensions", [])
    _housing_overall(dimensions, plan_view, front_view, (width, height), thickness, ratio)
    # Один нарисованный элемент — одному элементу спека: два одинаковых прилива
    # на левой и правой стенке видны на одном виде, и без этого оба размера
    # вставали на одну и ту же окружность.
    taken: list[tuple[int, float, float]] = []
    for item in walls:
        face = faces.get(str(item.get("on_plane")))
        if face is None:
            continue
        face_index, body, edge_index, depth_axis, edge_body = face
        if face_index is None:
            continue
        frame = _body_frame(views[face_index], body, ratio)
        drawn = _wall_feature_shape(views[face_index], item, ratio, body, taken, face_index, frame)
        if drawn is not None:
            taken.append((face_index, drawn["u"], drawn["v"]))
            _wall_feature_size(dimensions, face_index, item, drawn, ratio)
            _wall_feature_position(dimensions, face_index, drawn, frame, body, ratio)
        if edge_index is not None:
            _wall_feature_depth(
                dimensions,
                views[edge_index],
                edge_index,
                item,
                ratio,
                depth_axis,
                _body_frame(views[edge_index], edge_body, ratio),
                _edge_body_extent(faces, item, width, height, thickness),
            )


def _edge_body_extent(
    faces: dict, item: dict, width: float, height: float, thickness: float
) -> float:
    """Размер тела вдоль оси глубины на виде с ребра."""
    plane = str(item.get("on_plane"))
    if plane in ("top", "bottom"):
        return thickness
    if plane in ("front", "back"):
        return height
    return width


def _body_frame(
    view: dict, body: tuple[float, float], ratio: float
) -> tuple[float, float, float, float] | None:
    """Кромки ТЕЛА на виде: (u_min, u_max, v_min, v_max), мм листа.

    Центр вида тело не задаёт: приливы выходят за габарит и смещают рамку вида
    (корпус G2: вид «top» шире тела на вылет переднего прилива, и координаты
    уезжали на 1–3 мм). Кромки ищутся как линии длиной в габарит тела.
    """
    want_u, want_v = body[0] * ratio, body[1] * ratio
    tolerance = 0.3 * ratio
    levels_v: list[float] = []
    levels_u: list[float] = []
    for entry in (view.get("visible") or []) + (view.get("hidden") or []):
        if entry.get("type") != "line" or len(entry.get("points") or []) != 2:
            continue
        (au, av), (bu, bv) = ((float(p[0]), float(p[1])) for p in entry["points"])
        if abs(av - bv) <= 1e-6 and abs(abs(bu - au) - want_u) <= tolerance:
            levels_v.append(av)
        elif abs(au - bu) <= 1e-6 and abs(abs(bv - av) - want_v) <= tolerance:
            levels_u.append(au)
    if not levels_u or not levels_v:
        return None
    u_min, u_max = min(levels_u), max(levels_u)
    v_min, v_max = min(levels_v), max(levels_v)
    if abs((u_max - u_min) - want_u) > tolerance or abs((v_max - v_min) - want_v) > tolerance:
        return None
    return u_min, u_max, v_min, v_max


def _housing_overall(
    dimensions: list[dict],
    plan_view: int | None,
    front_view: int | None,
    body: tuple[float, float],
    thickness: float,
    ratio: float,
) -> None:
    """Габарит корпуса — по кромкам ТЕЛА.

    Габаритный размер ставится между крайними рёбрами вида, а у корпуса за
    габарит выходят приливы: размер не признавался своим (116 против 100) и на
    листе не появлялся вовсе.
    """
    width, height = body
    half_u, half_v = width * ratio / 2.0, height * ratio / 2.0
    half_t = thickness * ratio / 2.0
    wanted = []
    if plan_view is not None:
        wanted += [
            (plan_view, "DistanceX", width, [[-half_u, half_v], [half_u, half_v]]),
            (plan_view, "DistanceY", height, [[-half_u, -half_v], [-half_u, half_v]]),
        ]
    if front_view is not None:
        wanted.append((front_view, "DistanceY", thickness, [[half_u, -half_t], [half_u, half_t]]))
    for view_index, kind, value, anchors in wanted:
        if any(
            isinstance(item.get("value_mm"), (int, float))
            and abs(float(item["value_mm"]) - value) <= 0.05
            and item.get("view_index") == view_index
            and item.get("kind") == kind
            for item in dimensions
        ):
            continue
        dimensions.append(
            {
                "view_index": view_index,
                "kind": kind,
                "label": f"{value:g}",
                "anchors_mm": anchors,
                "value_mm": round(value, 3),
                "measured_by": "housing_overall",
                "ir_kind": "linear",
            }
        )


def _wall_feature_shape(
    view: dict,
    item: dict,
    ratio: float,
    body: tuple[float, float],
    taken: list[tuple[int, float, float]] | None = None,
    view_index: int = -1,
    frame: tuple[float, float, float, float] | None = None,
) -> dict[str, Any] | None:
    """Элемент на своём виде: круг нужного радиуса или прямоугольник нужного размера."""
    tolerance = 0.3 * ratio
    used = taken or []

    def free(u: float, v: float) -> bool:
        if frame is not None and not (
            frame[0] - tolerance <= u <= frame[1] + tolerance
            and frame[2] - tolerance <= v <= frame[3] + tolerance
        ):
            return False  # элемент лежит на своей грани, а не за её кромкой
        return not any(
            index == view_index and abs(u - ou) <= tolerance and abs(v - ov) <= tolerance
            for index, ou, ov in used
        )

    if item.get("profile") == "circle":
        radius = float(item.get("diameter_mm") or 0.0) / 2.0 * ratio
        # Прилив на дальней стенке виден на своём виде штриховой окружностью:
        # при поиске только по видимым второй Ø25 корпуса терялся.
        for entry in (view.get("visible") or []) + (view.get("hidden") or []):
            if entry.get("type") != "circle" or not entry.get("center"):
                continue
            if abs(float(entry.get("radius") or 0.0) - radius) <= tolerance:
                cu, cv = (float(value) for value in entry["center"])
                if not free(cu, cv):
                    continue
                return {"kind": "circle", "u": cu, "v": cv, "radius": float(entry["radius"])}
        return None
    sizes = sorted(
        (float(item.get("width_mm") or 0.0) * ratio, float(item.get("height_mm") or 0.0) * ratio)
    )
    horizontals: list[tuple[float, float, float]] = []
    verticals: list[tuple[float, float, float]] = []
    for entry in (view.get("visible") or []) + (view.get("hidden") or []):
        if entry.get("type") != "line" or len(entry.get("points") or []) != 2:
            continue
        (au, av), (bu, bv) = ((float(p[0]), float(p[1])) for p in entry["points"])
        if abs(av - bv) <= 1e-6 and abs(bu - au) > 1e-6:
            horizontals.append((min(au, bu), max(au, bu), av))
        elif abs(au - bu) <= 1e-6 and abs(bv - av) > 1e-6:
            verticals.append((min(av, bv), max(av, bv), au))

    def has_side(column: float, low: float, high: float) -> bool:
        return any(
            abs(u - column) <= tolerance and v0 <= low + tolerance and v1 >= high - tolerance
            for v0, v1, u in verticals
        )

    for u0, u1, v_a in horizontals:
        for u2, u3, v_b in horizontals:
            if v_b <= v_a or abs(u0 - u2) > tolerance or abs(u1 - u3) > tolerance:
                continue
            pair = sorted((u1 - u0, v_b - v_a))
            if (
                abs(pair[0] - sizes[0]) <= tolerance
                and abs(pair[1] - sizes[1]) <= tolerance
                # Обе боковые стороны на месте: без этого верхняя линия полости
                # и линия чужого кармана складывались в «прямоугольник».
                and has_side(u0, v_a, v_b)
                and has_side(u1, v_a, v_b)
                and free((u0 + u1) / 2.0, (v_a + v_b) / 2.0)
            ):
                return {
                    "kind": "rectangle",
                    "u": (u0 + u1) / 2.0,
                    "v": (v_a + v_b) / 2.0,
                    "width": u1 - u0,
                    "height": v_b - v_a,
                }
    return None


def _wall_feature_size(
    dimensions: list[dict], view_index: int, item: dict, drawn: dict, ratio: float
) -> None:
    import math

    if drawn["kind"] == "circle":
        angle = math.radians(45.0)
        du, dv = drawn["radius"] * math.cos(angle), drawn["radius"] * math.sin(angle)
        dimensions.append(
            {
                "view_index": view_index,
                "kind": "Diameter",
                "label": "",
                "anchors_mm": [
                    [drawn["u"] - du, drawn["v"] - dv],
                    [drawn["u"] + du, drawn["v"] + dv],
                ],
                "value_mm": round(2.0 * drawn["radius"] / ratio, 3),
                "measured_by": "wall_feature",
                "_centre": [round(drawn["u"], 3), round(drawn["v"], 3)],
            }
        )
        return
    half_u, half_v = drawn["width"] / 2.0, drawn["height"] / 2.0
    for kind, value, anchors in (
        (
            "DistanceX",
            drawn["width"],
            [
                [drawn["u"] - half_u, drawn["v"] + half_v],
                [drawn["u"] + half_u, drawn["v"] + half_v],
            ],
        ),
        (
            "DistanceY",
            drawn["height"],
            [
                [drawn["u"] + half_u, drawn["v"] - half_v],
                [drawn["u"] + half_u, drawn["v"] + half_v],
            ],
        ),
    ):
        dimensions.append(
            {
                "view_index": view_index,
                "kind": kind,
                "label": f"{value / ratio:g}",
                "anchors_mm": anchors,
                "value_mm": round(value / ratio, 3),
                "measured_by": "wall_feature",
                "ir_kind": "linear",
            }
        )


def _wall_feature_position(
    dimensions: list[dict],
    view_index: int,
    drawn: dict,
    frame: tuple[float, float, float, float] | None,
    body: tuple[float, float],
    ratio: float,
) -> None:
    """Координаты центра от кромок тела на том же виде (ГОСТ 2.307, от баз)."""
    if frame is not None:
        left, _right, bottom, _top = frame
    else:
        left, bottom = -body[0] * ratio / 2.0, -body[1] * ratio / 2.0
    for kind, edge, centre, anchors in (
        ("DistanceX", left, drawn["u"], [[left, drawn["v"]], [drawn["u"], drawn["v"]]]),
        ("DistanceY", bottom, drawn["v"], [[drawn["u"], bottom], [drawn["u"], drawn["v"]]]),
    ):
        value = (centre - edge) / ratio
        if value <= 0.05:
            continue
        dimensions.append(
            {
                "view_index": view_index,
                "kind": kind,
                "label": f"{value:g}",
                "anchors_mm": anchors,
                "value_mm": round(value, 3),
                "measured_by": "wall_feature_position",
                "ir_kind": "linear",
            }
        )


def _wall_feature_depth(
    dimensions: list[dict],
    view: dict,
    view_index: int,
    item: dict,
    ratio: float,
    axis: str,
    frame: tuple[float, float, float, float] | None,
    body_extent: float,
) -> None:
    """Вылет прилива (за кромку тела) или глубина кармана (внутрь от кромки).

    Ищется линия вида, отстоящая от кромки тела ровно на глубину элемента:
    у прилива — снаружи, у кармана — внутри (штриховая линия дна).
    """
    depth = float(item.get("depth_mm") or 0.0) * ratio
    half = body_extent * ratio / 2.0
    if depth <= 0 or half <= 0:
        return
    # Кромки тела на этом виде: вид смещён выступающими приливами, и отсчёт от
    # центра вида промахивался (корпус G2: кромки плана −23,5 и +26,5).
    if frame is not None:
        low_edge, high_edge = (frame[0], frame[1]) if axis == "u" else (frame[2], frame[3])
    else:
        low_edge, high_edge = -half, half
    centre = (low_edge + high_edge) / 2.0
    outward = item.get("kind") == "boss"
    tolerance = 0.3 * ratio
    lines = []
    for entry in (view.get("visible") or []) + (view.get("hidden") or []):
        if entry.get("type") != "line" or len(entry.get("points") or []) != 2:
            continue
        (au, av), (bu, bv) = ((float(p[0]), float(p[1])) for p in entry["points"])
        if axis == "v" and abs(av - bv) <= 1e-6:
            lines.append((av, min(au, bu), max(au, bu)))
        elif axis == "u" and abs(au - bu) <= 1e-6:
            lines.append((au, min(av, bv), max(av, bv)))
    for level, low, high in lines:
        for edge in (low_edge, high_edge):
            outer = edge > centre
            wanted = edge + depth * (1.0 if outer == outward else -1.0)
            if abs(level - wanted) > tolerance:
                continue
            if outward and low_edge - tolerance <= level <= high_edge + tolerance:
                continue  # прилив выходит ЗА кромку
            if not outward and not (low_edge + tolerance < level < high_edge - tolerance):
                continue  # дно кармана лежит внутри тела
            middle = (low + high) / 2.0
            first = [middle, level] if axis == "v" else [level, middle]
            second = [middle, edge] if axis == "v" else [edge, middle]
            dimensions.append(
                {
                    "view_index": view_index,
                    "kind": "DistanceY" if axis == "v" else "DistanceX",
                    "label": f"{depth / ratio:g}",
                    "anchors_mm": [first, second],
                    "value_mm": round(depth / ratio, 3),
                    "measured_by": "wall_feature_depth",
                    "ir_kind": "linear",
                }
            )
            return


def _hole_dimensions(drawing: dict, plan: SheetPlan, spec: dict | None = None) -> None:
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
    body = _body_rect_mm(spec or {}, ratio)
    for index, view in enumerate(drawing.get("views") or []):
        if view.get("kind") != "side" or index in plan.scaffold_views:
            continue
        bounds = view.get("bounds_mm") or {}
        if not bounds:
            continue
        u_min, v_min = float(bounds["u_min"]), float(bounds["v_min"])
        v_max = float(bounds["v_max"])
        if body is not None:
            # Кромки ТЕЛА, а не крайние линии вида: у корпуса за габарит
            # выходят приливы стенок, и координаты отверстий мерились от них
            # (корпус G2: 15,5 вместо 7,5, габарит 100,5 вместо 100).
            frame = _body_frame(view, (body[0] / ratio, body[1] / ratio), ratio)
            if frame is not None:
                u_min, v_min, v_max = frame[0], frame[2], frame[3]
            else:
                u_min, v_min = -body[0] / 2.0, -body[1] / 2.0
                v_max = body[1] / 2.0
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


def _arc_interval(item: dict) -> tuple[float, float]:
    """Дуга вида как ``(начальный угол, разворот)`` против часовой, через её середину."""
    import math

    cu, cv = (float(value) for value in item["center"])

    def angle(point) -> float:
        return math.degrees(math.atan2(float(point[1]) - cv, float(point[0]) - cu)) % 360.0

    points = item.get("points") or []
    if len(points) < 2:
        return 0.0, 0.0
    first, last = angle(points[0]), angle(points[-1])
    forward = (last - first) % 360.0
    if item.get("mid"):
        through = (angle(item["mid"]) - first) % 360.0 <= forward
    else:
        through = forward <= 180.0
    return (first, forward) if through else (last, 360.0 - forward)


def _arc_sweep(item: dict) -> float:
    """Разворот дуги вида в градусах (по середине, если ядро её отдало)."""
    return _arc_interval(item)[1]


def _covered_degrees(intervals: list[tuple[float, float]]) -> float:
    """Сколько градусов окружности покрыто хотя бы одной дугой."""
    pieces: list[tuple[float, float]] = []
    for start, sweep in intervals:
        end = start + sweep
        if end <= 360.0:
            pieces.append((start, end))
        else:
            pieces.extend(((start, 360.0), (0.0, end - 360.0)))
    total, reach = 0.0, None
    for low, high in sorted(pieces):
        if reach is None or low > reach:
            total += high - low
            reach = high
        elif high > reach:
            total += high - reach
            reach = high
    return min(total, 360.0)


def _arc_groups(view: dict, bounds: dict) -> list[tuple[float, float, float, float]]:
    """Дуги вида, слитые по общему центру и радиусу: ``(u, v, r, суммарный разворот)``.

    Ядро отдаёт одну окружность или полуокружность несколькими дугами: конец
    паза — двумя четвертями, поперечное отверстие Ø4 — дугами 150,5° + 29,5° +
    180° (проба shaft-1, вид `bottom`). Суммарный разворот и отличает: 180° —
    конец паза или прорези, 360° — отверстие. Без этого два одинаковых
    поперечных отверстия на оси вала сошли бы за концы одного паза.
    Скругления углов плана (дуга у угла) — не здесь, см. `_corner_radii`.
    """
    import math

    u_min, u_max = float(bounds["u_min"]), float(bounds["u_max"])
    v_min, v_max = float(bounds["v_min"]), float(bounds["v_max"])
    groups: list[list[Any]] = []
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
            continue
        interval = _arc_interval(item)
        for group in groups:
            if (
                math.hypot(au - group[0], av - group[1]) <= tolerance
                and abs(radius - group[2]) <= tolerance
            ):
                group[3].append(interval)
                break
        else:
            groups.append([au, av, radius, [interval]])
    # Покрытие, а не сумма: ядро отдаёт часть дуг дважды — отверстие Ø4 на
    # shaft-18 пришло дугами 180 + 150,5 + 29,5 + 180 (сумма 540°), конец паза
    # на shaft-3 — 90 + 74,9 + 74,9, и сумма не узнавала ни то, ни другое.
    return [(g[0], g[1], g[2], _covered_degrees(g[3])) for g in groups]


def _capsules(
    groups: list[tuple[float, float, float, float]],
) -> list[tuple[tuple[float, float, float], tuple[float, float, float], float]]:
    """Пары концов (полуокружностей одного радиуса на одной прямой): ``(low, high, r)``."""
    import math

    ends = [(u, v, r) for u, v, r, sweep in groups if abs(sweep - 180.0) <= 30.0]
    pairs = []
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
        _span, j = min(partners)
        used |= {i, j}
        low, high = sorted((first, ends[j]))
        pairs.append((low, high, first[2]))
    return pairs


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

    u_min = float(bounds["u_min"])
    centres: list[tuple[float, float, float]] = []
    for low, high, radius in _capsules(_arc_groups(view, bounds)):
        span = math.hypot(high[0] - low[0], high[1] - low[1])
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
        if horizontal:
            # Под вид: над ним — координаты от левой кромки, и выносная
            # координаты центра прорези шла ровно через середину этого размера
            # (проверяльщик размерных линий мерил половину, корпус v5 plate-3).
            distance["below"] = True
        else:
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


def _shaft_feature_dimensions(drawing: dict, spec: dict, plan: SheetPlan) -> None:
    """Пазы и поперечные отверстия вала — на главном виде лицом к ним (Ф3.0b).

    Базовая линия v2: ридер читал пазы наугад (12..42 вместо 16,3..35,3), а
    поперечные отверстия не находил вовсе — лист не проставлял ни их
    положения, ни длины паза. На виде `bottom` паз — замкнутый контур, отверстие
    — окружность (см. `_arc_groups`), и размеры строятся по ним:

    * паз — длина и положение начала от уступа своей ступени;
    * поперечное отверстие — Ø и положение центра от того же уступа.

    Уступ — по станции спека (левый торец ступени), а не по ближайшей вертикали
    вида: у торцов стоят вертикали фасок (−133 и 132,5 на shaft-1), и база
    съехала бы на полмиллиметра. Размеры встают ПОД вид: над ним — цепочка, и
    короткие размеры элементов разбили бы её по рядам.
    """
    import math

    if plan.part_class not in ("solid_rotation", "hollow_rotation"):
        return
    outer = [s for s in ((spec.get("main_view") or {}).get("outer") or []) if isinstance(s, dict)]
    lengths = [float(s.get("length_mm") or 0.0) for s in outer]
    if not lengths or not all(lengths):
        return
    starts = [sum(lengths[:i]) for i in range(len(lengths))]
    dimensions = drawing.setdefault("dimensions", [])
    ratio = plan.ratio or 1.0
    for index, view in enumerate(drawing.get("views") or []):
        if view.get("kind") != "bottom" or index in plan.scaffold_views:
            continue
        bounds = view.get("bounds_mm") or {}
        if not bounds:
            continue
        u_min = float(bounds["u_min"])

        def base_u(u: float) -> float:
            station = max((s for s in starts if u_min + s * ratio <= u + 0.2 * ratio), default=0.0)
            return u_min + station * ratio

        def below(first: tuple, second: tuple, value: float, measured_by: str) -> dict:
            return {
                "view_index": index,
                "kind": "DistanceX",
                "label": f"{value:g}",
                "anchors_mm": [list(first), list(second)],
                "value_mm": value,
                "measured_by": measured_by,
                "ir_kind": "linear",
                "below": True,
            }

        groups = _arc_groups(view, bounds)
        for low, high, radius in _capsules(groups):
            if abs(low[1] - high[1]) > 1e-3 * max(abs(high[0] - low[0]), 1.0):
                continue  # паз вдоль оси; поперёк — не паз вала
            start, end = low[0] - radius, high[0] + radius
            edge_v = low[1] - radius
            length = round((end - start) / ratio, 3)
            dimensions.append(below((start, edge_v), (end, edge_v), length, "keyway"))
            base = base_u(start)
            offset = round((start - base) / ratio, 3)
            if offset > 0.05:
                dimensions.append(below((base, edge_v), (start, edge_v), offset, "keyway"))

        circles = [(u, v, r) for u, v, r, sweep in groups if abs(sweep - 360.0) <= 30.0] + [
            (float(i["center"][0]), float(i["center"][1]), float(i["radius"]))
            for i in view.get("visible") or []
            if i.get("type") == "circle" and i.get("center") and i.get("radius")
        ]
        for u, v, r in circles:
            value = round(2.0 * r / ratio, 3)
            angle = math.radians(45.0)
            du, dv = r * math.cos(angle), r * math.sin(angle)
            dimensions.append(
                {
                    "view_index": index,
                    "kind": "Diameter",
                    "label": f"Ø{value:g}",
                    "anchors_mm": [[u - du, v - dv], [u + du, v + dv]],
                    "value_mm": value,
                    "measured_by": "cross_hole",
                    "ir_kind": "diameter",
                }
            )
            base = base_u(u)
            offset = round((u - base) / ratio, 3)
            if offset > 0.05:
                dimensions.append(below((base, v), (u, v), offset, "cross_hole"))


def _turned_detail_dimensions(drawing: dict, spec: dict, plan: SheetPlan) -> None:
    """Канавки и фаски вала — на главном продольном виде (Ф3.0c).

    Базовая линия ридера: канавки 0/2, фаски 1/3 — лист их не образмеривал,
    и ридер угадывал по картинке. Канавка выхода инструмента стоит у уступа
    (одна её стенка — сам уступ), поэтому её ширина b однозначно задаёт место;
    глубина — Ø дна вертикальным размером по середине канавки. Фаска на торце —
    «c×45°» (ГОСТ 2.307) коротким размером от торца до линии фаски. Всё — ПОД
    видом, как у паза: над ним цепочка длин.
    """
    if plan.part_class not in ("solid_rotation", "hollow_rotation"):
        return
    body = spec.get("main_view") or {}
    grooves = [
        g for g in body.get("grooves") or [] if isinstance(g, dict) and not g.get("internal")
    ]
    chamfers = [
        c
        for c in body.get("chamfers") or []
        if isinstance(c, dict) and c.get("location") in ("left_end", "right_end")
    ]
    outer = [s for s in body.get("outer") or [] if isinstance(s, dict)]
    lengths = [float(s.get("length_mm") or 0.0) for s in outer]
    if not (grooves or chamfers) or not lengths or not all(lengths):
        return
    starts = [sum(lengths[:i]) for i in range(len(lengths))]
    ratio = plan.ratio or 1.0
    target = next(
        (
            (index, view)
            for index, view in enumerate(drawing.get("views") or [])
            if index not in plan.scaffold_views
            and view.get("kind") in ("bottom", "front", "section")
            and view.get("bounds_mm")
        ),
        None,
    )
    if target is None:
        return
    index, view = target
    bounds = view["bounds_mm"]
    u_min = float(bounds["u_min"])
    axis_v = (float(bounds["v_min"]) + float(bounds["v_max"])) / 2.0
    dimensions = drawing.setdefault("dimensions", [])

    def step_at(station: float) -> float:
        for start, length, step in zip(starts, lengths, outer, strict=False):
            if start - 1e-6 <= station <= start + length + 1e-6:
                return float(step.get("diameter_mm") or 0.0)
        return float(outer[-1].get("diameter_mm") or 0.0)

    def below(first: tuple, second: tuple, value: float, label: str, measured_by: str) -> dict:
        return {
            "view_index": index,
            "kind": "DistanceX",
            "label": label,
            "anchors_mm": [list(first), list(second)],
            "value_mm": value,
            "measured_by": measured_by,
            "ir_kind": "linear",
            "below": True,
        }

    for groove in grooves:
        width = float(groove.get("width_mm") or 0.0)
        centre = float(groove.get("axial_position_mm") or 0.0)
        if width <= 0:
            continue
        root = groove.get("root_diameter_mm")
        if not root and groove.get("depth_mm"):
            root = step_at(centre) - 2.0 * float(groove["depth_mm"])
        radius = (float(root) if root else step_at(centre)) / 2.0 * ratio
        left = u_min + (centre - width / 2.0) * ratio
        right = u_min + (centre + width / 2.0) * ratio
        edge_v = axis_v - radius
        # Глубина — в подписи «b×t», а не вертикальным Ø дна через вид: линия
        # Ø по канавке в 2–3 мм сливалась со стенками и дном, и проверка
        # профиля вала на корпусе v8 упала с 96 до 57 % (уступ у канавки
        # сместился на её ширину).
        depth = groove.get("depth_mm")
        if not depth and root:
            depth = (step_at(centre) - float(root)) / 2.0
        label = f"{width:g}×{float(depth):g}" if depth else f"{width:g}"
        dimensions.append(below((left, edge_v), (right, edge_v), round(width, 3), label, "groove"))

    total = sum(lengths)
    for chamfer in chamfers:
        size = float(chamfer.get("size_mm") or 0.0)
        if size <= 0:
            continue
        angle = float(chamfer.get("angle_deg") or 45.0)
        at_left = chamfer["location"] == "left_end"
        end_u = u_min + (0.0 if at_left else total) * ratio
        line_u = end_u + (size if at_left else -size) * ratio
        edge_v = axis_v - step_at(0.0 if at_left else total) / 2.0 * ratio
        first, second = sorted(((end_u, edge_v), (line_u, edge_v)))
        dimensions.append(below(first, second, round(size, 3), f"{size:g}×{angle:g}°", "chamfer"))


def _keyway_section_dimensions(drawing: dict, spec: dict, plan: SheetPlan) -> None:
    """Ширина b и глубина t1 паза — на его вынесенном сечении (X1b).

    Сечение поперёк оси (`section_normal=axis`) ядро строит из самого тела: ось —
    начало координат вида, u = x, v = y в масштабе листа. Паз под углом a
    вырезает контур в направлении (cos a, sin a): b — между кромками выреза у
    поверхности, t1 — от поверхности до дна по направлению паза.
    """
    import math

    body = spec.get("main_view") or {}
    keyways = [k for k in body.get("keyways") or [] if isinstance(k, dict)]
    outer = [s for s in body.get("outer") or [] if isinstance(s, dict)]
    if not keyways or not outer:
        return
    lengths = [float(s.get("length_mm") or 0.0) for s in outer]
    ratio = plan.ratio or 1.0
    dimensions = drawing.setdefault("dimensions", [])
    for index, view in enumerate(drawing.get("views") or []):
        station = view.get("section_station_mm")
        if station is None or not view.get("bounds_mm"):
            continue
        keyway = next(
            (
                k
                for k in keyways
                if isinstance(k.get("axial_start_mm"), (int, float))
                and isinstance(k.get("length_mm"), (int, float))
                and float(k["axial_start_mm"])
                <= float(station)
                <= float(k["axial_start_mm"]) + float(k["length_mm"])
            ),
            None,
        )
        if keyway is None:
            continue
        width, depth = keyway.get("width_mm"), keyway.get("depth_mm")
        position = 0.0
        diameter = None
        for step, length in zip(outer, lengths, strict=False):
            if position - 1e-6 <= float(station) <= position + length + 1e-6:
                diameter = float(step.get("diameter_mm") or 0.0)
                break
            position += length
        if not width or not depth or not diameter:
            continue
        radius = diameter / 2.0
        angle = math.radians(float(keyway.get("angle_deg") or 0.0))
        along = (math.cos(angle), math.sin(angle))
        across = (-math.sin(angle), math.cos(angle))
        # Кромки выреза на окружности: смещение b/2 поперёк паза.
        half = float(width) / 2.0
        reach = math.sqrt(max(radius**2 - half**2, 0.0))

        def point(a: float, c: float) -> list[float]:
            return [
                round((a * along[0] + c * across[0]) * ratio, 6),
                round((a * along[1] + c * across[1]) * ratio, 6),
            ]

        horizontal = abs(along[0]) >= abs(along[1])
        dimensions.append(
            {
                "view_index": index,
                "kind": "DistanceY" if horizontal else "DistanceX",
                "label": f"{float(width):g}",
                "anchors_mm": [point(reach, -half), point(reach, half)],
                "value_mm": round(float(width), 3),
                "measured_by": "keyway_section_width",
                "ir_kind": "linear",
            }
        )
        dimensions.append(
            {
                "view_index": index,
                "kind": "DistanceX" if horizontal else "DistanceY",
                "label": f"{float(depth):g}",
                "anchors_mm": [point(radius - float(depth), 0.0), point(radius, 0.0)],
                "value_mm": round(float(depth), 3),
                "measured_by": "keyway_section_depth",
                "ir_kind": "linear",
            }
        )


# Восемь поворотов и отражений осей: вид ядра кладёт оси эскиза по-своему.
_ORTHOGONAL = tuple(
    (a, b, c, d)
    for a, b, c, d in (
        (1, 0, 0, 1),
        (-1, 0, 0, 1),
        (1, 0, 0, -1),
        (-1, 0, 0, -1),
        (0, 1, 1, 0),
        (0, -1, 1, 0),
        (0, 1, -1, 0),
        (0, -1, -1, 0),
    )
)


def _sketch_to_view(view: dict, sketch: list[dict], ratio: float):
    """Перевод точки эскиза сечения в координаты вида — по совпадению вершин.

    Вид ядра кладёт оси эскиза как ему удобно (поворот, отражение); числа
    из эскиза нельзя класть на лист, пока не найдено, как именно. Проверяется
    восемь вариантов: вершины эскиза, переведённые с выравниванием рамок,
    обязаны лечь на концы отрезков вида.
    """
    ends: list[tuple[float, float]] = []
    for item in view.get("visible") or []:
        for point in item.get("points") or []:
            ends.append((float(point[0]), float(point[1])))
    if not ends:
        return None
    vertices = [(0.0, 0.0)] + [(float(seg["to"][0]), float(seg["to"][1])) for seg in sketch]
    view_u = min(u for u, _v in ends)
    view_v = min(v for _u, v in ends)
    tolerance = 0.05 * ratio + 0.02
    best = None
    for a, b, c, d in _ORTHOGONAL:
        mapped = [(ratio * (a * x + b * y), ratio * (c * x + d * y)) for x, y in vertices]
        shift_u = view_u - min(u for u, _v in mapped)
        shift_v = view_v - min(v for _u, v in mapped)
        hits = sum(
            1
            for u, v in mapped
            if any(
                abs(u + shift_u - eu) <= tolerance and abs(v + shift_v - ev) <= tolerance
                for eu, ev in ends
            )
        )
        if best is None or hits > best[0]:
            best = (hits, (a, b, c, d), shift_u, shift_v)
    if best is None or best[0] < 0.9 * len(vertices):
        return None
    _hits, (a, b, c, d), shift_u, shift_v = best

    def transform(x: float, y: float) -> tuple[float, float]:
        return ratio * (a * x + b * y) + shift_u, ratio * (c * x + d * y) + shift_v

    def rotate(x: float, y: float) -> tuple[float, float]:
        return a * x + b * y, c * x + d * y

    return transform, rotate


def _sheet_metal_dimensions(drawing: dict, spec: dict, plan: SheetPlan) -> None:
    """Размеры гнутой детали (X4): полки по наружной поверхности, s, R, ширина.

    Ядро не назовёт наружный размер полки ребром — он идёт от торца до
    наружной поверхности соседней полки через гиб. Размеры строятся по
    эскизу сечения, переведённому в координаты вида по совпадению вершин.
    """
    import math

    from app.ai.sheet_metal import bent_section, flange_spans

    if plan.part_class != "sheet_metal":
        return
    sheet = (spec.get("main_view") or {}).get("sheet_metal") or {}
    try:
        flanges = [float(value) for value in sheet["flanges_mm"]]
        turns = [int(value) for value in sheet["turns"]]
        radius = float(sheet["radius_mm"])
        thickness = float(sheet["thickness_mm"])
        width = float(sheet["width_mm"])
        angles = (
            [float(value) for value in sheet["bend_angles_deg"]]
            if sheet.get("bend_angles_deg")
            else None
        )
    except (KeyError, TypeError, ValueError):
        return
    views = drawing.get("views") or []
    ratio = plan.ratio or 1.0
    dimensions = drawing.setdefault("dimensions", [])
    profile_index = next((i for i, view in enumerate(plan.views) if view["kind"] == "side"), None)
    width_index = next((i for i, view in enumerate(plan.views) if view["kind"] == "top"), None)
    if profile_index is not None and profile_index < len(views):
        mapping = _sketch_to_view(
            views[profile_index], bent_section(flanges, turns, radius, thickness, angles), ratio
        )
        if mapping is not None:
            transform, rotate = mapping
            for span in flange_spans(flanges, turns, radius, thickness, angles):
                first = transform(*span["start"])
                second = transform(*span["end"])
                out_u, out_v = rotate(*span["outward"])
                horizontal = abs(first[1] - second[1]) < abs(first[0] - second[0])
                aligned = span["axis"] == "aligned"
                dimensions.append(
                    {
                        "view_index": profile_index,
                        "kind": "Distance"
                        if aligned
                        else "DistanceX"
                        if horizontal
                        else "DistanceY",
                        "label": f"{span['value']:g}",
                        "anchors_mm": [list(first), list(second)],
                        "value_mm": round(span["value"], 3),
                        "measured_by": "sheet_metal_flange",
                        "ir_kind": "linear",
                        # Наружу от полки: под видом или справа от него.
                        "below": (out_v < 0) if horizontal else (out_u > 0),
                    }
                )
            # Угол между полками у гиба не на 90° — хордой между полками с
            # подписью угла (ГОСТ 2.307 ставит его дугой; отрисовка дуг размеров
            # пока не умеет, а число на листе нужно и человеку, и ридеру).
            for index, angle in enumerate(angles or []):
                if abs(angle - 90.0) < 1e-6:
                    continue
                spans = flange_spans(flanges, turns, radius, thickness, angles)
                corner = spans[index]["end"]
                reach = 0.6 * min(spans[index]["value"], spans[index + 1]["value"])
                before = spans[index]["start"]
                after = spans[index + 1]["end"]

                def toward(target, origin=corner, reach=reach):
                    du, dv = target[0] - origin[0], target[1] - origin[1]
                    norm = math.hypot(du, dv) or 1.0
                    return (origin[0] + du / norm * reach, origin[1] + dv / norm * reach)

                dimensions.append(
                    {
                        "view_index": profile_index,
                        "kind": "Angle",
                        "label": f"{180.0 - angle:g}°",
                        "anchors_mm": [
                            list(transform(*toward(before))),
                            list(transform(*toward(after))),
                        ],
                        "value_mm": round(180.0 - angle, 3),
                        "measured_by": "sheet_metal_angle",
                        "ir_kind": "angular",
                    }
                )
            # Толщина — поперёк свободного торца первой полки.
            start = transform(0.0, 0.0)
            end = transform(0.0, -thickness)
            vertical = abs(start[0] - end[0]) < abs(start[1] - end[1])
            dimensions.append(
                {
                    "view_index": profile_index,
                    "kind": "DistanceY" if vertical else "DistanceX",
                    "label": f"s{thickness:g}",
                    "anchors_mm": [list(start), list(end)],
                    "value_mm": round(thickness, 3),
                    "measured_by": "sheet_metal_thickness",
                    "ir_kind": "linear",
                }
            )
            # Внутренний радиус гиба — один размер на все гибы (он у них общий).
            if turns:
                for item in views[profile_index].get("visible") or []:
                    if item.get("type") != "arc" or not item.get("center"):
                        continue
                    if abs(float(item.get("radius") or 0.0) - radius * ratio) > 0.02 * ratio:
                        continue
                    cu, cv = (float(value) for value in item["center"])
                    points = item.get("points") or []
                    if len(points) < 2:
                        continue
                    mid_u = (float(points[0][0]) + float(points[-1][0])) / 2.0 - cu
                    mid_v = (float(points[0][1]) + float(points[-1][1])) / 2.0 - cv
                    norm = math.hypot(mid_u, mid_v) or 1.0
                    tip = (
                        cu + mid_u / norm * radius * ratio,
                        cv + mid_v / norm * radius * ratio,
                    )
                    dimensions.append(
                        {
                            "view_index": profile_index,
                            "kind": "Radius",
                            "label": f"R{radius:g}",
                            "anchors_mm": [[cu, cv], [tip[0], tip[1]]],
                            "value_mm": round(radius, 3),
                            "measured_by": "sheet_metal_radius",
                            "ir_kind": "radial",
                        }
                    )
                    break
    if width_index is not None and width_index < len(views):
        bounds = views[width_index].get("bounds_mm") or {}
        if bounds and abs((bounds["u_max"] - bounds["u_min"]) - width * ratio) <= 0.05 * ratio:
            dimensions.append(
                {
                    "view_index": width_index,
                    "kind": "DistanceX",
                    "label": f"{width:g}",
                    "anchors_mm": [
                        [bounds["u_min"], bounds["v_max"]],
                        [bounds["u_max"], bounds["v_max"]],
                    ],
                    "value_mm": round(width, 3),
                    "measured_by": "sheet_metal_width",
                    "ir_kind": "linear",
                }
            )
        elif bounds and abs((bounds["v_max"] - bounds["v_min"]) - width * ratio) <= 0.05 * ratio:
            dimensions.append(
                {
                    "view_index": width_index,
                    "kind": "DistanceY",
                    "label": f"{width:g}",
                    "anchors_mm": [
                        [bounds["u_max"], bounds["v_min"]],
                        [bounds["u_max"], bounds["v_max"]],
                    ],
                    "value_mm": round(width, 3),
                    "measured_by": "sheet_metal_width",
                    "ir_kind": "linear",
                    "below": True,
                }
            )


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


def _kernel_views(views: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Запрос видов ядру — без служебных меток листа (ядро их не принимает)."""
    return [{key: value for key, value in view.items() if key != "role"} for view in views]


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
        candidate, views=_kernel_views(plan.views), scale=plan.ratio, hidden_lines=True
    )
    if not drawing or not (drawing.get("views") or []):
        return None

    warnings = list(drawing.get("warnings") or [])
    requests = _dimension_requests(drawing, spec, plan)
    if requests:
        # A second pass, because an edge can only be named once the view exists.
        dimensioned = await draw_candidate_sheet(
            candidate,
            views=_kernel_views(plan.views),
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
    _hole_dimensions(drawing, plan, spec)
    _wall_feature_dimensions(drawing, spec, plan)
    _shaft_feature_dimensions(drawing, spec, plan)
    _turned_detail_dimensions(drawing, spec, plan)
    _keyway_section_dimensions(drawing, spec, plan)
    _sheet_metal_dimensions(drawing, spec, plan)
    weldment_dimensions(drawing, spec, plan)

    ir, extent = _assemble(drawing, spec, plan)
    geometry_verification = verify_views_against_solid(
        {
            str(view.get("kind")): view
            for view in (drawing.get("views") or [])
            if view.get("bounds_mm")
        },
        report,
        part_class=(
            "flange"
            if plan.part_class in ("flange", "plate")
            else plan.part_class
            if plan.part_class in ("sheet_metal", "weldment")
            else "rotation"
        ),
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


def _body_axes(views: list[dict], spec: dict, plan: SheetPlan) -> dict[int, float]:
    """Середина тела по u на плане корпуса и видах под ним — по кромкам тела.

    Ядро центрует вид по его рамке; приливы, видные на одном виде и не видные
    на другом, сдвигают рамки по-разному, и выравнивание по рамкам ломало
    проекционную связь (разрез корпуса на 5 мм левее плана, живой замер
    элементов стенок уезжал на столько же).
    """
    if plan.part_class not in ("flange", "plate") or not _has_wall_features(spec):
        return {}
    profile = ((spec.get("main_view") or {}).get("profile")) or {}
    width, height = profile.get("width_mm"), profile.get("height_mm")
    thickness = profile.get("thickness_mm")
    if not all(isinstance(v, (int, float)) for v in (width, height, thickness)):
        return {}
    ratio = plan.ratio or 1.0
    wanted = {}
    if plan.anchor_view is not None:
        wanted[plan.anchor_view] = (float(width), float(height))
    for index in plan.below_views:
        wanted[index] = (float(width), float(thickness))
    axes: dict[int, float] = {}
    for index, body in wanted.items():
        if index >= len(views):
            continue
        frame = _body_frame(views[index] or {}, body, ratio)
        if frame is not None:
            axes[index] = (frame[0] + frame[1]) / 2.0
    # Выравнивать можно, только если найдено у главного и у вида под ним.
    if plan.anchor_view not in axes:
        return {}
    return axes


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

    axis_u = _body_axes(views, spec, plan)
    # Lay the views out at the origin first, measure them, then centre.
    entities, placements = place_sheet_views(
        views,
        px_per_mm=PAPER_PX_PER_MM,
        skip=plan.scaffold_views,
        right=plan.right_views,
        below=plan.below_views,
        anchor=plan.anchor_view,
        axis_u=axis_u,
    )
    extent_w, extent_h = sheet_extent_mm(views, placements)
    # Развёртка стоит под видами — она тоже занимает место на листе.
    extent_h += _flat_pattern_height_mm(spec, plan) + weldment_extra_height_mm(spec, plan)
    offset_u = area_x0 + max((area_w - extent_w) / 2.0, 0.0)
    offset_v = area_y0 + max((area_h - extent_h) / 2.0, 0.0)
    entities, placements = place_sheet_views(
        views,
        px_per_mm=PAPER_PX_PER_MM,
        origin_u_mm=offset_u,
        origin_v_mm=offset_v,
        skip=plan.scaffold_views,
        right=plan.right_views,
        below=plan.below_views,
        anchor=plan.anchor_view,
        axis_u=axis_u,
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
    entities += _view_label_entities(views, placements, occupied=entities)
    entities += _flat_pattern_entities(spec, plan, views, placements)
    entities += weldment_entities(spec, plan, views, placements)
    entities += _cutting_plane_entities(views, placements, plan)
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


_FLAT_TITLE_MM = 20.0


def _flat_pattern_height_mm(spec: dict, plan: SheetPlan) -> float:
    from app.ai.cad_projection import VIEW_GAP_MM

    if plan.part_class != "sheet_metal":
        return 0.0
    sheet = (spec.get("main_view") or {}).get("sheet_metal") or {}
    width = sheet.get("width_mm")
    if not isinstance(width, (int, float)):
        return 0.0
    # Зазор, надпись, сама развёртка и размер длины под ней.
    return VIEW_GAP_MM + _FLAT_TITLE_MM + float(width) * (plan.ratio or 1.0) + 15.0


def _flat_length_mm(report: dict, bounds: dict) -> float:
    """Грубая длина развёртки для раскладки: сумма сторон сечения."""
    return float(bounds.get("x") or 0.0) + 2.0 * float(bounds.get("y") or 0.0)


def _flat_pattern_entities(
    spec: dict, plan: SheetPlan, views: list[dict], placements: list[dict | None]
) -> list[Any]:
    """Развёртка гнутой детали (X4, ГОСТ 2.109): прямоугольник длина × ширина,
    линии гибов тонкой штрихпунктирной, длина развёртки размером.

    Длина — по нейтральному слою (R + K·s), как её считает `sheet_metal`:
    по ней заготовку и режут. Ядро развёртку не строит — она выводится из
    той же геометрии, что и тело (E14: развёртка ↔ 3D 0,0000 мм).
    """
    import math

    from app.ai.cad_ir.schema import Point, Segment, TextEntity
    from app.ai.cad_projection import _ORIGIN, DIM_TEXT_MM, VIEW_GAP_MM, dimensions_from_kernel
    from app.ai.sheet_metal import developed_length

    if plan.part_class != "sheet_metal":
        return []
    sheet = (spec.get("main_view") or {}).get("sheet_metal") or {}
    try:
        flanges = [float(value) for value in sheet["flanges_mm"]]
        turns = [int(value) for value in sheet["turns"]]
        radius = float(sheet["radius_mm"])
        thickness = float(sheet["thickness_mm"])
        width = float(sheet["width_mm"])
        k_factor = float(sheet.get("k_factor") or 0.5)
        angles = (
            [float(value) for value in sheet["bend_angles_deg"]]
            if sheet.get("bend_angles_deg")
            else [90.0] * len(turns)
        )
    except (KeyError, TypeError, ValueError):
        return []
    placed = [
        (placement, view.get("bounds_mm"))
        for view, placement in zip(views, placements, strict=False)
        if placement and isinstance(view.get("bounds_mm"), dict)
    ]
    main = next(
        (
            (placement, view.get("bounds_mm"))
            for index, (view, placement) in enumerate(zip(views, placements, strict=False))
            if placement and plan.views[index]["kind"] == "side" and view.get("bounds_mm")
        ),
        None,
    )
    if not placed or main is None:
        return []
    ratio = plan.ratio or 1.0
    length = developed_length(flanges, len(turns), radius, thickness, k_factor, angles)
    arcs = [math.radians(angle) * (radius + k_factor * thickness) for angle in angles]
    # Нижний край занятого места на листе (y бумаги растёт вниз).
    lowest = max(placement["offset_v"] - float(box["v_min"]) for placement, box in placed)
    left = main[0]["offset_u"] + float(main[1]["u_min"])
    top = lowest + VIEW_GAP_MM + _FLAT_TITLE_MM / 2.0
    run, rise = length * ratio, width * ratio

    def point(u: float, v: float) -> Point:
        # u вправо, v вверх от нижнего левого угла развёртки.
        return Point(x=(left + u) * PAPER_PX_PER_MM, y=(top + rise - v) * PAPER_PX_PER_MM)

    corners = [(0.0, 0.0), (run, 0.0), (run, rise), (0.0, rise)]
    entities: list[Any] = [
        Segment(
            p1=point(*corners[index]),
            p2=point(*corners[(index + 1) % 4]),
            line_class="contour",
            width_class="main",
            **_ORIGIN,
        )
        for index in range(4)
    ]
    # Линия гиба — середина дуги гиба на развёртке.
    for index in range(len(turns)):
        station = sum(flanges[: index + 1]) + sum(arcs[:index]) + arcs[index] / 2.0
        entities.append(
            Segment(
                p1=point(station * ratio, -2.0),
                p2=point(station * ratio, rise + 2.0),
                line_class="axis",
                width_class="thin",
                **_ORIGIN,
            )
        )
    entities.append(
        TextEntity(
            position=Point(
                x=(left + run / 2.0) * PAPER_PX_PER_MM,
                y=(top - _FLAT_TITLE_MM / 4.0) * PAPER_PX_PER_MM,
            ),
            text="Развёртка",
            height=DIM_TEXT_MM * 1.4 * PAPER_PX_PER_MM,
            rotation=0.0,
            anchor="middle",
            line_class="dim",
            width_class="thin",
            **_ORIGIN,
        )
    )
    value = round(length, 1)
    entities += dimensions_from_kernel(
        [
            {
                "view_index": 0,
                "kind": "DistanceX",
                "label": f"{value:g}",
                "anchors_mm": [[0.0, 0.0], [run, 0.0]],
                "value_mm": value,
                "measured_by": "sheet_metal_flat_length",
                "ir_kind": "linear",
                "below": True,
            }
        ],
        {
            0: {
                "offset_u": left,
                "offset_v": top + rise,
                "bounds_mm": {
                    "u_min": 0.0,
                    "u_max": run,
                    "v_min": 0.0,
                    "v_max": rise,
                },
            }
        },
        [0],
        px_per_mm=PAPER_PX_PER_MM,
    )
    return entities


def _view_label_entities(
    views: list[dict[str, Any]],
    placements: list[dict[str, float] | None],
    *,
    occupied: list[Any] | None = None,
) -> list[Any]:
    """Обозначение разреза и сечения над видом («Б-Б», ГОСТ 2.305).

    Надписей видов лист не выводил вовсе: вынесенные сечения через пазы (X1b) и
    главный разрез полого вала стояли без букв, и связать их с листом было
    нечем — ни человеку, ни ридеру.
    """
    from app.ai.cad_ir.schema import Point, TextEntity
    from app.ai.cad_projection import _ORIGIN, DIM_TEXT_MM

    entities: list[Any] = []
    for view, placement in zip(views, placements, strict=False):
        label = str((view or {}).get("label") or "").strip()
        box = (view or {}).get("bounds_mm")
        if (
            not label
            or not placement
            or not isinstance(box, dict)
            or (view.get("kind") not in ("section", "removed_section"))
        ):
            continue
        u = placement["offset_u"] + (float(box["u_min"]) + float(box["u_max"])) / 2.0
        v = placement["offset_v"] - float(box["v_max"]) - _VIEW_LABEL_GAP_MM
        # Над самым верхним уже нарисованным над видом: у главного разреза вала
        # над контуром два ряда размеров, и «А-А» на 18 мм ложилась на габарит.
        left = (placement["offset_u"] + float(box["u_min"])) * PAPER_PX_PER_MM
        right = (placement["offset_u"] + float(box["u_max"])) * PAPER_PX_PER_MM
        contour_top = (placement["offset_v"] - float(box["v_max"])) * PAPER_PX_PER_MM
        tops = [
            y
            for x, y in _entity_points(occupied or [])
            if left - 1.0 <= x <= right + 1.0
            and contour_top - _VIEW_LABEL_REACH_MM * PAPER_PX_PER_MM <= y < contour_top
        ]
        if tops:
            v = min(v, min(tops) / PAPER_PX_PER_MM - DIM_TEXT_MM * 1.2)
        # Над видом тесно — там соседний вид (разрез корпуса под планом):
        # надпись встаёт слева от верхнего угла вида.
        width_mm = len(label) * DIM_TEXT_MM * 1.4 * 0.7
        label_box = (u - width_mm / 2, v - DIM_TEXT_MM * 1.4, u + width_mm / 2, v)
        if any(
            _boxes_overlap(label_box, other)
            for other in _view_boxes_mm(views, placements, exclude=view, margin=_VIEW_MARGIN_MM)
        ):
            u = placement["offset_u"] + float(box["u_min"]) - width_mm / 2 - DIM_TEXT_MM
            v = placement["offset_v"] - float(box["v_max"]) + DIM_TEXT_MM * 1.4
        entities.append(
            TextEntity(
                position=Point(x=u * PAPER_PX_PER_MM, y=v * PAPER_PX_PER_MM),
                text=label,
                height=DIM_TEXT_MM * 1.4 * PAPER_PX_PER_MM,
                rotation=0.0,
                anchor="middle",
                line_class="dim",
                width_class="thin",
                **_ORIGIN,
            )
        )
    return entities


_VIEW_MARGIN_MM = 4.0


def _view_boxes_mm(
    views: list[dict[str, Any]],
    placements: list[dict[str, float] | None],
    *,
    exclude: dict[str, Any] | None = None,
    margin: float = 0.0,
) -> list[tuple[float, float, float, float]]:
    """Рамки контуров видов на листе, мм (y вниз)."""
    boxes = []
    for view, placement in zip(views, placements, strict=False):
        box = (view or {}).get("bounds_mm")
        if view is exclude or not placement or not isinstance(box, dict):
            continue
        boxes.append(
            (
                placement["offset_u"] + float(box["u_min"]) - margin,
                placement["offset_v"] - float(box["v_max"]) - margin,
                placement["offset_u"] + float(box["u_max"]) + margin,
                placement["offset_v"] - float(box["v_min"]) + margin,
            )
        )
    return boxes


def _boxes_overlap(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _entity_points(entities: list[Any]) -> list[tuple[float, float]]:
    """Опорные точки нарисованного (px листа): концы отрезков, вершины, подписи."""
    points: list[tuple[float, float]] = []
    for item in entities:
        for name in ("p1", "p2", "position", "center"):
            point = getattr(item, name, None)
            if point is not None and hasattr(point, "x"):
                points.append((float(point.x), float(point.y)))
        for point in getattr(item, "points", None) or []:
            if hasattr(point, "x"):
                points.append((float(point.x), float(point.y)))
    return points


def _cutting_plane_entities(
    views: list[dict[str, Any]], placements: list[dict[str, float] | None], plan: SheetPlan
) -> list[Any]:
    """След секущей плоскости вынесенного сечения на главном виде (ГОСТ 2.305).

    Разомкнутая линия — два основных штриха за контуром на станции сечения,
    стрелки направления взгляда у внешних концов, буква у стрелок. Без неё
    сечение «Б-Б» над кругом нечем связать с местом на валу: ни человеку, ни
    межвидовому соответствию (z4-r4 cross_view).

    Сечение ядра строится в системе вида ``side`` (u = x, v = y) — это взгляд к
    началу оси, поэтому стрелки смотрят к левому торцу.
    """
    from app.ai.cad_ir.schema import Point, Segment, TextEntity
    from app.ai.cad_projection import _ORIGIN, DIM_TEXT_MM

    ratio = plan.ratio or 1.0
    main = next(
        (
            index
            for kind in ("bottom", "front")
            for index, view in enumerate(views)
            if (view or {}).get("kind") == kind
            and index not in plan.scaffold_views
            and index < len(placements)
            and placements[index]
            and isinstance(view.get("bounds_mm"), dict)
        ),
        None,
    )
    if main is None:
        return []
    placement, box = placements[main], views[main]["bounds_mm"]
    px = PAPER_PX_PER_MM

    def point(u: float, y: float) -> Point:
        return Point(x=u * px, y=y * px)

    entities: list[Any] = []
    for view in views:
        station = (view or {}).get("section_station_mm")
        label = str((view or {}).get("label") or "")
        # Ядро отдаёт осевое сечение видом removed_section со станцией, без
        # section_normal запроса.
        if station is None or "-" not in label:
            continue
        letter = label.split("-")[0].strip()
        u = placement["offset_u"] + float(box["u_min"]) + float(station) * ratio
        top = placement["offset_v"] - float(box["v_max"])
        bottom = placement["offset_v"] - float(box["v_min"])
        for inner, outer in (
            (top - _CUT_GAP_MM, top - _CUT_GAP_MM - _CUT_STROKE_MM),
            (bottom + _CUT_GAP_MM, bottom + _CUT_GAP_MM + _CUT_STROKE_MM),
        ):
            entities.append(
                Segment(p1=point(u, inner), p2=point(u, outer), line_class="contour", **_ORIGIN)
            )
            tip = u - _CUT_ARROW_MM
            entities.append(
                Segment(
                    p1=point(u, outer),
                    p2=point(tip, outer),
                    line_class="dim",
                    width_class="thin",
                    **_ORIGIN,
                )
            )
            for side in (-1.0, 1.0):
                entities.append(
                    Segment(
                        p1=point(tip, outer),
                        p2=point(tip + _CUT_HEAD_MM, outer + side * _CUT_HEAD_MM / 3.0),
                        line_class="dim",
                        width_class="thin",
                        **_ORIGIN,
                    )
                )
            # Буква — у середины штриха со стороны стрелки: за концом штриха
            # начинаются ряды размеров (цепочка, габарит).
            entities.append(
                TextEntity(
                    position=point(tip - 1.0, (inner + outer) / 2.0),
                    text=letter,
                    height=DIM_TEXT_MM * px,
                    rotation=0.0,
                    anchor="middle",
                    line_class="dim",
                    width_class="thin",
                    **_ORIGIN,
                )
            )
    return entities


# Разомкнутая линия, стрелка и буква умещаются между контуром и первым рядом
# размеров (DIM_OFFSET_MM = 8): штрих 8 мм по ГОСТ 2.303 пересекал цепочку, и
# буквы ложились на её стрелки (shaft-4, 1:4). Штрих короче минимума ГОСТ —
# уступка плотному листу, читаемость важнее.
_CUT_GAP_MM = 1.5
_CUT_STROKE_MM = 5.0
_CUT_ARROW_MM = 5.0
_CUT_HEAD_MM = 2.5


# Надпись вида — над рядом размеров над контуром (отступ размера 8 мм + число
# 3,5 мм + зазор): на 6 мм «Б-Б» ложилась на размерную линию глубины паза.
_VIEW_LABEL_GAP_MM = 18.0
# Полоса над видом, где ищутся его ряды размеров: выше — соседний вид (план
# над разрезом корпуса), его надпись не касается.
_VIEW_LABEL_REACH_MM = 30.0


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
