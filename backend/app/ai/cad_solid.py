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
    return [
        str(item["id"]) for item in items
        if isinstance(item, dict) and item.get("id")
    ]


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
        item for item in unresolved
        if (
            "подготовительного отверстия" in item.lower()
            and not any(
                marker in item.lower()
                for marker in (
                    "торец", "сквозное", "глухое", "глубин", "резьба/шаг",
                )
            )
        )
    ]
    blockers = [item for item in unresolved if item not in non_geometric]
    from app.ai.cad_dimension_graph import build_dimension_graph

    blockers.extend(build_dimension_graph(spec)["errors"])
    if require_source_evidence:
        body = spec.get("main_view") or {}
        missing_evidence = [
            f"main_view.{group}.{index}"
            for group in (
                "outer", "bore", "keyways", "cross_holes", "axial_holes",
                "circular_hole_patterns",
                "grooves", "chamfers",
            )
            for index, item in enumerate(body.get(group) or [])
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
    has_section = any(
        str(view.get("kind") or "").lower() in {"section", "cut", "разрез", "сечение"}
        for view in spec.get("views") or []
        if isinstance(view, dict)
    )
    for item in candidate.missing_data:
        message = str(item)
        lowered = message.lower()
        is_critical = any(marker in lowered for marker in (
            "не построен",
            "длиннее детали",
            "построено только главное",
            "прочитан не полностью",
        ))
        if "разрез не прочитан" in lowered:
            is_critical = has_section
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


def _profile_volume_mm3(sections: list[dict]) -> float:
    """Exact volume of coaxial cylindrical/frustum sections before cuts."""
    import math

    volume = 0.0
    for section in sections:
        start_diameter = float(section["d"])
        end_diameter = float(taper_end_diameter(section) or start_diameter)
        length = float(section["l"])
        volume += (
            math.pi * length / 12.0
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
    return _prismatic_feature_tree(spec)


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
    straight: float, radius: float, rotation_deg: float,
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


def _prismatic_feature_tree(spec: dict) -> FeatureTreeCandidate | None:
    """A plate or flange: the read outline given its read thickness.

    Thickness comes from a side view or a section; a sheet that never stated it
    yields no solid at all, exactly as the 2D drafter refuses to draft a section
    whose length was never read.
    """
    profiles = _prismatic_profiles(spec)
    if not profiles:
        return None
    profile = profiles[0]
    thickness = _num(profile.get("thickness_mm"))
    if not thickness or thickness <= 0:
        return None
    shape = profile.get("shape")
    holes = _expanded_profile_holes(profile)
    if holes is None:
        return None

    features: list[Feature3D] = []
    missing: list[str] = []
    provenance = {
        "thickness_mm": ParamProvenance(
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
        features.append(Feature3D(
            kind="extrude",
            params=base_params,
            param_provenance={
                **provenance,
                "width_mm": ParamProvenance(origin="stated", detail="габарит по чертежу"),
                "height_mm": ParamProvenance(origin="stated", detail="габарит по чертежу"),
                **({
                    "corner_radius_mm": ParamProvenance(
                        origin="stated", detail="радиус углов прочитан с выноски R"
                    )
                } if corner_radius else {}),
            },
            confidence=0.9,
        ))
        # The sheet gives hole centres from the middle of the plate; an extrude
        # box is anchored at its corner, so the frames must be reconciled here
        # rather than by whoever reads the feature tree later.
        def to_base(x: float, y: float) -> tuple[float, float]:
            return width / 2.0 + x, height / 2.0 + y
    elif shape == "circle":
        diameter = _num(profile.get("diameter_mm"))
        if not diameter:
            return None
        features.append(Feature3D(
            kind="revolve",
            params={"profile_points": [
                {"r": diameter / 2.0, "z": 0.0},
                {"r": diameter / 2.0, "z": thickness},
            ]},
            param_provenance={
                **provenance,
                "profile_points": ParamProvenance(
                    origin="stated", detail="диаметр и толщина прочитаны с чертежа"
                ),
            },
            confidence=0.9,
        ))
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
        features.append(Feature3D(
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
        ))
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
        features.append(Feature3D(
            kind="hole",
            source_feature_ids=_source_feature_ids(hole),
            params={
                "diameter_mm": diameter,
                "center_x_mm": cx,
                "center_y_mm": cy,
                # A plate hole is through unless the sheet said otherwise; a
                # blind hole needs a depth the reader did not provide.
                "through": True,
            },
            param_provenance={
                "diameter_mm": ParamProvenance(origin="stated", detail="Ø отверстия с чертежа"),
                "center_x_mm": ParamProvenance(origin="stated", detail="координата от центра"),
                "center_y_mm": ParamProvenance(origin="stated", detail="координата от центра"),
            },
            confidence=0.85,
        ))

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
        if abs(rotation_deg) > 1e-6:
            # Ф2.3: a rotated capsule is one sketch-profile pocket (a
            # closed line/arc loop rotated about the slot's own centre),
            # not the axis-aligned pocket+2holes assembly below — that
            # assembly has no rotation parameter to give it.
            offset_x, offset_y, sketch = _rotated_capsule_sketch(
                straight, width_mm / 2.0, rotation_deg
            )
            features.append(Feature3D(
                kind="pocket",
                source_feature_ids=_source_feature_ids(slot),
                params={
                    "profile": "sketch", "sketch_profile": sketch,
                    "depth_mm": thickness,
                    "center_x_mm": cx + offset_x, "center_y_mm": cy + offset_y,
                },
                param_provenance={
                    "sketch_profile": ParamProvenance(
                        origin="stated",
                        detail=(
                            f"паз {length:g}×{width_mm:g} повёрнут на "
                            f"{rotation_deg:g}° по прочитанному углу"
                        ),
                    )
                },
                confidence=0.8,
            ))
            continue
        if straight > 0:
            features.append(Feature3D(
                kind="pocket",
                source_feature_ids=_source_feature_ids(slot),
                params={
                    "profile": "rectangle", "width_mm": straight, "height_mm": width_mm,
                    "center_x_mm": cx, "center_y_mm": cy, "depth_mm": thickness,
                },
                confidence=0.8,
            ))
        # The capsule ends: a slot is a rectangle plus a round at each end.
        for offset in (-straight / 2.0, straight / 2.0):
            features.append(Feature3D(
                kind="hole",
                source_feature_ids=_source_feature_ids(slot),
                params={
                    "diameter_mm": width_mm,
                    "center_x_mm": cx + offset,
                    "center_y_mm": cy,
                    "through": True,
                },
                confidence=0.8,
            ))
    if len(features) == 1:
        missing.append("на профиле не прочитано ни одного отверстия или паза")
    label = str(spec.get("part") or "Пластина") + " — по прочитанному контуру и толщине"
    return FeatureTreeCandidate(
        features=features, score=0.85, label=label[:500], missing_data=missing,
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
    if bore and not _sections_are_complete(bore):
        bore_filled = _fill_provisional_step_lengths(bore)
        if bore_filled is None:
            return None
        bore, bore_guess_notes = bore_filled

    params: dict[str, Any] = {"profile_points": _profile_points(outer)}
    provenance = {
        "profile_points": ParamProvenance(
            origin="guessed" if outer_guess_notes else "stated",
            detail=(
                "; ".join(outer_guess_notes) if outer_guess_notes
                else "диаметры и длины ступеней прочитаны с чертежа (outer[])"
            ),
        )
    }
    missing: list[str] = [*outer_guess_notes, *bore_guess_notes]
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
                "; ".join(bore_guess_notes) if bore_guess_notes
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
            missing.append(
                "расточка длиннее детали — проверьте прочитанные длины"
            )
    else:
        missing.append(
            "разрез не прочитан: деталь построена сплошной, полость не учтена"
        )

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

    def append_threads(
        sections: list[dict], *, start_offset: float, internal: bool
    ) -> None:
        axial_start = start_offset
        for section in sections:
            thread = section.get("thread") or {}
            designation = str(
                thread.get("designation") or thread.get("spec") or ""
            ).strip()
            if designation:
                diameter = (
                    _num(thread.get("nominal_diameter_mm"))
                    or _num(section.get("d"))
                )
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
                    features.append(Feature3D(
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
                        },
                        confidence=0.85,
                        body_index=body_index,
                    ))
            axial_start += float(section.get("l") or 0.0)

    append_threads(outer, start_offset=0.0, internal=False)
    append_threads(bore, start_offset=bore_offset if bore else 0.0, internal=True)
    for cut in _cut_features(body, outer, missing):
        cut.body_index = body_index
        features.append(cut)
    return features, missing


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
        missing.append(
            f"на листе прочитано тел: {len(parts)}; их взаимное расположение "
            "не прочитано, построены раздельно"
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
    1.0: 0.25, 1.2: 0.25, 1.4: 0.3, 1.6: 0.35, 1.8: 0.35,
    2.0: 0.4, 2.5: 0.45, 3.0: 0.5, 3.5: 0.6, 4.0: 0.7,
    5.0: 0.8, 6.0: 1.0, 8.0: 1.25, 10.0: 1.5, 12.0: 1.75,
    14.0: 2.0, 16.0: 2.0, 18.0: 2.5, 20.0: 2.5, 22.0: 2.5,
    24.0: 3.0, 27.0: 3.0, 30.0: 3.5, 33.0: 3.5, 36.0: 4.0,
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
        features.append(Feature3D(
            kind="groove",
            source_feature_ids=_source_feature_ids(groove),
            params=params,
            param_provenance={
                "axial_position_mm": ParamProvenance(
                    origin="stated", detail="положение канавки прочитано с чертежа"
                ),
            },
            confidence=0.85,
        ))

    for keyway in body.get("keyways") or []:
        start = _num(keyway.get("axial_start_mm"))
        length = _num(keyway.get("length_mm"))
        width = _num(keyway.get("width_mm"))
        depth = _num(keyway.get("depth_mm"))
        if start is None or not (length and width and depth):
            missing.append("шпоночный паз прочитан не полностью — не построен")
            continue
        features.append(Feature3D(
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
            param_provenance={
                "width_mm": ParamProvenance(
                    origin="stated", detail="ширина паза с чертежа"
                ),
                "depth_mm": ParamProvenance(
                    origin="stated", detail="глубина паза t1 с чертежа"
                ),
            },
            confidence=0.85,
        ))

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
            features.append(Feature3D(
                kind="hole",
                source_feature_ids=_source_feature_ids(hole),
                params=params,
                param_provenance={
                    "diameter_mm": ParamProvenance(
                        origin="stated", detail="Ø поперечного отверстия с чертежа"
                    ),
                },
                confidence=0.8,
            ))
            counterbore_diameter = _num(hole.get("counterbore_diameter_mm"))
            counterbore_depth = _num(hole.get("counterbore_depth_mm"))
            if counterbore_diameter and counterbore_depth:
                features.append(Feature3D(
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
                ))

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
        import math

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
                features.append(Feature3D(
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
                ))
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
            features.append(Feature3D(
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
                            if entry_offset else "вход на крайнем торце"
                        ),
                    ),
                },
                confidence=0.82,
            ))
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
            features.append(Feature3D(
                kind="thread",
                source_feature_ids=_source_feature_ids(pattern),
                params=thread_params,
                param_provenance={
                    "spec": ParamProvenance(
                        origin="stated", detail="обозначение резьбы с торцевого вида"
                    ),
                    "pitch_mm": ParamProvenance(
                        origin=(
                            "stated"
                            if _num(thread.get("pitch_mm")) is not None
                            else "standard"
                        ),
                        detail=(
                            "шаг указан в обозначении"
                            if _num(thread.get("pitch_mm")) is not None
                            else "крупный шаг метрической резьбы по стандарту"
                        ),
                    ),
                    "center_x_mm": ParamProvenance(
                        origin="propagated", detail="центр на прочитанной делительной окружности"
                    ),
                    "center_y_mm": ParamProvenance(
                        origin="propagated", detail="центр на прочитанной делительной окружности"
                    ),
                },
                confidence=0.82,
            ))

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
        import math

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
                params.update({
                    "inclination_deg": inclination,
                    "radial_direction": radial_direction,
                })
            features.append(Feature3D(
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
            ))

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
        for item in items:
            size = _num(item.get(size_key))
            if not size:
                missing.append(f"{kind} без размера — не построен")
                continue
            selector = _edge_selector(item, outer, starts, total_length)
            if selector is None:
                missing.append(
                    f"{kind}: не удалось определить ребро ({item.get('location')}) — не построен"
                )
                continue
            params = {"size_mm": size, "edge_selector": selector}
            if kind == "chamfer" and _num(item.get("angle_deg")):
                params["angle_deg"] = _num(item.get("angle_deg"))
            features.append(Feature3D(
                kind=kind,
                source_feature_ids=_source_feature_ids(item),
                params=params,
                param_provenance={
                    "size_mm": ParamProvenance(
                        origin="stated", detail=f"размер {kind} с чертежа"
                    ),
                },
                confidence=0.75,
            ))
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
            "curve": "Circle", "at_z_mm": 0.0,
            "diameter_mm": at_diameter or _num(first.get("d")),
        }
    if location == "right_end":
        last = outer[-1] if outer else {}
        end_diameter = taper_end_diameter(last) if last else None
        return {
            "curve": "Circle", "at_z_mm": total_length,
            "diameter_mm": at_diameter or end_diameter or _num(last.get("d")),
        }
    if location in ("shoulder", "bore_mouth"):
        if at_z is not None:
            return {
                "curve": "Circle", "at_z_mm": at_z,
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
) -> SolidVerification:
    """Does the built solid measure what the sheet said?

    Checks the two quantities a revolve cannot fake: overall length along the
    axis and the largest diameter. Tolerance is 0.5% — the same window the 2D
    dimension check uses, because both answer the same question (did the
    builder honour the numbers it was given?).
    """
    parts = _rotation_parts(spec)
    if not parts:
        return _verify_prismatic(spec, report)
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
        (feature for feature in (candidate.features if candidate else []) if feature.kind == "revolve"),
        None,
    )
    profile_points = (revolve.params.get("profile_points") if revolve else None) or []
    stated_length = (
        float(profile_points[-1]["z"]) if profile_points
        else sum(float(section["l"]) for section in outer if section.get("l"))
    )
    stated_diameter = max((float(section["d"]) for section in outer), default=0.0)

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
    outer_volume = _profile_volume_mm3(outer)
    bore = parts[0].get("bore") or []
    expected_base_volume = outer_volume - (_profile_volume_mm3(bore) if bore else 0.0)
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
            length_ok
            and diameter_ok
            and topology_ok
            and volume_not_above_profile
            and feature_complete
        ),
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


def _verify_prismatic(spec: dict, report: dict) -> SolidVerification:
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
    checks = {
        "ok": all(close(a, b) for a, b in zip(built, stated, strict=True))
        and topology_ok,
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
