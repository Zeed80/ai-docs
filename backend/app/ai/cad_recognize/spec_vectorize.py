"""Understanding -> drafting vectorizer (the "two-model" path).

Model 1 (a VLM) reads a drawing into a structured feature/dimension SPEC;
Model 2 (here, a deterministic parametric drafter) constructs a CLEAN, editable
CAD IR from that spec. Nothing is traced pixel-by-pixel, so the result is clean
by construction and dimensionally driven by what the VLM read — and the same
drafter serves "draft from a description" (the spec can come from an engineer,
not only from an image).

This module holds the drafter (spec -> CadIR). The spec extractor (image ->
spec) lives alongside the existing VLM text reader.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from app.ai.cad_ir.schema import (
    Arc,
    CadIR,
    Circle,
    DimensionEntity,
    HatchRegion,
    Point,
    Segment,
    SourceInfo,
    TextEntity,
)


class SpecEvidence(BaseModel):
    """Auditable source observation backing one structured spec value."""

    image_index: int = Field(ge=0)
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    raw_text: str | None = None


class SpecTaper(BaseModel):
    """A section whose diameter changes along its length (ГОСТ 2.307).

    The sheet states a taper in whichever of three ways suits it — a ratio like
    7:24 on a spindle nose, an included angle, or simply the diameter at the far
    end. All three describe the same generatrix, so exactly one is required and
    the drafter derives the rest.
    """

    kind: Literal["ratio", "included_angle", "end_diameter"]
    ratio: str | None = None
    included_angle_deg: float | None = Field(default=None, gt=0, lt=180)
    end_diameter_mm: float | None = Field(default=None, gt=0)
    evidence: list[SpecEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_exactly_one_statement(self) -> "SpecTaper":
        stated = [
            self.ratio is not None,
            self.included_angle_deg is not None,
            self.end_diameter_mm is not None,
        ]
        if sum(stated) != 1:
            raise ValueError(
                "taper needs exactly one of ratio / included_angle_deg / end_diameter_mm"
            )
        return self


class SpecThread(BaseModel):
    """A threaded length (ГОСТ 2.311 — drawn conventionally, never modelled).

    ``nominal_diameter_mm`` is the OUTER diameter for an external thread, which
    is the one the stepped profile already carries: a thread does not change the
    silhouette, it annotates it.
    """

    designation: str = Field(min_length=2, max_length=40)
    system: Literal["metric", "trapezoidal", "pipe", "inch", "other"] = "metric"
    nominal_diameter_mm: float = Field(gt=0)
    pitch_mm: float | None = Field(default=None, gt=0, le=100)
    length_mm: float | None = Field(default=None, gt=0)
    hand: Literal["right", "left"] = "right"
    internal: bool = False
    evidence: list[SpecEvidence] = Field(default_factory=list)


class SpecChamfer(BaseModel):
    """A chamfer on one edge (ГОСТ 10948 sizes, "1x45°" on the sheet)."""

    size_mm: float = Field(gt=0)
    angle_deg: float = Field(default=45.0, gt=0, lt=90)
    # Where on the part: an end face, or the shoulder between two steps. The
    # exact edge is resolved by the kernel from the geometry it just built —
    # the reader states a PLACE, not an edge id it cannot know.
    location: Literal["left_end", "right_end", "shoulder", "bore_mouth"]
    at_z_mm: float | None = None
    at_diameter_mm: float | None = Field(default=None, gt=0)
    evidence: list[SpecEvidence] = Field(default_factory=list)


class SpecFillet(BaseModel):
    """A fillet radius, most often at a shoulder (stress relief)."""

    radius_mm: float = Field(gt=0)
    location: Literal["shoulder", "left_end", "right_end", "bore"]
    at_z_mm: float | None = None
    at_diameter_mm: float | None = Field(default=None, gt=0)
    evidence: list[SpecEvidence] = Field(default_factory=list)


class SpecGroove(BaseModel):
    """An annular groove cut into a turned surface (ГОСТ 8820 and friends).

    Either the depth or the root diameter says how deep it goes; stating both
    invites them to disagree, so exactly one is required.
    """

    kind: Literal[
        "relief", "o_ring", "retaining_ring", "thread_runout", "other"
    ] = "other"
    axial_position_mm: float
    width_mm: float = Field(gt=0)
    depth_mm: float | None = Field(default=None, gt=0)
    root_diameter_mm: float | None = Field(default=None, gt=0)
    internal: bool = False
    standard_ref: str | None = None
    evidence: list[SpecEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_one_depth_statement(self) -> "SpecGroove":
        if (self.depth_mm is None) == (self.root_diameter_mm is None):
            raise ValueError("groove needs exactly one of depth_mm / root_diameter_mm")
        return self


class SpecKeyway(BaseModel):
    """A keyway milled into a shaft (ГОСТ 23360 parallel, 24071 Woodruff).

    ``depth_mm`` is t1 — measured from the cylindrical surface inward, the way
    the standard tabulates it and the way the sheet dimensions it.
    """

    kind: Literal["parallel", "woodruff"] = "parallel"
    axial_start_mm: float
    length_mm: float = Field(gt=0)
    width_mm: float = Field(gt=0)
    depth_mm: float = Field(gt=0)
    angle_deg: float = 0.0
    end_type: Literal["closed", "open", "runout"] = "closed"
    standard_ref: str | None = None
    evidence: list[SpecEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_a_slot_not_a_point(self) -> "SpecKeyway":
        if self.length_mm < self.width_mm:
            raise ValueError("keyway length_mm must be at least its width_mm")
        return self


class SpecCrossHole(BaseModel):
    """A hole through the part ACROSS the axis — oil ways, cross-drillings."""

    diameter_mm: float = Field(gt=0)
    axial_position_mm: float
    angle_deg: float = 0.0
    through: bool | None = None
    depth_mm: float | None = Field(default=None, gt=0)
    counterbore_diameter_mm: float | None = Field(default=None, gt=0)
    counterbore_depth_mm: float | None = Field(default=None, gt=0)
    count: int = Field(default=1, ge=1, le=64)
    spacing_deg: float | None = None
    thread: SpecThread | None = None
    evidence: list[SpecEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_counterbore(self) -> "SpecCrossHole":
        if (self.counterbore_diameter_mm is None) != (self.counterbore_depth_mm is None):
            raise ValueError("counterbore needs both diameter and depth")
        if (
            self.counterbore_diameter_mm is not None
            and self.counterbore_diameter_mm <= self.diameter_mm
        ):
            raise ValueError("counterbore diameter must exceed pilot diameter")
        return self


class SpecAxialHolePattern(BaseModel):
    """Holes drilled from an end face, parallel to the rotation axis."""

    count: int = Field(ge=1, le=64)
    bolt_circle_diameter_mm: float = Field(gt=0)
    start_angle_deg: float = 0.0
    spacing_deg: float | None = None
    # The end view proves the pattern but does not, by itself, identify which
    # physical end face is shown. Keep that fact explicitly unknown.
    from_face: Literal["zmin", "zmax"] | None = None
    # Some tapped holes start on a recessed end-face pocket rather than on the
    # extreme envelope. This offset is measured inward from ``from_face``;
    # keeping it separate prevents the recess depth from being silently added
    # to, or subtracted from, the stated thread/drill depths.
    entry_offset_mm: float | None = Field(default=None, ge=0)
    entry_recess_diameter_mm: float | None = Field(default=None, gt=0)
    through: bool | None = None
    # A blind tapped hole has two different lengths on a real drawing: the
    # complete thread and the deeper drill point. ``depth_mm`` remains as a
    # compatibility alias for old records, but new reads keep both facts.
    depth_mm: float | None = Field(default=None, gt=0)
    thread_depth_mm: float | None = Field(default=None, gt=0)
    drill_depth_mm: float | None = Field(default=None, gt=0)
    # Manufacturing drill choice is NOT required geometry. When absent the
    # compiler derives the finished internal-thread minor diameter from the
    # metric thread standard and records that provenance explicitly.
    pilot_diameter_mm: float | None = Field(default=None, gt=0)
    # Diameter used to register the observed end view with a physical face.
    view_outer_diameter_mm: float | None = Field(default=None, gt=0)
    thread: SpecThread
    evidence: list[SpecEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _depth_matches_through_state(self) -> "SpecAxialHolePattern":
        depths = (self.depth_mm, self.thread_depth_mm, self.drill_depth_mm)
        if self.through is True and any(value is not None for value in depths):
            raise ValueError("through axial holes cannot also have blind depths")
        if self.through is False and not (self.drill_depth_mm or self.depth_mm):
            raise ValueError("blind axial holes require drill_depth_mm or depth_mm")
        thread_depth = self.thread_depth_mm or self.depth_mm
        drill_depth = self.drill_depth_mm or self.depth_mm
        if thread_depth and drill_depth and thread_depth > drill_depth:
            raise ValueError("thread_depth_mm cannot exceed drill_depth_mm")
        if (
            self.entry_recess_diameter_mm is not None
            and self.entry_recess_diameter_mm <= self.thread.nominal_diameter_mm
        ):
            raise ValueError("entry recess diameter must exceed thread diameter")
        return self


class SpecCircularHolePattern(BaseModel):
    """A repeated unthreaded hole family resolved across end/section views."""

    count: int = Field(ge=1, le=128)
    hole_diameter_mm: float = Field(gt=0)
    bolt_circle_diameter_mm: float = Field(gt=0)
    axis_mode: Literal["axial", "inclined"]
    start_angle_deg: float | None = None
    spacing_deg: float | None = Field(default=None, gt=0, le=360)
    from_face: Literal["zmin", "zmax"] | None = None
    entry_offset_mm: float = Field(default=0.0, ge=0)
    through: bool | None = None
    depth_mm: float | None = Field(default=None, gt=0)
    inclination_deg: float | None = Field(default=None, gt=0, lt=90)
    radial_direction: Literal["outward", "inward"] | None = None
    connection_station_mm: float | None = Field(default=None, ge=0)
    evidence: list[SpecEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _mode_has_build_geometry(self) -> "SpecCircularHolePattern":
        if self.through is True and self.depth_mm is not None:
            raise ValueError("through circular pattern cannot also have blind depth")
        if self.axis_mode == "axial":
            if self.inclination_deg is not None or self.radial_direction is not None:
                raise ValueError("axial pattern cannot carry inclined-hole direction")
            if self.through is False and self.depth_mm is None:
                raise ValueError("blind axial pattern requires depth_mm")
        elif self.inclination_deg is None or self.radial_direction is None:
            raise ValueError("inclined pattern requires inclination and radial direction")
        return self


class SpecSection(BaseModel):
    diameter_mm: float = Field(gt=0)
    length_mm: float | None = Field(default=None, gt=0)
    note: str | None = None
    # A conical step: the diameter above is the one at the section's START.
    taper: SpecTaper | None = None
    # A thread annotates this step; it never changes the silhouette.
    thread: SpecThread | None = None
    tolerance: str | None = None
    roughness: str | None = None
    evidence: list[SpecEvidence] = Field(default_factory=list)


class SpecHole(BaseModel):
    """Through-hole position relative to the profile centre, in millimetres."""

    center_x_mm: float
    center_y_mm: float
    diameter_mm: float = Field(gt=0)
    tolerance: str | None = None
    evidence: list[SpecEvidence] = Field(default_factory=list)


class SpecHolePattern(BaseModel):
    """Equally spaced through holes on a pitch circle."""

    kind: Literal["bolt_circle"] = "bolt_circle"
    count: int = Field(ge=2, le=128)
    bolt_circle_diameter_mm: float = Field(gt=0)
    hole_diameter_mm: float = Field(gt=0)
    start_angle_deg: float = 0.0
    tolerance: str | None = None
    evidence: list[SpecEvidence] = Field(default_factory=list)


class SpecSlot(BaseModel):
    """Capsule slot; length is the overall end-to-end dimension."""

    center_x_mm: float
    center_y_mm: float
    length_mm: float = Field(gt=0)
    width_mm: float = Field(gt=0)
    rotation_deg: float = 0.0
    tolerance: str | None = None
    evidence: list[SpecEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_capsule_length(self) -> "SpecSlot":
        if self.length_mm < self.width_mm:
            raise ValueError("slot length_mm must be greater than or equal to width_mm")
        return self


class SpecPrismaticProfile(BaseModel):
    shape: Literal["rectangle", "circle"]
    width_mm: float | None = Field(default=None, gt=0)
    height_mm: float | None = Field(default=None, gt=0)
    diameter_mm: float | None = Field(default=None, gt=0)
    corner_radius_mm: float | None = Field(default=None, gt=0)
    thickness_mm: float | None = Field(default=None, gt=0)
    holes: list[SpecHole] = Field(default_factory=list)
    hole_patterns: list[SpecHolePattern] = Field(default_factory=list)
    slots: list[SpecSlot] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_shape_dimensions(self) -> "SpecPrismaticProfile":
        if self.shape == "rectangle" and (self.width_mm is None or self.height_mm is None):
            raise ValueError("rectangle requires width_mm and height_mm")
        if (
            self.shape == "rectangle"
            and self.corner_radius_mm is not None
            and self.corner_radius_mm > min(self.width_mm, self.height_mm) / 2.0
        ):
            raise ValueError("corner_radius_mm exceeds half of the shorter side")
        if self.shape != "rectangle" and self.corner_radius_mm is not None:
            raise ValueError("corner_radius_mm is valid only for rectangle")
        if self.shape == "circle" and self.diameter_mm is None:
            raise ValueError("circle requires diameter_mm")
        return self


class SpecBody(BaseModel):
    name: str | None = None
    type: str = "unknown"
    outer: list[SpecSection] = Field(default_factory=list)
    bore: list[SpecSection] = Field(default_factory=list)
    profile: SpecPrismaticProfile | None = None
    # Where the bore starts and whether it comes out the other side. Before
    # this, every bore was drawn and built as a through hole from the left face,
    # because that was the only thing the contract could say — a blind or
    # offset bore came out as a different part with the same dimensions.
    bore_start_mm: float = Field(default=0.0, ge=0)
    bore_from_end: Literal["left", "right"] = "left"
    bore_blind: bool | None = None
    # Features cut INTO the body. None of these change the stepped silhouette,
    # which is why they live beside it rather than in outer[].
    chamfers: list[SpecChamfer] = Field(default_factory=list)
    fillets: list[SpecFillet] = Field(default_factory=list)
    grooves: list[SpecGroove] = Field(default_factory=list)
    keyways: list[SpecKeyway] = Field(default_factory=list)
    cross_holes: list[SpecCrossHole] = Field(default_factory=list)
    axial_holes: list[SpecAxialHolePattern] = Field(default_factory=list)
    circular_hole_patterns: list[SpecCircularHolePattern] = Field(default_factory=list)
    # Accepted only for compatibility with already stored prototype responses.
    # The deterministic drafter still requires explicit, complete outer[] data.
    features: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _features_must_fit_the_body(self) -> "SpecBody":
        """A feature outside the material it is cut into is a misread.

        Checked here rather than at build time because the kernel would either
        fail obscurely or, worse, succeed on a part that is not the one drawn.
        """
        total_length = sum(
            section.length_mm for section in self.outer if section.length_mm
        )
        max_radius = max(
            (section.diameter_mm / 2.0 for section in self.outer), default=0.0
        )
        if total_length <= 0:
            return self
        for index, groove in enumerate(self.grooves):
            if not (0.0 <= groove.axial_position_mm <= total_length):
                raise ValueError(
                    f"groove {index} sits at {groove.axial_position_mm} mm, "
                    f"outside the {total_length} mm part"
                )
            if groove.depth_mm and max_radius and groove.depth_mm >= max_radius:
                raise ValueError(f"groove {index} is deeper than the part's radius")
        for index, keyway in enumerate(self.keyways):
            if keyway.axial_start_mm < 0 or (
                keyway.axial_start_mm + keyway.length_mm > total_length + 1e-6
            ):
                raise ValueError(f"keyway {index} runs past the end of the part")
            if max_radius and keyway.depth_mm >= max_radius:
                raise ValueError(f"keyway {index} is deeper than the part's radius")
        for index, hole in enumerate(self.cross_holes):
            if not (0.0 <= hole.axial_position_mm <= total_length):
                raise ValueError(
                    f"cross hole {index} sits outside the {total_length} mm part"
                )
        return self


class SpecView(BaseModel):
    """One projection the source sheet shows for one body (ГОСТ 2.305).

    A view is a READ observation ("the sheet also carries a left view of body
    0"), never a drafting instruction the model invents: the drafter builds
    each requested projection from the SAME validated dimensions as the front
    view, so projection alignment is exact by construction.
    """

    kind: Literal["front", "top", "side", "section", "detail", "removed_section"]
    view_id: str | None = Field(default=None, max_length=40)
    body_index: int = Field(default=0, ge=0)
    label: str | None = None
    parent_view_id: str | None = Field(default=None, max_length=40)
    relation: Literal[
        "primary", "orthographic", "section", "detail", "removed_section"
    ] | None = None
    # Model-space cutting definition. It is optional because a plain central
    # longitudinal section needs no path; an offset section must carry one or
    # remain explicitly unsupported by the coverage gate.
    section_origin_mm: float | None = None
    section_path_mm: list[tuple[float, float, float]] = Field(
        default_factory=list, max_length=64
    )
    # A detail is a crop of its parent projection, never a separately guessed
    # sketch. Coordinates are in the parent's model-space projection: u grows
    # right from the part's left end for longitudinal views, v grows up from
    # the part axis/centre. Radius is a real part size; magnification affects
    # paper scale only.
    detail_center_mm: tuple[float, float] | None = None
    detail_radius_mm: float | None = Field(default=None, gt=0)
    detail_scale_factor: float = Field(default=2.0, ge=1.0, le=10.0)
    features_shown: list[str] = Field(default_factory=list, max_length=64)
    evidence: list[SpecEvidence] = Field(default_factory=list)

class SpecDimension(BaseModel):
    value: str = Field(min_length=1)
    applies_to: str = ""
    evidence: list[SpecEvidence] = Field(default_factory=list)


class SpecAnnotation(BaseModel):
    kind: Literal[
        "roughness", "hardness", "tolerance", "datum", "thread", "weld",
        "material", "other",
    ]
    text: str = Field(min_length=1)
    value: str | None = None
    symbol: str | None = None
    datum_refs: list[str] = Field(default_factory=list, max_length=8)
    evidence: list[SpecEvidence] = Field(default_factory=list)


def prismatic_profile_is_complete(profile: "SpecPrismaticProfile | None") -> bool:
    """Is this outline sufficient to build a plate/flange on its own?

    Shape dimensions come from the model validator, so only the thickness — the
    one value that needs a side view — has to be checked here.
    """
    if profile is None:
        return False
    return bool(profile.thickness_mm and profile.thickness_mm > 0)


class EngineeringDrawingSpec(BaseModel):
    """Fail-closed contract between drawing recognition and CAD drafting."""

    schema_version: Literal[1] = 1
    part: str = ""
    main_view: SpecBody
    parts: list[SpecBody] = Field(default_factory=list)
    # Extra projections the sheet carries. Empty = front view only, which is
    # what every spec produced before views existed — so old specs still draft.
    views: list[SpecView] = Field(default_factory=list)
    dimensions: list[SpecDimension] = Field(default_factory=list)
    annotations: list[SpecAnnotation] = Field(default_factory=list)
    title_block: dict[str, Any] = Field(default_factory=dict)
    unresolved: list[str] = Field(default_factory=list)
    optional_unresolved: list[str] = Field(default_factory=list)
    # A reader may recover useful dimensions/PMI while geometry is incomplete
    # or schema-invalid.  Keep those observations visible, but make it
    # impossible to mistake this envelope for buildable geometry.
    observation_only: bool = False
    geometry_validation_errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _record_incomplete_rotation_sections(self) -> "EngineeringDrawingSpec":
        bodies = [self.main_view, *self.parts]
        for body_index, body in enumerate(bodies):
            prismatic = any(
                word in body.type.lower()
                for word in ("призмат", "пласт", "флан", "plate", "flange")
            )
            if prismatic and body.profile is None:
                self.unresolved.append(f"body:{body_index}:profile-missing")
            rotation = any(word in body.type.lower() for word in ("вращ", "вал", "shaft"))
            if not rotation:
                continue
            # The TYPE is the model's classification and it is unreliable: a
            # flange read perfectly as {circle, Ø560, thickness 20, bolt circle}
            # was rejected live because the reader had also labelled it "тело
            # вращения" and the rotation checks demand stepped sections. What
            # can be built is decided by the DATA, not by the label.
            if prismatic_profile_is_complete(body.profile):
                continue
            if len(body.outer) < 2:
                self.unresolved.append(f"body:{body_index}:outer-profile-incomplete")
            for section_index, section in enumerate(body.outer):
                if section.length_mm is None:
                    self.unresolved.append(
                        f"body:{body_index}:outer:{section_index}:length-missing"
                    )
            for section_index, section in enumerate(body.bore):
                if section.length_mm is None:
                    self.unresolved.append(
                        f"body:{body_index}:bore:{section_index}:length-missing"
                    )
        for view_index, view in enumerate(self.views):
            if view.body_index >= len(bodies):
                self.unresolved.append(
                    f"view:{view_index}:body-index-out-of-range"
                )
        optional_markers = (
            "масштаб", "материал", "обозначен", "штамп", "основн", "масса",
            "scale", "material", "designation", "title block", "mass",
        )
        blocking = []
        optional = list(self.optional_unresolved)
        for item in self.unresolved:
            if any(marker in item.lower() for marker in optional_markers):
                optional.append(item)
            else:
                blocking.append(item)
        self.unresolved = sorted(set(blocking))
        self.optional_unresolved = sorted(set(optional))
        return self


def _whole_sheet_reader_schema() -> dict[str, Any]:
    """Compact structured-output schema used only by the whole-sheet reader.

    Dimensions, notes and the title block are read by dedicated fragment/OCR
    passes and merged back by ``read_spec_best_effort``. Asking the whole-sheet
    model to repeat them made a dense A3 shaft hit 6000 output tokens three
    times in a row before it could close the JSON. Evidence is also omitted
    here: uncertain values must become ``unresolved``; the source-image map and
    the exact raw response remain in the audit trail.

    The returned object is still validated against the full
    ``EngineeringDrawingSpec`` before it can reach geometry generation.
    """
    import copy

    schema = copy.deepcopy(EngineeringDrawingSpec.model_json_schema())
    properties = schema.get("properties") or {}
    for field in ("dimensions", "annotations", "title_block"):
        properties.pop(field, None)
    required = schema.get("required")
    if isinstance(required, list):
        schema["required"] = [
            field for field in required
            if field not in {"dimensions", "annotations", "title_block"}
        ]

    def strip_audit_fields(node: Any) -> None:
        if isinstance(node, dict):
            node_properties = node.get("properties")
            if isinstance(node_properties, dict):
                node_properties.pop("evidence", None)
                node_properties.pop("features", None)
            node_required = node.get("required")
            if isinstance(node_required, list):
                node["required"] = [
                    field for field in node_required
                    if field not in {"evidence", "features"}
                ]
            for value in node.values():
                strip_audit_fields(value)
        elif isinstance(node, list):
            for value in node:
                strip_audit_fields(value)

    strip_audit_fields(schema)
    return schema


_SPEC_PROMPT = (
    "Ты — инженер-конструктор. Изучи чертёж и опиши деталь СТРУКТУРНО для "
    "повторного черчения. Изображение 0 — общий вид листа, остальные изображения "
    "— полноразмерные перекрывающиеся фрагменты; их границы перечислены ниже. "
    "Верни СТРОГО JSON:\n"
    '{"schema_version":1,"part":"название",'
    '"main_view":{"type":"тело вращения (вал)|призматическая",'
    '"outer":[{"diameter_mm":0,"length_mm":0,"note":"резьба/конус/...",'
    '"evidence":[{"image_index":1,"bbox":[0,0,100,30],"raw_text":"Ø40"}]}],'
    '"bore":[{"diameter_mm":0,"length_mm":0,"note":"..."}],'
    '"profile":{"shape":"rectangle|circle","width_mm":0,"height_mm":0,'
    '"corner_radius_mm":null,'
    '"diameter_mm":null,"thickness_mm":0,"holes":['
    '{"center_x_mm":0,"center_y_mm":0,"diameter_mm":0,"tolerance":null}],'
    '"hole_patterns":[{"kind":"bolt_circle","count":6,'
    '"bolt_circle_diameter_mm":140,"hole_diameter_mm":14,'
    '"start_angle_deg":0,"tolerance":null}],'
    '"slots":[{"center_x_mm":0,"center_y_mm":0,"length_mm":40,'
    '"width_mm":12,"rotation_deg":0,"tolerance":null}]}},'
    '"parts":[{"name":"..","type":"..","outer":[...],"bore":[...]}],'
    '"views":[{"kind":"front|top|side|section|detail|removed_section",'
    '"view_id":"main","body_index":0,"label":"А-А",'
    '"parent_view_id":null,"relation":"primary|orthographic|section|detail|removed_section",'
    '"section_origin_mm":null,"section_path_mm":[],'
    '"detail_center_mm":null,"detail_radius_mm":null,"detail_scale_factor":2,'
    '"features_shown":[]}],'
    '"dimensions":[{"value":"Ø80js6","applies_to":".."}],'
    '"annotations":[{"kind":"roughness|hardness|tolerance|datum|thread|weld",'
    '"text":"..","value":null,"symbol":null,"datum_refs":[]}],'
    '"title_block":{"material":"..","scale":".."},'
    '"unresolved":["обязательная геометрия, которую не удалось доказать"],'
    '"optional_unresolved":["необязательные метаданные: материал/масштаб/штамп"]}\n'
    "ПРАВИЛА для тела вращения:\n"
    "1) outer[] — ВСЕ ступени наружного контура ПО ПОРЯДКУ слева направо, БЕЗ "
    "пропусков (включая резьбовые участки — бери наружный диаметр резьбы, и "
    "конусы — средний диаметр).\n"
    "2) length_mm — ОСЕВАЯ ДЛИНА ИМЕННО ЭТОЙ ступени, а НЕ размер с цепочки. "
    "Если на чертеже цепочка накопительных размеров от торца — вычисли длину "
    "ступени как РАЗНОСТЬ соседних размеров.\n"
    "3) Сумма length_mm ≈ полная длина детали (сверься с габаритным размером).\n"
    "4) Если деталь ПОЛАЯ (в разрезе видно осевое отверстие/расточку) — опиши "
    "внутренний контур в bore[] так же по порядку.\n"
    "5) Фаски/канавки/шпонпазы/поперечные отверстия в outer/bore НЕ включай — "
    "они не меняют силуэт. Для них есть отдельные массивы тела (все "
    "необязательные, пиши только реально видимое):\n"
    '"chamfers":[{"size_mm":1,"angle_deg":45,"location":"left_end|right_end|shoulder",'
    '"at_diameter_mm":null}],'
    '"grooves":[{"axial_position_mm":0,"width_mm":3,"depth_mm":1.5}],'
    '"keyways":[{"axial_start_mm":0,"length_mm":85,"width_mm":12,"depth_mm":5}],'
    '"cross_holes":[{"diameter_mm":9,"axial_position_mm":0,"count":1,"through":true}]\n'
    "Конус ступени: добавь ей "
    '"taper":{"kind":"ratio|included_angle|end_diameter","ratio":"7:24"} — '
    "ровно одно из трёх полей. Резьбовой участок: добавь ступени "
    '"thread":{"designation":"M75x1,5","nominal_diameter_mm":75,"pitch_mm":1.5}.\n'
    "Глухая или смещённая расточка: у тела есть bore_start_mm (от торца), "
    'bore_from_end:"left|right", bore_blind:true|false.\n'
    "ПРАВИЛО для местного вида detail: укажи detail_center_mm=[u,v] и "
    "detail_radius_mm только если граница увеличиваемой области однозначно "
    "связана с родительской проекцией. Для продольного вида u отсчитывается "
    "вправо от левого торца детали, v — вверх от оси; для торцевого вида обе "
    "координаты отсчитываются от центра. detail_scale_factor — отношение "
    "масштаба местного вида к масштабу родительского. Если центр или радиус "
    "не доказаны, оставь их null: такой detail должен остаться блокером.\n"
    "ПРАВИЛА для пластин/фланцев:\n"
    "1) profile обязателен: rectangle требует width_mm+height_mm, circle — diameter_mm; "
    "для явно скруглённых углов rectangle укажи corner_radius_mm по выноске R, не по картинке.\n"
    "2) Координаты holes задавай от ЦЕНТРА профиля: +x вправо, +y вверх.\n"
    "3) Включай только отверстия с доказанными диаметром и двумя координатами.\n"
    "4) Равномерный массив отверстий по делительной окружности задавай ОДНИМ "
    "hole_patterns: count, bolt_circle_diameter_mm (PCD), hole_diameter_mm и "
    "start_angle_deg; 0° означает первое отверстие справа, углы растут против часовой.\n"
    "5) Продолговатый паз задавай в slots: центр, габаритные length_mm и width_mm, "
    "rotation_deg; 0° — горизонтальный паз, углы растут против часовой.\n"
    "Если деталей несколько — каждую в parts[], главную продублируй в main_view.\n"
    "ПРАВИЛА для views[]: перечисли ТОЛЬКО те проекции, которые РЕАЛЬНО есть на "
    "листе, кроме главного вида (он подразумевается всегда). Для тела вращения "
    "вид слева — это концентрические окружности справа от главного вида. "
    "body_index нумерует тела как [main_view, parts[0], parts[1], ...]: 0 — "
    "главное тело. Не заказывай вид, которого на листе нет: чертёжник строит "
    "каждую проекцию из ТЕХ ЖЕ размеров, и лишний вид будет ложью об исходном "
    "листе.\n"
    "Читай только реально видимые значения. ЗАПРЕЩЕНО угадывать, усреднять или "
    "достраивать отсутствующие размеры. Неизвестное оставь null и добавь причину "
    "в unresolved.\n"
    # Output budget, not style. Measured on real answers: whitespace was 27-52%
    # of the response and \uXXXX escaping of Cyrillic another 30% — together
    # more than half the output. Two readers ran past the token limit and were
    # cut off mid-JSON, losing a correct reading entirely, so the contract is
    # deliberately terse.
    "ФОРМАТ ОТВЕТА (обязательно):\n"
    "1) Верни JSON ОДНОЙ строкой: без переносов строк, без отступов, без "
    "лишних пробелов.\n"
    "2) Кириллицу пиши буквами как есть. НЕ экранируй её как \\uXXXX — это "
    "впятеро раздувает ответ.\n"
    "3) note и applies_to — не длиннее 40 символов; если сказать нечего, ставь "
    "null, а не пустую строку или «..».\n"
    "4) evidence прикладывай ТОЛЬКО к значениям, которые трудно прочитать или "
    "в которых ты не уверен. Для очевидных размеров evidence не нужен.\n"
    "5) Никаких пояснений до или после JSON. Только JSON.\n"
    "6) views, dimensions, annotations, title_block, unresolved — поля ВЕРХНЕГО "
    "уровня, рядом с main_view и parts. НЕ вкладывай их внутрь main_view и НЕ "
    "открывай перед ними новый объект «{»: весь ответ — ОДИН объект."
)

_DESCRIPTION_SPEC_PROMPT = (
    "Ты преобразуешь текстовое техническое задание в EngineeringDrawingSpec. "
    "Не черти и не вычисляй отсутствующие размеры. Используй тот же JSON-контракт "
    "и правила, что ниже. Для текстового задания evidence оставляй пустым. Если "
    "ЗАПРОШЕННЫЙ обязательный размер не указан однозначно, добавь его в unresolved. "
    "Не требуй параметры, которых в задании нет: прямоугольный профиль означает "
    "прямые углы, пока скругления явно не упомянуты; неуказанные общие допуски, "
    "материал, шероховатость и данные штампа относятся к optional_unresolved и "
    "не блокируют номинальную геометрию. Только JSON.\n"
)


def _normalize_model_unresolved(spec: dict, description: str) -> dict:
    """Demote model-requested metadata that was never requested by the engineer."""
    source = description.lower()
    explicitly_requests_tolerance = any(
        marker in source for marker in ("допуск", "tolerance")
    )
    explicitly_requests_rounding = any(
        marker in source
        for marker in ("скругл", "радиус", "галтел", "rounded", "fillet", "radius")
    )
    blocking: list[str] = []
    optional = list(spec.get("optional_unresolved") or [])
    for item in spec.get("unresolved") or []:
        lowered = str(item).lower()
        model_added_tolerance = any(
            marker in lowered for marker in ("допуск", "tolerance")
        ) and not explicitly_requests_tolerance
        model_added_rounding = any(
            marker in lowered
            for marker in ("скругл", "радиус", "галтел", "rounded", "fillet", "radius")
        ) and not explicitly_requests_rounding
        if model_added_tolerance or model_added_rounding:
            optional.append(str(item))
        else:
            blocking.append(str(item))
    spec["unresolved"] = sorted(set(blocking))
    spec["optional_unresolved"] = sorted(set(optional))
    return spec


async def read_description_spec(
    description: str, *, router: Any | None = None, confidential: bool = True
) -> dict:
    """Turn an engineer's text into the same auditable drafting contract.

    A ready JSON spec bypasses the model but never Pydantic validation. Free
    text uses the locally assigned CAD_SPEC_READ model and cannot use cloud.
    """
    text = description.strip()
    if not text:
        return {}
    parsed = _coerce_spec_containers(_parse_spec_json(text))
    if parsed:
        try:
            return EngineeringDrawingSpec.model_validate(parsed).model_dump(mode="json")
        except ValidationError as exc:
            _log_spec_rejected("description_json", exc)
            return {}

    from app.ai.schemas import AIRequest, AITask, ChatMessage

    if router is None:
        from app.ai.router import ai_router

        router = ai_router
    request = AIRequest(
        task=AITask.CAD_SPEC_READ,
        messages=[ChatMessage(
            role="user",
            content=_DESCRIPTION_SPEC_PROMPT + _SPEC_PROMPT + "\nОПИСАНИЕ:\n" + text,
        )],
        confidential=confidential,
        allow_cloud=False,
    )
    try:
        response = await router.run(request)
    except Exception:  # noqa: BLE001
        return {}
    parsed = _coerce_spec_containers(_parse_spec_json(response.text or ""))
    if not parsed:
        return {}
    try:
        validated = EngineeringDrawingSpec.model_validate(parsed).model_dump(mode="json")
        return _normalize_model_unresolved(validated, text)
    except ValidationError as exc:
        _log_spec_rejected("description_model", exc)
        return {}


_MAX_SPEC_TILES = 8


def _tile_budget_boxes(
    boxes: list[tuple[int, int, int, int]], columns: int, rows: int, budget: int
) -> list[tuple[int, int, int, int]]:
    """Thin a tile grid down to ``budget`` WITHOUT dropping a band of the sheet.

    The tiles are a row-major grid, so picking evenly spaced entries of the flat
    list interacts with the row width and can miss whole COLUMNS: measured on a
    wide sheet, an 8x3 grid kept only 6 of the 8 columns, and 9x2 and 12x2 kept
    8 of 9 and 8 of 12. A vertical band of the drawing was simply never shown to
    the reader, and nothing reported it.

    So the grid is thinned by keeping at least one tile in every row and every
    column first, and only then spending what is left of the budget on the rest.
    """
    if len(boxes) <= budget:
        return boxes
    keep: set[int] = set()
    # One per row (walking the columns so the picks are not all in column 0),
    # then one per column — together this guarantees no band is invisible.
    for row in range(rows):
        keep.add(row * columns + (row % columns))
    for column in range(columns):
        if not any(index % columns == column for index in keep):
            keep.add((column % rows) * columns + column)
    if len(keep) > budget:
        keep = set(sorted(keep)[:budget])
    remaining = [index for index in range(len(boxes)) if index not in keep]
    room = budget - len(keep)
    if room > 0 and remaining:
        stride = max(1, len(remaining) // room)
        keep.update(remaining[::stride][:room])
    return [boxes[index] for index in sorted(keep)]


def _spec_images(
    image, *, tile_size: int = 1400, overlap: int = 160
) -> tuple[list[bytes], list[str], float]:
    """Build a context image plus source-resolution tiles without data loss.

    Returns the encoded images, their descriptions and the SHARE OF THE SHEET
    the tiles actually cover. A sheet too large for the tile budget is shown
    only in part, and the caller records that: a value the reader never saw is
    missing for a reason nobody could otherwise discover.
    """
    import io

    context = image.copy()
    context.thumbnail((1400, 1400))
    buffer = io.BytesIO()
    context.save(buffer, format="PNG")
    encoded = [buffer.getvalue()]
    descriptions = [f"image 0: overview 0,0,{image.width},{image.height}"]
    if image.width <= tile_size and image.height <= tile_size:
        return encoded, descriptions, 1.0
    step = tile_size - overlap
    xs = list(range(0, max(image.width - tile_size, 0) + 1, step))
    ys = list(range(0, max(image.height - tile_size, 0) + 1, step))
    if not xs or xs[-1] != max(image.width - tile_size, 0):
        xs.append(max(image.width - tile_size, 0))
    if not ys or ys[-1] != max(image.height - tile_size, 0):
        ys.append(max(image.height - tile_size, 0))
    # Bound latency for unusually large sheets while covering both edges and centre.
    boxes = [(x, y, min(x + tile_size, image.width), min(y + tile_size, image.height)) for y in ys for x in xs]
    full_grid = len(boxes)
    boxes = _tile_budget_boxes(boxes, len(xs), len(ys), _MAX_SPEC_TILES)
    for index, box in enumerate(boxes, start=1):
        tile = image.crop(box)
        tile_buffer = io.BytesIO()
        tile.save(tile_buffer, format="PNG")
        encoded.append(tile_buffer.getvalue())
        descriptions.append(f"image {index}: source bbox {box[0]},{box[1]},{box[2]},{box[3]}")
    coverage = _tile_coverage(boxes, image.width, image.height)
    if len(boxes) < full_grid:
        descriptions.append(
            f"внимание: показано {len(boxes)} из {full_grid} фрагментов листа"
        )
    return encoded, descriptions, coverage


def _tile_coverage(
    boxes: list[tuple[int, int, int, int]], width: int, height: int
) -> float:
    """Share of the sheet the chosen tiles cover, overlaps counted once."""
    if not boxes or width <= 0 or height <= 0:
        return 0.0
    # Sheets are small enough that a coarse occupancy grid is exact enough and
    # needs no geometry library.
    cells = 64
    seen: set[tuple[int, int]] = set()
    for x0, y0, x1, y1 in boxes:
        cx0 = int(x0 * cells / width)
        cx1 = max(cx0 + 1, math.ceil(x1 * cells / width))
        cy0 = int(y0 * cells / height)
        cy1 = max(cy0 + 1, math.ceil(y1 * cells / height))
        for cy in range(cy0, min(cy1, cells)):
            for cx in range(cx0, min(cx1, cells)):
                seen.add((cx, cy))
    return round(len(seen) / float(cells * cells), 3)


async def read_drawing_spec_consensus(
    image_bytes: bytes,
    *,
    passes: int = 3,
    router: Any | None = None,
    confidential: bool = True,
) -> dict:
    """Read the sheet several times and keep only what the reads agree on.

    Passes run SEQUENTIALLY on purpose: three concurrent vision calls share one
    GPU, and the first live attempt at that returned a truncated answer in 11
    seconds. A pass that raises (truncation, malformed JSON) is dropped rather
    than aborting the set — the remaining passes may still agree — but if every
    pass fails, the last error is re-raised so the caller reports the real
    reason instead of a bare "no spec".
    """
    from app.ai.cad_recognize.spec_consensus import consensus_spec

    if passes < 2:
        return await read_drawing_spec(
            image_bytes, router=router, confidential=confidential
        )

    reads: list[dict] = []
    last_error: Exception | None = None
    from app.ai.cad_process_log import record_cad_process_event

    for _attempt in range(passes):
        await record_cad_process_event(
            "reader.whole_sheet.pass",
            "started",
            f"Полное чтение листа: проход {_attempt + 1}/{passes}",
            {"pass": _attempt + 1, "passes": passes},
        )
        try:
            spec = await read_drawing_spec(
                image_bytes, router=router, confidential=confidential
            )
        except (SpecReadTruncatedError, SpecReadMalformedError) as exc:
            last_error = exc
            await record_cad_process_event(
                "reader.whole_sheet.pass",
                "failed",
                f"Полное чтение листа: проход {_attempt + 1}/{passes} отклонён",
                {"pass": _attempt + 1, "error": f"{type(exc).__name__}: {exc}"[:400]},
            )
            continue
        await record_cad_process_event(
            "reader.whole_sheet.pass",
            "completed" if spec else "failed",
            f"Полное чтение листа: проход {_attempt + 1}/{passes} завершён",
            {"pass": _attempt + 1, "valid_spec": bool(spec)},
        )
        if spec:
            reads.append(spec)
    if not reads and last_error is not None:
        raise last_error
    merged = consensus_spec(reads)
    if merged:
        merged["reader_attempts"] = [
            {"pass": index + 1, "mode": "whole_sheet", "spec": spec}
            for index, spec in enumerate(reads)
        ]
    return merged


async def read_drawing_spec(
    image_bytes: bytes, *, router: Any | None = None, confidential: bool = True
) -> dict:
    """Model 1: a VLM reads the drawing into a structured feature/dimension spec.

    Robust to real scans (understanding, not pixel localisation). Returns {} on
    failure so the caller can fall back to the tracing method.
    """
    import base64
    import hashlib
    import io
    import time

    from PIL import Image

    from app.ai.schemas import AIRequest, AITask, ChatMessage
    from app.ai.cad_process_log import record_cad_process_event
    from app.ai.vlm_dimensions import _parse_json_array  # tolerant fence stripping

    if router is None:
        from app.ai.router import ai_router

        router = ai_router
    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:  # noqa: BLE001
        return {}
    images, tile_descriptions, tile_coverage = _spec_images(image)
    source_images: list[dict[str, Any]] = []
    for image_index, (encoded, description) in enumerate(zip(images, tile_descriptions)):
        rendered = Image.open(io.BytesIO(encoded))
        numbers = [int(value) for value in re.findall(r"\d+", description)]
        source_bbox = (
            numbers[-4:]
            if len(numbers) >= 5
            else [0, 0, image.width, image.height]
        )
        source_images.append({
            "image_index": image_index,
            "image_width": rendered.width,
            "image_height": rendered.height,
            "source_bbox": source_bbox,
        })
    # Dedicated slot for the spec reader (Settings → Models → Оцифровка). When it
    # has no assignment, fall back to the shared drawing-analysis VLM so behaviour
    # is unchanged out of the box.
    from app.ai.task_routing import get_routing_for

    read_task = (
        AITask.CAD_SPEC_READ
        if get_routing_for(AITask.CAD_SPEC_READ).primary
        else AITask.DRAWING_ANALYSIS_VLM
    )
    # A text-only model in a vision slot burns minutes of GPU and answers with
    # an empty string, which reads as "unreadable drawing" rather than
    # "misassigned slot". Pin the first SEEING candidate; refuse outright when
    # the whole chain is blind.
    seeing_model, chain_can_see = _first_vision_model(read_task)
    if not chain_can_see:
        raise SpecReaderNotVisionError(
            "слот «Чтение чертежа (VLM)» назначен на модель без зрения "
            f"({get_routing_for(read_task).primary}). Назначьте vision-модель "
            "в Настройки → Модели → Оцифровка."
        )
    request = AIRequest(
        task=read_task,
        messages=[ChatMessage(
            role="user",
            content=(
                _SPEC_PROMPT
                + "\nДЛЯ ЭТОГО ПОЛНОГО ПРОХОДА верни только геометрию: "
                "main_view, parts, views, unresolved и optional_unresolved. "
                "Не повторяй dimensions, annotations, title_block и evidence — "
                "они читаются отдельными специализированными проходами."
                + "\nКАРТА ИЗОБРАЖЕНИЙ:\n"
                + "\n".join(tile_descriptions)
            ),
        )],
        images=[base64.b64encode(value).decode() for value in images],
        confidential=confidential,
        allow_cloud=False,
        preferred_model=seeing_model,
        # The old 24k budget let a misrouted thinking model burn the entire
        # Celery deadline on one answer.  Real valid specs are far smaller; 6k
        # keeps enough room for escaped Cyrillic while bounding a runaway pass.
        metadata={
            "num_predict": 6000,
            "json_schema": _whole_sheet_reader_schema(),
        },
    )
    started = time.monotonic()
    await record_cad_process_event(
        "reader.whole_sheet.request",
        "started",
        "VLM получил полный лист и карту фрагментов",
        {
            "model": seeing_model,
            "images": len(images),
            "tile_coverage": tile_coverage,
            "num_predict": 6000,
            "thinking": get_routing_for(read_task).thinking,
        },
    )
    try:
        response = await router.run(request)
    except Exception as exc:  # noqa: BLE001 — never sink the pipeline on a VLM error
        await record_cad_process_event(
            "reader.whole_sheet.request",
            "failed",
            "VLM-вызов полного листа завершился ошибкой",
            {
                "model": seeing_model,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "error": f"{type(exc).__name__}: {exc}"[:400],
            },
        )
        return {}
    answer = response.text or ""
    answer_sha256 = hashlib.sha256(answer.encode("utf-8")).hexdigest()
    response_details = {
        "model": response.model or seeing_model,
        "duration_ms": response.usage.latency_ms,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "answer_chars": len(answer),
        "answer_sha256": answer_sha256,
        "answer_preview": answer[:2000],
        "answer_tail": answer[-2000:],
        "thinking_chars": len(str((response.raw or {}).get("thinking") or "")),
        "done_reason": (response.raw or {}).get("done_reason"),
    }
    try:
        parsed = _coerce_spec_containers(_parse_spec_json(answer, strict=True))
    except (SpecReadTruncatedError, SpecReadMalformedError) as exc:
        await record_cad_process_event(
            "reader.whole_sheet.request",
            "failed",
            "Ответ полного чтения получен, но JSON не завершён",
            {**response_details, "error": f"{type(exc).__name__}: {exc}"[:400]},
        )
        raise
    if not parsed:
        await record_cad_process_event(
            "reader.whole_sheet.request",
            "failed",
            "Ответ полного чтения пуст или не является валидным JSON",
            {
                **response_details,
            },
        )
        return {}
    try:
        validated = EngineeringDrawingSpec.model_validate(parsed).model_dump(mode="json")
    except ValidationError as exc:
        _log_spec_rejected("drawing_image", exc)
        await record_cad_process_event(
            "reader.whole_sheet.request",
            "failed",
            "JSON полного чтения не прошёл EngineeringDrawingSpec",
            {
                "validation_errors": len(exc.errors()),
                "model": response.model or seeing_model,
                "answer_sha256": answer_sha256,
                "answer_preview": answer[:2000],
            },
        )
        return {}
    # A sheet the reader was only shown in part explains a missing value better
    # than any guess about the model. Optional: it never blocks geometry, but it
    # must be visible when something turns out to be missing.
    # Carried as a NOTE rather than a field: consensus rebuilds the spec dict
    # from scratch and keeps only the contract's own keys, so an extra field
    # would silently vanish on the multi-pass path while the note survives.
    if tile_coverage < 0.999:
        validated.setdefault("optional_unresolved", []).append(
            f"лист показан модели не полностью: покрыто {tile_coverage:.0%} площади"
        )
    validated["source_images"] = source_images
    # Keep the exact model answer beside the validated interpretation. This is
    # audit data, never geometry input: consensus rebuilds the accepted spec and
    # the raw answer remains under reader_attempts for a person to compare.
    validated["reader_raw_response"] = response.text or ""
    await record_cad_process_event(
        "reader.whole_sheet.request",
        "completed",
        "Ответ полного чтения принят в EngineeringDrawingSpec",
        {
            **response_details,
        },
    )
    return validated


_LIST_FIELDS_TOP = (
    "parts", "views", "dimensions", "annotations", "unresolved",
    "optional_unresolved",
)
_LIST_FIELDS_BODY = (
    "outer", "bore", "features", "chamfers", "fillets", "grooves",
    "keyways", "cross_holes", "axial_holes", "circular_hole_patterns",
)
_LIST_FIELDS_PROFILE = ("holes", "hole_patterns", "slots")
_ANNOTATION_KINDS = frozenset(
    {
        "roughness", "hardness", "tolerance", "datum", "thread", "weld",
        "material", "other",
    }
)


def _coerce_spec_containers(spec: dict) -> dict:  # noqa: C901
    """Normalise list-shaped fields the reader got structurally wrong.

    Models routinely return ``null`` or a bare object where the contract asks
    for a list — a live read of a real sheet was discarded whole because
    ``hole_patterns`` came back as one object instead of a one-element list.
    Only representation shape is repaired here: no measured value is invented
    or changed, so a missing dimension still blocks drafting exactly as before.
    """

    def as_list(value: Any) -> list:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    def clean_evidence(value: Any) -> list:
        """Evidence is provenance ABOUT a value, never the value itself.

        A malformed citation must not discard the dimension it annotates: a
        live read lost five perfectly good shaft sections because the model
        wrote its evidence entries in the wrong shape. Unusable entries are
        dropped; a plain string is kept as ``raw_text`` on the overview image,
        which is where the reader looks by default.
        """
        cleaned: list = []
        for item in as_list(value):
            if isinstance(item, dict):
                if isinstance(item.get("image_index"), bool) or not isinstance(
                    item.get("image_index"), int
                ):
                    item = {**item, "image_index": 0}
                bbox = item.get("bbox")
                if bbox is not None and (
                    not isinstance(bbox, list) or len(bbox) != 4
                    or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in bbox)
                ):
                    item = {**item, "bbox": None}
                cleaned.append(item)
            elif isinstance(item, str) and item.strip():
                cleaned.append({"image_index": 0, "raw_text": item.strip()[:200]})
        return cleaned

    if not isinstance(spec, dict):
        return spec
    for field in _LIST_FIELDS_TOP:
        if field in spec:
            spec[field] = as_list(spec[field])
    # ``unresolved``/``optional_unresolved`` are free-text REASONS. A reader that
    # writes one of them as an object (live: {"field": ..., "why": ...}) used to
    # discard the whole sheet — a note about what is missing must never delete
    # what was found.
    for field in ("unresolved", "optional_unresolved"):
        items = spec.get(field)
        if isinstance(items, list):
            spec[field] = [
                item if isinstance(item, str)
                else json.dumps(item, ensure_ascii=False)[:300]
                for item in items
                if item is not None
            ]
    bodies = [spec.get("main_view"), *(spec.get("parts") or [])]
    for body in bodies:
        if not isinstance(body, dict):
            continue
        for field in _LIST_FIELDS_BODY:
            if field in body:
                body[field] = as_list(body[field])
        profile = body.get("profile")
        if isinstance(profile, dict):
            for field in _LIST_FIELDS_PROFILE:
                if field in profile:
                    profile[field] = as_list(profile[field])
            # A hole or a pattern without a positive diameter describes nothing.
            # It used to invalidate the WHOLE sheet: a bearing-housing drawing
            # scored 0/2 diameters, 0/1 fits and 0/3 roughness purely because
            # one pattern came back with a null hole_diameter_mm. Drop the
            # unusable entry, keep the sheet.
            for field, size_key in (
                ("holes", "diameter_mm"),
                ("hole_patterns", "hole_diameter_mm"),
                ("slots", "width_mm"),
            ):
                items = profile.get(field)
                if isinstance(items, list):
                    profile[field] = [
                        item for item in items
                        if isinstance(item, dict)
                        and isinstance(item.get(size_key), (int, float))
                        and not isinstance(item.get(size_key), bool)
                        and item[size_key] > 0
                    ]
        for field in _LIST_FIELDS_BODY:
            for item in body.get(field) or []:
                if not isinstance(item, dict):
                    continue
                if "evidence" in item:
                    item["evidence"] = clean_evidence(item["evidence"])
                thread = item.get("thread")
                if isinstance(thread, dict):
                    if "evidence" in thread:
                        thread["evidence"] = clean_evidence(thread["evidence"])
                    # A metric designation already states its nominal. Models
                    # often repeat only "M8" in the nested object even though
                    # the same callout was read correctly. This is parsing the
                    # designation, not supplying a drill diameter or pitch.
                    if thread.get("nominal_diameter_mm") is None:
                        designation = str(thread.get("designation") or "")
                        nominal = re.search(
                            r"[MМ]\s*(\d+(?:[.,]\d+)?)",
                            designation,
                            re.IGNORECASE,
                        )
                        if nominal:
                            thread["nominal_diameter_mm"] = float(
                                nominal.group(1).replace(",", ".")
                            )
                taper = item.get("taper")
                if isinstance(taper, dict) and not taper.get("kind"):
                    stated = [
                        field
                        for field in ("ratio", "included_angle_deg", "end_diameter_mm")
                        if taper.get(field) is not None
                    ]
                    if len(stated) == 1:
                        taper["kind"] = {
                            "ratio": "ratio",
                            "included_angle_deg": "included_angle",
                            "end_diameter_mm": "end_diameter",
                        }[stated[0]]
    for field in ("dimensions", "annotations", "views"):
        for item in spec.get(field) or []:
            if isinstance(item, dict) and "evidence" in item:
                item["evidence"] = clean_evidence(item["evidence"])
    for index, item in enumerate(spec.get("views") or []):
        if not isinstance(item, dict):
            continue
        if item.get("detail_scale_factor") is None:
            item.pop("detail_scale_factor", None)
        path = item.get("section_path_mm")
        valid_path = (
            isinstance(path, list)
            and all(
                isinstance(point, (list, tuple))
                and len(point) == 3
                and all(
                    isinstance(value, (int, float)) and not isinstance(value, bool)
                    for value in point
                )
                for point in path
            )
        )
        if path and not valid_path:
            item["section_path_mm"] = []
            label = item.get("view_id") or item.get("label") or index
            spec.setdefault("unresolved", []).append(
                f"view:{label}: путь сечения не подтверждён: {str(path)[:80]}"
            )
    # Metadata whose exact wording the contract fixes, but the reader does not
    # know that: a null "applies_to" and an annotation kind outside the
    # enumeration are normalised rather than allowed to reject the sheet. The
    # VALUE is untouched — only the shape the schema insists on.
    for item in spec.get("dimensions") or []:
        if isinstance(item, dict) and item.get("applies_to") is None:
            item["applies_to"] = ""
    for item in spec.get("annotations") or []:
        if isinstance(item, dict) and item.get("kind") not in _ANNOTATION_KINDS:
            original = str(item.get("kind") or "").strip()
            item["kind"] = "other"
            if original and not str(item.get("text") or "").startswith(original):
                item["text"] = f"{original}: {item.get('text') or ''}".strip()
    return spec


class SpecReadTruncatedError(RuntimeError):
    """The reader ran out of output room and cut its JSON mid-object."""


class SpecReadMalformedError(RuntimeError):
    """The reader finished, but mis-nested the structure it was asked for."""


def _parse_spec_json(raw: str, *, strict: bool = False) -> dict:
    """Parse the reader's JSON. With ``strict``, a truncated answer RAISES.

    Truncation must never be salvaged by closing the open braces: the missing
    tail is usually the rest of ``outer[]``, so a "repaired" spec would draft a
    shorter part that looks perfectly valid. Better to fail loudly.
    """
    import json
    import re

    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if not (0 <= start < end):
        return {}
    body = text[start : end + 1]
    try:
        value = json.loads(body)
        return value if isinstance(value, dict) else {}
    except ValueError as first_error:
        repaired = _repair_early_close(body, first_error)
        if repaired is not None:
            return repaired
        exc = first_error
        if strict:
            # Truncation and a structural slip need different answers: one is
            # "the model ran out of room", the other is "the model mis-nested a
            # field". Reporting the wrong one sends the reader hunting for a
            # limit that was never hit. The discriminator is real nesting depth
            # — a cut-off answer often ends right after an inner "}", so
            # looking at the last character alone is not enough.
            if _unclosed_depth(body) > 0:
                raise SpecReadTruncatedError(
                    "модель чтения оборвала ответ на середине JSON (не хватило "
                    "лимита вывода). Попробуйте ещё раз или назначьте другую "
                    "модель в Настройки → Модели → Оцифровка."
                ) from None
            raise SpecReadMalformedError(
                f"модель чтения вернула структурно некорректный JSON: {exc}"
            ) from None
        return {}
    except TypeError:
        return {}


def _unclosed_depth(text: str) -> int:
    """How many containers are still open at the end, ignoring string literals.

    Braces and brackets inside quoted values (a note like "паз {2}") must not
    count, or every such reading would look truncated.
    """
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
    return max(depth, 0)


def _repair_early_close(body: str, error: ValueError) -> dict | None:
    """Re-join a top-level object the reader closed one brace too early.

    Observed live: a reader emitted ``...}]}},"parts":[],"views":[...]`` — the
    document was complete, but one stray ``}`` split it, and json.loads
    reported "Extra data". The continuation is the model's own output, so
    re-attaching it invents nothing; only the spurious brace is removed. If the
    result still does not parse, or the tail is not a continuation of the same
    object, nothing is repaired and the caller reports the malformation.

    Deliberately NOT applied to truncation: there the tail does not exist, so
    "repair" would mean fabricating the missing part of the drawing.
    """
    import json

    if "Extra data" not in str(error):
        return None
    position = getattr(error, "pos", None)
    if not isinstance(position, int) or position <= 0 or position >= len(body):
        return None
    head, tail = body[:position], body[position:]
    if not head.rstrip().endswith("}") or not tail.lstrip().startswith(","):
        return None
    candidate = head.rstrip()[:-1] + tail
    try:
        value = json.loads(candidate)
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    import structlog

    structlog.get_logger(__name__).info(
        "cad_spec_json_repaired", reason="early_close", position=position
    )
    return value


def _num(value: Any) -> float | None:
    """Best-effort numeric from a spec field (handles '30', 30, 'Ø30h6')."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    import re

    match = re.search(r"-?\d+(?:[.,]\d+)?", str(value))
    return float(match.group().replace(",", ".")) if match else None


# A section is any coaxial body-of-revolution segment. The VLM labels these
# inconsistently (cylinder/cone/step/neck/journal/shaft/…), so accept any
# feature that carries a diameter UNLESS it is clearly a sub-feature cut into
# the body (a hole/keyway/thread/chamfer/groove).
_SUB_FEATURES = {"hole", "keyway", "thread", "chamfer", "groove", "slot", "bore", "fillet"}


def _sections_from_list(items: Any) -> list[dict]:
    """Ordered (diameter, length) sections from a plain outer/bore list.

    The taper and the thread travel with the section rather than being dropped
    here: this compact form is what both the drafter and the solid builder
    consume, so anything left out of it cannot be built no matter how well the
    sheet was read.
    """
    sections: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        diameter = _num(it.get("diameter_mm")) or _num(it.get("diameter"))
        if diameter is not None and diameter > 0:
            sections.append({
                "d": diameter,
                "l": _num(it.get("length_mm")) or _num(it.get("length")),
                "note": it.get("note"),
                "taper": it.get("taper"),
                "thread": it.get("thread"),
                "tolerance": it.get("tolerance"),
                "roughness": it.get("roughness"),
            })
    return sections


def taper_end_diameter(section: dict) -> float | None:
    """The diameter at the FAR end of a conical section, in millimetres.

    A sheet states a cone in whichever way suits it — a ratio (7:24 on a
    spindle nose), an included angle, or the far diameter outright — and all
    three describe the same generatrix. Resolving them here means the drafter
    and the solid builder cannot disagree about what a taper is.

    Returns ``None`` for a plain cylindrical section, or when the statement is
    incomplete: a cone whose length is unknown has no computable end diameter,
    and guessing one would invent geometry.
    """
    taper = section.get("taper")
    if not isinstance(taper, dict):
        return None
    start = _num(section.get("d"))
    length = _num(section.get("l"))
    if not start or start <= 0:
        return None

    end = _num(taper.get("end_diameter_mm"))
    if end and end > 0:
        return end
    if not length or length <= 0:
        return None

    ratio_text = taper.get("ratio")
    if isinstance(ratio_text, str) and ratio_text.strip():
        # "7:24" means 7 of diameter change per 24 of length; "1:10" likewise.
        parts = ratio_text.replace(" ", "").split(":")
        if len(parts) == 2:
            try:
                rise, run = float(parts[0].replace(",", ".")), float(parts[1].replace(",", "."))
            except ValueError:
                return None
            if run > 0:
                change = rise / run * length
                return _tapered(start, change, taper)
        return None

    angle = _num(taper.get("included_angle_deg"))
    if angle and 0 < angle < 180:
        # The included angle spans BOTH generatrices, so the diameter changes
        # by 2 * L * tan(angle/2).
        change = 2.0 * length * math.tan(math.radians(angle / 2.0))
        return _tapered(start, change, taper)
    return None


def _tapered(start: float, change: float, taper: dict) -> float | None:
    """Apply a diameter change in the direction the sheet states."""
    end = start - change if taper.get("direction") != "increasing" else start + change
    return end if end > 0 else None


def _sections_from_features(features: Any) -> list[dict]:
    """Ordered sections from a legacy ``features`` list (kind-filtered)."""
    sections: list[dict] = []
    for feature in features or []:
        if not isinstance(feature, dict):
            continue
        kind = str(feature.get("kind", "")).lower()
        if any(sub in kind for sub in _SUB_FEATURES):
            continue
        diameter = _num(feature.get("diameter_mm")) or _num(feature.get("diameter"))
        if diameter is not None and diameter > 0:
            sections.append({
                "d": diameter,
                "l": _num(feature.get("length_mm")) or _num(feature.get("length")),
                "note": feature.get("note"),
            })
    return sections


def _outer_sections(node: dict) -> list[dict]:
    """Outer profile sections of a body node: prefer ``outer``, else ``features``."""
    if node.get("outer"):
        return _sections_from_list(node.get("outer"))
    return _sections_from_features(node.get("features", []))


def _bore_sections(node: dict) -> list[dict]:
    """Inner bore sections of a body node (empty when solid)."""
    return _sections_from_list(node.get("bore"))


def _rotation_sections(spec: dict) -> list[dict]:
    """Outer sections of the single main-view rotation body (back-compat helper)."""
    return _outer_sections(spec.get("main_view") or {})


def _rotation_parts(spec: dict) -> list[dict]:
    """One body descriptor PER rotation body: {"outer":[...], "bore":[...]}.

    Uses ``parts[]`` when the reader found several bodies, else ``main_view``.
    Only bodies with ≥2 outer sections qualify (a real stepped profile), so
    prismatic parts fall through to the generative model.

    ``body_index`` is carried through so ``views[]`` can name a body: it indexes
    ``[main_view, *parts]``, exactly like the spec validator's numbering.
    """
    result: list[dict] = []
    for offset, part in enumerate(spec.get("parts") or []):
        if not isinstance(part, dict):
            continue
        outer = _outer_sections(part)
        if len(outer) >= 2:
            result.append(_rotation_body(part, outer, offset + 1))
    if not result:
        main = spec.get("main_view") or {}
        outer = _outer_sections(main)
        if len(outer) >= 2:
            result.append(_rotation_body(main, outer, 0))
    return result


# Features cut into a turned body. They ride along with the profile because a
# consumer that receives the silhouette without them builds a smooth stand-in
# and has no way to know something was left behind.
_BODY_FEATURE_FIELDS = (
    "chamfers", "fillets", "grooves", "keyways", "cross_holes", "axial_holes",
    "circular_hole_patterns",
)


def _rotation_body(node: dict, outer: list[dict], body_index: int) -> dict:
    """One rotation body: its profile, its bore, and everything cut into it."""
    body = {
        "outer": outer,
        "bore": _bore_sections(node),
        "body_index": body_index,
        "bore_start_mm": _num(node.get("bore_start_mm")) or 0.0,
        "bore_from_end": node.get("bore_from_end") or "left",
        "bore_blind": node.get("bore_blind"),
    }
    for field in _BODY_FEATURE_FIELDS:
        items = node.get(field)
        body[field] = [item for item in items if isinstance(item, dict)] if items else []
    return body


def _sections_are_complete(sections: list[dict]) -> bool:
    """A missing dimension is an unresolved fact, never a drafting hint."""
    return bool(sections) and all(section.get("d") and section.get("l") for section in sections)


def _emit_profile(
    sections: list[dict], px_per_mm: float, x_left: float, axis_y: float, seg,
    bore: list[dict] | None = None, sectioned: bool = False,
) -> float:
    """Emit one stepped rotation profile (both generatrices + its OWN axis).

    The axis is CONSTRUCTED here, never guessed — the profile is exactly
    symmetric about it. When ``bore`` is given the part is hollow: its inner
    stepped contour is drawn symmetric about the same axis. Returns the right
    edge x (for canvas sizing).
    """
    x = x_left
    prev_r = None
    for s in sections:
        length_px = s["l"] * px_per_mm
        r = s["d"] * px_per_mm / 2.0
        if prev_r is None:
            seg(x, axis_y - r, x, axis_y + r)  # left end cap
        elif abs(r - prev_r) > 0.5:
            seg(x, axis_y - prev_r, x, axis_y - r)
            seg(x, axis_y + prev_r, x, axis_y + r)
        seg(x, axis_y - r, x + length_px, axis_y - r)  # top generatrix
        seg(x, axis_y + r, x + length_px, axis_y + r)  # bottom generatrix
        x += length_px
        prev_r = r
    right = x
    seg(x, axis_y - prev_r, x, axis_y + prev_r)  # right end cap

    if bore:
        # Inner bore contour (hollow part), symmetric about the same axis.
        # An unsectioned view hides the bore behind material: per ГОСТ 2.303
        # that is a dashed thin line. In a longitudinal section the same edges
        # are cut and become solid contour lines.
        bore_cls, bore_w = ("contour", "main") if sectioned else ("hidden", "thin")
        bx = x_left
        prev_br = None
        for s in bore:
            length_px = s["l"] * px_per_mm
            br = s["d"] * px_per_mm / 2.0
            if prev_br is None:
                seg(bx, axis_y - br, bx, axis_y + br, bore_cls, bore_w)  # mouth
            elif abs(br - prev_br) > 0.5:
                seg(bx, axis_y - prev_br, bx, axis_y - br, bore_cls, bore_w)
                seg(bx, axis_y + prev_br, bx, axis_y + br, bore_cls, bore_w)
            seg(bx, axis_y - br, bx + length_px, axis_y - br, bore_cls, bore_w)
            seg(bx, axis_y + br, bx + length_px, axis_y + br, bore_cls, bore_w)
            bx += length_px
            prev_br = br
        if prev_br is not None and bx < right:
            seg(bx, axis_y - prev_br, bx, axis_y + prev_br, bore_cls, bore_w)

    seg(x_left - 20, axis_y, right + 20, axis_y, cls="axis", width="thin")  # centreline
    return right


def _requested_view_kinds(spec: dict, body_indices: set[int]) -> set[str]:
    """Which extra projections the reader saw for this body.

    "front" is implicit (the drafter always builds it), so it is filtered out
    here — the caller only needs to know what to ADD. ``body_indices`` is a set
    because ``main_view`` duplicates the first entry of ``parts[]``: a view
    naming either index means the same body.
    """
    kinds: set[str] = set()
    for view in spec.get("views") or []:
        if not isinstance(view, dict):
            continue
        try:
            index = int(view.get("body_index", 0))
        except (TypeError, ValueError):
            continue
        kind = str(view.get("kind") or "")
        if index in body_indices and kind in ("top", "side", "section"):
            kinds.add(kind)
    return kinds


def _section_wall_loops(
    sections: list[dict], bore: list[dict], px_per_mm: float, x_left: float, axis_y: float
) -> list[list[Point]]:
    """Wall polygons of a longitudinal section, above and below the axis.

    A hollow rotation body is normally SHOWN in section, not with dashed bore
    lines: the material between the outer contour and the bore is what gets
    hatched. Both loops are built from the same stepped profiles the front view
    uses, sampled column by column, so the section can never disagree with the
    view it cuts.
    """

    def _profile_edges(steps: list[dict]) -> list[tuple[float, float, float]]:
        edges: list[tuple[float, float, float]] = []
        x = x_left
        for step in steps:
            length_px = float(step["l"]) * px_per_mm
            edges.append((x, x + length_px, float(step["d"]) * px_per_mm / 2.0))
            x += length_px
        return edges

    outer_edges = _profile_edges(sections)
    bore_edges = _profile_edges(bore)
    if not outer_edges or not bore_edges:
        return []
    # Every x where either contour changes radius — the exact column breaks.
    breaks = sorted({e[0] for e in outer_edges} | {e[1] for e in outer_edges}
                    | {e[0] for e in bore_edges} | {e[1] for e in bore_edges})

    def _radius_at(edges: list[tuple[float, float, float]], x: float) -> float:
        for x0, x1, radius in edges:
            if x0 - 1e-9 <= x < x1 - 1e-9:
                return radius
        return 0.0

    loops: list[list[Point]] = []
    for sign in (-1.0, 1.0):
        upper: list[Point] = []
        lower: list[Point] = []
        for x0, x1 in zip(breaks, breaks[1:], strict=False):
            outer_r = _radius_at(outer_edges, x0)
            bore_r = _radius_at(bore_edges, x0)
            if outer_r <= 0.0 or outer_r - bore_r <= 1e-6:
                continue
            outer_y = axis_y + sign * outer_r
            bore_y = axis_y + sign * bore_r
            upper += [Point(x=x0, y=outer_y), Point(x=x1, y=outer_y)]
            # The return path must run right-to-left WITHIN each column too,
            # otherwise the loop self-intersects and hatches as a bowtie.
            lower = [Point(x=x1, y=bore_y), Point(x=x0, y=bore_y)] + lower
        if len(upper) >= 2 and len(lower) >= 2:
            loops.append(upper + lower)
    return [loop for loop in loops if len(loop) >= 3]


# Gap between projections, in millimetres of the part (scaled with it).
_VIEW_GAP_MM = 15.0


def _log_spec_rejected(source: str, exc: ValidationError) -> None:
    """Record WHICH field killed a spec — a silent {} is undiagnosable.

    A whole live read of a real sheet was once discarded because one optional
    container came back as an object instead of a list, and nothing anywhere
    said so.
    """
    import structlog

    structlog.get_logger(__name__).warning(
        "cad_spec_rejected",
        source=source,
        fields=[
            ".".join(str(part) for part in err["loc"]) for err in exc.errors()[:8]
        ],
        messages=[err["msg"] for err in exc.errors()[:8]],
    )


class SpecReaderNotVisionError(RuntimeError):
    """The CAD reader slot points at a model that cannot see images."""


def _first_vision_model(task: Any) -> tuple[str | None, bool]:
    """First vision-capable model in this task's chain, and whether one exists.

    A text-only model answers an image request with an empty string and HTTP
    200, so the router never falls through to the next candidate — it looks
    like an unreadable drawing instead of a misassigned slot. Resolving the
    first SEEING model here and pinning it as ``preferred_model`` skips blind
    candidates without disabling the rest of the fallback chain.

    Unknown models are treated as capable: the catalog is not exhaustive, and
    refusing on a missing entry would be worse than trying.
    """
    from app.ai.model_registry import ModelRegistry
    from app.ai.task_routing import get_routing_for

    try:
        registry = ModelRegistry.from_yaml(
            "backend/app/ai/config/model_registry.yaml"
        )
    except Exception:  # noqa: BLE001 — never block drafting on a config read
        return None, True
    chain = list(get_routing_for(task).models or [])
    if not chain:
        return None, True
    for key in chain:
        capability = registry.models.get(key)
        if capability is None or "vision" in {m.value for m in capability.modalities}:
            return key, True
    return None, False


def _read_dimension_index(spec: dict) -> list[tuple[float, str, bool]]:
    """Index the dimensions the reader actually saw: (value, text, is_diameter).

    The drafter builds geometry from the nominal numbers, but a drawing is not
    its nominals: ``Ø80js6`` and ``80`` are different instructions to the shop.
    This index lets an emitted dimension carry the ORIGINAL string — tolerance,
    fit, prefix and all — instead of a re-formatted number.
    """
    index: list[tuple[float, str, bool]] = []
    for dim in spec.get("dimensions") or []:
        if not isinstance(dim, dict):
            continue
        text = str(dim.get("value") or "").strip()
        kind = _callout_kind(text)
        if kind is None:
            continue
        value = _num(text)
        if value is None or value <= 0:
            continue
        index.append((value, text, kind == "diameter"))
    return index


# A thread designation IS the diameter callout for that step — the sheet writes
# M75x1,5 where it would otherwise write Ø75, and drawing "Ø75" instead loses
# the thread. Anything matching here is a diameter.
_THREAD_CALLOUT = re.compile(r"^\s*(?:M|М|Tr|G|G1/)\s*\d", re.IGNORECASE)
# Not a size a dimension measures. Found live on the spindle: "R4" matched a
# 4 mm step and was drawn as its LENGTH, putting a radius label on a distance
# dimension. Angles, chamfer notes, roughness and hardness fail the same way.
_NOT_A_SIZE = re.compile(
    r"^\s*R\s*\d|°|\bRa\b|\bRz\b|\bHRC\b|\bHB\b|^\s*\d+\s*[x×хX]\s*\d+\s*°?\s*$",
    re.IGNORECASE,
)


def _callout_kind(text: str) -> str | None:
    """"diameter", "linear", or None when the callout is not a size at all.

    Letting a non-size into the nominal index means one of them eventually
    lands on a feature whose number happens to match, and the drawing then
    states something the sheet never did.
    """
    if not text:
        return None
    if _THREAD_CALLOUT.match(text):
        return "diameter"
    if _NOT_A_SIZE.search(text):
        return None
    return "diameter" if text.lstrip()[:1] in ("Ø", "⌀", "D", "d") else "linear"


def _dimension_text(
    index: list[tuple[float, str, bool]], value_mm: float, *, diameter: bool
) -> str:
    """The read text for this nominal, or a plain formatted number.

    Matching is exact-by-nominal (0.5% window for reading noise) and honours
    the Ø prefix, so a Ø40 diameter never steals a 40 mm length's tolerance.
    A nominal the reader never wrote down falls back to the bare number — the
    drafter states what it built and claims nothing more.
    """
    fallback = f"Ø{value_mm:g}" if diameter else f"{value_mm:g}"
    best: str | None = None
    for read_value, text, is_diameter in index:
        if is_diameter != diameter:
            continue
        if abs(read_value - value_mm) > max(0.05, abs(value_mm) * 0.005):
            continue
        # Prefer the richest reading (a tolerance/fit suffix beats a bare number).
        if best is None or len(text) > len(best):
            best = text
    return best or fallback


def _emit_rotation_side_view(
    sections: list[dict],
    bore: list[dict] | None,
    px_per_mm: float,
    center_x: float,
    center_y: float,
    entities: list[Any],
) -> None:
    """Draft the left view of a rotation body: concentric circles.

    Placed by the caller at the SAME ``center_y`` as the front view's axis, so
    ГОСТ 2.305 projection alignment holds exactly — it is constructed, not
    eyeballed. Every circle comes from a diameter the front view already used,
    so the two views can never disagree.
    """
    common = {"origin": "spec", "assurance": "inferred"}
    diameters = sorted({float(s["d"]) for s in sections}, reverse=True)
    for diameter in diameters:
        entities.append(
            Circle(
                center=Point(x=center_x, y=center_y),
                radius=diameter * px_per_mm / 2.0,
                line_class="contour",
                width_class="main",
                **common,
            )
        )
    for section in bore or []:
        entities.append(
            Circle(
                center=Point(x=center_x, y=center_y),
                radius=float(section["d"]) * px_per_mm / 2.0,
                line_class="hidden",
                width_class="thin",
                **common,
            )
        )
    # Both centrelines of the left view (ГОСТ 2.305 requires the axes shown).
    outer_r = diameters[0] * px_per_mm / 2.0 if diameters else 0.0
    over = outer_r + 4.0 * px_per_mm
    entities.append(
        Segment(
            p1=Point(x=center_x - over, y=center_y),
            p2=Point(x=center_x + over, y=center_y),
            line_class="axis", width_class="thin", **common,
        )
    )
    entities.append(
        Segment(
            p1=Point(x=center_x, y=center_y - over),
            p2=Point(x=center_x, y=center_y + over),
            line_class="axis", width_class="thin", **common,
        )
    )


# ГОСТ 2.301 sheet sizes (short, long) mm; ГОСТ 2.302 standard scale series.
_GOST_SHEETS: dict[str, tuple[float, float]] = {
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "A2": (420.0, 594.0),
    "A1": (594.0, 841.0),
    "A0": (841.0, 1189.0),
}
# ratio = drawn / real, descending (enlargements → 1:1 → reductions).
_STD_SCALES: list[tuple[float, str]] = [
    (100.0, "100:1"), (50.0, "50:1"), (40.0, "40:1"), (20.0, "20:1"),
    (10.0, "10:1"), (5.0, "5:1"), (4.0, "4:1"), (2.5, "2.5:1"), (2.0, "2:1"),
    (1.0, "1:1"),
    (1 / 2, "1:2"), (1 / 2.5, "1:2.5"), (1 / 4, "1:4"), (1 / 5, "1:5"),
    (1 / 10, "1:10"), (1 / 15, "1:15"), (1 / 20, "1:20"), (1 / 25, "1:25"),
    (1 / 40, "1:40"), (1 / 50, "1:50"), (1 / 75, "1:75"), (1 / 100, "1:100"),
    (1 / 200, "1:200"), (1 / 400, "1:400"), (1 / 500, "1:500"), (1 / 1000, "1:1000"),
]
_FRAME_LEFT_MM, _FRAME_OTHER_MM = 20.0, 5.0
# Below this share of the usable area a 1:1 drawing is too small to read, so an
# enlargement is justified; at or above it, 1:1 is kept (ГОСТ 2.302 preference).
_MIN_1TO1_FILL = 0.25
# ГОСТ 2.104 form-1 stamp height (mirrors blank_sheet/techdraw_reference).
_TITLE_BLOCK_H_MM = 55.0
# Evidence tag marking sheet furniture (frame + stamp) rather than part geometry.
SHEET_FRAME_EVIDENCE = "sheet_frame"
# Paper-space resolution of the drafted sheet canvas, px per sheet millimetre.
_PAPER_PX_PER_MM = 4.0


def _drawing_area_mm(
    sheet_format: str, landscape: bool, *, reserve_title_block: bool,
    reserve_notes_mm: float = 0.0,
) -> tuple[float, float, float, float, float, float]:
    """Paper size and the usable drawing area inside the ГОСТ frame, in mm.

    Returns ``(paper_w, paper_h, area_x0, area_y0, area_w, area_h)``. When the
    title block is drawn, the bottom-right stamp band is excluded from the
    usable area — on A4 portrait the stamp spans the whole frame width, so
    centring in the raw frame would put the part on top of it.
    """
    short, long = _GOST_SHEETS.get(sheet_format.upper(), _GOST_SHEETS["A4"])
    paper_w, paper_h = (long, short) if landscape else (short, long)
    area_x0, area_y0 = _FRAME_LEFT_MM, _FRAME_OTHER_MM
    area_w = paper_w - _FRAME_LEFT_MM - _FRAME_OTHER_MM
    area_h = paper_h - 2 * _FRAME_OTHER_MM
    if reserve_title_block:
        # Stamp band + a breathing gap; the drawing keeps everything above it.
        area_h -= _TITLE_BLOCK_H_MM + 5.0
    # The requirements column sits above the stamp, so it takes sheet from
    # the same direction: views that ignore it are drawn straight over it.
    area_h -= max(0.0, reserve_notes_mm)
    return paper_w, paper_h, area_x0, area_y0, area_w, max(area_h, 1.0)


def choose_standard_scale(
    obj_w_mm: float, obj_h_mm: float, sheet_format: str, *, landscape: bool = True,
    fill: float = 0.8, reserve_title_block: bool = False, reserve_notes_mm: float = 0.0,
) -> tuple[float, str]:
    """Pick the LARGEST ГОСТ 2.302 scale at which the object fits the sheet.

    Fits within ``fill`` of the usable drawing area (leaving room for
    dimensions, and for the stamp when it is drawn). Returns ``(ratio, label)``
    e.g. ``(0.5, "1:2")``.
    """
    _pw, _ph, _x0, _y0, area_w, area_h = _drawing_area_mm(
        sheet_format, landscape, reserve_title_block=reserve_title_block,
        reserve_notes_mm=reserve_notes_mm,
    )
    avail_w = area_w * fill
    avail_h = area_h * fill
    if obj_w_mm <= 0 or obj_h_mm <= 0:
        return 1.0, "1:1"
    # ГОСТ 2.302 prefers 1:1: enlarge only when the part would be too small to
    # read, never merely because the sheet has room. Taking the largest fitting
    # scale unconditionally would redraw a 100 mm shaft at 2.5:1 and silently
    # change the character of the sheet being reproduced.
    fills_sheet = (
        obj_w_mm >= avail_w * _MIN_1TO1_FILL or obj_h_mm >= avail_h * _MIN_1TO1_FILL
    )
    if obj_w_mm <= avail_w and obj_h_mm <= avail_h and fills_sheet:
        return 1.0, "1:1"
    for ratio, label in _STD_SCALES:
        if obj_w_mm * ratio <= avail_w and obj_h_mm * ratio <= avail_h:
            return ratio, label
    return _STD_SCALES[-1]


def _read_scale_ratio(spec: dict | None) -> tuple[float, str] | None:
    """The scale the reader saw in the source stamp, if it is a ГОСТ 2.302 one.

    Reproducing a sheet means reproducing its scale: an original drawn 1:2 must
    not come back 1:1 just because it would fit. Anything unparsable is ignored
    rather than guessed at.
    """
    if not spec:
        return None
    label = str((spec.get("title_block") or {}).get("scale") or "").strip()
    if not label:
        return None
    normalised = label.replace(" ", "").replace(",", ".")
    for ratio, known in _STD_SCALES:
        if normalised == known:
            return ratio, known
    return None



# ГОСТ 2.316 requirements column: same width as the title block, set directly
# above it. Both the drafter (which must keep the views clear of it) and the
# annotator (which draws it) size the block with this one function, so the
# space reserved and the space used cannot drift apart.
TECHNICAL_REQUIREMENTS_COLUMN_MM = 185.0
TECHNICAL_REQUIREMENTS_HEADING = "Технические требования"


def technical_requirements_lines(
    spec: dict, *, text_mm: float = 5.0, column_mm: float = TECHNICAL_REQUIREMENTS_COLUMN_MM
) -> list[str]:
    """The heading plus the numbered, wrapped requirement lines."""
    import textwrap

    lines: list[str] = []
    seen: set[str] = set()
    for annotation in spec.get("annotations") or []:
        text = str((annotation or {}).get("text", "")).strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            lines.append(text)
    material = str((spec.get("title_block") or {}).get("material") or "").strip()
    if material and material.casefold() not in seen:
        lines.append(material)
    if not lines:
        return []

    # Advance per character at this text height, measured against the
    # renderer rather than assumed: 0.55 let the longest requirement run
    # a few millimetres past the frame.
    per_line = max(20, int(column_mm / (text_mm * 0.62)))
    wrapped: list[str] = [TECHNICAL_REQUIREMENTS_HEADING]
    for index, text in enumerate(lines, start=1):
        body = f"{index}. {text}"
        wrapped.extend(textwrap.wrap(body, width=per_line) or [body])
    return wrapped


def technical_requirements_height_mm(spec: dict, *, text_mm: float = 5.0) -> float:
    """How much sheet the requirements block needs, in mm."""
    lines = technical_requirements_lines(spec, text_mm=text_mm)
    return (len(lines) * text_mm * 1.6 + 5.0) if lines else 0.0


def _place_on_sheet(
    layout_w_mm: float, layout_h_mm: float, sheet_format: str, landscape: bool,
    spec: dict | None = None,
) -> tuple[float, str, float, float, float, float, float]:
    """Pick the ГОСТ 2.302 scale and centre the drawing in the usable area.

    Shared by both drafters so a sheet always composes the same way. Returns
    ``(ratio, scale_label, paper_px_per_mm, paper_w_mm, paper_h_mm, x_left_px,
    y_top_px)``. The usable area excludes the stamp band, so the drawing never
    lands on top of the title block.
    """
    notes_mm = technical_requirements_height_mm(spec or {})
    ratio, scale_label = choose_standard_scale(
        layout_w_mm, layout_h_mm, sheet_format,
        landscape=landscape, reserve_title_block=True, reserve_notes_mm=notes_mm,
    )
    read_scale = _read_scale_ratio(spec)
    if read_scale is not None:
        _pw, _ph, _ax, _ay, fit_w, fit_h = _drawing_area_mm(
            sheet_format, landscape, reserve_title_block=True,
            reserve_notes_mm=notes_mm,
        )
        read_ratio, read_label = read_scale
        # Honour the source scale only if the drawing still fits the sheet;
        # otherwise the auto choice wins and the difference is visible in the
        # stamp, not hidden.
        if (
            layout_w_mm * read_ratio <= fit_w * 0.8
            and layout_h_mm * read_ratio <= fit_h * 0.8
        ):
            ratio, scale_label = read_ratio, read_label
    ppp = _PAPER_PX_PER_MM
    paper_w, paper_h, area_x0, area_y0, area_w, area_h = _drawing_area_mm(
        sheet_format, landscape, reserve_title_block=True, reserve_notes_mm=notes_mm,
    )
    drawn_w = layout_w_mm * ratio
    drawn_h = layout_h_mm * ratio
    x_left = (area_x0 + max((area_w - drawn_w) / 2.0, 0.0)) * ppp
    y_top = (area_y0 + max((area_h - drawn_h) / 2.0, 0.0)) * ppp
    return ratio, scale_label, ppp, paper_w, paper_h, x_left, y_top


def draft_sheet_without_geometry(
    spec: dict, *, sheet_format: str | None = None, landscape: bool = True
) -> CadIR | None:
    """The sheet a refusal can still hand over: everything EXCEPT the part.

    A fail-closed stop used to produce nothing at all, which threw away the
    work that was right along with the work that was wrong. On a sheet whose
    geometry could not be resolved the reader still had the stamp, the
    technical requirements and every dimension callout — and those were simply
    discarded, leaving the user to start from a blank page.

    So: frame, ГОСТ 2.104 stamp filled from the reading, the ГОСТ 2.316
    requirements block, and the callouts listed so nothing read has to be read
    again. NO part geometry — not a contour, not a circle, not a step. That is
    the whole point: what could not be verified is not drawn, and what the
    drafter hands over cannot be mistaken for a part.
    """
    from app.ai.cad_ir.schema import CadIR, Point, SourceInfo, TextEntity

    fmt = (sheet_format or "A3").upper()
    if fmt not in _GOST_SHEETS:
        fmt = "A3"
    paper_w, paper_h, _ax, _ay, _aw, _ah = _drawing_area_mm(
        fmt, landscape, reserve_title_block=True
    )
    ppp = _PAPER_PX_PER_MM
    entities = list(_sheet_frame_entities(paper_w, paper_h, ppp, spec, None))

    text_style = {
        "line_class": "dim", "width_class": "thin",
        "origin": "spec", "assurance": "inferred",
    }
    height = 5.0 * ppp
    lines = technical_requirements_lines(spec)
    y = 20.0 * ppp
    for line in lines:
        entities.append(
            TextEntity(
                position=Point(x=20.0 * ppp, y=y), text=line, height=height, **text_style
            )
        )
        y += height * 1.6

    # The callouts, so a person finishing this sheet by hand does not have to
    # read the source again for numbers the reader already got right.
    values = [
        str((item or {}).get("value") or "").strip()
        for item in (spec.get("dimensions") or [])
    ]
    values = [value for value in values if value]
    if values:
        y += height
        entities.append(
            TextEntity(
                position=Point(x=20.0 * ppp, y=y),
                text="Прочитанные размеры (геометрия не построена):",
                height=height, **text_style,
            )
        )
        y += height * 1.6
        import textwrap

        for chunk in textwrap.wrap(", ".join(values), width=110):
            entities.append(
                TextEntity(
                    position=Point(x=20.0 * ppp, y=y), text=chunk,
                    height=height * 0.8, **text_style,
                )
            )
            y += height * 1.3

    ir = CadIR(
        source=SourceInfo(
            image_width=int(paper_w * ppp), image_height=int(paper_h * ppp),
            kind="spec",
        ),
        # Paper-space pixels are generated at a known 4 px/mm. This makes the
        # editable fallback DXF metrically usable instead of exposing a download
        # button that inevitably returns SCALE_UNKNOWN/HTTP 409.
        scale=1.0 / ppp,
        scale_source="sheet_format",
        entities=entities,
        recognizer_used="spec-drafter-sheet-only",
        digitization_status="review_required",
    )
    ir.sheet = _sheet_info(fmt, spec, None)
    return ir


def _sheet_frame_entities(
    paper_w_mm: float, paper_h_mm: float, ppp: float, spec: dict, scale_label: str | None
) -> list[Any]:
    """ГОСТ 2.301 frame + ГОСТ 2.104 stamp, filled from the read title block.

    The stamp fields come from what the reader actually saw on the source
    sheet; nothing is invented, so an unread field stays blank rather than
    being guessed.
    """
    from app.ai.cad_ir.blank_sheet import frame_and_title_block_entities

    title = spec.get("title_block") or {}
    entities = list(
        frame_and_title_block_entities(
            paper_w_mm,
            paper_h_mm,
            ppp,
            name=str(spec.get("part") or title.get("name") or ""),
            designation=str(title.get("designation") or ""),
            company=str(title.get("company") or ""),
            # Read off the source sheet and previously dropped on the floor:
            # the stamp had no graph for them, so the material and the scale
            # were extracted, verified and then never drawn.
            material=str(title.get("material") or ""),
            scale=str(scale_label or title.get("scale") or ""),
            mass=str(title.get("mass") or ""),
            sheet=str(title.get("sheet") or ""),
            sheets=str(title.get("sheets") or ""),
        )
    )
    for entity in entities:
        # These are drafted, not human-approved: keep provenance honest, and
        # tag them so part geometry can be told from sheet furniture.
        entity.origin = "spec"
        entity.assurance = "inferred"
        entity.evidence = [SHEET_FRAME_EVIDENCE]
    return entities


def _sheet_info(sheet_format: str, spec: dict, scale_label: str | None) -> Any:
    """SheetInfo declaring the frame the drafter actually drew, plus the stamp
    fields it filled — so the editor and DXF export agree with the geometry."""
    from app.ai.cad_ir.schema import SheetInfo

    title = spec.get("title_block") or {}
    fields = {
        key: str(title[key])
        for key in ("designation", "material", "company", "mass")
        if title.get(key)
    }
    if spec.get("part"):
        fields["name"] = str(spec["part"])
    if scale_label:
        fields["scale"] = scale_label
    return SheetInfo(format=sheet_format.upper(), frame=True, title_block=fields)


def draft_rotation_body(
    spec: dict,
    *,
    px_per_mm: float | None = None,
    sheet_format: str | None = None,
    landscape: bool = True,
) -> CadIR | None:
    """Construct clean stepped-shaft main view(s) from a rotation-body spec.

    Handles MULTIPLE bodies (``parts[]``): each is drafted as an exact symmetric
    stepped profile about its OWN constructed axis, and the bodies are stacked
    vertically so they never overlap. The axis is never "found" — it is built,
    so the contour is always correct.

    When ``sheet_format`` is given, all bodies share one auto-chosen ГОСТ 2.302
    scale and are centred on that sheet. Otherwise they free-fit.

    Returns None when the spec has no usable rotation body (so the caller can
    fall back to the generative model for prismatic/complex geometry).
    """
    parts = _rotation_parts(spec)
    if not parts:
        return None
    for body in parts:
        if not _sections_are_complete(body["outer"]):
            return None
        if body.get("bore") and not _sections_are_complete(body["bore"]):
            return None

    part_dims = [
        (sum(s["l"] for s in body["outer"]), max(s["d"] for s in body["outer"]))
        for body in parts
    ]
    # A requested left view sits to the right of its front view, on the same
    # axis. Its footprint must enter the scale decision BEFORE a ГОСТ 2.302
    # scale is picked, or the extra projection would overflow the frame.
    part_view_kinds = [
        _requested_view_kinds(
            spec,
            {body.get("body_index", index)} | ({0} if index == 0 else set()),
        )
        for index, body in enumerate(parts)
    ]
    part_side_view = ["side" in kinds for kinds in part_view_kinds]
    # A section is only meaningful where there is a bore to cut through.
    part_section = [
        "section" in kinds and bool(body.get("bore"))
        for kinds, body in zip(part_view_kinds, parts, strict=True)
    ]
    part_block_w = [
        (w + _VIEW_GAP_MM + d) if has_side else w
        for (w, d), has_side in zip(part_dims, part_side_view, strict=True)
    ]
    layout_w = max(part_block_w)
    gap_mm = 0.2 * max(h for _, h in part_dims)  # vertical gap between bodies
    layout_h = sum(h for _, h in part_dims) + gap_mm * (len(parts) - 1)

    scale_label: str | None = None
    scale_source: str | None = None
    sheet_info = None
    if sheet_format:
        ratio, scale_label, ppp, pw_mm, ph_mm, x_left, y_top = _place_on_sheet(
            layout_w, layout_h, sheet_format, landscape, spec
        )
        px_per_mm = ratio * ppp
        width_px = pw_mm * ppp
        height_px = ph_mm * ppp
        scale_source = "sheet_format"
    else:
        if px_per_mm is None:
            px_per_mm = 900.0 / max(layout_w, 1.0)
        x_left = 60.0
        y_top = 60.0
        width_px = height_px = 0.0  # computed from content below

    entities: list[Any] = []

    def seg(x1, y1, x2, y2, cls="contour", width="main"):
        entities.append(
            Segment(
                p1=Point(x=x1, y=y1), p2=Point(x=x2, y=y2),
                line_class=cls, width_class=width, origin="spec", assurance="inferred",
            )
        )

    dim_index = _read_dimension_index(spec)
    cursor_y = y_top
    right_edge = x_left
    for body, (_w, h), has_side, sectioned in zip(
        parts, part_dims, part_side_view, part_section, strict=True
    ):
        axis_y = cursor_y + h * px_per_mm / 2.0
        front_right = _emit_profile(
            body["outer"], px_per_mm, x_left, axis_y, seg,
            bore=body.get("bore"), sectioned=sectioned,
        )
        right_edge = max(right_edge, front_right)
        if sectioned:
            for loop in _section_wall_loops(
                body["outer"], body["bore"], px_per_mm, x_left, axis_y
            ):
                entities.append(HatchRegion(
                    boundary=loop, pattern="ansi31",
                    origin="spec", assurance="inferred",
                ))
        if has_side:
            # Same axis_y = exact ГОСТ 2.305 projection alignment.
            side_center_x = front_right + (_VIEW_GAP_MM + h / 2.0) * px_per_mm
            _emit_rotation_side_view(
                body["outer"], body.get("bore"), px_per_mm,
                side_center_x, axis_y, entities,
            )
            right_edge = max(right_edge, side_center_x + h * px_per_mm / 2.0)
        section_x = x_left
        dim_y = axis_y + h * px_per_mm / 2.0 + 10.0 * px_per_mm
        for section in body["outer"]:
            length_px = section["l"] * px_per_mm
            diameter_px = section["d"] * px_per_mm
            entities.append(DimensionEntity(
                kind="linear",
                p1=Point(x=section_x, y=dim_y),
                p2=Point(x=section_x + length_px, y=dim_y),
                text=_dimension_text(dim_index, section["l"], diameter=False),
                value_mm=section["l"],
                origin="spec",
                assurance="inferred",
            ))
            mid_x = section_x + length_px / 2.0
            entities.append(DimensionEntity(
                kind="diameter",
                p1=Point(x=mid_x, y=axis_y - diameter_px / 2.0),
                p2=Point(x=mid_x, y=axis_y + diameter_px / 2.0),
                text=_dimension_text(dim_index, section["d"], diameter=True),
                value_mm=section["d"],
                origin="spec",
                assurance="inferred",
            ))
            section_x += length_px
        entities.append(DimensionEntity(
            kind="linear",
            p1=Point(x=x_left, y=dim_y + 8.0 * px_per_mm),
            p2=Point(x=section_x, y=dim_y + 8.0 * px_per_mm),
            text=_dimension_text(dim_index, _w, diameter=False),
            value_mm=_w,
            origin="spec",
            assurance="inferred",
        ))
        cursor_y += (h + gap_mm) * px_per_mm

    if sheet_format:
        entities += _sheet_frame_entities(pw_mm, ph_mm, ppp, spec, scale_label)
        sheet_info = _sheet_info(sheet_format, spec, scale_label)
    else:
        width_px = right_edge + 60.0
        height_px = cursor_y + 60.0

    extra = {"sheet": sheet_info} if sheet_info is not None else {}
    ir = CadIR(
        source=SourceInfo(image_width=int(width_px), image_height=int(height_px), kind="scan"),
        scale=1.0 / px_per_mm,
        scale_source=scale_source,
        entities=entities,
        recognizer_used="spec-drafter-rotation",
        digitization_status="review_required",
        **extra,
    )
    return ir


def _prismatic_profiles(spec: dict) -> list[dict]:
    bodies = [body for body in (spec.get("parts") or []) if isinstance(body, dict)]
    if not bodies:
        bodies = [spec.get("main_view") or {}]
    return [body["profile"] for body in bodies if isinstance(body.get("profile"), dict)]


def _expanded_profile_holes(profile: dict) -> list[dict] | None:
    """Expand exact pitch-circle declarations without model-generated coordinates."""
    holes = [dict(hole) for hole in profile.get("holes") or [] if isinstance(hole, dict)]
    if len(holes) != len(profile.get("holes") or []):
        return None
    for pattern in profile.get("hole_patterns") or []:
        if not isinstance(pattern, dict) or pattern.get("kind", "bolt_circle") != "bolt_circle":
            return None
        count = pattern.get("count")
        pcd = _num(pattern.get("bolt_circle_diameter_mm"))
        diameter = _num(pattern.get("hole_diameter_mm"))
        start = _num(pattern.get("start_angle_deg"))
        if not isinstance(count, int) or count < 2 or not pcd or not diameter or start is None:
            return None
        for index in range(count):
            angle = math.radians(start + index * 360.0 / count)
            holes.append({
                "center_x_mm": pcd * math.cos(angle) / 2.0,
                "center_y_mm": pcd * math.sin(angle) / 2.0,
                "diameter_mm": diameter,
                "tolerance": pattern.get("tolerance"),
            })
    return holes


def _feature_fits_profile(
    profile: dict, *, center_x: float, center_y: float, radius: float
) -> bool:
    """Conservatively prove a circular envelope lies inside its parent profile."""
    epsilon = 1e-9
    if profile.get("shape") == "rectangle":
        width = float(profile["width_mm"])
        height = float(profile["height_mm"])
        return (
            abs(center_x) + radius <= width / 2.0 + epsilon
            and abs(center_y) + radius <= height / 2.0 + epsilon
        )
    diameter = float(profile["diameter_mm"])
    return math.hypot(center_x, center_y) + radius <= diameter / 2.0 + epsilon


def _flange_section_view(
    profile: dict, *, px_per_mm: float, axis_y: float, left_x: float,
    dim_index: list, common: dict,
) -> tuple[list[Any], float]:
    """Longitudinal section of a flange, placed in projection with its face view.

    A round flat part is not described by its face alone: the thickness, the
    bore depth and the fact that the bolt holes go through all live in the cut.
    ГОСТ 2.305 puts that section beside the face view on the SAME horizontal
    axis, and it is built from the very numbers the face view uses, so the two
    cannot disagree.
    """
    from app.ai.cad_ir.schema import HatchRegion

    outer = float(profile["diameter_mm"])
    thickness = float(profile["thickness_mm"])
    half = outer * px_per_mm / 2.0
    width = thickness * px_per_mm
    entities: list[Any] = []

    bore = 0.0
    for hole in profile.get("holes") or []:
        if isinstance(hole, dict) and abs(_num(hole.get("center_x_mm")) or 0.0) < 1e-6 \
                and abs(_num(hole.get("center_y_mm")) or 0.0) < 1e-6:
            bore = max(bore, _num(hole.get("diameter_mm")) or 0.0)
    bore_half = bore * px_per_mm / 2.0

    def seg(x1, y1, x2, y2, cls="contour", w="main"):
        entities.append(Segment(
            p1=Point(x=x1, y=y1), p2=Point(x=x2, y=y2),
            line_class=cls, width_class=w, **common,
        ))

    right_x = left_x + width
    # Outer silhouette of the cut: two walls, top and bottom.
    for sign in (-1.0, 1.0):
        outer_y = axis_y + sign * half
        inner_y = axis_y + sign * bore_half if bore_half else axis_y
        seg(left_x, outer_y, right_x, outer_y)
        seg(left_x, outer_y, left_x, inner_y)
        seg(right_x, outer_y, right_x, inner_y)
        if bore_half:
            seg(left_x, inner_y, right_x, inner_y)
            entities.append(HatchRegion(
                boundary=[
                    Point(x=left_x, y=outer_y), Point(x=right_x, y=outer_y),
                    Point(x=right_x, y=inner_y), Point(x=left_x, y=inner_y),
                ],
                pattern="ansi31", **common,
            ))
    if not bore_half:
        entities.append(HatchRegion(
            boundary=[
                Point(x=left_x, y=axis_y - half), Point(x=right_x, y=axis_y - half),
                Point(x=right_x, y=axis_y + half), Point(x=left_x, y=axis_y + half),
            ],
            pattern="ansi31", **common,
        ))

    axis_common = {**common, "line_class": "axis", "width_class": "thin"}
    entities.append(Segment(
        p1=Point(x=left_x - 8 * px_per_mm, y=axis_y),
        p2=Point(x=right_x + 8 * px_per_mm, y=axis_y),
        **axis_common,
    ))
    # The thickness lives only here, so it is dimensioned only here.
    entities.append(DimensionEntity(
        kind="linear",
        p1=Point(x=left_x, y=axis_y + half + 12 * px_per_mm),
        p2=Point(x=right_x, y=axis_y + half + 12 * px_per_mm),
        text=_dimension_text(dim_index, thickness, diameter=False),
        value_mm=thickness, **common,
    ))
    entities.append(TextEntity(
        position=Point(x=left_x, y=axis_y - half - 6 * px_per_mm),
        text="А-А", height=5.0 * px_per_mm,
        line_class="dim", width_class="thin", **common,
    ))
    return entities, right_x


def _flange_face_extras(
    profile: dict, *, px_per_mm: float, center_x: float, center_y: float,
    common: dict,
) -> list[Any]:
    """What a face view of a flange needs beyond its circles.

    The pitch circle is a centre line, not an edge; each hole carries centre
    marks; and the part's own axes run past the outline. Without these the view
    is a picture of the shape rather than a drawing of the part.
    """
    entities: list[Any] = []
    axis_common = {**common, "line_class": "axis", "width_class": "thin"}
    outer_half = float(profile["diameter_mm"]) * px_per_mm / 2.0
    over = outer_half + 8 * px_per_mm
    entities.append(Segment(
        p1=Point(x=center_x - over, y=center_y), p2=Point(x=center_x + over, y=center_y),
        **axis_common,
    ))
    entities.append(Segment(
        p1=Point(x=center_x, y=center_y - over), p2=Point(x=center_x, y=center_y + over),
        **axis_common,
    ))
    for pattern in profile.get("hole_patterns") or []:
        pcd = _num(pattern.get("bolt_circle_diameter_mm"))
        if not pcd:
            continue
        entities.append(Circle(
            center=Point(x=center_x, y=center_y), radius=pcd * px_per_mm / 2.0,
            **axis_common,
        ))
    return entities


def draft_prismatic_body(
    spec: dict,
    *,
    px_per_mm: float | None = None,
    sheet_format: str | None = None,
    landscape: bool = True,
) -> CadIR | None:
    """Draft exact rectangular/circular plates and their through holes.

    Every coordinate comes from the validated spec. Missing shape dimensions
    decline before any entity is emitted; the generative fallback never fills
    these gaps silently.
    """
    profiles = _prismatic_profiles(spec)
    if not profiles:
        return None

    dimensions: list[tuple[float, float]] = []
    expanded_holes: list[list[dict]] = []
    for profile in profiles:
        shape = profile.get("shape")
        if shape == "rectangle":
            width, height = _num(profile.get("width_mm")), _num(profile.get("height_mm"))
        elif shape == "circle":
            width = height = _num(profile.get("diameter_mm"))
        else:
            return None
        if not width or not height:
            return None
        profile_holes = _expanded_profile_holes(profile)
        if profile_holes is None:
            return None
        for hole in profile_holes:
            if not isinstance(hole, dict) or not _num(hole.get("diameter_mm")):
                return None
            if _num(hole.get("center_x_mm")) is None or _num(hole.get("center_y_mm")) is None:
                return None
            if not _feature_fits_profile(
                profile,
                center_x=float(hole["center_x_mm"]),
                center_y=float(hole["center_y_mm"]),
                radius=float(hole["diameter_mm"]) / 2.0,
            ):
                return None
        for slot in profile.get("slots") or []:
            if not isinstance(slot, dict):
                return None
            length = _num(slot.get("length_mm"))
            slot_width = _num(slot.get("width_mm"))
            slot_x = _num(slot.get("center_x_mm"))
            slot_y = _num(slot.get("center_y_mm"))
            rotation = _num(slot.get("rotation_deg"))
            if (
                not length or not slot_width or length < slot_width
                or slot_x is None or slot_y is None or rotation is None
                or not _feature_fits_profile(
                    profile, center_x=slot_x, center_y=slot_y, radius=length / 2.0
                )
            ):
                return None
        dimensions.append((width, height))
        expanded_holes.append(profile_holes)

    layout_w = max(width for width, _ in dimensions)
    gap_mm = max(12.0, 0.2 * max(height for _, height in dimensions))
    layout_h = sum(height for _, height in dimensions) + gap_mm * (len(dimensions) - 1)
    scale_label = None
    sheet_info = None
    if sheet_format:
        (
            ratio, scale_label, paper_px_per_mm, paper_w, paper_h, x_left, y_top,
        ) = _place_on_sheet(layout_w, layout_h, sheet_format, landscape, spec)
        px_per_mm = ratio * paper_px_per_mm
        width_px, height_px = paper_w * paper_px_per_mm, paper_h * paper_px_per_mm
        scale_source = "sheet_format"
        sheet_info = _sheet_info(sheet_format, spec, scale_label)
    else:
        px_per_mm = px_per_mm or 4.0
        x_left = y_top = 60.0
        width_px = layout_w * px_per_mm + 120.0
        height_px = layout_h * px_per_mm + 120.0
        scale_source = "manual"

    common = {
        "origin": "spec",
        "assurance": "constraint_validated",
    }
    dim_index = _read_dimension_index(spec)
    entities: list[Any] = []
    cursor_y = y_top
    for profile, (width_mm, height_mm), profile_holes in zip(
        profiles, dimensions, expanded_holes, strict=True
    ):
        local_x = x_left + (layout_w - width_mm) * px_per_mm / 2.0
        local_y = cursor_y
        center_x = local_x + width_mm * px_per_mm / 2.0
        center_y = local_y + height_mm * px_per_mm / 2.0
        if profile["shape"] == "rectangle":
            right = local_x + width_mm * px_per_mm
            bottom = local_y + height_mm * px_per_mm
            corner_radius = (_num(profile.get("corner_radius_mm")) or 0.0) * px_per_mm
            if corner_radius:
                entities.extend([
                    Segment(p1=Point(x=local_x + corner_radius, y=local_y), p2=Point(x=right - corner_radius, y=local_y), **common),
                    Arc(center=Point(x=right - corner_radius, y=local_y + corner_radius), radius=corner_radius, start_angle=270, end_angle=360, **common),
                    Segment(p1=Point(x=right, y=local_y + corner_radius), p2=Point(x=right, y=bottom - corner_radius), **common),
                    Arc(center=Point(x=right - corner_radius, y=bottom - corner_radius), radius=corner_radius, start_angle=0, end_angle=90, **common),
                    Segment(p1=Point(x=right - corner_radius, y=bottom), p2=Point(x=local_x + corner_radius, y=bottom), **common),
                    Arc(center=Point(x=local_x + corner_radius, y=bottom - corner_radius), radius=corner_radius, start_angle=90, end_angle=180, **common),
                    Segment(p1=Point(x=local_x, y=bottom - corner_radius), p2=Point(x=local_x, y=local_y + corner_radius), **common),
                    Arc(center=Point(x=local_x + corner_radius, y=local_y + corner_radius), radius=corner_radius, start_angle=180, end_angle=270, **common),
                ])
            else:
                corners = [
                    Point(x=local_x, y=local_y),
                    Point(x=right, y=local_y),
                    Point(x=right, y=bottom),
                    Point(x=local_x, y=bottom),
                ]
                for p1, p2 in zip(corners, corners[1:] + corners[:1], strict=True):
                    entities.append(Segment(p1=p1, p2=p2, **common))
            dim_y = local_y + height_mm * px_per_mm + 10.0 * px_per_mm
            entities.append(DimensionEntity(
                kind="linear",
                p1=Point(x=local_x, y=dim_y),
                p2=Point(x=local_x + width_mm * px_per_mm, y=dim_y),
                text=_dimension_text(dim_index, width_mm, diameter=False),
                value_mm=width_mm, **common,
            ))
            dim_x = local_x + width_mm * px_per_mm + 10.0 * px_per_mm
            entities.append(DimensionEntity(
                kind="linear",
                p1=Point(x=dim_x, y=local_y),
                p2=Point(x=dim_x, y=local_y + height_mm * px_per_mm),
                text=_dimension_text(dim_index, height_mm, diameter=False),
                value_mm=height_mm, **common,
            ))
        else:
            radius = width_mm * px_per_mm / 2.0
            entities.append(Circle(center=Point(x=center_x, y=center_y), radius=radius, **common))
            entities.append(DimensionEntity(
                kind="diameter",
                p1=Point(x=center_x - radius, y=center_y),
                p2=Point(x=center_x + radius, y=center_y),
                text=_dimension_text(dim_index, width_mm, diameter=True),
                value_mm=width_mm, **common,
            ))

        axis_common = {**common, "line_class": "axis", "width_class": "thin"}
        entities.append(Segment(
            p1=Point(x=center_x - 6 * px_per_mm, y=center_y),
            p2=Point(x=center_x + 6 * px_per_mm, y=center_y),
            **axis_common,
        ))
        entities.append(Segment(
            p1=Point(x=center_x, y=center_y - 6 * px_per_mm),
            p2=Point(x=center_x, y=center_y + 6 * px_per_mm),
            **axis_common,
        ))
        for hole in profile_holes:
            hole_x = center_x + float(hole["center_x_mm"]) * px_per_mm
            # Spec uses engineering +y upward; image coordinates grow downward.
            hole_y = center_y - float(hole["center_y_mm"]) * px_per_mm
            diameter = float(hole["diameter_mm"])
            radius = diameter * px_per_mm / 2.0
            entities.append(Circle(
                center=Point(x=hole_x, y=hole_y), radius=radius, **common
            ))
            entities.append(DimensionEntity(
                kind="diameter",
                p1=Point(x=hole_x, y=hole_y - radius),
                p2=Point(x=hole_x, y=hole_y + radius),
                text=f"Ø{diameter:g}" + str(hole.get("tolerance") or ""),
                value_mm=diameter,
                tolerance=hole.get("tolerance") or None,
                **common,
            ))
        for pattern in profile.get("hole_patterns") or []:
            pcd = float(pattern["bolt_circle_diameter_mm"])
            pcd_radius = pcd * px_per_mm / 2.0
            entities.append(DimensionEntity(
                kind="diameter",
                p1=Point(x=center_x - pcd_radius, y=center_y),
                p2=Point(x=center_x + pcd_radius, y=center_y),
                text=f"Ø{pcd:g} PCD",
                value_mm=pcd,
                **common,
            ))
        for slot in profile.get("slots") or []:
            slot_x_mm = float(slot["center_x_mm"])
            slot_y_mm = float(slot["center_y_mm"])
            length_mm = float(slot["length_mm"])
            slot_width_mm = float(slot["width_mm"])
            theta = math.radians(float(slot.get("rotation_deg", 0.0)))
            ux, uy = math.cos(theta), math.sin(theta)
            vx, vy = -uy, ux
            half_straight = (length_mm - slot_width_mm) / 2.0
            radius_mm = slot_width_mm / 2.0

            def slot_point(x_mm: float, y_mm: float) -> Point:
                return Point(
                    x=center_x + (slot_x_mm + x_mm) * px_per_mm,
                    y=center_y - (slot_y_mm + y_mm) * px_per_mm,
                )

            left_x, left_y = -ux * half_straight, -uy * half_straight
            right_x, right_y = ux * half_straight, uy * half_straight
            entities.extend([
                Segment(
                    p1=slot_point(left_x + vx * radius_mm, left_y + vy * radius_mm),
                    p2=slot_point(right_x + vx * radius_mm, right_y + vy * radius_mm),
                    **common,
                ),
                Segment(
                    p1=slot_point(left_x - vx * radius_mm, left_y - vy * radius_mm),
                    p2=slot_point(right_x - vx * radius_mm, right_y - vy * radius_mm),
                    **common,
                ),
            ])
            image_angle = -math.degrees(theta)
            entities.extend([
                Arc(
                    center=slot_point(left_x, left_y),
                    radius=radius_mm * px_per_mm,
                    start_angle=image_angle + 90.0,
                    end_angle=image_angle + 270.0,
                    **common,
                ),
                Arc(
                    center=slot_point(right_x, right_y),
                    radius=radius_mm * px_per_mm,
                    start_angle=image_angle - 90.0,
                    end_angle=image_angle + 90.0,
                    **common,
                ),
            ])
            entities.extend([
                DimensionEntity(
                    kind="linear",
                    p1=slot_point(-ux * length_mm / 2.0, -uy * length_mm / 2.0),
                    p2=slot_point(ux * length_mm / 2.0, uy * length_mm / 2.0),
                    text=f"{length_mm:g}", value_mm=length_mm,
                    tolerance=slot.get("tolerance") or None,
                    **common,
                ),
                DimensionEntity(
                    kind="linear",
                    p1=slot_point(-vx * radius_mm, -vy * radius_mm),
                    p2=slot_point(vx * radius_mm, vy * radius_mm),
                    text=f"{slot_width_mm:g}", value_mm=slot_width_mm,
                    **common,
                ),
            ])
        cursor_y += (height_mm + gap_mm) * px_per_mm

    if sheet_format:
        entities += _sheet_frame_entities(
            paper_w, paper_h, paper_px_per_mm, spec, scale_label
        )
    extra = {"sheet": sheet_info} if sheet_info is not None else {}
    return CadIR(
        source=SourceInfo(
            image_width=int(width_px), image_height=int(height_px), kind="spec"
        ),
        scale=1.0 / px_per_mm,
        scale_source=scale_source,
        entities=entities,
        recognizer_used="spec-drafter-prismatic",
        digitization_status="review_required",
        **extra,
    )


def draft_from_spec(
    spec: dict,
    *,
    px_per_mm: float | None = None,
    draft_model: str | None = None,
    router: Any | None = None,
    sheet_format: str | None = None,
    landscape: bool = True,
) -> CadIR | None:
    """Dispatch a structured spec to a drafter (Model 2).

    When ``draft_model`` is set (Settings → Models → Оцифровка → «Чертёжник»),
    a generative model — e.g. a LoRA fine-tuned drafter — turns the spec into
    geometry. On any failure, or when no model is assigned, fall back to the
    deterministic parametric drafter (clean by construction, rotation bodies).

    ``sheet_format`` (+ ``landscape``) lays the part out on that ГОСТ sheet at an
    automatically chosen standard scale (ГОСТ 2.302).
    """
    # Deterministic-first: the parametric drafter is exact by construction for
    # what it handles (rotation bodies) — no model beats it there. A generative
    # model is used ONLY for parts it declines (returns None): prismatic/complex.
    deterministic = draft_rotation_body(
        spec, px_per_mm=px_per_mm, sheet_format=sheet_format, landscape=landscape
    )
    if deterministic is not None:
        return deterministic
    deterministic = draft_prismatic_body(
        spec, px_per_mm=px_per_mm, sheet_format=sheet_format, landscape=landscape
    )
    if deterministic is not None:
        return deterministic
    if draft_model:
        try:
            import asyncio

            generated = asyncio.get_event_loop().run_until_complete(
                _draft_generative(spec, draft_model, router=router)
            ) if not _in_running_loop() else None
            if generated is not None and generated.entities:
                return generated
        except Exception:  # noqa: BLE001 — never sink the pipeline on a model error
            pass
    return None


def _in_running_loop() -> bool:
    import asyncio

    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


async def draft_from_spec_async(
    spec: dict,
    *,
    px_per_mm: float | None = None,
    draft_model: str | None = None,
    router: Any | None = None,
    sheet_format: str | None = None,
    landscape: bool = True,
) -> CadIR | None:
    """Async variant: usable from inside a running event loop (the digitize task).

    Deterministic-first for rotation bodies: their axis+symmetry are CONSTRUCTED
    (never guessed), so the contour is always correct — and the parametric
    drafter now handles MULTIPLE bodies too. A generative model is used only for
    parts it declines (prismatic/complex), where free drawing is the only option.
    """
    deterministic = draft_rotation_body(
        spec, px_per_mm=px_per_mm, sheet_format=sheet_format, landscape=landscape
    )
    if deterministic is not None:
        return deterministic
    deterministic = draft_prismatic_body(
        spec, px_per_mm=px_per_mm, sheet_format=sheet_format, landscape=landscape
    )
    if deterministic is not None:
        return deterministic
    if draft_model:
        try:
            generated = await _draft_generative(
                spec, draft_model, router=router,
                sheet_format=sheet_format, landscape=landscape,
            )
            if generated is not None and generated.entities:
                return generated
        except Exception:  # noqa: BLE001
            pass
    return None


_DRAFT_PROMPT = (
    "Ты — генеративный чертёжник САПР. По СПЕЦИФИКАЦИИ построй ЧИСТУЮ геометрию "
    "главного вида (для тел вращения — продольный контур с осью; для "
    "призматических — очертание и отверстия). ВАЖНО:\n"
    "1) Если деталей/тел НЕСКОЛЬКО — начерти КАЖДОЕ, разнеся их по горизонтали, "
    "не накладывая друг на друга.\n"
    "2) Строго соблюдай ПРОПОРЦИИ по указанным размерам (диаметры/длины из "
    "features и dimensions). Ступень большего диаметра — шире по вертикали.\n"
    "3) Тело вращения симметрично относительно оси; вычерти обе образующие "
    "(верх и низ) и осевую линию.\n"
    "Верни СТРОГО JSON примитивов в изотропном пространстве 0..1000 (обе оси в "
    "одном масштабе, 0,0 — верхний левый угол):\n"
    '{"lines":[[x1,y1,x2,y2],...],"circles":[[cx,cy,r],...],'
    '"arcs":[[cx,cy,r,start_deg,end_deg],...],'
    '"polylines":[{"pts":[[x,y],...],"closed":0}],'
    '"axes":[[x1,y1,x2,y2],...]}\n'
    "Только JSON, без пояснений.\nСПЕЦИФИКАЦИЯ:\n"
)


async def _draft_generative(
    spec: dict,
    draft_model: str,
    *,
    router: Any | None = None,
    sheet_format: str | None = None,
    landscape: bool = True,
) -> CadIR | None:
    """Model 2 (generative): a model turns the spec text into a geometry DSL.

    Handles multiple bodies. When ``sheet_format`` is set, the generated geometry
    is laid out on that ГОСТ sheet at an auto-chosen standard scale.
    """
    import json

    from app.ai.schemas import AIRequest, AITask, ChatMessage

    if router is None:
        from app.ai.router import ai_router

        router = ai_router
    request = AIRequest(
        task=AITask.CAD_SPEC_DRAFT,
        messages=[ChatMessage(
            role="user",
            content=_DRAFT_PROMPT + json.dumps(spec, ensure_ascii=False),
        )],
        preferred_model=draft_model,
        confidential=True,
        allow_cloud=False,
    )
    response = await router.run(request)
    dsl = _parse_spec_json(response.text or "")
    if not dsl:
        return None
    ir = _dsl_to_ir(dsl)
    if ir is not None and sheet_format:
        _layout_on_sheet(ir, spec, sheet_format, landscape)
    return ir


def _dsl_to_ir(dsl: dict, *, canvas: int = 1000) -> CadIR | None:
    """Decode a 0..1000 isotropic geometry DSL into a clean CadIR.

    Inverse of ``tools/cad-dataset/build_vlm_sft.ir_to_dsl`` — the format the
    generative drafter is trained to emit.
    """
    from app.ai.cad_ir.schema import Arc, Circle, Polyline

    entities: list[Any] = []

    def _pt(x, y):
        return Point(x=float(x), y=float(y))

    for ln in dsl.get("lines", []) or []:
        if isinstance(ln, (list, tuple)) and len(ln) >= 4:
            entities.append(Segment(
                p1=_pt(ln[0], ln[1]), p2=_pt(ln[2], ln[3]),
                line_class="contour", width_class="main",
                origin="spec", assurance="inferred",
            ))
    for c in dsl.get("circles", []) or []:
        if isinstance(c, (list, tuple)) and len(c) >= 3:
            entities.append(Circle(
                center=_pt(c[0], c[1]), radius=float(c[2]),
                line_class="contour", width_class="main",
                origin="spec", assurance="inferred",
            ))
    for a in dsl.get("arcs", []) or []:
        if isinstance(a, (list, tuple)) and len(a) >= 5:
            entities.append(Arc(
                center=_pt(a[0], a[1]), radius=float(a[2]),
                start_angle=float(a[3]), end_angle=float(a[4]),
                line_class="contour", width_class="main",
                origin="spec", assurance="inferred",
            ))
    for pl in dsl.get("polylines", []) or []:
        if not isinstance(pl, dict):
            continue
        pts = [_pt(p[0], p[1]) for p in (pl.get("pts") or []) if len(p) >= 2]
        if len(pts) >= 2:
            entities.append(Polyline(
                points=pts, closed=bool(pl.get("closed")),
                line_class="contour", width_class="main",
                origin="spec", assurance="inferred",
            ))
    for ax in dsl.get("axes", []) or []:
        if isinstance(ax, (list, tuple)) and len(ax) >= 4:
            entities.append(Segment(
                p1=_pt(ax[0], ax[1]), p2=_pt(ax[2], ax[3]),
                line_class="axis", width_class="thin",
                origin="spec", assurance="inferred",
            ))
    if not entities:
        return None
    return CadIR(
        source=SourceInfo(image_width=canvas, image_height=canvas, kind="scan"),
        scale=1.0,
        entities=entities,
        recognizer_used="spec-drafter-generative",
        digitization_status="review_required",
    )


def _entity_points(e: Any) -> list[tuple[float, float]]:
    """All defining points of an entity, for bbox computation."""
    if e.type == "segment":
        return [(e.p1.x, e.p1.y), (e.p2.x, e.p2.y)]
    if e.type == "circle":
        return [(e.center.x - e.radius, e.center.y - e.radius),
                (e.center.x + e.radius, e.center.y + e.radius)]
    if e.type == "arc":
        return [(e.center.x - e.radius, e.center.y - e.radius),
                (e.center.x + e.radius, e.center.y + e.radius)]
    if e.type == "polyline":
        return [(p.x, p.y) for p in e.points]
    return []


def _translate_scale(e: Any, k: float, ox: float, oy: float, bx0: float, by0: float) -> None:
    """In-place map an entity from generated space to sheet px: (p-b0)*k+o."""
    def m(px, py):
        return (px - bx0) * k + ox, (py - by0) * k + oy

    if e.type == "segment":
        e.p1.x, e.p1.y = m(e.p1.x, e.p1.y)
        e.p2.x, e.p2.y = m(e.p2.x, e.p2.y)
    elif e.type in ("circle", "arc"):
        e.center.x, e.center.y = m(e.center.x, e.center.y)
        e.radius *= k
    elif e.type == "polyline":
        for p in e.points:
            p.x, p.y = m(p.x, p.y)


def _layout_on_sheet(ir: CadIR, spec: dict, sheet_format: str, landscape: bool) -> None:
    """Fit generated (relative 0..1000) geometry onto a ГОСТ sheet, in place.

    Chooses a standard ГОСТ 2.302 scale when the spec states a real overall size
    (the largest generated span maps to the largest stated dimension); otherwise
    fits the drawing into the frame without claiming a named scale.
    """
    from app.ai.cad_ir.schema import SheetInfo

    pts = [p for e in ir.entities for p in _entity_points(e)]
    if not pts:
        return
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    bx0, bx1, by0, by1 = min(xs), max(xs), min(ys), max(ys)
    gen_w = max(bx1 - bx0, 1e-6); gen_h = max(by1 - by0, 1e-6)

    short, long = _GOST_SHEETS.get(sheet_format.upper(), _GOST_SHEETS["A4"])
    pw_mm, ph_mm = (long, short) if landscape else (short, long)
    ppp = 4.0  # px per paper mm
    frame_x0 = _FRAME_LEFT_MM * ppp
    frame_y0 = _FRAME_OTHER_MM * ppp
    frame_w = (pw_mm - _FRAME_LEFT_MM - _FRAME_OTHER_MM) * ppp
    frame_h = (ph_mm - 2 * _FRAME_OTHER_MM) * ppp

    # Real overall dimensions from the spec (largest numeric on each axis).
    dims = []
    for d in spec.get("dimensions", []) or []:
        v = _num(d.get("value"))
        if v and v > 0:
            dims.append(v)
    real_max = max(dims) if dims else None

    scale_label = None
    if real_max:
        # Largest generated span == largest real dimension → mm per gen-unit.
        mm_per_unit = real_max / max(gen_w, gen_h)
        real_w = gen_w * mm_per_unit
        real_h = gen_h * mm_per_unit
        ratio, scale_label = choose_standard_scale(real_w, real_h, sheet_format, landscape=landscape)
        k = mm_per_unit * ratio * ppp  # gen-unit → paper px at the standard scale
        ir.scale = 1.0 / (ratio * ppp)  # real mm per px
        ir.scale_source = "sheet_format"
    else:
        k = min(frame_w / gen_w, frame_h / gen_h) * 0.8  # fit-to-frame, 80%

    draw_w = gen_w * k; draw_h = gen_h * k
    ox = frame_x0 + max((frame_w - draw_w) / 2.0, 0.0)
    oy = frame_y0 + max((frame_h - draw_h) / 2.0, 0.0)
    for e in ir.entities:
        _translate_scale(e, k, ox, oy, bx0, by0)

    ir.source.image_width = int(pw_mm * ppp)
    ir.source.image_height = int(ph_mm * ppp)
    ir.sheet = SheetInfo(
        format=sheet_format.upper(), frame=False,
        title_block={"scale": scale_label} if scale_label else {},
    )
