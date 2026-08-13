"""Canonical EngineeringModelGraph v1 contracts and deterministic operations.

The graph is the engineering source of truth.  Raster observations, model
responses, CadIR and kernel artifacts are evidence/projections and never
silently replace graph assertions.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


Profile = Literal[
    "mechanical", "assembly", "construction", "mep", "electrical",
    "hydraulic", "pid", "mixed",
]
NodeType = Literal[
    "DocumentSet", "Document", "Sheet", "View", "SourceRegion", "Product",
    "Component", "Feature", "Geometry", "Material", "Parameter", "Constraint",
    "System", "Port", "BuildOperation", "Artifact", "TopologyElement",
]
EdgeType = Literal[
    "contains", "part_of", "instance_of", "located_in", "represented_by",
    "same_object_across_views", "defines", "depends_on", "constrains",
    "applies_to", "mates_with", "connects_to", "opens_in", "generated_by",
    "maps_to_topology",
    # Ф2.6c: a compiled BuildOperation -> the descriptive native Feature
    # node(s) (Ф1.2) it was built from. Deliberately NOT in
    # critical_assertion_ids' traversal set below — a "proposed"-assurance
    # Feature assertion must not make an otherwise-clean build provisional
    # just because a realizes edge now makes it graph-reachable; it IS in
    # assertion_impact_report's own set, so the impact panel can show which
    # operations a Feature correction affects. Purely descriptive/UI, never
    # build-blocking.
    "realizes",
]
Origin = Literal["observed", "traced", "derived", "standard", "assumed", "human"]
Assurance = Literal[
    "proposed", "observed", "corroborated", "constraint_validated",
    "human_approved", "contradicted",
]
Impact = Literal[
    "base_topology", "component_count", "assembly_interface", "mate", "fit",
    "interchangeability", "envelope", "connection_opening", "load_path",
    "structural_capacity", "regulatory_check", "connectivity", "required_view",
    "required_section", "required_dimension", "manufacturing_safety",
    "operational_safety", "material_quantity", "mass", "visual_only",
]

# The two BuildOperation predicates every domain agrees on — compile_build_plan
# below orders operations by them regardless of profile. Canonical home for
# the literal: app.domain.emg_predicates (the curated predicate registry)
# re-exports these two rather than duplicating the string, since it imports
# FROM this module and a reverse import here would cycle.
PREDICATE_OPERATION_KIND = "operation.kind"
PREDICATE_OPERATION_SEQUENCE = "operation.sequence"


class ExactValue(StrictModel):
    kind: Literal["exact"]
    value: str | int | float | bool | dict[str, Any] | list[Any]


class IntervalValue(StrictModel):
    kind: Literal["interval"]
    lower: float
    upper: float
    lower_inclusive: bool = True
    upper_inclusive: bool = True

    @model_validator(mode="after")
    def validate_bounds(self) -> "IntervalValue":
        if self.lower > self.upper:
            raise ValueError("interval lower must not exceed upper")
        return self


class EnumSetValue(StrictModel):
    kind: Literal["enum_set"]
    values: list[str] = Field(min_length=1)


class ExpressionValue(StrictModel):
    kind: Literal["expression"]
    expression: str = Field(min_length=1)
    variable_assertion_ids: list[str] = Field(default_factory=list)


class UnknownValue(StrictModel):
    kind: Literal["unknown"]
    reason: str | None = None


AssertionValue = Annotated[
    ExactValue | IntervalValue | EnumSetValue | ExpressionValue | UnknownValue,
    Field(discriminator="kind"),
]


class GraphSource(StrictModel):
    id: str
    document_id: str | None = None
    uri: str | None = None
    sha256: str
    revision: str | None = None
    media_type: str | None = None


class GraphNode(StrictModel):
    id: str
    type: NodeType
    name: str | None = None
    namespace: str | None = None
    extension: dict[str, Any] | None = None


class GraphEdge(StrictModel):
    id: str
    type: EdgeType
    source_id: str
    target_id: str
    namespace: str | None = None
    extension: dict[str, Any] | None = None


class Evidence(StrictModel):
    id: str
    kind: Literal[
        "raster_region", "vector_entity", "text", "dimension", "standard",
        "calculation", "trace_run", "visual_verification", "human_decision",
        "kernel_topology", "projection_comparison", "model_raw_output",
    ]
    source_id: str | None = None
    source_region_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    sha256: str | None = None


class Assertion(StrictModel):
    id: str
    subject_id: str
    predicate: str
    value: AssertionValue
    unit: str | None = None
    coordinate_system: str | None = None
    origin: Origin
    assurance: Assurance = "proposed"
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    impacts: list[Impact] = Field(default_factory=list)
    impact_magnitude_percent: dict[str, float] = Field(default_factory=dict)
    hypothesis_id: str | None = None
    supersedes_assertion_id: str | None = None
    state: Literal["active", "superseded", "retracted"] = "active"
    namespace: str | None = None
    extension: dict[str, Any] | None = None


class HypothesisOption(StrictModel):
    id: str
    assertion_ids: list[str]
    hard_constraints_satisfied: bool = False
    evidence_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    cross_view_consistency: float = Field(default=0.0, ge=0.0, le=1.0)
    assumption_count: int = Field(default=0, ge=0)
    domain_prior: float = Field(default=0.0, ge=0.0, le=1.0)
    hybrid_trace_score: float = Field(default=0.0, ge=0.0, le=1.0)


class HypothesisSet(StrictModel):
    id: str
    option_ids: list[str] = Field(min_length=1)
    selected_option_id: str | None = None


class Requirement(StrictModel):
    id: str
    kind: Literal[
        "topology", "interface", "envelope", "load", "connectivity", "view",
        "section", "dimension", "safety", "quantity", "mass", "domain",
    ]
    target_node_ids: list[str] = Field(default_factory=list)
    assertion_ids: list[str] = Field(default_factory=list)
    mandatory: bool = True


class BuildTarget(StrictModel):
    id: str
    kind: Literal[
        "preview_brep", "production_step", "provisional_step", "preview_ifc",
        "production_ifc", "provisional_ifc", "stl", "dxf", "pdf",
    ]
    root_node_ids: list[str]
    requirement_ids: list[str] = Field(default_factory=list)
    critical_impacts: list[Impact] = Field(default_factory=lambda: [
        "base_topology", "component_count", "assembly_interface", "mate", "fit",
        "interchangeability", "envelope", "connection_opening", "load_path",
        "structural_capacity", "regulatory_check", "connectivity", "required_view",
        "required_section", "required_dimension", "manufacturing_safety",
        "operational_safety", "material_quantity", "mass",
    ])
    mass_tolerance_percent: float = Field(default=1.0, ge=0.0)


class VerificationState(StrictModel):
    comprehension: Literal["accumulating", "converged", "contradictory", "review_needed"] = "accumulating"
    build: Literal["not_ready", "preview_ready", "preview_built", "verified"] = "not_ready"
    release: Literal["blocked", "approved", "certified"] = "blocked"
    issue_codes: list[str] = Field(default_factory=list)
    critical_unresolved_assertion_ids: list[str] = Field(default_factory=list)
    checked_levels: list[int] = Field(default_factory=list)


class ReaderManifest(StrictModel):
    max_wall_seconds: int = Field(default=900, ge=1)
    max_model_calls: int = Field(default=32, ge=1)
    call_timeout_seconds: int = Field(default=90, ge=1)
    no_progress_pass_limit: int = Field(default=2, ge=1)
    calls_used: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0.0, ge=0.0)
    no_progress_passes: int = Field(default=0, ge=0)
    ordinary_attempts: dict[str, int] = Field(default_factory=dict)
    stop_reason: str | None = None


class ExtensionRegistration(StrictModel):
    namespace: str
    version: str
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EngineeringModelGraph(StrictModel):
    graph_id: str
    schema_version: Literal["emg/1.0"] = "emg/1.0"
    revision: int = Field(default=0, ge=0)
    parent_revision: int | None = None
    canonical_sha256: str = ""
    profile: Profile
    sources: list[GraphSource] = Field(default_factory=list)
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    assertions: list[Assertion] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    hypothesis_options: list[HypothesisOption] = Field(default_factory=list)
    hypothesis_sets: list[HypothesisSet] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    build_targets: list[BuildTarget] = Field(default_factory=list)
    verification: VerificationState = Field(default_factory=VerificationState)
    reader_manifest: ReaderManifest = Field(default_factory=ReaderManifest)
    extension_registry: list[ExtensionRegistration] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_integrity(self) -> "EngineeringModelGraph":
        collections = {
            "source": self.sources, "node": self.nodes, "edge": self.edges,
            "assertion": self.assertions, "evidence": self.evidence,
            "hypothesis option": self.hypothesis_options,
            "hypothesis set": self.hypothesis_sets, "requirement": self.requirements,
            "build target": self.build_targets,
        }
        for label, items in collections.items():
            ids = [item.id for item in items]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {label} ids")
        node_ids = {item.id for item in self.nodes}
        source_ids = {item.id for item in self.sources}
        evidence_ids = {item.id for item in self.evidence}
        assertion_ids = {item.id for item in self.assertions}
        option_ids = {item.id for item in self.hypothesis_options}
        requirement_ids = {item.id for item in self.requirements}
        for edge in self.edges:
            if edge.source_id not in node_ids or edge.target_id not in node_ids:
                raise ValueError(f"broken edge reference: {edge.id}")
        for item in self.evidence:
            if item.source_id and item.source_id not in source_ids:
                raise ValueError(f"broken evidence source: {item.id}")
            if item.source_region_id and item.source_region_id not in node_ids:
                raise ValueError(f"broken evidence region: {item.id}")
        for item in self.assertions:
            if item.subject_id not in node_ids:
                raise ValueError(f"broken assertion subject: {item.id}")
            if any(ref not in evidence_ids for ref in item.evidence_ids):
                raise ValueError(f"broken assertion evidence: {item.id}")
            if item.supersedes_assertion_id and item.supersedes_assertion_id not in assertion_ids:
                raise ValueError(f"broken superseded assertion: {item.id}")
        for option in self.hypothesis_options:
            if any(ref not in assertion_ids for ref in option.assertion_ids):
                raise ValueError(f"broken hypothesis assertion: {option.id}")
        for item in self.hypothesis_sets:
            if any(ref not in option_ids for ref in item.option_ids):
                raise ValueError(f"broken hypothesis option: {item.id}")
            if item.selected_option_id and item.selected_option_id not in item.option_ids:
                raise ValueError(f"selected option outside hypothesis set: {item.id}")
        for item in self.requirements:
            if any(ref not in node_ids for ref in item.target_node_ids):
                raise ValueError(f"broken requirement node: {item.id}")
            if any(ref not in assertion_ids for ref in item.assertion_ids):
                raise ValueError(f"broken requirement assertion: {item.id}")
        for item in self.build_targets:
            if any(ref not in node_ids for ref in item.root_node_ids):
                raise ValueError(f"broken build target root: {item.id}")
            if any(ref not in requirement_ids for ref in item.requirement_ids):
                raise ValueError(f"broken build target requirement: {item.id}")
        registry = {item.namespace for item in self.extension_registry}
        extensible = [*self.nodes, *self.edges, *self.assertions]
        if any(item.extension is not None and item.namespace not in registry for item in extensible):
            raise ValueError("unregistered extension namespace")
        if self.parent_revision is not None and self.parent_revision >= self.revision:
            raise ValueError("parent revision must precede revision")
        return self

    def calculated_sha256(self) -> str:
        payload = self.model_dump(mode="json")
        payload["canonical_sha256"] = ""
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def sealed(self) -> "EngineeringModelGraph":
        return self.model_copy(update={"canonical_sha256": self.calculated_sha256()})


class GraphPatch(StrictModel):
    schema_version: Literal["emg-patch/1.0"] = "emg-patch/1.0"
    patch_id: str
    base_revision: int = Field(ge=0)
    base_sha256: str
    producer: Literal["reader", "tracer", "visual_verifier", "system", "human"]
    pass_id: str
    idempotency_key: str
    add_nodes: list[GraphNode] = Field(default_factory=list)
    add_edges: list[GraphEdge] = Field(default_factory=list)
    add_assertions: list[Assertion] = Field(default_factory=list)
    add_evidence: list[Evidence] = Field(default_factory=list)
    add_hypothesis_options: list[HypothesisOption] = Field(default_factory=list)
    add_hypothesis_sets: list[HypothesisSet] = Field(default_factory=list)
    replace_requirements: list[Requirement] | None = None
    replace_build_targets: list[BuildTarget] | None = None
    supersede_assertion_ids: list[str] = Field(default_factory=list)
    retract_assertion_ids: list[str] = Field(default_factory=list)
    resolved_question_ids: list[str] = Field(default_factory=list)
    remaining_contradictions: list[str] = Field(default_factory=list)
    reader_call_count: int = Field(default=0, ge=0, le=4)
    reader_call_elapsed_seconds: float = Field(default=0.0, ge=0.0)
    reader_attempt_assertion_ids: list[str] = Field(default_factory=list)
    reader_stop_reason: str | None = None


class PatchMergeError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


_MODEL_PRODUCERS = {"reader", "tracer", "visual_verifier"}
_FORBIDDEN_MODEL_ASSURANCE = {"constraint_validated", "human_approved"}


def apply_graph_patch(
    graph: EngineeringModelGraph,
    patch: GraphPatch,
    *,
    applied_idempotency_keys: set[str] | None = None,
) -> EngineeringModelGraph:
    """Apply one patch atomically or raise with all deterministic errors."""
    errors: list[str] = []
    if patch.base_revision != graph.revision or patch.base_sha256 != graph.canonical_sha256:
        errors.append("stale_base_revision")
    if applied_idempotency_keys and patch.idempotency_key in applied_idempotency_keys:
        errors.append("duplicate_idempotency_key")
    if patch.producer in _MODEL_PRODUCERS and any(
        item.assurance in _FORBIDDEN_MODEL_ASSURANCE for item in patch.add_assertions
    ):
        errors.append("producer_cannot_validate_or_approve")
    if patch.reader_stop_reason and patch.producer != "system":
        errors.append("only_system_can_stop_reader")
    if patch.reader_call_count and patch.producer not in {*_MODEL_PRODUCERS, "system"}:
        errors.append("reader_runtime_metadata_requires_reader_or_system")
    if not patch.reader_call_count and (
        patch.reader_call_elapsed_seconds or patch.reader_attempt_assertion_ids
    ):
        errors.append("reader_runtime_metadata_requires_call")
    if (
        patch.replace_requirements is not None
        or patch.replace_build_targets is not None
    ) and patch.producer not in {"system", "human"}:
        errors.append("contract_replacement_requires_system_or_human")
    if patch.replace_requirements is not None:
        current_ids = {item.id for item in graph.requirements}
        replacement_ids = {item.id for item in patch.replace_requirements}
        if current_ids - replacement_ids:
            errors.append("contract_replacement_cannot_remove_requirements")
    if patch.replace_build_targets is not None:
        current_ids = {item.id for item in graph.build_targets}
        replacement_ids = {item.id for item in patch.replace_build_targets}
        if current_ids - replacement_ids:
            errors.append("contract_replacement_cannot_remove_build_targets")
    current = {item.id: item for item in graph.assertions}
    unknown_attempts = sorted(set(patch.reader_attempt_assertion_ids) - current.keys())
    if unknown_attempts:
        errors.append("unknown_reader_attempts:" + ",".join(unknown_attempts))
    touched = set(patch.supersede_assertion_ids) | set(patch.retract_assertion_ids)
    missing = sorted(touched - current.keys())
    if missing:
        errors.append("unknown_assertions:" + ",".join(missing))
    protected = [
        item_id for item_id in touched
        if item_id in current
        and current[item_id].assurance in _FORBIDDEN_MODEL_ASSURANCE
        and patch.producer != "human"
    ]
    if protected:
        errors.append("confirmed_assertions_require_human:" + ",".join(sorted(protected)))
    superseding = {item.supersedes_assertion_id for item in patch.add_assertions}
    if set(patch.supersede_assertion_ids) - superseding:
        errors.append("supersede_requires_replacement_assertion")
    if errors:
        raise PatchMergeError(errors)

    assertions = []
    for item in graph.assertions:
        state = item.state
        if item.id in patch.supersede_assertion_ids:
            state = "superseded"
        elif item.id in patch.retract_assertion_ids:
            state = "retracted"
        assertions.append(item.model_copy(update={"state": state}))
    assertions.extend(patch.add_assertions)
    reader_manifest = graph.reader_manifest
    if patch.reader_call_count:
        supported_evidence_ids = {
            evidence_id
            for assertion in patch.add_assertions
            for evidence_id in assertion.evidence_ids
        }
        contradicted = {
            item.id for item in graph.assertions if item.assurance == "contradicted"
        }
        progress = ReaderProgress(
            # Keep every raw response in the journal, but do not call a repeated
            # unreadable answer progress unless a new assertion actually uses it.
            new_evidence=sum(
                item.id in supported_evidence_ids for item in patch.add_evidence
            ),
            contradictions_closed=len(
                contradicted
                & (
                    set(patch.supersede_assertion_ids)
                    | set(patch.retract_assertion_ids)
                )
            ),
            validated_assertions_added=sum(
                item.assurance in _FORBIDDEN_MODEL_ASSURANCE
                for item in patch.add_assertions
            ),
        )
        attempts = dict(reader_manifest.ordinary_attempts)
        for assertion_id in patch.reader_attempt_assertion_ids:
            attempts[assertion_id] = attempts.get(assertion_id, 0) + 1
        reader_manifest = next_reader_manifest(
            reader_manifest,
            progress,
            call_elapsed_seconds=patch.reader_call_elapsed_seconds,
            call_count=patch.reader_call_count,
        ).model_copy(update={"ordinary_attempts": attempts})
    if patch.reader_stop_reason:
        reader_manifest = reader_manifest.model_copy(
            update={"stop_reason": patch.reader_stop_reason}
        )

    try:
        merged = graph.model_copy(update={
            "revision": graph.revision + 1,
            "parent_revision": graph.revision,
            "canonical_sha256": "",
            "nodes": [*graph.nodes, *patch.add_nodes],
            "edges": [*graph.edges, *patch.add_edges],
            "assertions": assertions,
            "evidence": [*graph.evidence, *patch.add_evidence],
            "hypothesis_options": [*graph.hypothesis_options, *patch.add_hypothesis_options],
            "hypothesis_sets": [*graph.hypothesis_sets, *patch.add_hypothesis_sets],
            "requirements": (
                patch.replace_requirements
                if patch.replace_requirements is not None
                else graph.requirements
            ),
            "build_targets": (
                patch.replace_build_targets
                if patch.replace_build_targets is not None
                else graph.build_targets
            ),
            "reader_manifest": reader_manifest,
        })
        merged = EngineeringModelGraph.model_validate(merged.model_dump(mode="json"))
    except ValueError as exc:
        raise PatchMergeError(["graph_integrity", str(exc)]) from exc
    return merged.sealed()


def graph_contract_upgrade_patch(
    current: EngineeringModelGraph,
    desired: EngineeringModelGraph,
    *,
    patch_prefix: str,
) -> GraphPatch | None:
    """Create a non-destructive system patch for a newer adapter contract."""
    active_keys = {
        (item.subject_id, item.predicate)
        for item in current.assertions if item.state == "active"
    }
    known_assertion_ids = {item.id for item in current.assertions}
    add_assertions = [
        item for item in desired.assertions
        if (item.subject_id, item.predicate) not in active_keys
        and item.id not in known_assertion_ids
    ]
    current_requirements = {item.id: item for item in current.requirements}
    desired_requirements = {item.id: item for item in desired.requirements}
    merged_requirements = [
        desired_requirements.get(item.id, item) for item in current.requirements
    ]
    merged_requirements.extend(
        item for item in desired.requirements if item.id not in current_requirements
    )
    current_targets = {item.id: item for item in current.build_targets}
    desired_targets = {item.id: item for item in desired.build_targets}
    merged_targets = [
        desired_targets.get(item.id, item) for item in current.build_targets
    ]
    merged_targets.extend(
        item for item in desired.build_targets if item.id not in current_targets
    )
    requirements_changed = merged_requirements != current.requirements
    targets_changed = merged_targets != current.build_targets
    if not add_assertions and not requirements_changed and not targets_changed:
        return None
    payload = {
        "assertions": [item.model_dump(mode="json") for item in add_assertions],
        "requirements": [item.model_dump(mode="json") for item in merged_requirements],
        "build_targets": [item.model_dump(mode="json") for item in merged_targets],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return GraphPatch(
        patch_id=f"{patch_prefix}:{digest[:20]}",
        base_revision=current.revision,
        base_sha256=current.canonical_sha256,
        producer="system",
        pass_id=f"{patch_prefix}:r{current.revision + 1}",
        idempotency_key=f"{patch_prefix}:{current.canonical_sha256}:{digest}",
        add_assertions=add_assertions,
        replace_requirements=merged_requirements if requirements_changed else None,
        replace_build_targets=merged_targets if targets_changed else None,
    )


def select_hypothesis(options: list[HypothesisOption]) -> str | None:
    """Select consistently; stable ID order is the final tie-break."""
    eligible = [item for item in options if item.hard_constraints_satisfied]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda item: (
            -item.evidence_coverage,
            -item.cross_view_consistency,
            item.assumption_count,
            -item.domain_prior,
            -item.hybrid_trace_score,
            item.id,
        ),
    ).id


def critical_assertion_ids(graph: EngineeringModelGraph, target_id: str) -> set[str]:
    """Derive criticality from target reachability and declared domain impacts."""
    target = next((item for item in graph.build_targets if item.id == target_id), None)
    if target is None:
        raise KeyError(target_id)
    requirements = {item.id: item for item in graph.requirements}
    directly_required = {
        assertion_id
        for requirement_id in target.requirement_ids
        for assertion_id in requirements[requirement_id].assertion_ids
    }
    reverse: dict[str, set[str]] = defaultdict(set)
    for edge in graph.edges:
        if edge.type in {"depends_on", "constrains", "defines", "applies_to", "part_of", "contains"}:
            reverse[edge.target_id].add(edge.source_id)
            reverse[edge.source_id].add(edge.target_id)
    reachable = set(target.root_node_ids)
    queue = deque(target.root_node_ids)
    while queue:
        reachable.update(new := reverse[queue.popleft()] - reachable)
        queue.extend(sorted(new))
    critical_impacts = set(target.critical_impacts)
    result = set()
    for item in graph.assertions:
        if item.state != "active":
            continue
        if item.id in directly_required:
            result.add(item.id)
            continue
        relevant = set(item.impacts) & critical_impacts
        for impact in relevant:
            if impact in {"mass", "material_quantity"}:
                magnitude = item.impact_magnitude_percent.get(impact)
                if magnitude is not None and magnitude <= target.mass_tolerance_percent:
                    continue
            if item.subject_id in reachable:
                result.add(item.id)
                break
    return result


class AssertionImpactReport(StrictModel):
    """Target-specific rebuild impact for one immutable assertion."""

    assertion_id: str
    target_id: str
    subject_node_id: str
    critical_for_target: bool
    classification: Literal["critical_for_target", "non_critical_for_target"]
    declared_impacts: list[Impact]
    direct_dependency_node_ids: list[str]
    affected_node_ids: list[str]
    affected_build_operation_ids: list[str]
    affected_artifact_ids: list[str]
    affected_topology_element_ids: list[str]
    affected_view_ids: list[str]
    affected_bim_object_ids: list[str]
    evidence_ids: list[str]
    superseded_by_assertion_ids: list[str]
    dependency_paths: dict[str, list[str]]


def assertion_impact_report(
    graph: EngineeringModelGraph,
    assertion_id: str,
    target_id: str,
) -> AssertionImpactReport:
    """Trace deterministic downstream rebuild impact without mutating the graph.

    Edge directions describe graph facts, not always influence. For example,
    ``operation depends_on feature`` means a feature edit affects the operation,
    while ``artifact generated_by operation`` means an operation edit affects
    the artifact. Symmetric engineering relations are traversed both ways.
    """
    assertion = next((item for item in graph.assertions if item.id == assertion_id), None)
    if assertion is None:
        raise KeyError(assertion_id)
    critical = critical_assertion_ids(graph, target_id)
    node_by_id = {item.id: item for item in graph.nodes}
    adjacency: dict[str, set[str]] = defaultdict(set)
    symmetric = {"same_object_across_views", "mates_with", "connects_to"}
    forward = {
        "contains", "represented_by", "defines", "constrains", "applies_to",
        "opens_in", "maps_to_topology",
    }
    reverse = {
        "part_of", "instance_of", "located_in", "depends_on", "generated_by",
        # realizes: source=operation, target=feature — correcting the
        # Feature assertion must show the operation as affected.
        "realizes",
    }
    for edge in graph.edges:
        if edge.type in symmetric:
            adjacency[edge.source_id].add(edge.target_id)
            adjacency[edge.target_id].add(edge.source_id)
        elif edge.type in forward:
            adjacency[edge.source_id].add(edge.target_id)
        elif edge.type in reverse:
            adjacency[edge.target_id].add(edge.source_id)

    subject_id = assertion.subject_id
    parents: dict[str, str | None] = {subject_id: None}
    queue = deque([subject_id])
    while queue:
        current = queue.popleft()
        for next_id in sorted(adjacency[current]):
            if next_id in parents:
                continue
            parents[next_id] = current
            queue.append(next_id)

    affected_ids = sorted(parents)
    paths: dict[str, list[str]] = {}
    for node_id in affected_ids:
        path: list[str] = []
        current: str | None = node_id
        while current is not None:
            path.append(current)
            current = parents[current]
        paths[node_id] = list(reversed(path))

    def affected_of_type(node_type: NodeType) -> list[str]:
        return sorted(
            node_id for node_id in affected_ids
            if node_by_id[node_id].type == node_type
        )

    is_critical = assertion_id in critical
    bim_profiles = {"construction", "mep", "electrical", "hydraulic", "pid", "mixed"}
    explicit_bim_node_ids = {
        item.subject_id
        for item in graph.assertions
        if item.state == "active"
        and item.predicate.startswith(("ifc.", "bim.", "spatial."))
    }
    affected_bim_object_ids = sorted(
        node_id
        for node_id in affected_ids
        if node_id in explicit_bim_node_ids
        or (
            graph.profile in bim_profiles
            and node_by_id[node_id].type in {"Product", "Component", "System", "Port"}
        )
    )
    return AssertionImpactReport(
        assertion_id=assertion_id,
        target_id=target_id,
        subject_node_id=subject_id,
        critical_for_target=is_critical,
        classification=(
            "critical_for_target" if is_critical else "non_critical_for_target"
        ),
        declared_impacts=assertion.impacts,
        direct_dependency_node_ids=sorted(adjacency[subject_id]),
        affected_node_ids=affected_ids,
        affected_build_operation_ids=affected_of_type("BuildOperation"),
        affected_artifact_ids=affected_of_type("Artifact"),
        affected_topology_element_ids=affected_of_type("TopologyElement"),
        affected_view_ids=affected_of_type("View"),
        affected_bim_object_ids=affected_bim_object_ids,
        evidence_ids=assertion.evidence_ids,
        superseded_by_assertion_ids=sorted(
            item.id for item in graph.assertions
            if item.supersedes_assertion_id == assertion_id
        ),
        dependency_paths=paths,
    )


class TracePrimitive(StrictModel):
    kind: Literal["segment", "arc", "circle", "polyline", "spline"]
    parameters: dict[str, float | int | list[float]]


class DeterministicTraceChecks(StrictModel):
    connected: bool
    closed: bool
    no_self_intersections: bool
    no_dangling_ends: bool
    anchors_satisfied: bool
    dimensions_satisfied: bool
    forbidden_geometry_clear: bool
    pixel_precision: float = Field(ge=0.0, le=1.0)
    pixel_recall: float = Field(ge=0.0, le=1.0)
    critical_impact_detected: bool = False

    @property
    def passed(self) -> bool:
        return all((
            self.connected, self.closed, self.no_self_intersections,
            self.no_dangling_ends, self.anchors_satisfied,
            self.dimensions_satisfied, self.forbidden_geometry_clear,
            self.pixel_precision >= 0.75,
            self.pixel_recall >= 0.70,
            not self.critical_impact_detected,
        ))


class TraceProposal(StrictModel):
    id: str
    source_region_id: str
    hypothesis_id: str
    primitives: list[TracePrimitive]
    trace_parameters: dict[str, Any] = Field(default_factory=dict)
    source_bbox: tuple[float, float, float, float]
    uncertainty: float = Field(ge=0.0, le=1.0)
    checks: DeterministicTraceChecks


class VisualDifference(StrictModel):
    kind: Literal["shape", "position", "orientation", "connectivity", "count", "other"]
    bbox: tuple[float, float, float, float]
    detail: str


class VisualVerification(StrictModel):
    proposal_id: str
    verdict: Literal["match", "mismatch", "unreadable"]
    element_count: int | None = None
    element_types: list[str] = Field(default_factory=list)
    shape_matches: bool | None = None
    position_matches: bool | None = None
    orientation_matches: bool | None = None
    connectivity_matches: bool | None = None
    differences: list[VisualDifference] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    raw_output: str
    verifier_model: str


class TraceAdmission(StrictModel):
    accepted: bool
    reason_codes: list[str]
    score: float


def evaluate_trace_admission(
    proposal: TraceProposal,
    visual: VisualVerification,
    *,
    assertion_is_non_critical: bool,
    conflicts_with_validated: bool,
) -> TraceAdmission:
    reasons: list[str] = []
    if not proposal.checks.passed:
        reasons.append("deterministic_checks_failed")
    if visual.verdict != "match":
        reasons.append(f"visual_{visual.verdict}")
    if not assertion_is_non_critical or proposal.checks.critical_impact_detected:
        reasons.append("critical_dependency")
    if conflicts_with_validated:
        reasons.append("validated_assertion_conflict")
    score = round(
        0.35 * proposal.checks.pixel_precision
        + 0.35 * proposal.checks.pixel_recall
        + 0.25 * visual.confidence
        + 0.05 * (1.0 - proposal.uncertainty),
        8,
    )
    return TraceAdmission(accepted=not reasons, reason_codes=reasons, score=score)


class BuildPlan(StrictModel):
    graph_id: str
    revision: int
    graph_sha256: str
    target_id: str
    selected_hypothesis_ids: list[str]
    operation_node_ids: list[str]
    critical_assumption_ids: list[str]
    provisional: bool
    production_export_allowed: bool
    artifact_hash: str


BuildGenerator = Literal["mechanical_brep", "assembly_step", "construction_ifc"]


class BuildAdmissionBlocker(StrictModel):
    """One deterministic reason why a domain generator must not be called."""

    code: str
    message: str
    assertion_id: str | None = None
    subject_id: str | None = None
    predicate: str | None = None
    verification_level: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class BuildAdmissionQuestion(StrictModel):
    """Actionable question that can resolve an assertion-level blocker."""

    assertion_id: str
    subject_id: str
    predicate: str
    prompt: str
    reason: str | None = None


class BuildAdmissionReport(StrictModel):
    """Fail-closed graph-to-generator boundary contract."""

    schema_version: Literal["emg-build-admission/1.0"] = "emg-build-admission/1.0"
    graph_id: str
    revision: int
    graph_sha256: str
    profile: Profile
    target_id: str
    target_kind: str
    generator: BuildGenerator
    allowed: bool
    review_required: bool
    blockers: list[BuildAdmissionBlocker]
    questions: list[BuildAdmissionQuestion]
    verification_issue_codes: list[str]
    advisory_verification_issue_codes: list[str]
    pending_output_assertion_ids: list[str]
    plan: BuildPlan


def compile_build_plan(graph: EngineeringModelGraph, target_id: str) -> BuildPlan:
    critical = critical_assertion_ids(graph, target_id)
    assertions = {item.id: item for item in graph.assertions}
    unresolved = sorted(
        item_id for item_id in critical
        if assertions[item_id].assurance not in {"constraint_validated", "human_approved"}
        or assertions[item_id].value.kind == "unknown"
    )
    selected = sorted(
        item.selected_option_id for item in graph.hypothesis_sets if item.selected_option_id
    )
    selected_set = set(selected)
    active_operation_subjects = {
        item.subject_id
        for item in graph.assertions
        if item.state == "active"
        and item.predicate == PREDICATE_OPERATION_KIND
        and (item.hypothesis_id is None or item.hypothesis_id in selected_set)
    }
    node_order = {item.id: index for index, item in enumerate(graph.nodes)}
    operation_sequence = {
        item.subject_id: int(item.value.value)
        for item in graph.assertions
        if item.state == "active"
        and item.predicate == PREDICATE_OPERATION_SEQUENCE
        and item.value.kind == "exact"
        and isinstance(item.value.value, int)
        and not isinstance(item.value.value, bool)
    }
    operations = sorted(
        (
            item.id
            for item in graph.nodes
            if item.type == "BuildOperation" and item.id in active_operation_subjects
        ),
        key=lambda item_id: (
            operation_sequence.get(item_id, node_order[item_id]),
            node_order[item_id],
            item_id,
        ),
    )
    target = next(item for item in graph.build_targets if item.id == target_id)
    production = target.kind.startswith("production_") or target.kind in {"dxf", "pdf"}
    payload = {
        "graph_sha256": graph.canonical_sha256,
        "target_id": target_id,
        "selected": selected,
        "operations": operations,
        "critical_assumptions": unresolved,
    }
    artifact_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return BuildPlan(
        graph_id=graph.graph_id,
        revision=graph.revision,
        graph_sha256=graph.canonical_sha256,
        target_id=target_id,
        selected_hypothesis_ids=selected,
        operation_node_ids=operations,
        critical_assumption_ids=unresolved,
        provisional=bool(unresolved),
        production_export_allowed=not production or not unresolved,
        artifact_hash=artifact_hash,
    )


class ReaderProgress(StrictModel):
    new_evidence: int = Field(default=0, ge=0)
    contradictions_closed: int = Field(default=0, ge=0)
    validated_assertions_added: int = Field(default=0, ge=0)


def next_reader_manifest(
    manifest: ReaderManifest,
    progress: ReaderProgress,
    *,
    call_elapsed_seconds: float,
    call_count: int = 1,
) -> ReaderManifest:
    useful = bool(
        progress.new_evidence
        or progress.contradictions_closed
        or progress.validated_assertions_added
    )
    updated = manifest.model_copy(update={
        "calls_used": manifest.calls_used + call_count,
        "elapsed_seconds": manifest.elapsed_seconds + call_elapsed_seconds,
        "no_progress_passes": 0 if useful else manifest.no_progress_passes + 1,
    })
    reason = None
    if updated.elapsed_seconds >= updated.max_wall_seconds:
        reason = "wall_budget_exhausted"
    elif updated.calls_used >= updated.max_model_calls:
        reason = "model_call_budget_exhausted"
    elif updated.no_progress_passes >= updated.no_progress_pass_limit:
        reason = "fixed_point_no_progress"
    return updated.model_copy(update={"stop_reason": reason})


class ReaderPassPlan(StrictModel):
    kind: Literal["read_critical", "read_focused", "hybrid_trace", "stop"]
    assertion_ids: list[str] = Field(default_factory=list)
    source_region_ids: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    reason: str


def plan_next_reader_pass(
    graph: EngineeringModelGraph,
    *,
    target_id: str,
    ordinary_attempts: dict[str, int] | None = None,
) -> ReaderPassPlan:
    """Recalculate the unresolved frontier and schedule one narrow pass."""
    if graph.reader_manifest.stop_reason:
        return ReaderPassPlan(kind="stop", reason=graph.reader_manifest.stop_reason)
    attempts = (
        ordinary_attempts
        if ordinary_attempts is not None
        else graph.reader_manifest.ordinary_attempts
    )
    critical = critical_assertion_ids(graph, target_id)
    unresolved = [
        item for item in graph.assertions
        if item.state == "active"
        and item.value.kind == "unknown"
        and item.assurance not in {"constraint_validated", "human_approved"}
    ]
    critical_frontier = sorted(item.id for item in unresolved if item.id in critical)
    if critical_frontier:
        return ReaderPassPlan(
            kind="read_critical", assertion_ids=critical_frontier[:8],
            questions=[f"Resolve build-critical assertion {item}" for item in critical_frontier[:8]],
            reason="build_critical_unresolved",
        )
    ordinary = sorted(item.id for item in unresolved if attempts.get(item.id, 0) == 0)
    if ordinary:
        return ReaderPassPlan(
            kind="read_focused", assertion_ids=ordinary[:8],
            questions=[f"Read assertion {item} from its evidence region" for item in ordinary[:8]],
            reason="unresolved_observation_frontier",
        )
    traceable = sorted(
        item.id
        for item in unresolved
        if item.id not in critical
        and attempts.get(item.id, 0) > 0
        and is_hybrid_trace_candidate(graph, item)
    )
    if traceable:
        region_ids = sorted({
            evidence.source_region_id
            for assertion in unresolved if assertion.id in traceable
            for evidence_id in assertion.evidence_ids
            for evidence in graph.evidence if evidence.id == evidence_id and evidence.source_region_id
        })
        return ReaderPassPlan(
            kind="hybrid_trace", assertion_ids=traceable[:1], source_region_ids=region_ids[:1],
            reason="non_critical_reading_exhausted",
        )
    if unresolved:
        return ReaderPassPlan(kind="stop", reason="unresolved_non_traceable")
    return ReaderPassPlan(kind="stop", reason="frontier_resolved")


def is_hybrid_trace_candidate(
    graph: EngineeringModelGraph,
    assertion: Assertion,
) -> bool:
    """Allow tracing only for localized visual shape assertions declared by the adapter."""
    adapter = DOMAIN_ADAPTERS.get(graph.profile)
    if adapter is None or not adapter.hybrid_trace_cases:
        return False
    predicate = assertion.predicate.lower()
    shape_tokens = ("contour", "profile", "shape", "outline", "fillet", "decor")
    if not any(token in predicate for token in shape_tokens):
        return False
    evidence = {item.id: item for item in graph.evidence}
    regions = [
        evidence[evidence_id]
        for evidence_id in assertion.evidence_ids
        if evidence_id in evidence
        and evidence[evidence_id].kind == "raster_region"
        and evidence[evidence_id].source_region_id
    ]
    return bool(regions) and all(
        not bool(item.payload.get("fallback")) for item in regions
    )


def rank_trace_proposals(
    candidates: list[tuple[TraceProposal, VisualVerification]],
    *,
    assertion_is_non_critical: bool,
    conflicts_with_validated: bool,
) -> list[tuple[TraceProposal, TraceAdmission]]:
    """Evaluate at most three proposals and rank accepted variants deterministically."""
    if len(candidates) > 3:
        raise ValueError("at most three trace proposals are allowed per region")
    evaluated = [
        (
            proposal,
            evaluate_trace_admission(
                proposal, visual,
                assertion_is_non_critical=assertion_is_non_critical,
                conflicts_with_validated=conflicts_with_validated,
            ),
        )
        for proposal, visual in candidates
    ]
    return sorted(evaluated, key=lambda item: (not item[1].accepted, -item[1].score, item[0].id))


class DomainAdapter(StrictModel):
    profile: Profile
    supported_node_types: list[NodeType]
    supported_edge_types: list[EdgeType]
    critical_impacts: list[Impact]
    hybrid_trace_cases: list[str]
    mandatory_assertions: list[str]
    build_gates: list[str]
    release_gates: list[str]


DOMAIN_ADAPTERS: dict[str, DomainAdapter] = {
    "mechanical": DomainAdapter(
        profile="mechanical",
        supported_node_types=["DocumentSet", "Document", "Sheet", "View", "SourceRegion", "Product", "Component", "Feature", "Geometry", "Material", "Parameter", "Constraint", "BuildOperation", "Artifact", "TopologyElement"],
        supported_edge_types=["contains", "part_of", "represented_by", "same_object_across_views", "defines", "depends_on", "constrains", "applies_to", "generated_by", "maps_to_topology", "realizes"],
        critical_impacts=["base_topology", "envelope", "assembly_interface", "fit", "connection_opening", "manufacturing_safety", "mass"],
        hybrid_trace_cases=["decorative_profile", "secondary_fillet", "visual_local_contour"],
        mandatory_assertions=["operation.kind", "material.designation"],
        build_gates=["critical_constraints_satisfied", "build_plan_compiles"],
        release_gates=["critical_assertions_approved", "brep_reopens", "required_2d_complete"],
    ),
    "assembly": DomainAdapter(
        profile="assembly",
        supported_node_types=["DocumentSet", "Document", "Sheet", "View", "SourceRegion", "Product", "Component", "Feature", "Geometry", "Material", "Parameter", "Constraint", "BuildOperation", "Artifact", "TopologyElement"],
        supported_edge_types=["contains", "part_of", "instance_of", "represented_by", "applies_to", "mates_with", "depends_on", "generated_by", "maps_to_topology"],
        critical_impacts=["component_count", "assembly_interface", "mate", "fit", "interchangeability", "envelope", "operational_safety", "mass"],
        hybrid_trace_cases=["non_functional_instance_outline"],
        mandatory_assertions=["component.quantity", "mate.type"],
        build_gates=["components_resolved", "mates_solvable"],
        release_gates=["critical_mates_approved", "degrees_of_freedom_validated", "required_2d_complete"],
    ),
    "construction": DomainAdapter(
        profile="construction",
        supported_node_types=["DocumentSet", "Document", "Sheet", "View", "Product", "Component", "Feature", "Geometry", "Material", "System", "Port", "BuildOperation", "Artifact", "TopologyElement"],
        supported_edge_types=["contains", "part_of", "located_in", "connects_to", "opens_in", "depends_on", "generated_by", "maps_to_topology"],
        critical_impacts=["base_topology", "envelope", "connection_opening", "load_path", "structural_capacity", "regulatory_check", "operational_safety", "material_quantity"],
        hybrid_trace_cases=["finish_boundary", "non_structural_symbolic_outline"],
        mandatory_assertions=["spatial.level", "element.material"],
        build_gates=["spatial_hierarchy_valid", "openings_contained"],
        release_gates=["load_path_validated", "ifc_reopens", "required_sheets_complete"],
    ),
    "mep": DomainAdapter(
        profile="mep",
        supported_node_types=["DocumentSet", "Document", "Sheet", "View", "SourceRegion", "Component", "Feature", "Geometry", "System", "Port", "Parameter", "Constraint", "BuildOperation", "Artifact", "TopologyElement"],
        supported_edge_types=["contains", "part_of", "represented_by", "connects_to", "opens_in", "depends_on", "generated_by", "maps_to_topology"],
        critical_impacts=["connectivity", "connection_opening", "envelope", "regulatory_check", "operational_safety"],
        hybrid_trace_cases=["symbol_outline", "non_connective_route_shape"],
        mandatory_assertions=["system.kind", "port.kind"],
        build_gates=["ports_typed", "connectivity_closed"],
        release_gates=["connectivity_validated", "system_rules_passed", "required_2d_complete"],
    ),
}


def domain_adapter_for(profile: Profile) -> DomainAdapter:
    if profile in {"electrical", "hydraulic", "pid"}:
        base = DOMAIN_ADAPTERS["mep"]
        return base.model_copy(update={"profile": profile})
    if profile == "mixed":
        adapters = list(DOMAIN_ADAPTERS.values())
        return DomainAdapter(
            profile="mixed",
            supported_node_types=sorted({item for adapter in adapters for item in adapter.supported_node_types}),
            supported_edge_types=sorted({item for adapter in adapters for item in adapter.supported_edge_types}),
            critical_impacts=sorted({item for adapter in adapters for item in adapter.critical_impacts}),
            hybrid_trace_cases=sorted({item for adapter in adapters for item in adapter.hybrid_trace_cases}),
            mandatory_assertions=sorted({item for adapter in adapters for item in adapter.mandatory_assertions}),
            build_gates=sorted({item for adapter in adapters for item in adapter.build_gates}),
            release_gates=sorted({item for adapter in adapters for item in adapter.release_gates}),
        )
    return DOMAIN_ADAPTERS[profile]


ASSERTION_VALUE_ADAPTER = TypeAdapter(AssertionValue)
