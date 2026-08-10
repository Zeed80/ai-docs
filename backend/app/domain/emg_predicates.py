"""EngineeringModelGraph predicate registry: the same philosophy as
:mod:`app.domain.table_spec` applied to ``Assertion.predicate`` — a curated,
whitelisted catalog instead of hand-typed strings scattered across four
domain builders (``assembly_emg.py``, ``construction_emg.py``,
``system_emg.py``, ``mixed_emg.py``) and the API layer.

``Assertion.predicate`` itself stays a plain ``str`` (see
``engineering_model_graph.py``) — EMG is a graph, not a fixed schema, and a
human correction or a domain extension can legitimately introduce a
predicate this registry has never seen. What the registry buys is a single
place where the CORE vocabulary is named once, so ``"assembly.interference_
clear"`` cannot quietly drift into ``"assembly.interference_ok"`` in one
builder while the other three still write the original spelling. Import the
``PREDICATE`` constants below instead of retyping the string literal.

Two predicate families are deliberately NOT enumerated here:

- Templated families (``operation.param.*``, ``build.unresolved.<n>``,
  ``build.removed_assertion.<n>``) — registered once as a *pattern*, not one
  entry per instance; see ``TEMPLATED_PREDICATES``.
- The legacy-spec compatibility passthrough
  (``app.ai.cad_emg_compat.legacy_spec_as_low_assurance``): every leaf of the
  old free-form ``EngineeringDrawingSpec`` JSON becomes an assertion whose
  predicate IS that JSON's own dotted path (e.g.
  ``main_view.outer[0].diameter_mm``). That "vocabulary" already has a
  schema — the ``EngineeringDrawingSpec`` Pydantic model — and mirroring it
  here would mean maintaining the same set of names twice. Assertions on
  subject ``product:legacy-spec`` are exempt; see
  ``is_legacy_spec_passthrough``.

Use :func:`classify_predicate` to check an arbitrary ``(predicate,
subject_id)`` pair — the same classifier the registry self-test
(``backend/tests/domain/test_emg_predicates.py``) runs against every
graph the four domain builders actually produce.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.domain.engineering_model_graph import (
    PREDICATE_OPERATION_KIND,
    PREDICATE_OPERATION_SEQUENCE,
    NodeType,
)

Domain = Literal["shared", "assembly", "construction", "system", "mixed", "mechanical"]
ValueKind = Literal["exact", "interval", "enum_set", "expression", "unknown"]

# Predicate stage mirrors the project's model-catalog convention (CLAUDE.md:
# "core-набор = production ... дубли/устаревшее → disabled") — "example" marks
# vocabulary demonstrated only in tests, documenting where a future native
# mechanical builder should land once it stops leaning on the legacy-spec
# passthrough.
Stage = Literal["production", "example"]


@dataclass(frozen=True)
class PredicateDef:
    key: str
    domain: Domain
    subject_node_types: tuple[NodeType, ...]
    value_kinds: tuple[ValueKind, ...]
    unit: str | None
    description: str
    stage: Stage = "production"


def _predicates(*defs: PredicateDef) -> dict[str, PredicateDef]:
    seen: dict[str, PredicateDef] = {}
    for item in defs:
        if item.key in seen:
            raise ValueError(f"duplicate predicate key in registry: {item.key}")
        seen[item.key] = item
    return seen


# ── Shared across domains (Artifact / BuildOperation bookkeeping) ──────────────

_SHARED = (
    PredicateDef(
        "artifact.media_type", "shared", ("Artifact",), ("exact",), None,
        "MIME/media type of a rendered or exported artifact (svg/pdf/step/ifc/...).",
    ),
    PredicateDef(
        "artifact.sha256", "shared", ("Artifact",), ("exact",), None,
        "Content hash of a rebuilt artifact — the evidence a kernel reopen verified it.",
    ),
    PredicateDef(
        "artifact.bundle_sha256", "shared", ("Artifact",), ("exact",), None,
        "Hash of a composed multi-domain release bundle (zip/manifest).",
    ),
    PredicateDef(
        "artifact.bundle_complete", "shared", ("Artifact",), ("exact",), None,
        "Whether every required member artifact is present in a mixed-profile bundle.",
    ),
    PredicateDef(
        PREDICATE_OPERATION_KIND, "shared", ("BuildOperation",), ("exact",), None,
        "What kind of build step this operation is (e.g. assembly_instance, assembly_compile). "
        "compile_build_plan orders operations by this + operation.sequence regardless of profile.",
    ),
    PredicateDef(
        PREDICATE_OPERATION_SEQUENCE, "shared", ("BuildOperation",), ("exact",), None,
        "This operation's order among its siblings.",
    ),
)

# ── Assembly domain (assembly_emg.py) ───────────────────────────────────────────

_ASSEMBLY = (
    PredicateDef(
        "product.name", "assembly", ("Product",), ("exact",), None,
        "Assembly display name.",
    ),
    PredicateDef(
        "product.designation", "assembly", ("Product",), ("exact",), None,
        "Assembly designation/drawing number.",
    ),
    PredicateDef(
        "component.instance_key", "assembly", ("Component",), ("exact",), None,
        "Stable key of one component instance within the assembly.",
    ),
    PredicateDef(
        "component.designation", "assembly", ("Component",), ("exact",), None,
        "Designation of the component definition this instance is of.",
    ),
    PredicateDef(
        "component.quantity", "assembly", ("Component",), ("exact",), "1",
        "How many of this instance the assembly uses.",
    ),
    PredicateDef(
        "component.transform", "assembly", ("Component",), ("exact",), None,
        "Position/orientation of this instance within the assembly.",
    ),
    PredicateDef(
        "component.grounded", "assembly", ("Component",), ("exact",), None,
        "Whether this instance is grounded (fixed) for the DOF solver.",
    ),
    PredicateDef(
        "mate.type", "assembly", ("Constraint",), ("exact",), None,
        "Kind of mate constraint (coincident/concentric/distance/...).",
    ),
    PredicateDef(
        "mate.parameters", "assembly", ("Constraint",), ("exact",), None,
        "Mate-specific parameters (offset, angle, ...).",
    ),
    PredicateDef(
        "assembly.degrees_of_freedom", "assembly", ("Product",), ("exact",), "1",
        "Remaining unconstrained DOF of the assembly, from the declared-mate-rank solver.",
    ),
    PredicateDef(
        "assembly.interference_clear", "assembly", ("Product",), ("exact",), None,
        "Whether the exact B-Rep + AABB-fallback interference check found no collisions.",
    ),
    PredicateDef(
        "assembly.artifact_reopen_valid", "assembly", ("Product",), ("exact",), None,
        "Whether the compiled assembly STEP was verified by reopening it in the kernel.",
    ),
    PredicateDef(
        "assembly.required_2d_complete", "assembly", ("Product",), ("exact",), None,
        "Whether every required 2D assembly/construction sheet has been built.",
    ),
)

# ── Construction domain (construction_emg.py) ───────────────────────────────────

_CONSTRUCTION = (
    PredicateDef(
        "spatial.level", "construction", ("Component",), ("exact",), "mm",
        "Storey elevation.",
    ),
    PredicateDef(
        "element.kind", "construction", ("Feature",), ("exact",), None,
        "Building element kind (wall/slab/column/opening/...).",
    ),
    PredicateDef(
        "element.material", "construction", ("Feature",), ("exact", "unknown"), None,
        "Building element material.",
    ),
    PredicateDef(
        "geometry.box", "construction", ("Feature",), ("exact",), "mm",
        "Axis-aligned bounding box of a building element.",
    ),
    PredicateDef(
        "construction.openings_contained", "construction", ("System",), ("exact",), None,
        "Whether every opening element is fully contained by its host wall/slab.",
    ),
    PredicateDef(
        "construction.ifc_reopen_valid", "construction", ("System",), ("exact",), None,
        "Whether the compiled IFC was verified by reopening it.",
    ),
    PredicateDef(
        "construction.required_sheets_complete", "construction", ("System",), ("exact",), None,
        "Whether every required plan/section sheet has been built.",
    ),
)

# ── System / MEP domain (system_emg.py) ─────────────────────────────────────────

_SYSTEM = (
    PredicateDef(
        "system.kind", "system", ("System",), ("exact",), None,
        "System kind (mep/electrical/hydraulic/pid).",
    ),
    PredicateDef(
        "equipment.type", "system", ("Component",), ("exact",), None,
        "Equipment type of a system component.",
    ),
    PredicateDef(
        "port.direction", "system", ("Port",), ("exact",), None,
        "Port flow direction (in/out/bidirectional).",
    ),
    PredicateDef(
        "port.kind", "system", ("Port",), ("exact",), None,
        "Port kind (supply/return/...).",
    ),
    PredicateDef(
        "port.medium", "system", ("Port",), ("exact",), None,
        "Medium carried by this port (water/air/signal/...).",
    ),
    PredicateDef(
        "port.nominal_size", "system", ("Port",), ("exact",), None,
        "Nominal size/diameter of this port.",
    ),
    PredicateDef(
        "system.connectivity_closed", "system", ("System",), ("exact",), None,
        "Whether every port in the system connects to exactly one counterpart.",
    ),
    PredicateDef(
        "system.required_diagram_complete", "system", ("System",), ("exact",), None,
        "Whether the required connectivity diagram has been built.",
    ),
)

# ── Mixed / cross-profile (mixed_emg.py) ─────────────────────────────────────────

_MIXED = (
    PredicateDef(
        "cross_profile.link", "mixed", ("Constraint",), ("exact",), None,
        "One declared link between two member profiles in a mixed bundle "
        "(e.g. a construction opening a hydraulic run passes through).",
    ),
)

# ── Mechanical: literal (non-legacy-passthrough) predicates ─────────────────────
# cad_emg_compat.py — the digitization-graph and drawing-graph projections,
# distinct from legacy_spec_as_low_assurance's dotted-path passthrough.

_MECHANICAL = (
    PredicateDef(
        "observation.cadir_entity_json", "mechanical", ("Geometry",), ("exact",), None,
        "Raw CadIR entity, serialized — one vectorized/hybrid-traced drawing entity.",
    ),
    PredicateDef(
        "scale.mm_per_px", "mechanical", ("Sheet",), ("exact",), "mm",
        "Sheet scale recovered from the drawing graph.",
    ),
    PredicateDef(
        "material.designation", "mechanical", ("Product", "Component"), ("exact",), None,
        "Material designation (ГОСТ/DIN/...).",
    ),
    # native_feature_graph_additions (cad_emg_compat.py) — one Feature node
    # per read chamfer/fillet/groove/keyway/cross_hole/hole-pattern/profile-
    # section, replacing the flat legacy-spec dotted-path passthrough for
    # elements the reader gave a stable id (assign_stable_feature_ids).
    PredicateDef(
        "feature.kind", "mechanical", ("Feature",), ("exact",), None,
        "Feature kind — the read spec's own list name (chamfer/fillet/groove/"
        "keyway/cross_hole/axial_hole_pattern/circular_hole_pattern/hole/"
        "hole_pattern/slot/section_outer/section_bore).",
    ),
    PredicateDef(
        "feature.location", "mechanical", ("Feature",), ("exact",), None,
        "Where on the body a chamfer/fillet sits (left_end/right_end/"
        "shoulder/bore_mouth/bore) — the spec's own placement vocabulary, "
        "never a resolved edge id (the kernel resolves the edge itself).",
    ),
)

PREDICATES: dict[str, PredicateDef] = _predicates(
    *_SHARED, *_ASSEMBLY, *_CONSTRUCTION, *_SYSTEM, *_MIXED, *_MECHANICAL,
)

# Templated families: one entry per *pattern*, matched by prefix. The
# dynamic suffix (a parameter name or a 0-based index) carries no vocabulary
# of its own — see the call sites in assembly_emg.py / cad_emg_compat.py.
TEMPLATED_PREDICATES: dict[str, PredicateDef] = _predicates(
    PredicateDef(
        "operation.param.", "shared", ("BuildOperation",), ("exact",), None,
        "One named build-input parameter, keyed by name (operation.param.<name>).",
    ),
    PredicateDef(
        "build.unresolved.", "mechanical", ("BuildOperation",), ("exact",), None,
        "One unresolved-reading note carried into the build, by index.",
    ),
    PredicateDef(
        "build.removed_assertion.", "mechanical", ("BuildOperation",), ("exact",), None,
        "Record of one assertion a correction rebuild superseded, by index.",
    ),
    PredicateDef(
        "feature.param.", "mechanical", ("Feature",), ("exact",), None,
        "One named numeric/text field of a read feature, keyed by its spec "
        "field name (feature.param.diameter_mm, feature.param.depth_mm, ...) "
        "— mirrors operation.param. for the same reason: the field-name "
        "vocabulary is the spec's own Pydantic model, not worth duplicating "
        "one predicate per field here.",
    ),
)

# Prefix constants for the templated families above — import these instead of
# retyping the prefix string when building the dynamic predicate name.
OPERATION_PARAM_PREFIX = "operation.param."
BUILD_UNRESOLVED_PREFIX = "build.unresolved."
BUILD_REMOVED_ASSERTION_PREFIX = "build.removed_assertion."
FEATURE_PARAM_PREFIX = "feature.param."

# Subjects whose predicates are the legacy EngineeringDrawingSpec's own
# dotted JSON paths (app.ai.cad_emg_compat.legacy_spec_as_low_assurance) —
# an intentionally open namespace this registry does not enumerate.
LEGACY_SPEC_PASSTHROUGH_SUBJECTS = frozenset({"product:legacy-spec"})


def is_legacy_spec_passthrough(subject_id: str) -> bool:
    return subject_id in LEGACY_SPEC_PASSTHROUGH_SUBJECTS


Classification = Literal["registered", "templated", "legacy_passthrough", "unregistered"]


def classify_predicate(predicate: str, subject_id: str) -> Classification:
    """Classify one ``(predicate, subject_id)`` pair against the registry.

    Used by the registry's own self-test to check every assertion the four
    domain builders actually produce — not just the literals declared above.
    """
    if predicate in PREDICATES:
        return "registered"
    if any(predicate.startswith(prefix) and len(predicate) > len(prefix)
           for prefix in TEMPLATED_PREDICATES):
        return "templated"
    if is_legacy_spec_passthrough(subject_id):
        return "legacy_passthrough"
    return "unregistered"


def is_known_predicate(predicate: str, subject_id: str) -> bool:
    return classify_predicate(predicate, subject_id) != "unregistered"


# ── Named constants — import these instead of retyping string literals ─────────
# One attribute-style constant per production PredicateDef, so a typo shows up
# as an AttributeError at import time instead of a silent new predicate name.


class _PredicateConstants:
    def __init__(self, catalog: dict[str, PredicateDef]) -> None:
        for item in catalog.values():
            attr = item.key.upper().replace(".", "_").rstrip("_")
            setattr(self, attr, item.key)


PREDICATE = _PredicateConstants(PREDICATES)
