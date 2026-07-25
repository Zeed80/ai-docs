"""Stage 5 of the 3D-first redraw: machining data from the model, not the picture.

The ЕСТД process generator (``tp_generator``) already knows how to pick blanks,
operations, equipment, cutting parameters and time norms. What it never had was
trustworthy input: its surface specs came from features recognised on a raster,
where a nominal is a reading and an internal surface is a guess from a view
label.

With a compiled solid both become facts. A nominal is MEASURED off the B-Rep the
kernel built, a bore is internal because it is a bore, and a fit class comes
from the callout that was bound to that exact edge in stage 3. This module turns
that pair into the surface specs the generator already consumes, so the existing
ЕСТД machinery runs on measured geometry instead of on a hopeful read.

Tolerances deserve a word. ``Ø80js6`` states a grade, not a number; the ±9.5 µm
behind it comes from ГОСТ 25346 (ISO 286). That value is DERIVED from the
standard, never read off the sheet, and every spec says so in ``tolerance_source``
so nobody later mistakes a table lookup for a measurement.
"""

from __future__ import annotations

import re
from typing import Any

# ГОСТ 25346 / ISO 286-1 fundamental tolerances, micrometres, by nominal range.
# Ranges are upper-inclusive in millimetres; grades IT5..IT14 cover everything
# this pipeline can currently build.
_IT_RANGES_MM = (3, 6, 10, 18, 30, 50, 80, 120, 180, 250, 315, 400, 500)
_IT_MICRONS: dict[int, tuple[int, ...]] = {
    5:  (4, 5, 6, 8, 9, 11, 13, 15, 18, 20, 23, 25, 27),
    6:  (6, 8, 9, 11, 13, 16, 19, 22, 25, 29, 32, 36, 40),
    7:  (10, 12, 15, 18, 21, 25, 30, 35, 40, 46, 52, 57, 63),
    8:  (14, 18, 22, 27, 33, 39, 46, 54, 63, 72, 81, 89, 97),
    9:  (25, 30, 36, 43, 52, 62, 74, 87, 100, 115, 130, 140, 155),
    10: (40, 48, 58, 70, 84, 100, 120, 140, 160, 185, 210, 230, 250),
    11: (60, 75, 90, 110, 130, 160, 190, 220, 250, 290, 320, 360, 400),
    12: (100, 120, 150, 180, 210, 250, 300, 350, 400, 460, 520, 570, 630),
    13: (140, 180, 220, 270, 330, 390, 460, 540, 630, 720, 810, 890, 970),
    14: (250, 300, 360, 430, 520, 620, 740, 870, 1000, 1150, 1300, 1400, 1550),
}

_FIT_GRADE = re.compile(r"([A-Za-z]{1,3})(\d{1,2})")


def it_tolerance_mm(nominal_mm: float, grade: int) -> float | None:
    """Fundamental tolerance for a nominal and IT grade, in millimetres."""
    values = _IT_MICRONS.get(grade)
    if values is None or nominal_mm <= 0:
        return None
    for limit, microns in zip(_IT_RANGES_MM, values, strict=True):
        if nominal_mm <= limit:
            return microns / 1000.0
    return None


def deviations_from_fit(nominal_mm: float, fit: str | None) -> tuple[float | None, float | None, str]:
    """Upper/lower deviation implied by a fit class, in millimetres.

    Only the cases a shop actually reads off a shaft or bore drawing are
    resolved: ``js``/``JS`` (symmetric), lowercase ``h`` (shaft, zero above), and
    uppercase ``H`` (hole, zero below). Any other letter yields the grade width
    without a side, because guessing which way a ``k6`` shifts would silently
    move the part into the wrong half of its fit.
    """
    if not fit:
        return None, None, "not_stated"
    match = _FIT_GRADE.fullmatch(fit.strip())
    if match is None:
        return None, None, "unparsed"
    letters, grade_text = match.group(1), match.group(2)
    try:
        grade = int(grade_text)
    except ValueError:
        return None, None, "unparsed"
    width = it_tolerance_mm(nominal_mm, grade)
    if width is None:
        return None, None, "out_of_table"
    if letters in ("js", "JS"):
        return width / 2.0, -width / 2.0, "gost_25346"
    if letters == "h":
        return 0.0, -width, "gost_25346"
    if letters == "H":
        return width, 0.0, "gost_25346"
    return None, None, f"grade_only_it{grade}"


def _ra_from_notes(properties: dict[str, Any]) -> float | None:
    """The part-level roughness the sheet stated, in µm.

    A sheet usually carries one general roughness plus local overrides; only the
    general one is known here, so it is applied as the part default and never
    presented as a per-surface reading.
    """
    for text in (properties.get("notes") or {}).get("roughness") or []:
        match = re.search(r"Ra\s*(\d+(?:[.,]\d+)?)", str(text), re.IGNORECASE)
        if match:
            return float(match.group(1).replace(",", "."))
    return None


def surface_specs_from_solid(
    semantics: dict[str, Any],
    properties: dict[str, Any],
    *,
    process_plan_id: str | None = None,
) -> list[dict[str, Any]]:
    """Machining surfaces in the shape ``tp_generator`` already consumes.

    Every entry is backed by a bound callout, so a surface exists here only when
    the sheet stated something about it AND the model contains it.
    """
    from app.ai.tp_generator import _roughness_stage, _surface_to_method

    default_ra = _ra_from_notes(properties)
    specs: list[dict[str, Any]] = []
    for binding in semantics.get("bindings") or []:
        nominal = float(binding.get("nominal_mm") or 0.0)
        if nominal <= 0:
            continue
        fit = binding.get("fit")
        upper, lower, source = deviations_from_fit(nominal, fit)
        if upper is None and binding.get("deviation"):
            # An explicitly written deviation beats any table lookup.
            try:
                value = float(str(binding["deviation"]).replace(",", ".").replace("±", ""))
                if str(binding["deviation"]).strip().startswith("-"):
                    upper, lower, source = 0.0, value, "stated"
                else:
                    upper, lower, source = value, 0.0, "stated"
            except ValueError:
                pass

        thread = binding.get("thread")
        is_diameter = binding.get("kind") == "diameter"
        if not is_diameter and not thread:
            # An axial length is a DIMENSION, not a machined surface: turning it
            # into a "flat to be milled" would put a fictitious operation on a
            # shaft's process sheet.
            continue
        # A bore is internal by construction: its callout was bound to the bore
        # edges, not guessed from a view label.
        internal = bool(binding.get("applies_to") and "расточ" in str(binding["applies_to"]).lower())
        surface_type = (
            "thread" if thread else ("hole" if internal else "external_cylindrical")
        )
        specs.append({
            "process_plan_id": process_plan_id,
            "surface_type": surface_type,
            "nominal_mm": nominal,
            "upper_tol": upper,
            "lower_tol": lower,
            "roughness_ra": default_ra,
            "fit_system": fit,
            "machining_method": _surface_to_method(surface_type, nominal, default_ra),
            "machining_stage": _roughness_stage(default_ra),
            "confidence": 0.9,
            "is_internal": internal or surface_type == "hole",
            # Provenance is part of the contract: a derived tolerance must never
            # read as something a person measured.
            "tolerance_source": source,
            "source_callout": binding.get("text"),
            "edge_keys": binding.get("edge_keys") or [],
            "thread": thread,
        })
    return specs


# Machining allowance on the diameter of turned stock, millimetres. Coarse on
# purpose: a real shop picks from a standards table, and inventing a precise
# number here would look authoritative without being so.
_TURNING_ALLOWANCE_MM = 4.0
_FACING_ALLOWANCE_MM = 3.0


def blank_from_solid(properties: dict[str, Any]) -> dict[str, Any] | None:
    """Stock the part fits in, measured from the solid rather than estimated.

    Returns ``None`` when the model never produced an envelope, so a caller
    never plans a cut against an imagined billet.
    """
    envelope = properties.get("stock_envelope_mm") or {}
    length = envelope.get("length")
    width = envelope.get("width")
    height = envelope.get("height")
    if not length or not width or not height:
        return None

    round_diameter = properties.get("round_stock_diameter_mm")
    part_volume = properties.get("volume_mm3")
    is_turned = bool(round_diameter) and abs(float(width) - float(height)) <= max(
        0.05, float(width) * 0.02
    )
    if is_turned:
        blank_diameter = float(round_diameter) + _TURNING_ALLOWANCE_MM
        blank_length = float(length) + _FACING_ALLOWANCE_MM
        blank_volume = 3.141592653589793 * (blank_diameter / 2.0) ** 2 * blank_length
        kind = "round_bar"
        dimensions = {"diameter_mm": round(blank_diameter, 2), "length_mm": round(blank_length, 2)}
    else:
        blank_volume = (
            (float(width) + _FACING_ALLOWANCE_MM)
            * (float(height) + _FACING_ALLOWANCE_MM)
            * (float(length) + _FACING_ALLOWANCE_MM)
        )
        kind = "plate"
        dimensions = {
            "width_mm": round(float(width) + _FACING_ALLOWANCE_MM, 2),
            "height_mm": round(float(height) + _FACING_ALLOWANCE_MM, 2),
            "thickness_mm": round(float(length) + _FACING_ALLOWANCE_MM, 2),
        }

    utilisation = (
        round(float(part_volume) / blank_volume, 4)
        if part_volume and blank_volume > 0 else None
    )
    return {
        "kind": kind,
        "dimensions": dimensions,
        "material": properties.get("material"),
        "blank_volume_mm3": round(blank_volume, 1),
        "part_volume_mm3": part_volume,
        # КИМ: how much of the billet survives as the part.
        "material_utilisation": utilisation,
        "allowance_source": "geometry+standard_allowance",
    }
