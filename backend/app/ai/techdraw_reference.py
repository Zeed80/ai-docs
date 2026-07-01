"""Engineering reference data for techdraw: tolerances, materials, threads, sheets.

Pure data + lookup helpers, no ORM/DB dependency — this module sits *below*
``normcontrol_agent.py``/``tp_generator.py`` in the dependency graph (they may
import from here; this module never imports from them), so shared constants
(e.g. the standard Ra series) live here once and domain modules alias them.

The tolerance table is derived from ISO 286-1 (mirrored by ГОСТ 25346-89 /
ГОСТ 25347-82 "Единая система допусков и посадок") rather than hand-copied per
symbol: IT-grade widths and shaft fundamental deviations are tabulated
separately and combined by ``tolerance_band``. This keeps the numeric surface
small enough to manually verify against the standard before merging (see the
5-6 control points in ``tests/ai/test_techdraw_reference.py``) instead of a
sprawling hand-typed matrix that is easy to mistranscribe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import structlog

logger = structlog.get_logger()

# ── Roughness (ГОСТ 2789-73 standard Ra series, µm) ────────────────────────────

STANDARD_RA_SERIES: frozenset[float] = frozenset({
    0.025, 0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 3.2, 6.3, 12.5, 25.0, 50.0, 100.0,
})


def nearest_ra(value: float) -> float:
    """Closest value in the standard Ra series (used for repair-prompt hints)."""
    return min(STANDARD_RA_SERIES, key=lambda ra: abs(ra - value))


# ── ISO 286 / ЕСДП: tolerances and fits ────────────────────────────────────────

ISO_SIZE_RANGES: tuple[tuple[float, float], ...] = (
    (1, 3), (3, 6), (6, 10), (10, 18), (18, 30), (30, 50),
    (50, 80), (80, 120), (120, 180), (180, 250), (250, 315), (315, 400),
)

# IT-grade tolerance width (µm) per size range, for the grades this module
# supports. Source: ISO 286-1 / ГОСТ 25346-89 standard tolerance grade table.
_IT_GRADE_UM: dict[int, tuple[int, ...]] = {
    # range index aligns with ISO_SIZE_RANGES
    6:  (6, 8, 9, 11, 13, 16, 19, 22, 25, 29, 32, 36),
    7:  (10, 12, 15, 18, 21, 25, 30, 35, 40, 46, 52, 57),
    8:  (14, 18, 22, 27, 33, 39, 46, 54, 63, 72, 81, 89),
    9:  (25, 30, 36, 43, 52, 62, 74, 87, 100, 115, 130, 140),
    11: (60, 75, 90, 110, 130, 160, 190, 220, 250, 290, 320, 360),
}

# Shaft fundamental (lower) deviation ei (µm) for transition/interference
# letters, valid up to IT8 (k6/m6/n6/p6 — the practical set this module
# supports). Source: ISO 286-1 fundamental deviation table for shafts.
_SHAFT_FUNDAMENTAL_EI_UM: dict[str, tuple[int, ...]] = {
    "k": (0, 1, 1, 1, 2, 2, 2, 3, 3, 4, 4, 4),
    "m": (2, 4, 6, 7, 8, 9, 11, 13, 15, 17, 20, 21),
    "n": (4, 8, 10, 12, 15, 17, 20, 23, 27, 31, 34, 37),
    "p": (6, 12, 15, 18, 22, 26, 32, 37, 43, 50, 56, 62),
}

_SHAFT_GRADES = {"h": (6, 7, 8, 9, 11), "js": (6,), "k": (6,), "m": (6,), "n": (6,), "p": (6,)}
_HOLE_GRADES = {"H": (7, 8, 9, 11), "JS": (7,)}

_TOL_SYMBOL_RE = re.compile(r"^([A-Za-z]{1,2})(\d{1,2})$")


@dataclass(frozen=True)
class ToleranceBand:
    es_um: float  # upper deviation, µm (sign matters)
    ei_um: float  # lower deviation, µm


def _range_index(nominal_mm: float) -> int | None:
    for i, (lo, hi) in enumerate(ISO_SIZE_RANGES):
        if lo < nominal_mm <= hi:
            return i
    return None


def is_valid_tolerance_symbol(symbol: str) -> bool:
    """Format check: one/two letters (h/H/js/JS/k/m/n/p) + quality grade digits."""
    m = _TOL_SYMBOL_RE.match((symbol or "").strip())
    if not m:
        return False
    letter, grade = m.group(1), int(m.group(2))
    if letter in _SHAFT_GRADES:
        return grade in _SHAFT_GRADES[letter]
    if letter in _HOLE_GRADES:
        return grade in _HOLE_GRADES[letter]
    return False


def tolerance_band(symbol: str, nominal_mm: float) -> ToleranceBand | None:
    """Resolve a fit symbol (``h6``, ``H7``, ``js6``, ``k6``...) for a diameter.

    Returns ``None`` if the symbol is malformed, unsupported, or the diameter
    falls outside the tabulated size ranges (1..400mm).
    """
    m = _TOL_SYMBOL_RE.match((symbol or "").strip())
    if not m:
        return None
    letter, grade = m.group(1), int(m.group(2))
    idx = _range_index(nominal_mm)
    if idx is None:
        return None

    if letter == "h" and grade in _SHAFT_GRADES["h"]:
        it = _IT_GRADE_UM[grade][idx]
        return ToleranceBand(es_um=0.0, ei_um=-float(it))
    if letter == "H" and grade in _HOLE_GRADES["H"]:
        it = _IT_GRADE_UM[grade][idx]
        return ToleranceBand(es_um=float(it), ei_um=0.0)
    if letter in ("js", "JS") and grade in (6, 7):
        it = _IT_GRADE_UM[grade][idx]
        half = it / 2.0
        return ToleranceBand(es_um=half, ei_um=-half)
    if letter in _SHAFT_FUNDAMENTAL_EI_UM and grade == 6:
        it6 = _IT_GRADE_UM[6][idx]
        ei = float(_SHAFT_FUNDAMENTAL_EI_UM[letter][idx])
        return ToleranceBand(es_um=ei + it6, ei_um=ei)
    return None


def fit_clearance_kind(hole_symbol: str, shaft_symbol: str) -> str:
    """'clearance' | 'transition' | 'interference' for a hole+shaft fit pair.

    Compares the tightest hole (EI) against the loosest shaft (es) and vice
    versa at the same nominal size band is the caller's job (pass matching
    diameters' bands in); here we classify from already-resolved deviations.
    """
    hole = tolerance_band(hole_symbol, 20.0)  # any nominal in a shared range; letter shape is what matters
    shaft = tolerance_band(shaft_symbol, 20.0)
    if not hole or not shaft:
        return "unknown"
    if shaft.es_um <= hole.ei_um:
        return "clearance"
    if shaft.ei_um >= hole.es_um:
        return "interference"
    return "transition"


# ── Materials ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MaterialSpec:
    designation: str
    group: str  # steel_carbon | steel_alloy | cast_iron | aluminum | stainless
    density_kg_m3: float
    default_hatch_pitch_mm: float


MATERIAL_CATALOG: dict[str, MaterialSpec] = {
    "сталь 45 гост 1050-2013": MaterialSpec("Сталь 45 ГОСТ 1050-2013", "steel_carbon", 7850.0, 2.5),
    "сталь 20 гост 1050-2013": MaterialSpec("Сталь 20 ГОСТ 1050-2013", "steel_carbon", 7850.0, 2.5),
    "сталь 40х гост 4543-71": MaterialSpec("Сталь 40Х ГОСТ 4543-71", "steel_alloy", 7850.0, 2.5),
    "сталь 30хгса гост 4543-71": MaterialSpec("Сталь 30ХГСА ГОСТ 4543-71", "steel_alloy", 7850.0, 2.5),
    "сталь 12х18н10т гост 5632-2014": MaterialSpec("Сталь 12Х18Н10Т ГОСТ 5632-2014", "stainless", 7900.0, 3.0),
    "сч20 гост 1412-85": MaterialSpec("СЧ20 ГОСТ 1412-85", "cast_iron", 7200.0, 3.0),
    "сч30 гост 1412-85": MaterialSpec("СЧ30 ГОСТ 1412-85", "cast_iron", 7300.0, 3.0),
    "д16т гост 4784-97": MaterialSpec("Д16Т ГОСТ 4784-97", "aluminum", 2780.0, 2.0),
    "амг6 гост 4784-97": MaterialSpec("АМг6 ГОСТ 4784-97", "aluminum", 2640.0, 2.0),
}


def classify_material(text: str) -> MaterialSpec | None:
    """Best-effort MaterialSpec lookup, reusing tp_generator's group keywords."""
    if not text:
        return None
    key = text.strip().lower()
    if key in MATERIAL_CATALOG:
        return MATERIAL_CATALOG[key]
    for spec in MATERIAL_CATALOG.values():
        if spec.designation.lower() in key or key in spec.designation.lower():
            return spec
    try:
        from app.ai.tp_generator import material_group
    except Exception:  # noqa: BLE001
        return None
    group = material_group(text)
    # material_group() defaults to "steel_carbon" when NO keyword matched at
    # all — that default is meant for "classify this known material string",
    # not for "does this arbitrary text mention a material?". Require an
    # explicit carbon-steel root before trusting the default here, so free
    # text with no material mention (e.g. "нарисуй эскиз установки") returns
    # None instead of a false-positive "Сталь 45".
    if group == "steel_carbon" and "стал" not in key and "steel" not in key:
        return None
    for spec in MATERIAL_CATALOG.values():
        if spec.group == group:
            return spec
    return None


def canonical_material_designation(text: str) -> str:
    spec = classify_material(text)
    return spec.designation if spec else text


# ── Metric threads (ГОСТ 8724-2002 coarse + common fine pitches) ─────────────

@dataclass(frozen=True)
class ThreadSpec:
    designation: str
    major_d_mm: float
    coarse_pitch_mm: float
    fine_pitches_mm: tuple[float, ...] = ()


METRIC_THREAD_TABLE: dict[float, ThreadSpec] = {
    d: ThreadSpec(f"M{d:g}", d, p, fine)
    for d, p, fine in (
        (3, 0.5, ()),
        (4, 0.7, ()),
        (5, 0.8, ()),
        (6, 1.0, ()),
        (8, 1.25, (1.0,)),
        (10, 1.5, (1.25, 1.0)),
        (12, 1.75, (1.5, 1.25)),
        (14, 2.0, (1.5,)),
        (16, 2.0, (1.5,)),
        (18, 2.5, (2.0, 1.5)),
        (20, 2.5, (2.0, 1.5)),
        (22, 2.5, (2.0, 1.5)),
        (24, 3.0, (2.0, 1.5)),
        (27, 3.0, (2.0, 1.5)),
        (30, 3.5, (2.0, 1.5)),
        (33, 3.5, (2.0, 1.5)),
        (36, 4.0, (3.0, 2.0)),
        (39, 4.0, (3.0, 2.0)),
        (42, 4.5, (3.0, 2.0)),
        (45, 4.5, (3.0, 2.0)),
        (48, 5.0, (3.0, 2.0)),
        (52, 5.0, (4.0, 3.0, 2.0)),
        (56, 5.5, (4.0, 3.0, 2.0)),
        (60, 5.5, (4.0, 3.0, 2.0)),
    )
}

_THREAD_DESIGNATION_RE = re.compile(
    r"^M\s*(\d+(?:[.,]\d+)?)(?:\s*[×xXхХ]\s*(\d+(?:[.,]\d+)?))?$"
)


def parse_thread(designation: str) -> ThreadSpec | None:
    """Parse 'M20', 'M20x1.5', 'M20×1,5' → ThreadSpec, validated against the table."""
    if not designation:
        return None
    m = _THREAD_DESIGNATION_RE.match(designation.strip())
    if not m:
        return None
    d = float(m.group(1).replace(",", "."))
    base = METRIC_THREAD_TABLE.get(d)
    if not base:
        return None
    pitch_str = m.group(2)
    if not pitch_str:
        return base
    pitch = float(pitch_str.replace(",", "."))
    if pitch == base.coarse_pitch_mm:
        return base
    if pitch in base.fine_pitches_mm:
        return ThreadSpec(f"M{d:g}x{pitch:g}", d, pitch, ())
    return None


def minor_diameter_mm(thread: ThreadSpec, pitch_mm: float | None = None) -> float:
    """Internal (minor) diameter d1 = D - 1.0825*P (ISO 68-1)."""
    p = pitch_mm if pitch_mm is not None else thread.coarse_pitch_mm
    return thread.major_d_mm - 1.0825 * p


# ── Sheet formats (ГОСТ 2.301) ────────────────────────────────────────────────

@dataclass(frozen=True)
class SheetFormat:
    name: str
    width_mm: float
    height_mm: float


SHEET_FORMATS: dict[str, SheetFormat] = {
    "A4": SheetFormat("A4", 297, 210),
    "A3": SheetFormat("A3", 420, 297),
    "A2": SheetFormat("A2", 594, 420),
}

# Title block (ГОСТ 2.104 form 1) is a fixed 185×55mm regardless of sheet size —
# only its position on the sheet changes, never its own dimensions.
TITLE_BLOCK_W_MM = 185.0
TITLE_BLOCK_H_MM = 55.0

# Escalate to the next sheet size before falling back to a coarser scale than
# this — readability matters more than saving paper. 1:5 is still the hard
# ceiling ГОСТ 2.302 tolerates for a legible drawing, but we'd rather bump A4→
# A3→A2 than actually use it. A2 is this module's size ceiling (no A1/A0).
_PREFERRED_MAX_REDUCTION = 2


def choose_sheet_format(extent_mm: float, prefer: str | None = None) -> SheetFormat:
    """Auto-pick A4→A3→A2 so the drawing needs no worse than ~1:2 scale.

    Falls back to A2 (the biggest format this module supports) if even A2
    would need a coarser reduction — better than silently exceeding it.
    """
    if prefer and prefer in SHEET_FORMATS:
        return SHEET_FORMATS[prefer]
    for name in ("A4", "A3", "A2"):
        fmt = SHEET_FORMATS[name]
        usable = min(fmt.width_mm, fmt.height_mm) - 40  # margin/title-block allowance
        if extent_mm / usable <= _PREFERRED_MAX_REDUCTION:
            return fmt
    return SHEET_FORMATS["A2"]


# ── Hatching (ГОСТ 2.306) ─────────────────────────────────────────────────────
#
# All metals use the SAME hatch symbol (parallel 45° lines) regardless of the
# actual material — ГОСТ 2.306 does not encode material via hatch pattern.
# What the standard actually asks for: adjacent parts in an assembly section
# are hatched at alternating angles (or spacing) so they read as distinct
# components. Material only nudges the *pitch* (finer for small/precise
# sections), and drives the "Материал" text in the title block.


def hatch_angle_for_index(component_index: int) -> float:
    return 45.0 if component_index % 2 == 0 else -45.0


def hatch_pitch_mm(material: MaterialSpec | None, section_extent_mm: float) -> float:
    base = material.default_hatch_pitch_mm if material else 2.5
    if section_extent_mm < 10:
        return max(1.0, base * 0.6)
    return base
