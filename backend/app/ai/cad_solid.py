"""Stage 1 of the 3D-first redraw: an EXACT solid from the read spec.

A body of revolution is not a shape-recognition problem — it is a profile spun
about an axis. Every number in that profile was already read off the sheet
(``outer[]``/``bore[]``), so the solid is correct BY CONSTRUCTION rather than
by a model's guess, in the same way the 2D drafter builds the axis instead of
finding it. No neural reconstruction is involved and none is needed here.

Downstream this is the foundation the roadmap needs: derived orthographic
views (ГОСТ 2.305 projection becomes arithmetic), mass and stock size from the
kernel's own B-Rep report, stable edge keys to hang tolerances and machining
operations on, and STEP/IGES for CAM.

What this module deliberately does NOT do: invent a dimension. A spec that
never stated a length produces no solid, exactly like the 2D drafter.
"""

from __future__ import annotations

import math
import re
from typing import Any

from app.ai.cad_ir.feature_tree import Feature3D, FeatureTreeCandidate, ParamProvenance
from app.ai.cad_recognize.spec_vectorize import (
    _expanded_profile_holes,
    _num,
    _prismatic_profiles,
    _rotation_parts,
    _sections_are_complete,
    taper_end_diameter,
)


def _source_feature_ids(*items: Any) -> list[str]:
    """``Feature3D.source_feature_ids`` for one or more spec list items.

    Each id is exactly what ``assign_stable_feature_ids`` assigned the item
    (``f"{body_index}:{list_name}:{index}"``) — this is what lets
    ``spec_feature_tree_as_graph`` draw a ``realizes`` edge from the compiled
    ``BuildOperation`` back to the descriptive ``Feature`` node(s) it came
    from (Ф2.6c). An item with no id (reader never tagged it, or it is a
    pattern-synthesized point with nothing of its own to point at) is
    skipped rather than guessed — the operation simply has no realizes edge,
    same fail-closed default as every other id-tagged reference in this
    codebase.
    """
    return [str(item["id"]) for item in items if isinstance(item, dict) and item.get("id")]


def _fill_provisional_step_lengths(
    sections: list[dict],
) -> tuple[list[dict], list[str]] | None:
    """A stepped profile with a stated diameter but no length per step used to
    discard the WHOLE candidate (see git history on this function's callers)
    — the single most common way a real, otherwise-fully-read shaft produced
    no 3D at all and no editable draft either, just a text warning list.

    Never guesses a diameter (that is the part's actual fit/size — genuinely
    unknowable from context). A missing LENGTH is different: it is filled
    with the average of the other stated lengths IN THE SAME outer/bore list,
    every filled step is named in the returned notes (never silently), and
    the caller stamps the whole profile's ``ParamProvenance.origin`` as
    ``"guessed"`` — the schema's own vocabulary for "the server's own
    unconfirmed guess" — so ``verify_solid_against_spec`` refuses acceptance
    until a human has gone through ``feature_tree_from_spec`` again with a
    corrected (or explicitly re-affirmed) spec.

    Returns ``None`` — never a fabricated shape — when any section lacks a
    diameter, or when NO section in this list has a stated length to average
    from (nothing honest to anchor a guess to).
    """
    if not sections:
        return None
    if any(not section.get("d") for section in sections):
        return None
    known = [float(section["l"]) for section in sections if section.get("l")]
    if not known:
        return None
    guessed_length = round(sum(known) / len(known), 2)
    filled: list[dict] = []
    notes: list[str] = []
    for section in sections:
        if section.get("l"):
            filled.append(section)
            continue
        filled.append({**section, "l": guessed_length})
        notes.append(
            f"{section.get('id') or '?'}: длина ступени Ø{section.get('d')} не указана "
            f"— построено с предположением {guessed_length:g} мм (среднее по прочитанным "
            "ступеням), требует подтверждения в редакторе"
        )
    return filled, notes


class SolidVerification:
    """Kernel report vs the numbers the sheet stated. Nothing here trusts the
    builder: the solid is measured after the fact and compared to the source."""

    def __init__(self, checks: dict[str, Any]) -> None:
        self.checks = checks

    @property
    def ok(self) -> bool:
        return bool(self.checks.get("ok"))

    def as_dict(self) -> dict[str, Any]:
        return dict(self.checks)


# Сомнения ридера в СВОИХ свидетельствах, а не в геометрии: когда каждую
# ступень подтвердил сам лист, они не блокируют (живая втулка part_03: станции
# собраны разрезом и надписями, а осевой локализатор ридера размерных линий не
# нашёл — «осевые позиции не подтверждены»).
_READER_EVIDENCE_DOUBTS = (
    "не удалось отделить геометрию от аннотаций",
    "осевые позиции не подтверждены локализованными размерными линиями",
)


def solid_build_gate(
    spec: dict,
    candidate: FeatureTreeCandidate,
    *,
    require_source_evidence: bool = False,
) -> dict[str, list[str] | bool]:
    """Classify what may and may not cross the spec -> CAD boundary.

    ``missing_data`` historically mixed harmless review notes with facts that
    change the part.  That allowed a sectioned hollow spindle to be compiled as
    a solid shaft.  The gate is deliberately conservative: unresolved reader
    facts and omitted requested geometry are blockers; an absent bore is a
    blocker when the drawing explicitly contains a section, otherwise it stays
    a visible warning for a potentially solid shaft.
    """
    unresolved = [str(item) for item in spec.get("unresolved") or [] if str(item)]
    non_geometric = [
        item
        for item in unresolved
        if (
            "подготовительного отверстия" in item.lower()
            and not any(
                marker in item.lower()
                for marker in (
                    "торец",
                    "сквозное",
                    "глухое",
                    "глубин",
                    "резьба/шаг",
                )
            )
        )
    ]
    # Не про геометрию тела: нечитаемые рамки допусков формы и расположения
    # (живой z4-r4: «PMI: 4 рамок с неразличимым знаком»), и сомнение в
    # свидетельствах ридера, когда каждую ступень и паз подтвердил сам лист
    # (`sheet_verified`, проверка по листу, опровергнутого нет).
    sheet_verified = bool((spec.get("sheet_verified") or {}).get("all_confirmed"))
    advisory = [
        item
        for item in unresolved
        if item not in non_geometric
        and (
            item.startswith("PMI:")
            or (sheet_verified and any(doubt in item for doubt in _READER_EVIDENCE_DOUBTS))
        )
    ]
    blockers = [item for item in unresolved if item not in non_geometric and item not in advisory]
    from app.ai.cad_dimension_graph import build_dimension_graph

    blockers.extend(build_dimension_graph(spec)["errors"])
    if require_source_evidence:
        body = spec.get("main_view") or {}
        missing_evidence = [
            f"main_view.{group}.{index}"
            for group in (
                "outer",
                "bore",
                "keyways",
                "cross_holes",
                "axial_holes",
                "circular_hole_patterns",
                "grooves",
                "chamfers",
            )
            for index, item in enumerate(body.get(group) or [])
            if isinstance(item, dict) and not item.get("evidence")
        ]
        # Пластина и фланец: отверстия и массивы профиля — то же правило. Гейт
        # знал только группы тела вращения, и геометрия пластины собиралась без
        # единого свидетельства (план, Ф4). Прорези — нет: их проверяльщика
        # ещё нет, и свидетельство им взять неоткуда.
        profile = body.get("profile") or {}
        missing_evidence += [
            f"main_view.profile.{group}.{index}"
            for group in ("holes", "hole_patterns")
            for index, item in enumerate(profile.get(group) or [])
            if isinstance(item, dict) and not item.get("evidence")
        ]
        if missing_evidence:
            blockers.append(
                "геометрия без локализованного evidence: "
                + ", ".join(missing_evidence[:8])
                + (f" и ещё {len(missing_evidence) - 8}" if len(missing_evidence) > 8 else "")
            )
    warnings: list[str] = [
        item + " (не блокирует: это технологический параметр, геометрия резьбы из стандарта)"
        for item in non_geometric
    ]
    warnings.extend(
        item
        + (
            " (не блокирует: допуски формы и расположения на тело не влияют)"
            if item.startswith("PMI:")
            else " (не блокирует: вся геометрия подтверждена по самому листу)"
        )
        for item in advisory
    )
    has_section = any(
        str(view.get("kind") or "").lower() in {"section", "cut", "разрез", "сечение"}
        for view in spec.get("views") or []
        if isinstance(view, dict)
    )
    for item in candidate.missing_data:
        message = str(item)
        lowered = message.lower()
        is_critical = any(
            marker in lowered
            for marker in (
                "не построен",
                "длиннее детали",
                "построено только главное",
                "прочитан не полностью",
            )
        )
        if "разрез не прочитан" in lowered:
            # Сечения по листу — сплошные круги (живой z4-r4: А-А и Б-Б через
            # пазы): вал сплошной доказан, замечание остаётся предупреждением.
            is_critical = has_section and not (spec.get("sections_solid") or {}).get("solid")
        (blockers if is_critical else warnings).append(message)
    blockers = list(dict.fromkeys(blockers))
    warnings = [item for item in dict.fromkeys(warnings) if item not in blockers]
    return {"allowed": not blockers, "blockers": blockers, "warnings": warnings}


_PREVIEW_EXCLUDABLE_MARKERS = (
    "— не построен",
    "фасок, локализовано",
    "малые элементы: осевые отверстия",
    "малые элементы: массив",
    "малые элементы: указано ",
    "малые элементы: круговой массив",
    "малые элементы: группа отверстий",
    # A3: a callout the reader FOUND on the sheet but could not place — no
    # cross_holes[]/axial_holes[]/thread was ever added for it in the first
    # place, so there is no feature to build wrong, only a base body that
    # already builds fine without it. Real live wording, not a guess: e.g.
    # "малые элементы: поперечное отверстие Ø0.6 указано, но не
    # локализовано" / "малые элементы: резьбы указаны, но не привязаны к
    # участкам: ...". An evidence/colour-separation note ("малые элементы:
    # evidence: ...") never contains either stem — doubt about the read
    # itself stays a hard blocker, unaffected by this addition.
    "не локализован",
    "не привязан",
)


def solid_preview_gate(build_gate: dict[str, Any]) -> dict[str, Any]:
    """Decide whether the proven subset may be compiled as a review preview.

    A final model is still governed by :func:`solid_build_gate`.  This second
    gate only permits omissions that are already explicit feature-level cuts
    (a hole, keyway, groove or chamfer which the compiler skipped).  Profile,
    dimension-chain and evidence failures remain hard blockers: a preview with
    the wrong base body would be more misleading than no preview at all.
    """
    blockers = [str(item) for item in build_gate.get("blockers") or []]
    excluded = [
        item
        for item in blockers
        if any(marker in item.lower() for marker in _PREVIEW_EXCLUDABLE_MARKERS)
    ]
    hard_blockers = [item for item in blockers if item not in excluded]
    return {
        "allowed": bool(blockers) and not hard_blockers,
        "hard_blockers": hard_blockers,
        "excluded": excluded,
    }


def _profile_points(sections: list[dict]) -> list[dict[str, float]]:
    """Ordered (r, z) polyline of a stepped profile, in millimetres.

    Two points per section — enter and leave — so a step is a true right-angle
    shoulder rather than a taper interpolated between section centres. A section
    the sheet declares CONICAL is the exception: there the two radii differ, and
    that difference is the whole feature. A 7:24 spindle nose built as a
    cylinder is a different part that fits nothing.
    """
    points: list[dict[str, float]] = []
    z = 0.0
    for section in sections:
        radius = float(section["d"]) / 2.0
        length = float(section["l"])
        end_diameter = taper_end_diameter(section)
        end_radius = float(end_diameter) / 2.0 if end_diameter else radius
        points.append({"r": radius, "z": z})
        z += length
        points.append({"r": end_radius, "z": z})
    return points


def _profile_volume_from_points(points: list[dict]) -> float:
    """Same frustum-sum math as :func:`_profile_volume_mm3`, from compiled
    ``profile_points``/``bore_points`` (two per section: enter, leave)
    instead of raw spec sections.

    A2: when a step's length was provisionally filled in, the raw spec
    section's own ``"l"`` is still ``None`` — ``_profile_volume_mm3`` would
    crash on it (``float(None)``). ``profile_points`` is what was actually
    built, guessed length included, so reading the volume from there is
    exact for a normal fully-stated build too (same arithmetic) and correct
    for a guessed one.
    """
    volume = 0.0
    for index in range(0, len(points) - 1, 2):
        start, end = points[index], points[index + 1]
        length = float(end["z"]) - float(start["z"])
        start_diameter = float(start["r"]) * 2.0
        end_diameter = float(end["r"]) * 2.0
        volume += (
            math.pi
            * length
            / 12.0
            * (start_diameter**2 + start_diameter * end_diameter + end_diameter**2)
        )
    return volume


def _profile_volume_mm3(sections: list[dict]) -> float:
    """Exact volume of coaxial cylindrical/frustum sections before cuts."""

    volume = 0.0
    for section in sections:
        start_diameter = float(section["d"])
        end_diameter = float(taper_end_diameter(section) or start_diameter)
        length = float(section["l"])
        volume += (
            math.pi
            * length
            / 12.0
            * (start_diameter**2 + start_diameter * end_diameter + end_diameter**2)
        )
    return volume


def feature_tree_from_spec(spec: dict) -> FeatureTreeCandidate | None:
    """Build a solid feature tree from the read spec.

    Two part classes are expressed exactly today: a body of revolution (a
    profile spun about its axis) and a plate/flange (a thickness given to a
    read outline, with its holes and slots cut from it). Anything else returns
    None so the caller keeps the 2D result rather than inventing a solid.
    """
    rotation = _rotation_feature_tree(spec)
    if rotation is not None:
        return rotation
    sheet = _sheet_metal_feature_tree(spec)
    if sheet is not None:
        return sheet
    return _prismatic_feature_tree(spec)


def _sheet_metal_feature_tree(spec: dict) -> FeatureTreeCandidate | None:
    """Гнутая деталь из листа (X4): сечение с гибами, выдавленное на ширину.

    Верстак SheetMetal не нужен (E14): эскиз основания ядра принимает дуги,
    и сечение полок с гибами, выдавленное на ширину, — это и есть деталь; объём
    совпадает с формулой, развёртка с 3D — до 0,0000 мм.
    """
    from app.ai.sheet_metal import bent_section, developed_length

    body = spec.get("main_view") or {}
    sheet = body.get("sheet_metal")
    if not isinstance(sheet, dict):
        return None
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
        sketch = bent_section(flanges, turns, radius, thickness, angles)
    except (KeyError, TypeError, ValueError):
        return None
    stated = {
        "sketch_profile": ParamProvenance(
            origin="stated", detail="сечение из прочитанных полок, гибов, радиуса и толщины"
        ),
        "depth_mm": ParamProvenance(origin="stated", detail="ширина листовой детали с чертежа"),
    }
    flat = developed_length(
        flanges, len(turns), radius, thickness, float(sheet.get("k_factor") or 0.5), angles
    )
    return FeatureTreeCandidate(
        features=[
            Feature3D(
                kind="extrude",
                params={"sketch_profile": sketch, "depth_mm": width},
                param_provenance=stated,
                confidence=0.85,
            )
        ],
        score=0.9,
        label=(
            str(spec.get("part") or "Листовая деталь")
            + f" — гибов {len(turns)}, развёртка {flat:.2f} мм"
        )[:500],
        missing_data=[],
    )


# A closed loop the sheet's own read coordinates should return to exactly
# (up to rounding in how the dimensions were transcribed) — not the 1e-6
# used for values this module derives itself by arithmetic.
_SKETCH_CLOSURE_TOLERANCE_MM = 0.01


def _sketch_closure_error(sketch: list[dict]) -> float | None:
    """Distance between where a line/arc chain ends and its implicit (0, 0)
    start — a real geometric property of the read vertices, computed
    directly rather than through a constraint solver: every vertex is
    already an absolute coordinate the reader stated, not an unknown a
    solver would need to find. None when a segment is malformed."""
    x = y = 0.0
    for segment in sketch:
        if not isinstance(segment, dict):
            return None
        to = segment.get("to")
        if not (isinstance(to, (list, tuple)) and len(to) == 2):
            return None
        try:
            x, y = float(to[0]), float(to[1])
        except (TypeError, ValueError):
            return None
    return math.hypot(x, y)


def _rotated_capsule_sketch(
    straight: float,
    radius: float,
    rotation_deg: float,
) -> tuple[float, float, list[dict]]:
    """Ф2.3: a capsule (stadium) slot's line/arc chain, rotated about its
    own centre — the kernel's ``sketch``-profile boss/pocket tool
    (``_sketch_tool``) never gets a rotation parameter itself, it only
    translates a local-frame wire, so the rotation is applied HERE, in the
    numbers, not asked of the kernel.

    Returns ``(offset_x, offset_y, segments)``: ``offset`` is added to the
    slot's own read centre to get the tool's kernel-side translation (its
    local (0, 0) is one corner of the capsule, not the interior centre a
    wire's start vertex cannot legally be); ``segments`` is the chain from
    that same corner. ``straight <= 0`` degenerates to a plain circle (two
    semicircle arcs, no straight sides) — a round slot is a legitimate
    input (``SpecSlot`` only requires ``length_mm >= width_mm``).
    """
    theta = math.radians(rotation_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)

    def rotate(px: float, py: float) -> tuple[float, float]:
        return px * cos_t - py * sin_t, px * sin_t + py * cos_t

    if straight <= 1e-6:
        bottom = rotate(0.0, -radius)
        top = rotate(0.0, radius)
        center = rotate(0.0, 0.0)  # rotation about the origin fixes it there
        rel = lambda point: [point[0] - bottom[0], point[1] - bottom[1]]  # noqa: E731
        segments = [
            {"kind": "arc", "to": rel(top), "center": rel(center), "clockwise": False},
            {"kind": "arc", "to": rel(bottom), "center": rel(center), "clockwise": False},
        ]
        return bottom[0], bottom[1], segments

    a = rotate(-straight / 2.0, -radius)
    b = rotate(straight / 2.0, -radius)
    c = rotate(straight / 2.0, radius)
    d = rotate(-straight / 2.0, radius)
    arc1_center = rotate(straight / 2.0, 0.0)
    arc2_center = rotate(-straight / 2.0, 0.0)
    rel = lambda point: [point[0] - a[0], point[1] - a[1]]  # noqa: E731
    segments = [
        {"kind": "line", "to": rel(b)},
        {"kind": "arc", "to": rel(c), "center": rel(arc1_center), "clockwise": False},
        {"kind": "line", "to": rel(d)},
        {"kind": "arc", "to": rel(a), "center": rel(arc2_center), "clockwise": False},
    ]
    return a[0], a[1], segments


# X2 (корпуса): грань габарита → (оси спека → оси грани ядра, размеры грани).
# У ядра свой порядок осей на каждой грани; в спеке u/v — вдоль детали:
# top/bottom — ширина и высота, front/back — ширина и толщина, left/right —
# высота и толщина.
_WALL_PLANES = {
    "top": ("uv", "width", "height"),
    "bottom": ("uv", "width", "height"),
    "front": ("uv", "width", "depth"),
    "back": ("vu", "depth", "width"),
    "left": ("vu", "depth", "height"),
    "right": ("uv", "height", "depth"),
}


def _wall_feature_params(
    item: dict, *, width: float, height: float, thickness: float
) -> dict[str, Any] | None:
    """Параметры кармана/прилива на грани в системе ядра (от угла грани)."""
    plane = str(item.get("on_plane") or "")
    if plane not in _WALL_PLANES:
        return None
    order, first, second = _WALL_PLANES[plane]
    sizes = {"width": width, "height": height, "depth": thickness}
    face_x, face_y = sizes[first], sizes[second]
    u, v = _num(item.get("center_u_mm")) or 0.0, _num(item.get("center_v_mm")) or 0.0
    local_x, local_y = (u, v) if order == "uv" else (v, u)
    depth = _num(item.get("depth_mm"))
    if not depth:
        return None
    params: dict[str, Any] = {
        "on_plane": plane,
        "depth_mm": depth,
        "center_x_mm": face_x / 2.0 + local_x,
        "center_y_mm": face_y / 2.0 + local_y,
    }
    if item.get("profile") == "rectangle":
        feature_width, feature_height = _num(item.get("width_mm")), _num(item.get("height_mm"))
        if not feature_width or not feature_height:
            return None
        params["profile"] = "rectangle"
        # Прямоугольник задан по осям детали — как и центр.
        params["width_mm"], params["height_mm"] = (
            (feature_width, feature_height) if order == "uv" else (feature_height, feature_width)
        )
    else:
        diameter = _num(item.get("diameter_mm"))
        if not diameter:
            return None
        params["profile"] = "circle"
        params["diameter_mm"] = diameter
    return params


def _prismatic_feature_tree(spec: dict) -> FeatureTreeCandidate | None:
    """Плоские и призматические тела листа — каждое своим поддеревом.

    Строилось только ПЕРВОЕ тело из ``parts[]``: остальные пластины сварного
    узла пропадали без следа и без замечания. Теперь каждое тело — со своим
    ``body_index`` и размещением относительно первого (X3); тело, которое не
    строится, отказывает всему дереву — строить часть узла молча нельзя.
    """
    bodies = [body for body in (spec.get("parts") or []) if isinstance(body, dict)]
    if not bodies:
        bodies = [spec.get("main_view") or {}]
    bodies = [body for body in bodies if isinstance(body.get("profile"), dict)]
    if not bodies:
        return None
    if len(bodies) == 1:
        return _one_prismatic_tree(spec, bodies[0]["profile"])
    features: list[Feature3D] = []
    missing: list[str] = []
    unplaced = 0
    for body_index, body in enumerate(bodies):
        tree = _one_prismatic_tree(spec, body["profile"])
        if tree is None:
            return None
        for feature in tree.features:
            feature.body_index = body_index
        placement = body.get("placement")
        if body_index > 0:
            if isinstance(placement, dict) and placement.get("position_mm"):
                base = tree.features[0]
                base.params["placement"] = {
                    "position_mm": [float(v) for v in placement["position_mm"]],
                    "axis": [float(v) for v in placement.get("axis") or [0.0, 0.0, 1.0]],
                    "angle_deg": float(placement.get("angle_deg") or 0.0),
                }
                base.param_provenance["placement"] = ParamProvenance(
                    origin="stated", detail="размещение тела прочитано с листа"
                )
            else:
                unplaced += 1
        features.extend(tree.features)
        missing.extend(note for note in tree.missing_data if "ни одного отверстия" not in note)
    if unplaced:
        missing.append(
            f"на листе прочитано тел: {len(bodies)}; взаимное расположение {unplaced} "
            "из них не прочитано, построены раздельно"
        )
    else:
        beads, weld_notes = _weld_beads(spec, bodies, start_index=len(bodies))
        features.extend(beads)
        missing.extend(weld_notes)
    label = str(spec.get("part") or "Узел") + f" — {len(bodies)} тел по прочитанным контурам"
    return FeatureTreeCandidate(
        features=features, score=0.85, label=label[:500], missing_data=missing
    )


def _rotation_of(placement: dict | None) -> list[list[float]]:
    """Матрица поворота размещения (ось-угол, как у ядра)."""
    import math

    if not isinstance(placement, dict):
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    x, y, z = (float(v) for v in placement.get("axis") or [0.0, 0.0, 1.0])
    norm = math.sqrt(x * x + y * y + z * z) or 1.0
    x, y, z = x / norm, y / norm, z / norm
    angle = math.radians(float(placement.get("angle_deg") or 0.0))
    c, s_, t = math.cos(angle), math.sin(angle), 1.0 - math.cos(angle)
    return [
        [t * x * x + c, t * x * y - s_ * z, t * x * z + s_ * y],
        [t * x * y + s_ * z, t * y * y + c, t * y * z - s_ * x],
        [t * x * z - s_ * y, t * y * z + s_ * x, t * z * z + c],
    ]


def _axis_angle(matrix: list[list[float]]) -> tuple[list[float], float]:
    """Ось и угол (°) поворота по его матрице."""
    import math

    trace = matrix[0][0] + matrix[1][1] + matrix[2][2]
    angle = math.acos(max(-1.0, min(1.0, (trace - 1.0) / 2.0)))
    if angle < 1e-9:
        return [0.0, 0.0, 1.0], 0.0
    if abs(angle - math.pi) < 1e-6:
        # Поворот на 180°: ось — столбец с наибольшей диагональю (M + I)/2.
        diagonal = [math.sqrt(max(0.0, (matrix[i][i] + 1.0) / 2.0)) for i in range(3)]
        index = max(range(3), key=lambda i: diagonal[i])
        axis = [0.0, 0.0, 0.0]
        axis[index] = diagonal[index]
        for other in range(3):
            if other != index:
                axis[other] = matrix[index][other] / (2.0 * diagonal[index])
        return axis, 180.0
    denominator = 2.0 * math.sin(angle)
    axis = [
        (matrix[2][1] - matrix[1][2]) / denominator,
        (matrix[0][2] - matrix[2][0]) / denominator,
        (matrix[1][0] - matrix[0][1]) / denominator,
    ]
    return axis, math.degrees(angle)


def _body_box(body: dict) -> tuple[list[float], list[float]] | None:
    """Габарит прямоугольного тела в системе первого тела (после размещения)."""
    profile = body.get("profile") or {}
    if profile.get("shape") != "rectangle":
        return None
    size = [_num(profile.get(key)) for key in ("width_mm", "height_mm", "thickness_mm")]
    if not all(size):
        return None
    placement = body.get("placement") if isinstance(body.get("placement"), dict) else None
    rotation = _rotation_of(placement)
    position = [float(v) for v in (placement or {}).get("position_mm") or [0.0, 0.0, 0.0]]
    corners = [
        [
            sum(rotation[row][col] * point[col] for col in range(3)) + position[row]
            for row in range(3)
        ]
        for point in (
            (x, y, z) for x in (0.0, size[0]) for y in (0.0, size[1]) for z in (0.0, size[2])
        )
    ]
    return (
        [min(corner[axis] for corner in corners) for axis in range(3)],
        [max(corner[axis] for corner in corners) for axis in range(3)],
    )


def _weld_beads(
    spec: dict, bodies: list[dict], *, start_index: int
) -> tuple[list[Feature3D], list[str]]:
    """Валики угловых швов (X3): треугольник с катетом по стыку двух тел.

    Стык ищется по габаритам размещённых тел: грани встык, площадка касания —
    след меньшего тела на большем. Валик — вдоль длинной стороны площадки, с
    одной стороны (Т1, У4) или с обеих (``both_sides``, Т3). Шов без катета
    или между телами, которые не касаются, не строится и уходит замечанием.
    """
    features: list[Feature3D] = []
    notes: list[str] = []
    index = start_index
    for weld in spec.get("welds") or []:
        if not isinstance(weld, dict):
            continue
        pair = weld.get("bodies") or []
        leg = _num(weld.get("leg_mm"))
        name = str(weld.get("designation") or "шов")
        if len(pair) != 2 or not leg or not all(0 <= int(i) < len(bodies) for i in pair):
            notes.append(f"сварной шов {name}: тела или катет не прочитаны — не построен")
            continue
        boxes = [_body_box(bodies[int(i)]) for i in pair]
        if None in boxes:
            notes.append(f"сварной шов {name}: стык не прямоугольных тел — не построен")
            continue
        contact = _contact(boxes[0], boxes[1])
        if contact is None:
            notes.append(f"сварной шов {name}: тела {pair[0]} и {pair[1]} не касаются")
            continue
        normal_axis, level, up, (lo, hi) = contact
        others = [axis for axis in range(3) if axis != normal_axis]
        long_axis = max(others, key=lambda axis: hi[axis] - lo[axis])
        side_axis = next(axis for axis in others if axis != long_axis)
        sides = (-1.0, 1.0) if weld.get("both_sides") else (-1.0,)
        for sign in sides:
            out = [0.0, 0.0, 0.0]
            out[side_axis] = sign
            rise = [0.0, 0.0, 0.0]
            rise[normal_axis] = up
            along = [
                out[1] * rise[2] - out[2] * rise[1],
                out[2] * rise[0] - out[0] * rise[2],
                out[0] * rise[1] - out[1] * rise[0],
            ]
            corner = [0.0, 0.0, 0.0]
            corner[normal_axis] = level
            corner[side_axis] = lo[side_axis] if sign < 0 else hi[side_axis]
            corner[long_axis] = lo[long_axis] if along[long_axis] > 0 else hi[long_axis]
            axis, angle = _axis_angle([[out[row], rise[row], along[row]] for row in range(3)])
            features.append(
                Feature3D(
                    kind="extrude",
                    body_index=index,
                    params={
                        "sketch_profile": [
                            {"kind": "line", "to": [leg, 0.0]},
                            {"kind": "line", "to": [0.0, leg]},
                            {"kind": "line", "to": [0.0, 0.0]},
                        ],
                        "depth_mm": round(hi[long_axis] - lo[long_axis], 6),
                        "placement": {
                            "position_mm": [round(v, 6) for v in corner],
                            "axis": [round(v, 9) for v in axis],
                            "angle_deg": round(angle, 9),
                        },
                    },
                    param_provenance={
                        "sketch_profile": ParamProvenance(
                            origin="stated", detail=f"катет шва {name} △{leg:g} по чертежу"
                        ),
                        "depth_mm": ParamProvenance(
                            origin="propagated", detail="шов по всей длине стыка тел"
                        ),
                        "placement": ParamProvenance(
                            origin="propagated", detail="место шва — стык размещённых тел"
                        ),
                    },
                    confidence=0.8,
                )
            )
            index += 1
    return features, notes


def _contact(first, second):
    """Площадка касания двух габаритов: (ось нормали, уровень, знак «к меньшему»,
    (min, max) площадки) или None."""
    (lo1, hi1), (lo2, hi2) = first, second
    for axis in range(3):
        for level, up_from_first in ((hi1[axis], 1.0), (lo1[axis], -1.0)):
            other = lo2[axis] if up_from_first > 0 else hi2[axis]
            if abs(level - other) > 1e-6:
                continue
            lo = [max(lo1[i], lo2[i]) for i in range(3)]
            hi = [min(hi1[i], hi2[i]) for i in range(3)]
            if any(hi[i] - lo[i] <= 1e-6 for i in range(3) if i != axis):
                continue
            area_first = 1.0
            area_second = 1.0
            for i in range(3):
                if i != axis:
                    area_first *= hi1[i] - lo1[i]
                    area_second *= hi2[i] - lo2[i]
            # «Вверх» — от большего тела к меньшему (ребро стоит на основании).
            up = up_from_first if area_second <= area_first else -up_from_first
            return axis, level, up, (lo, hi)
    return None


def _one_prismatic_tree(spec: dict, profile: dict) -> FeatureTreeCandidate | None:
    """A plate or flange: the read outline given its read thickness.

    Thickness comes from a side view or a section; a sheet that never stated it
    yields no solid at all, exactly as the 2D drafter refuses to draft a section
    whose length was never read.
    """
    thickness = _num(profile.get("thickness_mm"))
    if not thickness or thickness <= 0:
        return None
    shape = profile.get("shape")
    holes = _expanded_profile_holes(profile)
    if holes is None:
        return None

    features: list[Feature3D] = []
    missing: list[str] = []
    # Keyed by the operation's own parameter: under "thickness_mm" the graph
    # saw depth_mm with no source and counted it guessed — every plate became
    # a draft (live part_04: 90 × 100 × 3 built and verified, still preview).
    provenance = {
        "depth_mm": ParamProvenance(
            origin="stated", detail="толщина прочитана с чертежа (profile.thickness_mm)"
        )
    }

    if shape == "rectangle":
        width = _num(profile.get("width_mm"))
        height = _num(profile.get("height_mm"))
        if not width or not height:
            return None
        corner_radius = _num(profile.get("corner_radius_mm"))
        if corner_radius and corner_radius > min(width, height) / 2.0:
            return None
        base_params = {
            "width_mm": width,
            "height_mm": height,
            "depth_mm": thickness,
        }
        if corner_radius:
            base_params["corner_radius_mm"] = corner_radius
        features.append(
            Feature3D(
                kind="extrude",
                params=base_params,
                param_provenance={
                    **provenance,
                    "width_mm": ParamProvenance(origin="stated", detail="габарит по чертежу"),
                    "height_mm": ParamProvenance(origin="stated", detail="габарит по чертежу"),
                    **(
                        {
                            "corner_radius_mm": ParamProvenance(
                                origin="stated", detail="радиус углов прочитан с выноски R"
                            )
                        }
                        if corner_radius
                        else {}
                    ),
                },
                confidence=0.9,
            )
        )

        # The sheet gives hole centres from the middle of the plate; an extrude
        # box is anchored at its corner, so the frames must be reconciled here
        # rather than by whoever reads the feature tree later.
        def to_base(x: float, y: float) -> tuple[float, float]:
            return width / 2.0 + x, height / 2.0 + y
    elif shape == "circle":
        diameter = _num(profile.get("diameter_mm"))
        if not diameter:
            return None
        features.append(
            Feature3D(
                kind="revolve",
                params={
                    "profile_points": [
                        {"r": diameter / 2.0, "z": 0.0},
                        {"r": diameter / 2.0, "z": thickness},
                    ]
                },
                param_provenance={
                    **provenance,
                    "profile_points": ParamProvenance(
                        origin="stated", detail="диаметр и толщина прочитаны с чертежа"
                    ),
                },
                confidence=0.9,
            )
        )

        # A turned base is addressed from its axis, which is the same frame the
        # drawing uses for a flange — no conversion needed.
        def to_base(x: float, y: float) -> tuple[float, float]:
            return x, y
    elif shape == "sketch":
        sketch = profile.get("sketch")
        if not sketch:
            return None
        closure_error = _sketch_closure_error(sketch)
        if closure_error is None or closure_error > _SKETCH_CLOSURE_TOLERANCE_MM:
            # A profile that does not return to its own start is not a
            # rectangle read wrong — it is an open contour, and extruding an
            # open wire is not a real solid. Refused, never force-closed.
            return None
        features.append(
            Feature3D(
                kind="extrude",
                params={"sketch_profile": sketch, "depth_mm": thickness},
                param_provenance={
                    **provenance,
                    "sketch_profile": ParamProvenance(
                        origin="stated",
                        detail="контур прочитан как последовательность линий/дуг от центра профиля",
                    ),
                },
                confidence=0.85,
            )
        )

        # Every hole/slot on this profile is already given relative to the
        # profile's own centre — the same origin the sketch's first implicit
        # vertex (0, 0) starts from. No corner to translate to.
        def to_base(x: float, y: float) -> tuple[float, float]:
            return x, y
    else:
        return None

    for hole in holes:
        diameter = _num(hole.get("diameter_mm"))
        x, y = _num(hole.get("center_x_mm")), _num(hole.get("center_y_mm"))
        if not diameter or x is None or y is None:
            return None
        cx, cy = to_base(x, y)
        depth = _num(hole.get("depth_mm"))
        thread = hole.get("thread") if isinstance(hole.get("thread"), dict) else None
        cut = diameter
        cut_provenance = ParamProvenance(origin="stated", detail="Ø отверстия с чертежа")
        if thread is not None:
            # Резьбовое (X1): режется Ø по впадинам резьбы, сама резьба —
            # косметическая (ГОСТ 2.311), как у вала.
            geometry = metric_thread_geometry(thread)
            if geometry is None:
                return None
            cut = float(geometry["minor_diameter_mm"])
            cut_provenance = ParamProvenance(
                origin="standard",
                detail=f"Ø по впадинам {thread.get('designation')} (ГОСТ, шаг — "
                + ("с листа" if geometry["pitch_source"] == "stated" else "крупный по ГОСТ")
                + ")",
            )
        params: dict[str, Any] = {"diameter_mm": cut, "center_x_mm": cx, "center_y_mm": cy}
        provenance_map = {
            "diameter_mm": cut_provenance,
            "center_x_mm": ParamProvenance(origin="stated", detail="координата от центра"),
            "center_y_mm": ParamProvenance(origin="stated", detail="координата от центра"),
        }
        if depth is not None:
            # Глухое — от лицевой грани плана. План (`side` ядра) смотрит на
            # грань zmin: с zmax отверстие выходило на плане штриховым, и лист
            # его не образмеривал (корпус пластин: 0 из 6 глухих).
            params.update({"through": False, "depth_mm": depth, "from_face": "zmin"})
            provenance_map["through"] = ParamProvenance(origin="stated", detail="глубина на листе")
            provenance_map["depth_mm"] = ParamProvenance(
                origin="stated", detail="глубина с чертежа"
            )
            provenance_map["from_face"] = ParamProvenance(
                origin="standard", detail="глухое отверстие — с лицевой грани плана"
            )
        else:
            # A plate hole is through unless the sheet said otherwise.
            params["through"] = True
            provenance_map["through"] = ParamProvenance(
                origin="standard", detail="отверстие без глубины на листе — сквозное"
            )
        features.append(
            Feature3D(
                kind="hole",
                source_feature_ids=_source_feature_ids(hole),
                params=params,
                param_provenance=provenance_map,
                confidence=0.85,
            )
        )
        if thread is not None:
            thread_params: dict[str, Any] = {
                "spec": str(thread.get("designation")),
                "diameter_mm": diameter,
                "center_x_mm": cx,
                "center_y_mm": cy,
                "internal": True,
                "from_face": "zmin",
            }
            length = _num(thread.get("length_mm")) or depth
            if length is not None:
                thread_params["length_mm"] = length
            features.append(
                Feature3D(
                    kind="thread",
                    source_feature_ids=_source_feature_ids(hole),
                    params=thread_params,
                    param_provenance={
                        key: ParamProvenance(origin="stated", detail="резьба с чертежа")
                        for key in thread_params
                    },
                    confidence=0.8,
                )
            )

    # Карманы и приливы на гранях (X2): только у прямоугольного контура —
    # грани габарита у круга и эскиза пришлось бы угадывать.
    for item in profile.get("wall_features") or []:
        if not isinstance(item, dict) or shape != "rectangle":
            continue
        params = _wall_feature_params(
            item,
            width=_num(profile.get("width_mm")) or 0.0,
            height=_num(profile.get("height_mm")) or 0.0,
            thickness=thickness,
        )
        kind = str(item.get("kind") or "")
        if params is None or kind not in ("pocket", "boss"):
            return None
        features.append(
            Feature3D(
                kind=kind,
                source_feature_ids=_source_feature_ids(item),
                params=params,
                param_provenance={
                    name: ParamProvenance(
                        origin="stated",
                        detail=f"{'карман' if kind == 'pocket' else 'прилив'} на грани "
                        f"{params['on_plane']} по чертежу",
                    )
                    for name in params
                },
                confidence=0.85,
            )
        )

    for slot in profile.get("slots") or []:
        if not isinstance(slot, dict):
            continue
        length = _num(slot.get("length_mm"))
        width_mm = _num(slot.get("width_mm"))
        x, y = _num(slot.get("center_x_mm")), _num(slot.get("center_y_mm"))
        rotation_deg = _num(slot.get("rotation_deg")) or 0.0
        if not length or not width_mm or x is None or y is None:
            return None
        cx, cy = to_base(x, y)
        straight = max(length - width_mm, 0.0)
        # Every parameter of the capsule's operations needs a source, or the
        # graph counts it guessed and the whole plate builds as a draft.
        slot_provenance = {
            "profile": ParamProvenance(origin="standard", detail="прорезь — капсула"),
            "depth_mm": ParamProvenance(origin="stated", detail="сквозь толщину пластины"),
            "center_x_mm": ParamProvenance(origin="stated", detail="центр прорези с чертежа"),
            "center_y_mm": ParamProvenance(origin="stated", detail="центр прорези с чертежа"),
            "width_mm": ParamProvenance(origin="propagated", detail="длина прорези минус ширина"),
            "height_mm": ParamProvenance(origin="stated", detail="ширина прорези с чертежа"),
            "diameter_mm": ParamProvenance(origin="stated", detail="ширина прорези с чертежа"),
            "through": ParamProvenance(origin="standard", detail="прорезь сквозная"),
        }

        def provenance_of(params: dict) -> dict:
            return {key: slot_provenance[key] for key in params if key in slot_provenance}

        if abs(rotation_deg) > 1e-6:
            # Ф2.3: a rotated capsule is one sketch-profile pocket (a
            # closed line/arc loop rotated about the slot's own centre),
            # not the axis-aligned pocket+2holes assembly below — that
            # assembly has no rotation parameter to give it.
            offset_x, offset_y, sketch = _rotated_capsule_sketch(
                straight, width_mm / 2.0, rotation_deg
            )
            features.append(
                Feature3D(
                    kind="pocket",
                    source_feature_ids=_source_feature_ids(slot),
                    params={
                        "profile": "sketch",
                        "sketch_profile": sketch,
                        "depth_mm": thickness,
                        "center_x_mm": cx + offset_x,
                        "center_y_mm": cy + offset_y,
                    },
                    param_provenance={
                        **slot_provenance,
                        "sketch_profile": ParamProvenance(
                            origin="stated",
                            detail=(
                                f"паз {length:g}×{width_mm:g} повёрнут на "
                                f"{rotation_deg:g}° по прочитанному углу"
                            ),
                        ),
                    },
                    confidence=0.8,
                )
            )
            continue
        if straight > 0:
            pocket_params = {
                "profile": "rectangle",
                "width_mm": straight,
                "height_mm": width_mm,
                "center_x_mm": cx,
                "center_y_mm": cy,
                "depth_mm": thickness,
            }
            features.append(
                Feature3D(
                    kind="pocket",
                    source_feature_ids=_source_feature_ids(slot),
                    params=pocket_params,
                    param_provenance=provenance_of(pocket_params),
                    confidence=0.8,
                )
            )
        # The capsule ends: a slot is a rectangle plus a round at each end.
        for offset in (-straight / 2.0, straight / 2.0):
            end_params = {
                "diameter_mm": width_mm,
                "center_x_mm": cx + offset,
                "center_y_mm": cy,
                "through": True,
            }
            features.append(
                Feature3D(
                    kind="hole",
                    source_feature_ids=_source_feature_ids(slot),
                    params=end_params,
                    param_provenance=provenance_of(end_params),
                    confidence=0.8,
                )
            )
    if len(features) == 1:
        missing.append("на профиле не прочитано ни одного отверстия или паза")
    label = str(spec.get("part") or "Пластина") + " — по прочитанному контуру и толщине"
    return FeatureTreeCandidate(
        features=features,
        score=0.85,
        label=label[:500],
        missing_data=missing,
    )


def _one_rotation_body_features(body: dict) -> tuple[list[Feature3D], list[str]] | None:
    """revolve + threads + cuts for ONE rotation body, every feature tagged
    with this body's ``body_index`` (Ф2.1). Returns None when the body is
    incomplete or contradictory — the caller refuses the whole candidate
    rather than guess around a missing/broken body.
    """
    body_index = int(body.get("body_index") or 0)
    outer = body.get("outer") or []
    if _sections_are_complete(outer):
        outer_guess_notes: list[str] = []
    else:
        outer_filled = _fill_provisional_step_lengths(outer)
        if outer_filled is None:
            return None
        outer, outer_guess_notes = outer_filled
    bore = body.get("bore") or []
    bore_guess_notes: list[str] = []
    bore_unusable_note: str | None = None
    if bore and not _sections_are_complete(bore):
        bore_filled = _fill_provisional_step_lengths(bore)
        if bore_filled is None:
            # Unlike a step in a multi-section OUTER profile, a bore with NO
            # known length anywhere in it (a real live case: one Ø15.7
            # section, no length at all) has nothing honest to interpolate
            # a depth from — a cavity could plausibly be 5mm or 300mm deep.
            # Build the solid WITHOUT the bore instead of refusing the whole
            # part: the SAME already-existing, already-reviewable outcome as
            # a sheet with no section view at all (see the "else" branch
            # below) — never a fabricated depth for a hollow.
            bore_unusable_note = (
                "расточка прочитана без длины и не может быть построена даже "
                "предположительно (нет других участков расточки для оценки) "
                "— деталь построена сплошной, полость не учтена"
            )
            bore = []
        else:
            bore, bore_guess_notes = bore_filled

    params: dict[str, Any] = {"profile_points": _profile_points(outer)}
    provenance = {
        "profile_points": ParamProvenance(
            origin="guessed" if outer_guess_notes else "stated",
            detail=(
                "; ".join(outer_guess_notes)
                if outer_guess_notes
                else "диаметры и длины ступеней прочитаны с чертежа (outer[])"
            ),
        )
    }
    missing: list[str] = [*outer_guess_notes, *bore_guess_notes]
    if bore_unusable_note:
        missing.append(bore_unusable_note)
    bore_offset = 0.0
    if bore:
        bore_points = _profile_points(bore)
        outer_max_r = max(point["r"] for point in params["profile_points"])
        if max(point["r"] for point in bore_points) >= outer_max_r:
            # Contradictory read; refuse rather than "fix" it into a solid.
            return None
        params["bore_points"] = bore_points
        provenance["bore_points"] = ParamProvenance(
            origin="guessed" if bore_guess_notes else "stated",
            detail=(
                "; ".join(bore_guess_notes)
                if bore_guess_notes
                else "внутренний контур прочитан с разреза (bore[])"
            ),
        )
        bore_length = sum(float(section["l"]) for section in bore)
        outer_length = sum(float(section["l"]) for section in outer)
        bore_start = _num(body.get("bore_start_mm")) or 0.0
        if body.get("bore_from_end") == "right":
            bore_offset = outer_length - bore_start - bore_length
        else:
            bore_offset = bore_start
        if bore_offset < -1e-6 or bore_offset + bore_length > outer_length + 1e-6:
            return None
        for point in bore_points:
            point["z"] += bore_offset
        if bore_length > outer_length + 1e-6:
            missing.append("расточка длиннее детали — проверьте прочитанные длины")
    elif not bore_unusable_note:
        missing.append("разрез не прочитан: деталь построена сплошной, полость не учтена")

    features = [
        Feature3D(
            kind="revolve",
            source_feature_ids=_source_feature_ids(*outer, *bore),
            params=params,
            param_provenance=provenance,
            confidence=0.9,
            body_index=body_index,
        )
    ]

    def append_threads(sections: list[dict], *, start_offset: float, internal: bool) -> None:
        axial_start = start_offset
        for section in sections:
            thread = section.get("thread") or {}
            designation = str(thread.get("designation") or thread.get("spec") or "").strip()
            if designation:
                diameter = _num(thread.get("nominal_diameter_mm")) or _num(section.get("d"))
                if diameter:
                    thread_params: dict[str, Any] = {
                        "spec": designation,
                        "diameter_mm": diameter,
                        "axial_start_mm": axial_start,
                        "length_mm": float(section.get("l") or 0.0),
                        "internal": internal,
                    }
                    pitch = _num(thread.get("pitch_mm"))
                    if pitch:
                        thread_params["pitch_mm"] = pitch
                    features.append(
                        Feature3D(
                            kind="thread",
                            source_feature_ids=_source_feature_ids(section),
                            params=thread_params,
                            param_provenance={
                                "spec": ParamProvenance(
                                    origin="stated",
                                    detail="обозначение резьбы прочитано с чертежа",
                                ),
                                "diameter_mm": ParamProvenance(
                                    origin="stated", detail="номинальный диаметр резьбы"
                                ),
                                "axial_start_mm": ParamProvenance(
                                    origin="propagated", detail="начало резьбовой ступени"
                                ),
                                "length_mm": ParamProvenance(
                                    origin="propagated", detail="длина резьбовой ступени"
                                ),
                                "internal": ParamProvenance(
                                    origin="propagated",
                                    detail="внутренняя — на расточке, наружная — на ступени",
                                ),
                                "pitch_mm": ParamProvenance(
                                    origin="stated" if pitch else "standard",
                                    detail=(
                                        "шаг указан в обозначении"
                                        if pitch
                                        else "крупный шаг метрической резьбы по стандарту"
                                    ),
                                ),
                            },
                            confidence=0.85,
                            body_index=body_index,
                        )
                    )
            axial_start += float(section.get("l") or 0.0)

    append_threads(outer, start_offset=0.0, internal=False)
    append_threads(bore, start_offset=bore_offset if bore else 0.0, internal=True)
    for cut in _cut_features(body, outer, missing):
        cut.body_index = body_index
        features.append(cut)
    total = sum(float(section.get("l") or 0.0) for section in outer)
    for index, groove in enumerate(body.get("face_grooves") or []):
        feature = _face_groove_feature(groove, total) if isinstance(groove, dict) else None
        if feature is None:
            missing.append(f"выточка на торце {index + 1}: размеры не прочитаны — не построена")
            continue
        feature.body_index = body_index
        features.append(feature)
    for index, flange in enumerate(body.get("flanges") or []):
        flange_features = _flange_features(flange) if isinstance(flange, dict) else None
        if flange_features is None:
            missing.append(f"фланец {index + 1}: контур или отверстия не прочитаны — не построен")
            continue
        for feature in flange_features:
            feature.body_index = body_index
            features.append(feature)
    return features, missing


def _face_groove_feature(groove: dict, total_length: float) -> Feature3D | None:
    """Кольцевая выточка на торце: карман-кольцо на станции от торца."""
    depth = _num(groove.get("depth_mm"))
    outer = _num(groove.get("outer_diameter_mm"))
    inner = _num(groove.get("inner_diameter_mm"))
    if not depth or not outer or inner is None or inner >= outer or total_length <= depth:
        return None
    station = 0.0 if groove.get("end", "left") == "left" else total_length - depth
    stated = ParamProvenance(origin="stated", detail="выточка на торце с чертежа")
    return Feature3D(
        kind="pocket",
        source_feature_ids=_source_feature_ids(groove),
        params={
            "profile": "circle",
            "diameter_mm": outer,
            "inner_diameter_mm": inner,
            "center_x_mm": 0.0,
            "center_y_mm": 0.0,
            "depth_mm": depth,
            "axial_start_mm": station,
        },
        param_provenance={
            "profile": ParamProvenance(origin="standard", detail="кольцевая выточка"),
            "diameter_mm": stated,
            "inner_diameter_mm": stated,
            "depth_mm": stated,
            "center_x_mm": ParamProvenance(origin="standard", detail="на оси детали"),
            "center_y_mm": ParamProvenance(origin="standard", detail="на оси детали"),
            "axial_start_mm": ParamProvenance(
                origin="propagated", detail="от торца: 0 или длина минус глубина"
            ),
        },
        confidence=0.85,
    )


def _flange_features(flange: dict) -> list[Feature3D] | None:
    """A flange across a turned body: a boss at its axial station, then its
    holes as pockets through the flange thickness at the same station.

    Coordinates are from the axis (the kernel's frame for a revolve base);
    a sketch outline is placed by ``sketch_origin_mm``. Every parameter has
    a source — an unsourced one reads as guessed and makes the part a draft.
    """
    station = _num(flange.get("axial_start_mm"))
    thickness = _num(flange.get("thickness_mm"))
    profile = flange.get("profile")
    if station is None or not thickness or not isinstance(profile, dict):
        return None
    common = {
        "depth_mm": ParamProvenance(origin="stated", detail="толщина фланца с чертежа"),
        "axial_start_mm": ParamProvenance(
            origin="stated", detail="положение фланца по оси с чертежа"
        ),
    }
    shape = profile.get("shape")
    params: dict[str, Any] = {"depth_mm": thickness, "axial_start_mm": station}
    provenance = dict(common)
    if shape == "circle":
        diameter = _num(profile.get("diameter_mm"))
        if not diameter:
            return None
        params.update(profile="circle", diameter_mm=diameter, center_x_mm=0.0, center_y_mm=0.0)
        provenance["diameter_mm"] = ParamProvenance(origin="stated", detail="Ø фланца с чертежа")
    elif shape == "rectangle":
        width, height = _num(profile.get("width_mm")), _num(profile.get("height_mm"))
        if not width or not height:
            return None
        params.update(
            profile="rectangle", width_mm=width, height_mm=height, center_x_mm=0.0, center_y_mm=0.0
        )
        provenance["width_mm"] = ParamProvenance(origin="stated", detail="габарит фланца")
        provenance["height_mm"] = ParamProvenance(origin="stated", detail="габарит фланца")
    elif shape == "sketch":
        sketch = profile.get("sketch")
        closure = _sketch_closure_error(sketch) if sketch else None
        origin = flange.get("sketch_origin_mm") or (0.0, 0.0)
        if closure is None or closure > _SKETCH_CLOSURE_TOLERANCE_MM:
            return None
        params.update(
            profile="sketch",
            sketch_profile=sketch,
            center_x_mm=float(origin[0]),
            center_y_mm=float(origin[1]),
        )
        provenance["sketch_profile"] = ParamProvenance(
            origin="stated", detail="контур фланца — линии и дуги с чертежа"
        )
    else:
        return None
    provenance.setdefault(
        "profile", ParamProvenance(origin="standard", detail="форма контура фланца")
    )
    for axis in ("center_x_mm", "center_y_mm"):
        provenance.setdefault(axis, ParamProvenance(origin="stated", detail="от оси детали"))
    features = [Feature3D(kind="boss", params=params, param_provenance=provenance, confidence=0.85)]
    holes = _expanded_profile_holes(profile)
    if holes is None:
        return None
    for hole in holes:
        diameter = _num(hole.get("diameter_mm"))
        x, y = _num(hole.get("center_x_mm")), _num(hole.get("center_y_mm"))
        if not diameter or x is None or y is None:
            return None
        features.append(
            Feature3D(
                kind="pocket",
                source_feature_ids=_source_feature_ids(hole),
                params={
                    "profile": "circle",
                    "diameter_mm": diameter,
                    "center_x_mm": x,
                    "center_y_mm": y,
                    "depth_mm": thickness,
                    "axial_start_mm": station,
                },
                param_provenance={
                    **common,
                    "profile": ParamProvenance(origin="standard", detail="отверстие фланца"),
                    "diameter_mm": ParamProvenance(origin="stated", detail="Ø отверстия с чертежа"),
                    "center_x_mm": ParamProvenance(origin="stated", detail="от оси детали"),
                    "center_y_mm": ParamProvenance(origin="stated", detail="от оси детали"),
                },
                confidence=0.85,
            )
        )
    return features


def _rotation_feature_tree(spec: dict) -> FeatureTreeCandidate | None:
    """Build a revolve feature tree from a rotation-body spec.

    Every body the sheet reads (``parts[]``, or ``main_view`` alone when
    there is no ``parts[]``) is compiled into its own independent
    Feature3D subtree, tagged with its ``body_index`` — the kernel builds
    each as its own solid rather than only the first (Ф2.1). Returns None
    when any body is incomplete — the caller falls back to the 2D-only
    result rather than build some bodies and silently drop others.
    """
    parts = _rotation_parts(spec)
    if not parts:
        return None

    features: list[Feature3D] = []
    missing: list[str] = []
    for body in parts:
        built = _one_rotation_body_features(body)
        if built is None:
            return None
        body_features, body_missing = built
        features.extend(body_features)
        missing.extend(body_missing)

    if len(parts) > 1:
        # X3: размещение тела — параметр его базовой операции (первое тело —
        # опорное). Не прочитанное — строится раздельно, с замечанием.
        unplaced = 0
        for body in parts[1:]:
            placement = body.get("placement")
            if isinstance(placement, dict) and placement.get("position_mm"):
                body_index = int(body.get("body_index") or 0)
                base = next(
                    (
                        feature
                        for feature in features
                        if feature.body_index == body_index and feature.kind == "revolve"
                    ),
                    None,
                )
                if base is not None:
                    base.params["placement"] = {
                        "position_mm": [float(v) for v in placement["position_mm"]],
                        "axis": [float(v) for v in placement.get("axis") or [0.0, 0.0, 1.0]],
                        "angle_deg": float(placement.get("angle_deg") or 0.0),
                    }
                    base.param_provenance["placement"] = ParamProvenance(
                        origin="stated", detail="размещение тела прочитано с листа"
                    )
                    continue
            unplaced += 1
        if unplaced:
            missing.append(
                f"на листе прочитано тел: {len(parts)}; взаимное расположение {unplaced} "
                "из них не прочитано, построены раздельно"
            )

    label = str(spec.get("part") or "Тело вращения") + " — revolve по прочитанному профилю"
    return FeatureTreeCandidate(
        features=features,
        score=0.9,
        label=label[:500],
        missing_data=missing,
    )


def _section_starts(outer: list[dict]) -> list[float]:
    """Axial position where each section begins, from the left face."""
    starts: list[float] = []
    z = 0.0
    for section in outer:
        starts.append(z)
        z += float(section.get("l") or 0.0)
    return starts


_METRIC_COARSE_PITCH_MM = {
    1.0: 0.25,
    1.2: 0.25,
    1.4: 0.3,
    1.6: 0.35,
    1.8: 0.35,
    2.0: 0.4,
    2.5: 0.45,
    3.0: 0.5,
    3.5: 0.6,
    4.0: 0.7,
    5.0: 0.8,
    6.0: 1.0,
    8.0: 1.25,
    10.0: 1.5,
    12.0: 1.75,
    14.0: 2.0,
    16.0: 2.0,
    18.0: 2.5,
    20.0: 2.5,
    22.0: 2.5,
    24.0: 3.0,
    27.0: 3.0,
    30.0: 3.5,
    33.0: 3.5,
    36.0: 4.0,
}


def metric_thread_geometry(thread: dict) -> dict[str, float | str] | None:
    """Finished ISO metric internal-thread geometry, not a tap-drill guess.

    A drawing designation such as M8 defines the basic thread profile even
    when a workshop drill size is intentionally absent. For the deterministic
    B-Rep cut we use the ISO basic internal minor diameter D1 = D - 1.082532P.
    The drill selected by manufacturing may differ and is outside this model.
    """
    designation = str(thread.get("designation") or "").replace("М", "M")
    nominal = _num(thread.get("nominal_diameter_mm"))
    if nominal is None:
        match = re.search(r"M\s*(\d+(?:[.,]\d+)?)", designation, re.IGNORECASE)
        nominal = _num(match.group(1)) if match else None
    if nominal is None or not designation.upper().startswith("M"):
        return None
    pitch = _num(thread.get("pitch_mm"))
    pitch_source = "stated"
    if pitch is None:
        pitch = _METRIC_COARSE_PITCH_MM.get(float(nominal))
        pitch_source = "standard"
    if pitch is None:
        return None
    minor = nominal - 1.082532 * pitch
    if minor <= 0:
        return None
    return {
        "nominal_diameter_mm": nominal,
        "pitch_mm": pitch,
        "minor_diameter_mm": round(minor, 6),
        "pitch_source": pitch_source,
    }


def _cut_features(body: dict, outer: list[dict], missing: list[str]) -> list[Feature3D]:
    """Grooves, keyways, cross holes and edge work, as kernel operations.

    Everything here was READ off the sheet: the sizes are the drawing's own, and
    the only thing computed is where an edge sits, because the reader states a
    place ("the shoulder at Ø80") and the kernel needs an edge. A feature whose
    position cannot be resolved is declared in ``missing_data`` rather than
    placed somewhere plausible — a chamfer on the wrong shoulder is a part that
    looks right and is not.
    """
    features: list[Feature3D] = []
    total_length = sum(float(section.get("l") or 0.0) for section in outer)
    starts = _section_starts(outer)

    for groove in body.get("grooves") or []:
        position = _num(groove.get("axial_position_mm"))
        width = _num(groove.get("width_mm"))
        if position is None or not width:
            missing.append("канавка без положения или ширины — не построена")
            continue
        params: dict[str, Any] = {
            "axial_position_mm": position,
            "width_mm": width,
            "internal": bool(groove.get("internal")),
        }
        depth, root = _num(groove.get("depth_mm")), _num(groove.get("root_diameter_mm"))
        if depth:
            params["depth_mm"] = depth
        elif root:
            params["root_diameter_mm"] = root
        else:
            missing.append("канавка без глубины — не построена")
            continue
        features.append(
            Feature3D(
                kind="groove",
                source_feature_ids=_source_feature_ids(groove),
                params=params,
                param_provenance={
                    "axial_position_mm": ParamProvenance(
                        origin="stated", detail="положение канавки прочитано с чертежа"
                    ),
                },
                confidence=0.85,
            )
        )

    for keyway in body.get("keyways") or []:
        start = _num(keyway.get("axial_start_mm"))
        length = _num(keyway.get("length_mm"))
        width = _num(keyway.get("width_mm"))
        depth = _num(keyway.get("depth_mm"))
        if start is None or not (length and width and depth):
            missing.append("шпоночный паз прочитан не полностью — не построен")
            continue
        features.append(
            Feature3D(
                kind="keyway",
                source_feature_ids=_source_feature_ids(keyway),
                params={
                    "axial_start_mm": start,
                    "length_mm": length,
                    "width_mm": width,
                    "depth_mm": depth,
                    "angle_deg": _num(keyway.get("angle_deg")) or 0.0,
                    "end_type": keyway.get("end_type") or "closed",
                },
                # Каждый параметр — с происхождением: без него граф считает
                # значение угаданным, и проверенный по листу паз уводил всю
                # сборку в черновик (живой z4-r4).
                param_provenance={
                    "axial_start_mm": ParamProvenance(
                        origin="stated", detail="положение паза с чертежа"
                    ),
                    "length_mm": ParamProvenance(origin="stated", detail="длина паза с чертежа"),
                    "width_mm": ParamProvenance(origin="stated", detail="ширина паза с чертежа"),
                    "depth_mm": ParamProvenance(
                        origin="stated", detail="глубина паза t1 с чертежа"
                    ),
                    "angle_deg": ParamProvenance(
                        origin="stated"
                        if _num(keyway.get("angle_deg")) is not None
                        else "standard",
                        detail=(
                            "угловое положение паза с чертежа"
                            if _num(keyway.get("angle_deg")) is not None
                            else "паз лицом к главному виду"
                        ),
                    ),
                    "end_type": ParamProvenance(
                        origin="stated" if keyway.get("end_type") else "standard",
                        detail=(
                            "исполнение паза с чертежа"
                            if keyway.get("end_type")
                            else "закрытый призматический паз по ГОСТ 23360"
                        ),
                    ),
                },
                confidence=0.85,
            )
        )

    for hole in body.get("cross_holes") or []:
        diameter = _num(hole.get("diameter_mm"))
        position = _num(hole.get("axial_position_mm"))
        if not diameter or position is None:
            missing.append("поперечное отверстие прочитано не полностью — не построено")
            continue
        count = int(hole.get("count") or 1)
        spacing = _num(hole.get("spacing_deg"))
        base_angle = _num(hole.get("angle_deg")) or 0.0
        step = spacing if spacing else (360.0 / count if count > 1 else 0.0)
        for index in range(max(1, count)):
            params = {
                "axis": "radial",
                "diameter_mm": diameter,
                "axial_position_mm": position,
                "angle_deg": base_angle + index * step,
                "center_x_mm": 0.0,
                "center_y_mm": 0.0,
            }
            through = hole.get("through")
            if through is not None:
                params["through"] = bool(through)
            depth = _num(hole.get("depth_mm"))
            if depth and through is False:
                params["depth_mm"] = depth
            features.append(
                Feature3D(
                    kind="hole",
                    source_feature_ids=_source_feature_ids(hole),
                    params=params,
                    param_provenance={
                        "diameter_mm": ParamProvenance(
                            origin="stated", detail="Ø поперечного отверстия с чертежа"
                        ),
                    },
                    confidence=0.8,
                )
            )
            counterbore_diameter = _num(hole.get("counterbore_diameter_mm"))
            counterbore_depth = _num(hole.get("counterbore_depth_mm"))
            if counterbore_diameter and counterbore_depth:
                features.append(
                    Feature3D(
                        kind="hole",
                        source_feature_ids=_source_feature_ids(hole),
                        params={
                            "axis": "radial",
                            "diameter_mm": counterbore_diameter,
                            "axial_position_mm": position,
                            "angle_deg": base_angle + index * step,
                            "center_x_mm": 0.0,
                            "center_y_mm": 0.0,
                            "through": False,
                            "depth_mm": counterbore_depth,
                        },
                        param_provenance={
                            "diameter_mm": ParamProvenance(
                                origin="stated",
                                detail="Ø цековки поперечного отверстия с чертежа",
                            ),
                            "depth_mm": ParamProvenance(
                                origin="stated",
                                detail="глубина цековки с чертежа",
                            ),
                        },
                        confidence=0.8,
                    )
                )

    for pattern in body.get("axial_holes") or []:
        count = int(pattern.get("count") or 0)
        pcd = _num(pattern.get("bolt_circle_diameter_mm"))
        pilot = _num(pattern.get("pilot_diameter_mm"))
        from_face = pattern.get("from_face")
        entry_offset = _num(pattern.get("entry_offset_mm")) or 0.0
        entry_recess_diameter = _num(pattern.get("entry_recess_diameter_mm"))
        through = pattern.get("through")
        legacy_depth = _num(pattern.get("depth_mm"))
        drill_depth = _num(pattern.get("drill_depth_mm")) or legacy_depth
        thread_depth = _num(pattern.get("thread_depth_mm")) or legacy_depth
        thread = pattern.get("thread") or {}
        designation = str(thread.get("designation") or "")
        nominal = _num(thread.get("nominal_diameter_mm"))
        thread_geometry = metric_thread_geometry(thread)
        incomplete = []
        if count < 1 or pcd is None:
            incomplete.append("количество/делительная окружность")
        if from_face not in {"zmin", "zmax"}:
            incomplete.append("торец")
        if through is None:
            incomplete.append("сквозное/глухое исполнение")
        if through is False and drill_depth is None:
            incomplete.append("глубина сверления")
        if entry_offset > 0 and entry_recess_diameter is None:
            incomplete.append("Ø входной выборки")
        if not designation or nominal is None:
            incomplete.append("резьба")
        if pilot is None and thread_geometry is None:
            incomplete.append("профиль резьбы/шаг")
        if incomplete:
            missing.append(
                "осевой шаблон отверстий прочитан не полностью ("
                + ", ".join(incomplete)
                + ") — не построен"
            )
            continue
        spacing = _num(pattern.get("spacing_deg"))
        start_angle = _num(pattern.get("start_angle_deg")) or 0.0
        step = spacing if spacing is not None else 360.0 / count

        cut_diameter = pilot or float(thread_geometry["minor_diameter_mm"])
        pitch = _num(thread.get("pitch_mm"))
        if pitch is None and thread_geometry is not None:
            pitch = float(thread_geometry["pitch_mm"])
        diameter_origin = "stated" if pilot is not None else "standard"
        diameter_detail = (
            "Ø подготовки явно указан на чертеже"
            if pilot is not None
            else "основной внутренний диаметр D1 выведен из стандартного профиля резьбы"
        )

        for index in range(count):
            angle = start_angle + index * step
            radius = pcd / 2.0
            center_x = radius * math.cos(math.radians(angle))
            center_y = radius * math.sin(math.radians(angle))
            if entry_offset > 0 and entry_recess_diameter is not None:
                features.append(
                    Feature3D(
                        kind="hole",
                        source_feature_ids=_source_feature_ids(pattern),
                        params={
                            "axis": "z",
                            "diameter_mm": entry_recess_diameter,
                            "center_x_mm": round(center_x, 6),
                            "center_y_mm": round(center_y, 6),
                            "through": False,
                            "from_face": from_face,
                            "entry_offset_mm": 0.0,
                            "depth_mm": entry_offset,
                            "role": "entry_recess",
                        },
                        param_provenance={
                            "diameter_mm": ParamProvenance(
                                origin="stated",
                                detail="Ø входной выборки перед осевым резьбовым отверстием",
                            ),
                            "depth_mm": ParamProvenance(
                                origin="measured",
                                detail="глубина выборки измерена по продольному векторному контуру",
                            ),
                        },
                        confidence=0.78,
                    )
                )
            params: dict[str, Any] = {
                "axis": "z",
                "diameter_mm": cut_diameter,
                "center_x_mm": round(center_x, 6),
                "center_y_mm": round(center_y, 6),
                "through": bool(through),
                "from_face": from_face,
                "entry_offset_mm": entry_offset,
            }
            if through is False:
                params["depth_mm"] = drill_depth
            features.append(
                Feature3D(
                    kind="hole",
                    source_feature_ids=_source_feature_ids(pattern),
                    params=params,
                    param_provenance={
                        "diameter_mm": ParamProvenance(
                            origin=diameter_origin,
                            detail=diameter_detail,
                        ),
                        "center_x_mm": ParamProvenance(
                            origin="propagated",
                            detail="координата из прочитанной делительной окружности",
                        ),
                        "center_y_mm": ParamProvenance(
                            origin="propagated",
                            detail="координата из прочитанной делительной окружности",
                        ),
                        "entry_offset_mm": ParamProvenance(
                            origin="measured" if entry_offset else "propagated",
                            detail=(
                                "смещённая входная плоскость измерена по векторному контуру продольного разреза"
                                if entry_offset
                                else "вход на крайнем торце"
                            ),
                        ),
                    },
                    confidence=0.82,
                )
            )
            thread_params: dict[str, Any] = {
                "spec": designation,
                "diameter_mm": nominal,
                "internal": True,
                "center_x_mm": round(center_x, 6),
                "center_y_mm": round(center_y, 6),
                "from_face": from_face,
                "entry_offset_mm": entry_offset,
            }
            if pitch is not None:
                thread_params["pitch_mm"] = pitch
            if thread_depth is not None:
                thread_params["length_mm"] = thread_depth
            features.append(
                Feature3D(
                    kind="thread",
                    source_feature_ids=_source_feature_ids(pattern),
                    params=thread_params,
                    param_provenance={
                        "spec": ParamProvenance(
                            origin="stated", detail="обозначение резьбы с торцевого вида"
                        ),
                        "pitch_mm": ParamProvenance(
                            origin=(
                                "stated" if _num(thread.get("pitch_mm")) is not None else "standard"
                            ),
                            detail=(
                                "шаг указан в обозначении"
                                if _num(thread.get("pitch_mm")) is not None
                                else "крупный шаг метрической резьбы по стандарту"
                            ),
                        ),
                        "center_x_mm": ParamProvenance(
                            origin="propagated",
                            detail="центр на прочитанной делительной окружности",
                        ),
                        "center_y_mm": ParamProvenance(
                            origin="propagated",
                            detail="центр на прочитанной делительной окружности",
                        ),
                    },
                    confidence=0.82,
                )
            )

    for pattern in body.get("circular_hole_patterns") or []:
        count = int(pattern.get("count") or 0)
        diameter = _num(pattern.get("hole_diameter_mm"))
        pcd = _num(pattern.get("bolt_circle_diameter_mm"))
        start_angle = _num(pattern.get("start_angle_deg"))
        spacing = _num(pattern.get("spacing_deg"))
        from_face = pattern.get("from_face")
        through = pattern.get("through")
        depth = _num(pattern.get("depth_mm"))
        entry_offset = _num(pattern.get("entry_offset_mm")) or 0.0
        axis_mode = pattern.get("axis_mode")
        inclination = _num(pattern.get("inclination_deg"))
        radial_direction = pattern.get("radial_direction")
        incomplete = []
        if count < 1 or diameter is None or pcd is None:
            incomplete.append("количество/Ø/делительная окружность")
        if start_angle is None:
            incomplete.append("угловая фаза")
        if from_face not in {"zmin", "zmax"}:
            incomplete.append("торец")
        if through is None:
            incomplete.append("сквозное/глухое исполнение")
        if through is False and depth is None:
            incomplete.append("глубина")
        if axis_mode == "inclined" and (
            inclination is None or radial_direction not in {"outward", "inward"}
        ):
            incomplete.append("наклон/радиальное направление")
        if axis_mode not in {"axial", "inclined"}:
            incomplete.append("тип оси")
        if incomplete:
            missing.append(
                f"массив {count}×Ø{diameter or 0:g} прочитан не полностью ("
                + ", ".join(incomplete)
                + ") — не построен"
            )
            continue

        step = spacing if spacing is not None else 360.0 / count
        for index in range(count):
            angle = float(start_angle) + index * step
            radius = float(pcd) / 2.0
            center_x = radius * math.cos(math.radians(angle))
            center_y = radius * math.sin(math.radians(angle))
            params: dict[str, Any] = {
                "axis": "z" if axis_mode == "axial" else "inclined",
                "diameter_mm": diameter,
                "center_x_mm": round(center_x, 6),
                "center_y_mm": round(center_y, 6),
                "through": bool(through),
                "from_face": from_face,
                "entry_offset_mm": entry_offset,
                "pattern_angle_deg": angle,
            }
            if through is False:
                params["depth_mm"] = depth
            if axis_mode == "inclined":
                params.update(
                    {
                        "inclination_deg": inclination,
                        "radial_direction": radial_direction,
                    }
                )
            features.append(
                Feature3D(
                    kind="hole",
                    source_feature_ids=_source_feature_ids(pattern),
                    params=params,
                    param_provenance={
                        "diameter_mm": ParamProvenance(
                            origin="stated", detail="Ø группового отверстия с разреза"
                        ),
                        "center_x_mm": ParamProvenance(
                            origin="propagated", detail="координата из PCD и угловой фазы"
                        ),
                        "center_y_mm": ParamProvenance(
                            origin="propagated", detail="координата из PCD и угловой фазы"
                        ),
                    },
                    confidence=0.78,
                )
            )

    features.extend(_edge_features(body, outer, starts, total_length, missing))
    return features


def _edge_features(
    body: dict,
    outer: list[dict],
    starts: list[float],
    total_length: float,
    missing: list[str],
) -> list[Feature3D]:
    """Chamfers and fillets, each pointed at the edge the sheet means."""
    features: list[Feature3D] = []
    for kind, items, size_key in (
        ("chamfer", body.get("chamfers") or [], "size_mm"),
        ("fillet", body.get("fillets") or [], "radius_mm"),
    ):
        for index, item in enumerate(items):
            size = _num(item.get(size_key))
            if not size:
                missing.append(f"{kind}[{index}] без размера — не построен")
                continue
            selector = _edge_selector(item, outer, starts, total_length)
            if selector is None:
                # C: the index is embedded so the editor can parse WHICH
                # chamfers[i]/fillets[i] row to repair — a human clicking the
                # real edge in the 3D viewport supplies at_z_mm/at_diameter_mm
                # directly, the same fields this selector reads below.
                missing.append(
                    f"{kind}[{index}]: не удалось определить ребро "
                    f"({item.get('location')}) — не построен"
                )
                continue
            params = {"size_mm": size, "edge_selector": selector}
            if kind == "chamfer" and _num(item.get("angle_deg")):
                params["angle_deg"] = _num(item.get("angle_deg"))
            features.append(
                Feature3D(
                    kind=kind,
                    source_feature_ids=_source_feature_ids(item),
                    params=params,
                    param_provenance={
                        "size_mm": ParamProvenance(
                            origin="stated", detail=f"размер {kind} с чертежа"
                        ),
                        "edge_selector": ParamProvenance(
                            origin="propagated",
                            detail=f"ребро по месту на детали ({item.get('location')})",
                        ),
                        **(
                            {
                                "angle_deg": ParamProvenance(
                                    origin="stated", detail="угол фаски с чертежа"
                                )
                            }
                            if "angle_deg" in params
                            else {}
                        ),
                    },
                    confidence=0.75,
                )
            )
    return features


def _edge_selector(
    item: dict, outer: list[dict], starts: list[float], total_length: float
) -> dict | None:
    """Turn "the shoulder at Ø80" into something the kernel can resolve.

    The reader names a PLACE, because an edge id exists only once a solid does.
    Here that place becomes an axial position and a diameter; the kernel matches
    it against the shape as it stands, with every preceding cut applied.
    """
    location = str(item.get("location") or "")
    at_z = _num(item.get("at_z_mm"))
    at_diameter = _num(item.get("at_diameter_mm"))

    if location == "left_end":
        first = outer[0] if outer else {}
        return {
            "curve": "Circle",
            "at_z_mm": 0.0,
            "diameter_mm": at_diameter or _num(first.get("d")),
        }
    if location == "right_end":
        last = outer[-1] if outer else {}
        end_diameter = taper_end_diameter(last) if last else None
        return {
            "curve": "Circle",
            "at_z_mm": total_length,
            "diameter_mm": at_diameter or end_diameter or _num(last.get("d")),
        }
    # "bore_mouth" (chamfer) and "bore" (fillet — SpecFillet's own location
    # literal has no "bore_mouth" spelling) name the same edge; both resolve
    # the same way once a position is known.
    if location in ("shoulder", "bore_mouth", "bore"):
        if at_z is not None:
            return {
                "curve": "Circle",
                "at_z_mm": at_z,
                **({"diameter_mm": at_diameter} if at_diameter else {}),
            }
        if at_diameter:
            # The shoulder where a step of this diameter meets its neighbour.
            for index, section in enumerate(outer):
                if _num(section.get("d")) == at_diameter and index + 1 < len(outer):
                    return {
                        "curve": "Circle",
                        "at_z_mm": starts[index] + float(section.get("l") or 0.0),
                        "diameter_mm": at_diameter,
                    }
        return None
    return None


def verify_solid_against_spec(
    report: dict,
    spec: dict,
    candidate: FeatureTreeCandidate | None = None,
    *,
    require_envelope_match: bool = True,
) -> SolidVerification:
    """Does the built solid measure what the sheet said?

    Checks the two quantities a revolve cannot fake: overall length along the
    axis and the largest diameter. Tolerance is 0.5% — the same window the 2D
    dimension check uses, because both answer the same question (did the
    builder honour the numbers it was given?).

    ``require_envelope_match=False`` (Ф3 нового CAD-редактора,
    add_feature_to_graph) reports length/diameter/volume-above-profile
    exactly as always, but excludes them from the blocking ``ok`` verdict.
    Those three checks assume the built solid is EXACTLY the read profile
    plus its declared cuts — an assumption a human-added feature (a boss on
    the end face growing the envelope; any additive feature growing volume
    above the profile) breaks on purpose, not by a reading error. Topology
    (a real, valid, manifold solid) and feature_complete (every OTHER
    declared operation still built) remain blocking regardless — this never
    waves through a broken build, only a legitimately bigger one.
    """
    if isinstance((spec.get("main_view") or {}).get("sheet_metal"), dict):
        return _verify_sheet_metal(spec, report, require_envelope_match=require_envelope_match)
    if (
        spec.get("welds")
        or len([p for p in spec.get("parts") or [] if isinstance(p, dict) and p.get("profile")]) > 1
    ):
        return _verify_weldment(spec, report, require_envelope_match=require_envelope_match)
    parts = _rotation_parts(spec)
    if not parts:
        return _verify_prismatic(spec, report, require_envelope_match=require_envelope_match)
    outer = parts[0].get("outer") or []
    # A2: when a step's length was provisionally filled in (ParamProvenance.
    # origin="guessed" — see _fill_provisional_step_lengths), the raw spec's
    # OWN outer[] still has that section's "l" missing, so summing only the
    # STATED lengths here would under-count against what was actually built
    # and this check would fail-closed reject every guessed-length preview
    # outright. The compiled revolve's own profile_points is the single
    # source of truth for what length was actually asked of the kernel —
    # reading the total from there is exact for a normal fully-stated build
    # too (same arithmetic), and correct for a guessed one.
    revolve = next(
        (
            feature
            for feature in (candidate.features if candidate else [])
            if feature.kind == "revolve"
        ),
        None,
    )
    profile_points = (revolve.params.get("profile_points") if revolve else None) or []
    stated_length = (
        float(profile_points[-1]["z"])
        if profile_points
        else sum(float(section["l"]) for section in outer if section.get("l"))
    )
    stated_diameter = max((float(section["d"]) for section in outer), default=0.0)
    # Фланец поперёк оси (бобышка на станции) выходит за Ø профиля и добавляет
    # объём: без него верная втулка part_03 (27,4 × 24 поперёк оси при Ø16)
    # отклонялась как «размеры не совпали».
    flange_extent, flange_volume = _flange_envelope(candidate, profile_points)
    stated_diameter = max(stated_diameter, flange_extent)
    # A2: candidate is the only trustworthy source once it exists — its
    # bore_points is correctly empty when an unguessable bore was omitted
    # (a real production case), whereas the raw spec's bore[] still has that
    # section with "l": None and would crash _profile_volume_mm3. Only fall
    # back to the raw spec when there is no candidate at all (a test
    # convenience; the one production caller always passes one).
    bore_points = (revolve.params.get("bore_points") if revolve else None) or []
    if candidate is not None:
        bore_volume = _profile_volume_from_points(bore_points) if bore_points else 0.0
    else:
        raw_bore = parts[0].get("bore") or []
        bore_volume = _profile_volume_mm3(raw_bore) if raw_bore else 0.0

    bounds = report.get("bounds_mm") or {}
    built_length = float(bounds.get("z") or 0.0)
    built_diameter = max(float(bounds.get("x") or 0.0), float(bounds.get("y") or 0.0))

    def close(built: float, stated: float) -> bool:
        if stated <= 0:
            return False
        return abs(built - stated) <= max(0.05, stated * 0.005)

    length_ok = close(built_length, stated_length)
    diameter_ok = close(built_diameter, stated_diameter)
    kernel_warnings = [str(item) for item in report.get("warnings") or []]
    feature_results = [
        item for item in report.get("feature_results") or [] if isinstance(item, dict)
    ]
    failed_features = [
        str(item.get("reason") or f"{item.get('kind')} не построен")
        for item in feature_results
        if item.get("status") != "built"
    ] or [item for item in kernel_warnings if "not built" in item.lower()]
    unlocalized_features = [
        f"{item.get('kind')}[{item.get('feature_index')}]: изменение B-Rep не локализовано"
        for item in feature_results
        if item.get("status") == "built" and item.get("localization_ok") is not True
    ]
    failed_features.extend(unlocalized_features)
    requested_features = [feature.kind for feature in candidate.features] if candidate else []
    feature_complete = not failed_features
    topology_ok = bool(
        report.get("brep_valid")
        and report.get("manifold")
        and report.get("solid_count") == 1
        and float(report.get("volume_mm3") or 0.0) > 0
    )
    # Same candidate-first, raw-spec-fallback rule as stated_length above.
    outer_volume = (
        _profile_volume_from_points(profile_points)
        if profile_points
        else _profile_volume_mm3(outer)
    )
    expected_base_volume = outer_volume - bore_volume + flange_volume
    built_volume = float(report.get("volume_mm3") or 0.0)
    # Every post-base rotation feature is subtractive or cosmetic. Therefore a
    # volume ABOVE the read outer-minus-bore profile proves that a cavity/cut
    # was omitted, even when the envelope and B-Rep validity still look right.
    volume_not_above_profile = (
        expected_base_volume > 0
        and built_volume <= expected_base_volume + max(0.1, expected_base_volume * 0.005)
    )
    checks = {
        "ok": bool(
            (length_ok or not require_envelope_match)
            and (diameter_ok or not require_envelope_match)
            and topology_ok
            and (volume_not_above_profile or not require_envelope_match)
            and feature_complete
        ),
        "envelope_match_required": require_envelope_match,
        "stated_length_mm": round(stated_length, 3),
        "built_length_mm": round(built_length, 3),
        "length_ok": length_ok,
        "stated_diameter_mm": round(stated_diameter, 3),
        "built_diameter_mm": round(built_diameter, 3),
        "diameter_ok": diameter_ok,
        "brep_valid": bool(report.get("brep_valid")),
        "manifold": bool(report.get("manifold")),
        "solid_count": report.get("solid_count"),
        "shell_count": report.get("shell_count"),
        "face_count": report.get("face_count"),
        "edge_count": report.get("edge_count"),
        "vertex_count": report.get("vertex_count"),
        "volume_mm3": report.get("volume_mm3"),
        "topology_ok": topology_ok,
        "profile_volume_upper_mm3": round(expected_base_volume, 3),
        "volume_not_above_profile": volume_not_above_profile,
        "feature_complete": feature_complete,
        "requested_features": requested_features,
        "failed_features": failed_features,
        "unlocalized_features": unlocalized_features,
        "feature_results": feature_results,
    }
    return SolidVerification(checks)


def _flange_envelope(
    candidate: FeatureTreeCandidate | None, profile_points: list[dict]
) -> tuple[float, float]:
    """Размах контура фланцев поперёк оси (мм) и их добавленный объём —
    по заявленным параметрам бобышек на станции, не по отчёту ядра."""
    from app.ai.cad_dimension_graph import sketch_outline

    extent = 0.0
    volume = 0.0
    for feature in candidate.features if candidate else []:
        params = feature.params
        if feature.kind != "boss" or params.get("axial_start_mm") is None:
            continue
        depth = float(params.get("depth_mm") or 0.0)
        cx, cy = float(params.get("center_x_mm") or 0.0), float(params.get("center_y_mm") or 0.0)
        profile = params.get("profile")
        if profile == "circle":
            diameter = float(params.get("diameter_mm") or 0.0)
            span, area = diameter, math.pi * diameter**2 / 4.0
        elif profile == "rectangle":
            width, height = (
                float(params.get("width_mm") or 0.0),
                float(params.get("height_mm") or 0.0),
            )
            span, area = max(width, height), width * height
        else:
            polygon = sketch_outline(params.get("sketch_profile"), step_deg=1.0) or []
            if not polygon:
                continue
            xs = [x + cx for x, _y in polygon]
            ys = [y + cy for _x, y in polygon]
            span = max(max(xs) - min(xs), max(ys) - min(ys))
            area = 0.5 * abs(
                sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(polygon, polygon[1:] + polygon[:1]))
            )
        start = float(params["axial_start_mm"])
        radii = [
            float(point["r"])
            for point in profile_points
            if start - 1e-6 <= float(point["z"]) <= start + depth + 1e-6
        ]
        body = math.pi * min(radii) ** 2 if radii else 0.0
        extent = max(extent, span)
        volume += max(0.0, area - body) * depth
    return extent, volume


def _close(a: float, b: float) -> bool:
    return b > 0 and abs(a - b) <= max(0.05, b * 0.005)


def _built_envelope(report: dict) -> tuple[float, float, float]:
    bounds = report.get("bounds_mm") or {}
    return (
        float(bounds.get("x") or 0.0),
        float(bounds.get("y") or 0.0),
        float(bounds.get("z") or 0.0),
    )


def _verify_sheet_metal(
    spec: dict, report: dict, *, require_envelope_match: bool = True
) -> SolidVerification:
    """Гнутая деталь (X4): габарит сечения и ширина, объём — сечение × ширина.

    Живой прогон: сверка тела знала только вал и пластину, и собранный
    швеллер отклонялся как «no_supported_body».
    """
    from app.ai.cad_dimension_graph import sketch_outline
    from app.ai.sheet_metal import bent_section, section_area

    sheet = spec["main_view"]["sheet_metal"]
    try:
        flanges = [float(v) for v in sheet["flanges_mm"]]
        turns = [int(v) for v in sheet["turns"]]
        radius, thickness = float(sheet["radius_mm"]), float(sheet["thickness_mm"])
        width = float(sheet["width_mm"])
        angles = (
            [float(v) for v in sheet["bend_angles_deg"]] if sheet.get("bend_angles_deg") else None
        )
        outline = sketch_outline(bent_section(flanges, turns, radius, thickness, angles)) or []
    except (KeyError, TypeError, ValueError):
        return SolidVerification({"ok": False, "reason": "sheet_metal_unreadable"})
    stated_x = max(p[0] for p in outline) - min(p[0] for p in outline)
    stated_y = max(p[1] for p in outline) - min(p[1] for p in outline)
    stated = sorted((stated_x, stated_y, width))
    built = sorted(_built_envelope(report))
    volume = float(report.get("volume_mm3") or 0.0)
    expected = section_area(flanges, len(turns), radius, thickness, angles) * width
    topology_ok = bool(
        report.get("brep_valid") and report.get("manifold") and report.get("solid_count") == 1
    )
    envelope_ok = all(abs(a - b) <= max(0.1, b * 0.005) for a, b in zip(built, stated, strict=True))
    volume_ok = _close(volume, expected)
    return SolidVerification(
        {
            "ok": topology_ok and volume_ok and (envelope_ok or not require_envelope_match),
            "envelope_ok": envelope_ok,
            "envelope_match_required": require_envelope_match,
            "stated_envelope_mm": [round(v, 3) for v in stated],
            "built_envelope_mm": [round(v, 3) for v in built],
            "volume_ok": volume_ok,
            "volume_mm3": volume,
            "expected_volume_mm3": round(expected, 3),
            "topology_ok": topology_ok,
            "brep_valid": bool(report.get("brep_valid")),
            "manifold": bool(report.get("manifold")),
            "solid_count": report.get("solid_count"),
        }
    )


def _verify_weldment(
    spec: dict, report: dict, *, require_envelope_match: bool = True
) -> SolidVerification:
    """Сварной узел (X3): габарит размещённых тел, объём пластин и валиков,
    число тел.

    Живой прогон: узел сверялся как пластина по одному основанию (высота 8
    против 58) и отклонялся.
    """
    bodies = [p for p in spec.get("parts") or [] if isinstance(p, dict) and p.get("profile")]
    boxes = [_body_box(body) for body in bodies]
    if not boxes or None in boxes:
        return SolidVerification({"ok": False, "reason": "weldment_bodies_not_rectangular"})
    stated = tuple(
        max(box[1][axis] for box in boxes) - min(box[0][axis] for box in boxes) for axis in range(3)
    )
    built = _built_envelope(report)
    expected = sum(
        float(b["profile"]["width_mm"])
        * float(b["profile"]["height_mm"])
        * float(b["profile"]["thickness_mm"])
        for b in bodies
    )
    beads, _notes = _weld_beads(spec, bodies, start_index=len(bodies))
    for bead in beads:
        leg = float(bead.params["sketch_profile"][0]["to"][0])
        expected += leg * leg / 2.0 * float(bead.params["depth_mm"])
    volume = float(report.get("volume_mm3") or 0.0)
    solids = len(bodies) + len(beads)
    topology_ok = bool(
        report.get("brep_valid") and (report.get("solid_count") in (None, solids)) and volume > 0
    )
    envelope_ok = all(_close(a, b) for a, b in zip(built, stated, strict=True))
    volume_ok = _close(volume, expected)
    return SolidVerification(
        {
            "ok": topology_ok and volume_ok and (envelope_ok or not require_envelope_match),
            "envelope_ok": envelope_ok,
            "envelope_match_required": require_envelope_match,
            "stated_envelope_mm": [round(v, 3) for v in stated],
            "built_envelope_mm": [round(v, 3) for v in built],
            "volume_ok": volume_ok,
            "volume_mm3": volume,
            "expected_volume_mm3": round(expected, 3),
            "solids_expected": solids,
            "solid_count": report.get("solid_count"),
            "topology_ok": topology_ok,
            "brep_valid": bool(report.get("brep_valid")),
        }
    )


def _verify_prismatic(
    spec: dict, report: dict, *, require_envelope_match: bool = True
) -> SolidVerification:
    """A plate is checked on all three read extents, holes included.

    The outline and thickness come from the sheet, so the built envelope must
    reproduce them; a hole cut outside the material would have been refused by
    the kernel, and one cut in the wrong place still keeps the envelope — which
    is why the hole COUNT is reported for review rather than silently trusted.
    """
    profiles = _prismatic_profiles(spec)
    if not profiles:
        return SolidVerification({"ok": False, "reason": "no_supported_body"})
    profile = profiles[0]
    thickness = _num(profile.get("thickness_mm")) or 0.0
    if profile.get("shape") == "rectangle":
        stated_x = _num(profile.get("width_mm")) or 0.0
        stated_y = _num(profile.get("height_mm")) or 0.0
    elif profile.get("shape") == "sketch":
        # Габарит эскиза — по его вершинам и дугам (живая планка part_04:
        # тело 90 × 100 × 3 верно, а сверялось с Ø 0 — «B-Rep отклонён»).
        from app.ai.cad_dimension_graph import sketch_outline

        outline = sketch_outline(profile.get("sketch")) or [(0.0, 0.0)]
        stated_x = max(p[0] for p in outline) - min(p[0] for p in outline)
        stated_y = max(p[1] for p in outline) - min(p[1] for p in outline)
    else:
        stated_x = stated_y = _num(profile.get("diameter_mm")) or 0.0

    bounds = report.get("bounds_mm") or {}
    built = (
        float(bounds.get("x") or 0.0),
        float(bounds.get("y") or 0.0),
        float(bounds.get("z") or 0.0),
    )
    stated = (stated_x, stated_y, thickness)

    def close(a: float, b: float) -> bool:
        if b <= 0:
            return False
        return abs(a - b) <= max(0.05, b * 0.005)

    holes = _expanded_profile_holes(profile) or []
    topology_ok = bool(
        report.get("brep_valid")
        and report.get("manifold")
        and report.get("solid_count") == 1
        and float(report.get("volume_mm3") or 0.0) > 0
    )
    envelope_ok = all(close(a, b) for a, b in zip(built, stated, strict=True))
    checks = {
        "ok": (envelope_ok or not require_envelope_match) and topology_ok,
        "envelope_ok": envelope_ok,
        "envelope_match_required": require_envelope_match,
        "stated_envelope_mm": [round(value, 3) for value in stated],
        "built_envelope_mm": [round(value, 3) for value in built],
        "holes_expected": len(holes),
        "brep_valid": bool(report.get("brep_valid")),
        "manifold": bool(report.get("manifold")),
        "solid_count": report.get("solid_count"),
        "shell_count": report.get("shell_count"),
        "face_count": report.get("face_count"),
        "edge_count": report.get("edge_count"),
        "vertex_count": report.get("vertex_count"),
        "volume_mm3": report.get("volume_mm3"),
        "topology_ok": topology_ok,
    }
    return SolidVerification(checks)


# Density of the materials the reader most often finds in a ГОСТ title block,
# g/cm³. Absent material → no mass claim, rather than a steel-shaped guess.
_DENSITY_G_CM3: dict[str, float] = {
    "сталь": 7.85,
    "чугун": 7.2,
    "алюмин": 2.7,
    "латун": 8.5,
    "бронз": 8.8,
    "медь": 8.96,
    "титан": 4.5,
    "капролон": 1.15,
    "полиамид": 1.14,
}


def estimate_mass_kg(volume_mm3: float | None, material: str | None) -> float | None:
    """Mass from the kernel's own volume and the material read off the stamp.

    ГОСТ 2.104 wants a mass in the title block; with a real solid it is a
    measurement, not an estimate. Unknown material yields None — a wrong
    density is worse than an empty field.
    """
    if not volume_mm3 or volume_mm3 <= 0 or not material:
        return None
    lowered = material.lower()
    for marker, density in _DENSITY_G_CM3.items():
        if marker in lowered:
            return round(volume_mm3 / 1000.0 * density / 1000.0, 3)
    return None
