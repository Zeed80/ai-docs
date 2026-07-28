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
    blockers = [str(item) for item in spec.get("unresolved") or [] if str(item)]
    from app.ai.cad_dimension_graph import build_dimension_graph

    blockers.extend(build_dimension_graph(spec)["errors"])
    if require_source_evidence:
        body = spec.get("main_view") or {}
        missing_evidence = [
            f"main_view.{group}.{index}"
            for group in ("outer", "bore", "keyways", "cross_holes", "grooves", "chamfers")
            for index, item in enumerate(body.get(group) or [])
            if isinstance(item, dict) and not item.get("evidence")
        ]
        if missing_evidence:
            blockers.append(
                "геометрия без локализованного evidence: "
                + ", ".join(missing_evidence[:8])
                + (f" и ещё {len(missing_evidence) - 8}" if len(missing_evidence) > 8 else "")
            )
    warnings: list[str] = []
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
        features.append(Feature3D(
            kind="extrude",
            params={"width_mm": width, "height_mm": height, "depth_mm": thickness},
            param_provenance={
                **provenance,
                "width_mm": ParamProvenance(origin="stated", detail="габарит по чертежу"),
                "height_mm": ParamProvenance(origin="stated", detail="габарит по чертежу"),
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
        if abs(rotation_deg) > 1e-6:
            # A rotated pocket is not expressible in the kernel's axis-aligned
            # feature frame; saying so beats cutting it in the wrong direction.
            missing.append(
                f"паз повёрнут на {rotation_deg:g}° — в 3D не построен"
            )
            continue
        cx, cy = to_base(x, y)
        straight = max(length - width_mm, 0.0)
        if straight > 0:
            features.append(Feature3D(
                kind="pocket",
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


def _rotation_feature_tree(spec: dict) -> FeatureTreeCandidate | None:
    """Build a revolve feature tree from a rotation-body spec.

    Returns None when the spec has no complete rotation body — the caller falls
    back to the 2D-only result rather than to a guessed solid.
    """
    parts = _rotation_parts(spec)
    if not parts:
        return None
    body = parts[0]
    outer = body.get("outer") or []
    if not _sections_are_complete(outer):
        return None
    bore = body.get("bore") or []
    if bore and not _sections_are_complete(bore):
        return None

    params: dict[str, Any] = {"profile_points": _profile_points(outer)}
    provenance = {
        "profile_points": ParamProvenance(
            origin="stated",
            detail="диаметры и длины ступеней прочитаны с чертежа (outer[])",
        )
    }
    missing: list[str] = []
    if bore:
        bore_points = _profile_points(bore)
        outer_max_r = max(point["r"] for point in params["profile_points"])
        if max(point["r"] for point in bore_points) >= outer_max_r:
            # Contradictory read; refuse rather than "fix" it into a solid.
            return None
        params["bore_points"] = bore_points
        provenance["bore_points"] = ParamProvenance(
            origin="stated",
            detail="внутренний контур прочитан с разреза (bore[])",
        )
        bore_length = sum(float(section["l"]) for section in bore)
        outer_length = sum(float(section["l"]) for section in outer)
        if bore_length > outer_length + 1e-6:
            missing.append(
                "расточка длиннее детали — проверьте прочитанные длины"
            )
    else:
        missing.append(
            "разрез не прочитан: деталь построена сплошной, полость не учтена"
        )

    if len(parts) > 1:
        missing.append(
            f"на листе прочитано тел: {len(parts)}; в 3D построено только главное"
        )

    label = str(spec.get("part") or "Тело вращения") + " — revolve по прочитанному профилю"
    features = [
        Feature3D(
            kind="revolve",
            params=params,
            param_provenance=provenance,
            confidence=0.9,
        )
    ]
    axial_start = 0.0
    for section in outer:
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
                }
                pitch = _num(thread.get("pitch_mm"))
                if pitch:
                    thread_params["pitch_mm"] = pitch
                features.append(Feature3D(
                    kind="thread",
                    params=thread_params,
                    param_provenance={
                        "spec": ParamProvenance(
                            origin="stated", detail="обозначение резьбы прочитано с чертежа"
                        ),
                        "diameter_mm": ParamProvenance(
                            origin="stated", detail="номинальный диаметр резьбы"
                        ),
                    },
                    confidence=0.85,
                ))
        axial_start += float(section.get("l") or 0.0)
    features.extend(_cut_features(body, outer, missing))
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
                params=params,
                param_provenance={
                    "diameter_mm": ParamProvenance(
                        origin="stated", detail="Ø поперечного отверстия с чертежа"
                    ),
                },
                confidence=0.8,
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
    stated_length = sum(float(section["l"]) for section in outer if section.get("l"))
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
    requested_features = [feature.kind for feature in candidate.features] if candidate else []
    feature_complete = not failed_features
    checks = {
        "ok": bool(
            length_ok
            and diameter_ok
            and report.get("brep_valid")
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
        "volume_mm3": report.get("volume_mm3"),
        "feature_complete": feature_complete,
        "requested_features": requested_features,
        "failed_features": failed_features,
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
    checks = {
        "ok": all(close(a, b) for a, b in zip(built, stated, strict=True))
        and bool(report.get("brep_valid")),
        "stated_envelope_mm": [round(value, 3) for value in stated],
        "built_envelope_mm": [round(value, 3) for value in built],
        "holes_expected": len(holes),
        "brep_valid": bool(report.get("brep_valid")),
        "manifold": bool(report.get("manifold")),
        "solid_count": report.get("solid_count"),
        "volume_mm3": report.get("volume_mm3"),
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
