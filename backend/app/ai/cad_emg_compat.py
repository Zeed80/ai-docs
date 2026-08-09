"""Compatibility projections into and out of EngineeringModelGraph v1.

Legacy drawing graphs are observations only.  Legacy specs are low-assurance
derived assertions.  Feature trees are compiled only from a sealed EMG
revision, never directly from a reader response.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.ai.cad_drawing_graph import EngineeringDrawingGraph
from app.ai.cad_ir.feature_tree import (
    Feature3D,
    FeatureTreeCandidate,
    ParamProvenance,
)
from app.domain.engineering_model_graph import (
    Assertion,
    BuildTarget,
    EngineeringModelGraph,
    Evidence,
    ExactValue,
    GraphEdge,
    GraphNode,
    GraphPatch,
    GraphSource,
    HypothesisOption,
    HypothesisSet,
    ReaderManifest,
    Requirement,
    UnknownValue,
    compile_build_plan,
)


def drawing_graph_as_observations(
    drawing: EngineeringDrawingGraph,
    *,
    graph_id: str,
    profile: str = "mechanical",
) -> EngineeringModelGraph:
    source_id = "source:sheet-0"
    nodes = [
        GraphNode(id="document-set:root", type="DocumentSet", name="Source document set"),
        GraphNode(id="sheet:0", type="Sheet", name=drawing.sheet.format or "Sheet 1"),
    ]
    edges = [
        GraphEdge(id="contains:root-sheet", type="contains", source_id="document-set:root", target_id="sheet:0")
    ]
    for view in drawing.views:
        nodes.append(GraphNode(id=f"view:{view.id}", type="View", name=view.label or view.kind))
        edges.append(GraphEdge(
            id=f"contains:sheet:{view.id}", type="contains", source_id="sheet:0", target_id=f"view:{view.id}"
        ))
    evidence = []
    for item in drawing.evidence:
        region_id = f"region:{item.id}"
        nodes.append(GraphNode(id=region_id, type="SourceRegion", name=item.kind))
        edges.append(GraphEdge(
            id=f"located:{region_id}", type="located_in", source_id=region_id, target_id="sheet:0"
        ))
        evidence.append(Evidence(
            id=f"evidence:{item.id}",
            kind="text" if item.kind == "ocr" else "raster_region",
            source_id=source_id,
            source_region_id=region_id,
            payload={
                "bbox": item.region.model_dump(mode="json"),
                "raw_text": item.raw_text,
                "legacy_kind": item.kind,
                "confidence": item.confidence,
            },
        ))
    evidence_ids = {item.id: f"evidence:{item.id}" for item in drawing.evidence}
    assertions = []
    entity_view = {
        entity_id: view.id for view in drawing.views for entity_id in view.entity_ids
    }
    for entity in drawing.entities:
        node_id = f"geometry:{entity.id}"
        nodes.append(GraphNode(id=node_id, type="Geometry", name=entity.type))
        if entity.id in entity_view:
            edges.append(GraphEdge(
                id=f"represented:{entity.id}", type="represented_by",
                source_id=node_id, target_id=f"view:{entity_view[entity.id]}",
            ))
        refs = [evidence_ids[item] for item in entity.evidence if item in evidence_ids]
        assertions.append(Assertion(
            id=f"assertion:geometry:{entity.id}",
            subject_id=node_id,
            predicate="observation.cadir_entity_json",
            value=ExactValue(
                kind="exact",
                value=json.dumps(entity.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
            ),
            coordinate_system=f"view:{entity_view[entity.id]}" if entity.id in entity_view else None,
            origin="human" if entity.origin == "human" else "observed",
            assurance={
                "inferred": "proposed",
                "observed": "observed",
                "constraint_validated": "constraint_validated",
                "human_approved": "human_approved",
            }.get(entity.assurance, "proposed"),
            evidence_ids=refs,
            confidence=entity.confidence,
        ))
    if drawing.scale_mm_per_px is not None:
        assertions.append(Assertion(
            id="assertion:sheet-scale", subject_id="sheet:0", predicate="scale.mm_per_px",
            value=ExactValue(kind="exact", value=drawing.scale_mm_per_px), unit="mm",
            origin="observed", assurance="observed", confidence=0.8,
        ))
    graph = EngineeringModelGraph(
        graph_id=graph_id,
        profile=profile,
        sources=[GraphSource(id=source_id, sha256=drawing.source.sha256, media_type=drawing.source.kind)],
        nodes=nodes,
        edges=edges,
        assertions=assertions,
        evidence=evidence,
        reader_manifest=ReaderManifest(**{
            key: value for key, value in drawing.reader_manifest.items()
            if key in ReaderManifest.model_fields
        }),
        build_targets=[BuildTarget(id="preview", kind="preview_brep", root_node_ids=["document-set:root"])],
    )
    return graph.sealed()


def legacy_spec_as_low_assurance(
    spec: Any,
    *,
    graph_id: str,
    source_sha256: str | None = None,
) -> EngineeringModelGraph:
    payload = spec.model_dump(mode="json") if hasattr(spec, "model_dump") else dict(spec)
    digest = source_sha256 or hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()
    assertions: list[Assertion] = []

    def collect(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key in sorted(value):
                collect(value[key], f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                collect(item, f"{path}[{index}]")
        elif isinstance(value, (str, int, float, bool)):
            assertions.append(Assertion(
                id=f"assertion:legacy-spec:{len(assertions)}",
                subject_id="product:legacy-spec",
                predicate=path,
                value=ExactValue(kind="exact", value=value),
                origin="derived",
                assurance="proposed",
                confidence=0.25,
            ))
        elif value is None:
            assertions.append(Assertion(
                id=f"assertion:legacy-spec:{len(assertions)}",
                subject_id="product:legacy-spec",
                predicate=path,
                value=UnknownValue(kind="unknown", reason="missing in legacy spec"),
                origin="derived",
                assurance="proposed",
            ))

    collect(payload, "")
    return EngineeringModelGraph(
        graph_id=graph_id,
        profile="mechanical",
        sources=[GraphSource(id="source:legacy-spec", sha256=digest, media_type="application/json")],
        nodes=[
            GraphNode(id="document-set:root", type="DocumentSet"),
            GraphNode(id="product:legacy-spec", type="Product", name="Legacy EngineeringDrawingSpec"),
        ],
        edges=[GraphEdge(
            id="contains:legacy-product", type="contains",
            source_id="document-set:root", target_id="product:legacy-spec",
        )],
        assertions=assertions,
        build_targets=[BuildTarget(id="preview", kind="preview_brep", root_node_ids=["product:legacy-spec"])],
    ).sealed()


def feature_tree_from_graph(
    graph: EngineeringModelGraph,
    *,
    target_id: str,
) -> FeatureTreeCandidate:
    """Compile a stable legacy kernel payload from BuildOperation assertions."""
    plan = compile_build_plan(graph, target_id)
    assertions_by_subject: dict[str, list[Assertion]] = {}
    for assertion in graph.assertions:
        if assertion.state == "active":
            assertions_by_subject.setdefault(assertion.subject_id, []).append(assertion)
    features = []
    missing = []
    allowed_kinds = set(Feature3D.model_fields["kind"].annotation.__args__)
    for operation_id in plan.operation_node_ids:
        values = {
            item.predicate: item for item in assertions_by_subject.get(operation_id, [])
        }
        kind_assertion = values.get("operation.kind")
        if not kind_assertion or kind_assertion.value.kind != "exact":
            missing.append(f"{operation_id}: operation.kind")
            continue
        kind = str(kind_assertion.value.value)
        if kind not in allowed_kinds:
            missing.append(f"{operation_id}: unsupported kind {kind}")
            continue
        params: dict[str, Any] = {}
        provenance: dict[str, ParamProvenance] = {}
        for predicate, assertion in sorted(values.items()):
            if not predicate.startswith("operation.param.") or assertion.value.kind != "exact":
                continue
            name = predicate.removeprefix("operation.param.")
            params[name] = assertion.value.value
            provenance[name] = ParamProvenance(
                origin={
                    "observed": "measured", "human": "stated", "standard": "standard",
                    "assumed": "guessed", "derived": "propagated", "traced": "measured",
                }[assertion.origin],
                detail=f"EMG {assertion.id} ({assertion.assurance})",
            )
        features.append(Feature3D(
            kind=kind,
            source_entity_ids=[],
            params=params,
            param_provenance=provenance,
            confidence=min((item.confidence for item in values.values()), default=0.0),
        ))
    missing.extend(f"critical assertion {item}" for item in plan.critical_assumption_ids)
    return FeatureTreeCandidate(
        features=features,
        score=1.0 if not missing else max(0.0, 1.0 - 0.1 * len(missing)),
        label=f"EMG {graph.graph_id} r{graph.revision} {graph.canonical_sha256[:12]}",
        missing_data=missing,
    )


def spec_feature_tree_as_graph(
    spec: Any,
    candidate: FeatureTreeCandidate,
    *,
    graph_id: str,
    source_sha256: str | None = None,
) -> EngineeringModelGraph:
    """Create the initial EMG revision for the legacy mechanical reader.

    The compatibility reader may seed assertions, but the candidate sent to
    the kernel is compiled back from this sealed graph by
    :func:`feature_tree_from_graph`.
    """
    graph = legacy_spec_as_low_assurance(
        spec, graph_id=graph_id, source_sha256=source_sha256
    )
    nodes = list(graph.nodes)
    edges = list(graph.edges)
    assertions = list(graph.assertions)
    operation_assertion_ids: list[str] = []
    provenance_origin = {
        "measured": "observed",
        "stated": "observed",
        "standard": "standard",
        "guessed": "assumed",
        "propagated": "derived",
    }
    for index, feature in enumerate(candidate.features):
        operation_id = f"operation:{index}"
        nodes.append(GraphNode(
            id=operation_id,
            type="BuildOperation",
            name=f"{index + 1}. {feature.kind}",
        ))
        edges.append(GraphEdge(
            id=f"depends:{operation_id}",
            type="depends_on",
            source_id=operation_id,
            target_id="product:legacy-spec",
        ))
        kind_id = f"assertion:{operation_id}:kind"
        assertions.append(Assertion(
            id=kind_id,
            subject_id=operation_id,
            predicate="operation.kind",
            value=ExactValue(kind="exact", value=feature.kind),
            origin="derived",
            assurance="proposed",
            confidence=feature.confidence,
            impacts=["base_topology"],
            hypothesis_id="hypothesis:legacy-reader",
        ))
        operation_assertion_ids.append(kind_id)
        sequence_id = f"assertion:{operation_id}:sequence"
        assertions.append(Assertion(
            id=sequence_id,
            subject_id=operation_id,
            predicate="operation.sequence",
            value=ExactValue(kind="exact", value=index),
            origin="derived",
            assurance="proposed",
            confidence=1.0,
            impacts=["base_topology"],
            hypothesis_id="hypothesis:legacy-reader",
        ))
        operation_assertion_ids.append(sequence_id)
        for name, value in sorted(feature.params.items()):
            if value is None:
                continue
            provenance = feature.param_provenance.get(name)
            origin = provenance_origin.get(
                provenance.origin if provenance else "guessed", "assumed"
            )
            assertion_id = f"assertion:{operation_id}:param:{name}"
            assertions.append(Assertion(
                id=assertion_id,
                subject_id=operation_id,
                predicate=f"operation.param.{name}",
                value=ExactValue(kind="exact", value=value),
                unit="mm" if name.endswith("_mm") else None,
                origin=origin,
                assurance="observed" if origin in {"observed", "standard"} else "proposed",
                confidence=feature.confidence,
                impacts=["base_topology"],
                hypothesis_id="hypothesis:legacy-reader",
            ))
            operation_assertion_ids.append(assertion_id)
    for index, item in enumerate(candidate.missing_data):
        assertion_id = f"assertion:unresolved:{index}"
        assertions.append(Assertion(
            id=assertion_id,
            subject_id="product:legacy-spec",
            predicate=f"build.unresolved.{index}",
            value=UnknownValue(kind="unknown", reason=str(item)),
            origin="derived",
            assurance="proposed",
            impacts=["base_topology"],
            hypothesis_id="hypothesis:legacy-reader",
        ))
        operation_assertion_ids.append(assertion_id)
    requirement = Requirement(
        id="requirement:production-geometry",
        kind="topology",
        target_node_ids=["product:legacy-spec"],
        assertion_ids=operation_assertion_ids,
    )
    return EngineeringModelGraph(
        **{
            **graph.model_dump(mode="json"),
            "canonical_sha256": "",
            "nodes": nodes,
            "edges": edges,
            "assertions": assertions,
            "hypothesis_options": [HypothesisOption(
                id="hypothesis:legacy-reader",
                assertion_ids=operation_assertion_ids,
                hard_constraints_satisfied=not candidate.missing_data,
                evidence_coverage=max(0.0, min(1.0, candidate.score)),
                cross_view_consistency=max(0.0, min(1.0, candidate.score)),
                assumption_count=sum(
                    1 for item in assertions if item.origin == "assumed"
                ),
            )],
            "hypothesis_sets": [HypothesisSet(
                id="hypothesis-set:build",
                option_ids=["hypothesis:legacy-reader"],
                selected_option_id="hypothesis:legacy-reader",
            )],
            "requirements": [requirement],
            "build_targets": [
                BuildTarget(
                    id="preview",
                    kind="preview_brep",
                    root_node_ids=["product:legacy-spec"],
                    critical_impacts=[],
                ),
                BuildTarget(
                    id="production",
                    kind="production_step",
                    root_node_ids=["product:legacy-spec"],
                    requirement_ids=[requirement.id],
                ),
            ],
        }
    ).sealed()


def feature_tree_revision_patch(
    graph: EngineeringModelGraph,
    spec: Any,
    candidate: FeatureTreeCandidate,
    *,
    producer: str,
    pass_id: str,
    idempotency_key: str,
) -> GraphPatch:
    """Represent a corrected legacy spec as an atomic EMG revision.

    Existing assertions are retained as history and superseded by namespaced
    assertions. Old operation nodes deliberately remain immutable; the build
    planner selects only operations with an active ``operation.kind`` in the
    selected hypothesis.
    """
    if producer not in {"system", "human"}:
        raise ValueError("feature-tree revision producer must be system or human")
    next_revision = graph.revision + 1
    prefix = f"r{next_revision}"
    digest = hashlib.sha256(
        json.dumps(
            {
                "spec": spec.model_dump(mode="json") if hasattr(spec, "model_dump") else spec,
                "candidate": candidate.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    hypothesis_id = f"hypothesis:{prefix}:{digest[:16]}"
    add_nodes: list[GraphNode] = []
    add_edges: list[GraphEdge] = []
    add_assertions: list[Assertion] = []
    assertion_ids: list[str] = []
    provenance_origin = {
        "measured": "observed",
        "stated": "human" if producer == "human" else "observed",
        "standard": "standard",
        "guessed": "assumed",
        "propagated": "derived",
    }
    active_operation_ids = compile_build_plan(graph, "preview").operation_node_ids

    payload = spec.model_dump(mode="json") if hasattr(spec, "model_dump") else dict(spec)
    flattened: list[tuple[str, Any]] = []

    def collect(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key in sorted(value):
                collect(value[key], f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                collect(item, f"{path}[{index}]")
        else:
            flattened.append((path, value))

    collect(payload, "")
    for index, (path, value) in enumerate(flattened):
        old_id = next((
            item.id for item in graph.assertions
            if item.state == "active"
            and item.subject_id == "product:legacy-spec"
            and item.predicate == path
        ), None)
        assertion_id = f"assertion:{prefix}:spec:{index}"
        add_assertions.append(Assertion(
            id=assertion_id,
            subject_id="product:legacy-spec",
            predicate=path,
            value=(
                UnknownValue(kind="unknown", reason="missing in corrected spec")
                if value is None
                else ExactValue(kind="exact", value=value)
            ),
            origin="human" if producer == "human" else "derived",
            assurance="human_approved" if producer == "human" else "proposed",
            confidence=1.0 if producer == "human" else 0.5,
            hypothesis_id=hypothesis_id,
            supersedes_assertion_id=old_id,
        ))
        assertion_ids.append(assertion_id)

    for index, feature in enumerate(candidate.features):
        operation_id = f"operation:{prefix}:{index:04d}"
        add_nodes.append(GraphNode(
            id=operation_id, type="BuildOperation", name=f"{index + 1}. {feature.kind}",
        ))
        add_edges.append(GraphEdge(
            id=f"depends:{operation_id}", type="depends_on",
            source_id=operation_id, target_id="product:legacy-spec",
        ))
        entries = [
            ("kind", feature.kind),
            ("sequence", index),
            *sorted(feature.params.items()),
        ]
        for name, value in entries:
            if value is None:
                continue
            predicate = (
                "operation.kind" if name == "kind"
                else "operation.sequence" if name == "sequence"
                else f"operation.param.{name}"
            )
            previous_operation_id = (
                active_operation_ids[index]
                if index < len(active_operation_ids) else None
            )
            old_id = next((
                item.id for item in graph.assertions
                if item.state == "active"
                and item.subject_id == previous_operation_id
                and item.predicate == predicate
            ), None)
            assertion_id = f"assertion:{operation_id}:{name}"
            provenance = (
                feature.param_provenance.get(name)
                if name not in {"kind", "sequence"} else None
            )
            origin = (
                "derived" if name in {"kind", "sequence"} else provenance_origin.get(
                    provenance.origin if provenance else "guessed", "assumed"
                )
            )
            add_assertions.append(Assertion(
                id=assertion_id,
                subject_id=operation_id,
                predicate=predicate,
                value=ExactValue(kind="exact", value=value),
                unit="mm" if name.endswith("_mm") else None,
                origin=origin,
                # Correcting the source spec approves those source values, not
                # every downstream operation inferred from them.
                assurance=(
                    "observed" if origin in {"observed", "standard"} else "proposed"
                ),
                confidence=feature.confidence,
                impacts=["base_topology"],
                hypothesis_id=hypothesis_id,
                supersedes_assertion_id=old_id,
            ))
            assertion_ids.append(assertion_id)

    replaced = {
        item.supersedes_assertion_id
        for item in add_assertions
        if item.supersedes_assertion_id is not None
    }
    active_operation_set = set(active_operation_ids)
    removed = [
        item for item in graph.assertions
        if item.state == "active"
        and item.subject_id in active_operation_set
        and item.id not in replaced
    ]
    for index, old in enumerate(removed):
        assertion_id = f"assertion:{prefix}:removed:{index}"
        add_assertions.append(Assertion(
            id=assertion_id,
            subject_id="product:legacy-spec",
            predicate=f"build.removed_assertion.{index}",
            value=ExactValue(kind="exact", value=old.id),
            origin="human" if producer == "human" else "derived",
            assurance="human_approved" if producer == "human" else "proposed",
            confidence=1.0 if producer == "human" else 0.5,
            hypothesis_id=hypothesis_id,
            supersedes_assertion_id=old.id,
        ))
        assertion_ids.append(assertion_id)
        replaced.add(old.id)
    superseded = sorted(replaced)
    return GraphPatch(
        patch_id=f"patch:{prefix}:{digest[:16]}",
        base_revision=graph.revision,
        base_sha256=graph.canonical_sha256,
        producer=producer,
        pass_id=pass_id,
        idempotency_key=idempotency_key,
        add_nodes=add_nodes,
        add_edges=add_edges,
        add_assertions=add_assertions,
        add_hypothesis_options=[HypothesisOption(
            id=hypothesis_id,
            assertion_ids=assertion_ids,
            hard_constraints_satisfied=not candidate.missing_data,
            evidence_coverage=max(0.0, min(1.0, candidate.score)),
            cross_view_consistency=max(0.0, min(1.0, candidate.score)),
            assumption_count=sum(1 for item in add_assertions if item.origin == "assumed"),
        )],
        add_hypothesis_sets=[HypothesisSet(
            id=f"hypothesis-set:{prefix}",
            option_ids=[hypothesis_id],
            selected_option_id=hypothesis_id,
        )],
        supersede_assertion_ids=superseded,
    )
