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
from app.domain.emg_predicates import (
    BUILD_REMOVED_ASSERTION_PREFIX,
    BUILD_UNRESOLVED_PREFIX,
    FEATURE_PARAM_PREFIX,
    OPERATION_PARAM_PREFIX,
    PREDICATE,
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
            predicate=PREDICATE.OBSERVATION_CADIR_ENTITY_JSON,
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
            id="assertion:sheet-scale", subject_id="sheet:0", predicate=PREDICATE.SCALE_MM_PER_PX,
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
    source_uri: str | None = None,
) -> EngineeringModelGraph:
    payload = spec.model_dump(mode="json") if hasattr(spec, "model_dump") else dict(spec)
    digest = source_sha256 or hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()
    assertions: list[Assertion] = []
    whole_sheet_evidence_id = "evidence:whole-sheet" if source_uri else None
    provenance = payload.get("value_provenance") or {}
    nodes = [
        GraphNode(id="document-set:root", type="DocumentSet"),
        GraphNode(
            id="product:legacy-spec",
            type="Product",
            name="Legacy EngineeringDrawingSpec",
        ),
    ]
    edges = [GraphEdge(
        id="contains:legacy-product",
        type="contains",
        source_id="document-set:root",
        target_id="product:legacy-spec",
    )]
    evidence: list[Evidence] = []

    def provenance_key(path: str) -> str:
        return path.replace(".", "/").replace("[", "/").replace("]", "")

    def localized_evidence(path: str) -> list[str]:
        if not source_uri:
            return []
        item = provenance.get(provenance_key(path))
        if not isinstance(item, dict):
            return []
        candidates = [
            value for value in item.get("evidence") or []
            if isinstance(value, dict)
            and isinstance(value.get("source_bbox"), (list, tuple))
            and len(value["source_bbox"]) == 4
        ]
        if not candidates:
            return []
        raw = candidates[0]
        try:
            x0, y0, x1, y1 = (float(value) for value in raw["source_bbox"])
        except (TypeError, ValueError):
            return []
        if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
            return []
        suffix = f"{len(evidence):04d}"
        region_id = f"region:legacy-spec:{suffix}"
        evidence_id = f"evidence:legacy-spec:{suffix}"
        nodes.append(GraphNode(
            id=region_id,
            type="SourceRegion",
            name=f"Observed region for {path}",
        ))
        edges.append(GraphEdge(
            id=f"located:{region_id}",
            type="located_in",
            source_id=region_id,
            target_id="document-set:root",
        ))
        evidence.append(Evidence(
            id=evidence_id,
            kind="raster_region",
            source_id="source:legacy-spec",
            source_region_id=region_id,
            payload={
                "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
                "fallback": False,
                "raw_text": raw.get("raw_text"),
                "image_index": raw.get("image_index"),
                "reader_pass": raw.get("pass"),
                "provenance_path": provenance_key(path),
            },
            sha256=digest,
        ))
        return [evidence_id]

    def collect(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key in sorted(value):
                if not path and key == "value_provenance":
                    continue
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
                evidence_ids=localized_evidence(path),
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
                evidence_ids=(
                    [whole_sheet_evidence_id] if whole_sheet_evidence_id else []
                ),
                impacts=["base_topology"],
            ))

    collect(payload, "")
    if source_uri:
        nodes.append(GraphNode(
            id="region:whole-sheet",
            type="SourceRegion",
            name="Normalized whole sheet fallback",
        ))
        edges.append(GraphEdge(
            id="located:whole-sheet",
            type="located_in",
            source_id="region:whole-sheet",
            target_id="document-set:root",
        ))
        evidence.append(Evidence(
            id="evidence:whole-sheet",
            kind="raster_region",
            source_id="source:legacy-spec",
            source_region_id="region:whole-sheet",
            payload={
                "bbox_normalized": [0.0, 0.0, 1.0, 1.0],
                "fallback": True,
            },
            sha256=digest,
        ))
    return EngineeringModelGraph(
        graph_id=graph_id,
        profile="mechanical",
        sources=[GraphSource(
            id="source:legacy-spec",
            uri=source_uri,
            sha256=digest,
            media_type="image/png" if source_uri else "application/json",
        )],
        nodes=nodes,
        edges=edges,
        assertions=assertions,
        evidence=evidence,
        build_targets=[BuildTarget(id="preview", kind="preview_brep", root_node_ids=["product:legacy-spec"])],
    ).sealed()


def feature_tree_from_graph(
    graph: EngineeringModelGraph,
    *,
    target_id: str,
) -> FeatureTreeCandidate:
    """Compile a stable legacy kernel payload from BuildOperation assertions."""
    plan = compile_build_plan(graph, target_id)
    realized_features: dict[str, list[str]] = {}
    for edge in graph.edges:
        if edge.type == "realizes":
            realized_features.setdefault(edge.source_id, []).append(
                edge.target_id.removeprefix("feature:")
            )
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
        kind_assertion = values.get(PREDICATE.OPERATION_KIND)
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
            source_feature_ids=sorted(realized_features.get(operation_id, [])),
            params=params,
            param_provenance=provenance,
            confidence=min((item.confidence for item in values.values()), default=0.0),
        ))
    # The human-readable notes the reader/builder actually wrote (e.g. A2's
    # "0:outer:4: длина ступени Ø29.5 не указана — построено с
    # предположением..."), persisted verbatim as build.unresolved.<n> on
    # product:legacy-spec — ahead of the raw "critical assertion <id>"
    # entries below, which name WHICH assertion is unconfirmed but not WHY
    # in words a person reads.
    missing = [
        str(item.value.reason)
        for item in sorted(graph.assertions, key=lambda item: item.predicate)
        if item.state == "active"
        and item.subject_id == "product:legacy-spec"
        and item.predicate.startswith(BUILD_UNRESOLVED_PREFIX)
        and item.value.kind == "unknown"
    ] + missing
    missing.extend(f"critical assertion {item}" for item in plan.critical_assumption_ids)
    return FeatureTreeCandidate(
        features=features,
        score=1.0 if not missing else max(0.0, 1.0 - 0.1 * len(missing)),
        label=f"EMG {graph.graph_id} r{graph.revision} {graph.canonical_sha256[:12]}",
        missing_data=missing,
    )


# Numeric/text fields worth surfacing as feature.param.<name> assertions.
# Deliberately not every key on the item — evidence/id/kind are structural,
# not a build parameter; taper/thread are their own nested objects, left for
# a follow-up once they need their own Feature/Parameter nodes.
_FEATURE_PARAM_FIELDS = (
    "diameter_mm", "length_mm", "width_mm", "depth_mm", "size_mm", "angle_deg",
    "radius_mm", "root_diameter_mm", "axial_position_mm", "axial_start_mm",
    "at_z_mm", "at_diameter_mm", "rotation_deg", "count",
    "bolt_circle_diameter_mm", "hole_diameter_mm", "start_angle_deg",
    "internal", "through",
)

# feature.kind value per spec list name — the spec's own vocabulary, not a
# new taxonomy invented here.
_FEATURE_LIST_KIND = {
    "chamfers": "chamfer", "fillets": "fillet", "grooves": "groove",
    "keyways": "keyway", "cross_holes": "cross_hole",
    "axial_holes": "axial_hole_pattern",
    "circular_hole_patterns": "circular_hole_pattern",
    "outer": "section_outer", "bore": "section_bore",
    "holes": "hole", "hole_patterns": "hole_pattern", "slots": "slot",
}


def feature_spec_path(feature_id: str) -> str | None:
    """Decode a stable Feature id back into its legacy-spec dotted JSON path.

    ``assign_stable_feature_ids`` (spec_vectorize.py) is the SOLE producer of
    this format — ``f"{body_index}:{list_name}:{index}"`` — and never
    reassigns an id once present. Decoding it here is not a guess about the
    drawing; it is inverting our own documented encoding, the same way a URL
    router recovers path parameters. ``body_index`` 0 is ``main_view``, N>0
    is ``parts[N-1]`` — the exact numbering ``SpecView.body_index`` and
    ``_rotation_parts`` already use.

    This is what makes a human's correction of a ``Feature`` node's own
    ``feature.param.<name>``/``feature.location`` assertion able to reach
    geometry at all: :func:`app.api.image_generation._apply_compat_spec_update`
    mirrors it onto this SAME path in the compatibility spec, and the
    existing rebuild (``feature_tree_from_spec`` → EMG graph, unchanged)
    recompiles from there — never a value copied straight onto a
    ``BuildOperation``, which would be wrong wherever the kernel param is
    itself derived (e.g. a bolt-circle diameter becoming ``center_x_mm``/
    ``center_y_mm`` via trigonometry, not a passthrough).

    Returns ``None`` — never a best-effort guess — for any id that does not
    conform (hand-set, or from before ``assign_stable_feature_ids`` ran);
    such a Feature simply stays graph-only, exactly as today.
    """
    parts = feature_id.split(":")
    if len(parts) != 3:
        return None
    body_index_str, list_name, index_str = parts
    if not body_index_str.isdigit() or not index_str.isdigit() or not list_name:
        return None
    body_index = int(body_index_str)
    body_prefix = "main_view" if body_index == 0 else f"parts[{body_index - 1}]"
    return f"{body_prefix}.{list_name}[{index_str}]"


def _emit_native_feature(
    item: Any,
    list_name: str,
    *,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    assertions: list[Assertion],
    features_shown_by_view: dict[str, list[str]],
    evidence_ids: list[str],
) -> None:
    """One Feature node + its kind/location/param assertions + represented_by
    edges to every view that names it — the body of
    ``native_feature_graph_additions``, factored out for one item at a time.
    """
    if not isinstance(item, dict):
        return
    feature_id = item.get("id")
    if not feature_id:
        return
    node_id = f"feature:{feature_id}"
    kind = _FEATURE_LIST_KIND.get(list_name, list_name)
    nodes.append(GraphNode(id=node_id, type="Feature", name=f"{kind} {feature_id}"))
    # Confidence is a coarse, honest floor — 0.6 when the reader localized
    # this element on the sheet (evidence bbox present), 0.3 when it did not.
    # Neither is a real read confidence score (the reader does not compute
    # one per feature today); refine once it does, rather than inventing a
    # more precise-looking number now.
    confidence = 0.6 if item.get("evidence") else 0.3
    assertions.append(Assertion(
        id=f"assertion:{node_id}:kind",
        subject_id=node_id,
        predicate=PREDICATE.FEATURE_KIND,
        value=ExactValue(kind="exact", value=kind),
        origin="observed",
        assurance="proposed",
        confidence=confidence,
        evidence_ids=list(evidence_ids),
        impacts=["base_topology"],
    ))
    location = item.get("location")
    if isinstance(location, str) and location:
        assertions.append(Assertion(
            id=f"assertion:{node_id}:location",
            subject_id=node_id,
            predicate=PREDICATE.FEATURE_LOCATION,
            value=ExactValue(kind="exact", value=location),
            origin="observed",
            assurance="proposed",
            confidence=confidence,
            evidence_ids=list(evidence_ids),
            impacts=["base_topology"],
        ))
    for field in _FEATURE_PARAM_FIELDS:
        value = item.get(field)
        if value is None:
            continue
        assertions.append(Assertion(
            id=f"assertion:{node_id}:param:{field}",
            subject_id=node_id,
            predicate=f"{FEATURE_PARAM_PREFIX}{field}",
            value=ExactValue(kind="exact", value=value),
            unit=(
                "mm" if field.endswith("_mm")
                else "deg" if field.endswith("_deg") else None
            ),
            origin="observed",
            assurance="proposed",
            confidence=confidence,
            evidence_ids=list(evidence_ids),
            impacts=["base_topology"],
        ))
    # Fail-closed by construction: the ONLY source for this edge is a view's
    # own features_shown explicitly naming this feature id. No proximity, no
    # body-membership heuristic — see native_feature_graph_additions.
    for view_id, shown in features_shown_by_view.items():
        if feature_id in shown:
            edges.append(GraphEdge(
                id=f"shown:{node_id}:{view_id}",
                type="represented_by",
                source_id=node_id,
                target_id=f"view:{view_id}",
            ))


def native_feature_graph_additions(
    spec_payload: dict,
    *,
    source_uri: str | None = None,
) -> tuple[list[GraphNode], list[GraphEdge], list[Assertion]]:
    """Sheet/View/Feature nodes for every id-tagged element of a read spec.

    Additive to ``legacy_spec_as_low_assurance``'s flat dotted-path
    passthrough, never a replacement for it — an element with no stable id
    (``assign_stable_feature_ids`` never ran, or it is a leaf this function
    does not cover yet, e.g. taper/thread as their own nodes) still only
    exists as a legacy-spec assertion, which is exactly the honest fallback
    the passthrough exists for.

    Every ``represented_by`` edge is sourced from a view's OWN
    ``features_shown`` — never inferred from proximity or body membership.
    Until the reader populates ``features_shown``, every Feature node this
    emits is legitimately disconnected from any View: a feature the sheet's
    views were never asked to confirm is not "shown" anywhere as far as this
    graph is concerned, and inventing that link would be exactly the guess
    this pipeline's whole design refuses to make.
    """
    nodes: list[GraphNode] = [GraphNode(id="sheet:0", type="Sheet", name="Sheet 1")]
    edges: list[GraphEdge] = [GraphEdge(
        id="contains:root-sheet", type="contains",
        source_id="document-set:root", target_id="sheet:0",
    )]
    assertions: list[Assertion] = []
    evidence_ids = ["evidence:whole-sheet"] if source_uri else []

    features_shown_by_view: dict[str, list[str]] = {}
    seen_view_ids: set[str] = set()
    for position, view in enumerate(spec_payload.get("views") or []):
        if not isinstance(view, dict):
            continue
        view_id = str(view.get("view_id") or f"view:{position}")
        if view_id in seen_view_ids:
            continue
        seen_view_ids.add(view_id)
        node_id = f"view:{view_id}"
        nodes.append(GraphNode(
            id=node_id, type="View",
            name=str(view.get("label") or view.get("kind") or view_id),
        ))
        edges.append(GraphEdge(
            id=f"contains:sheet:{view_id}", type="contains",
            source_id="sheet:0", target_id=node_id,
        ))
        features_shown_by_view[view_id] = [
            str(item) for item in (view.get("features_shown") or []) if item
        ]

    from app.ai.cad_recognize.spec_vectorize import (
        _BODY_FEATURE_FIELDS,
        _BODY_SECTION_FIELDS,
        _PROFILE_FEATURE_FIELDS,
    )

    bodies = [spec_payload.get("main_view"), *(spec_payload.get("parts") or [])]
    for body in bodies:
        if not isinstance(body, dict):
            continue
        for list_name in (*_BODY_FEATURE_FIELDS, *_BODY_SECTION_FIELDS):
            for item in body.get(list_name) or []:
                _emit_native_feature(
                    item, list_name, nodes=nodes, edges=edges,
                    assertions=assertions,
                    features_shown_by_view=features_shown_by_view,
                    evidence_ids=evidence_ids,
                )
        profile = body.get("profile")
        if isinstance(profile, dict):
            for list_name in _PROFILE_FEATURE_FIELDS:
                for item in profile.get(list_name) or []:
                    _emit_native_feature(
                        item, list_name, nodes=nodes, edges=edges,
                        assertions=assertions,
                        features_shown_by_view=features_shown_by_view,
                        evidence_ids=evidence_ids,
                    )
    return nodes, edges, assertions


def spec_feature_tree_as_graph(
    spec: Any,
    candidate: FeatureTreeCandidate,
    *,
    graph_id: str,
    source_sha256: str | None = None,
    source_uri: str | None = None,
) -> EngineeringModelGraph:
    """Create the initial EMG revision for the legacy mechanical reader.

    The compatibility reader may seed assertions, but the candidate sent to
    the kernel is compiled back from this sealed graph by
    :func:`feature_tree_from_graph`.
    """
    graph = legacy_spec_as_low_assurance(
        spec,
        graph_id=graph_id,
        source_sha256=source_sha256,
        source_uri=source_uri,
    )
    nodes = list(graph.nodes)
    edges = list(graph.edges)
    assertions = list(graph.assertions)
    operation_assertion_ids: list[str] = []
    spec_payload = spec.model_dump(mode="json") if hasattr(spec, "model_dump") else dict(spec)
    # Фаза 1.2: native Feature/Sheet/View nodes for every element the reader
    # gave a stable id — alongside, not instead of, the legacy passthrough
    # above. See native_feature_graph_additions for the fail-closed contract
    # on represented_by edges.
    native_nodes, native_edges, native_assertions = native_feature_graph_additions(
        spec_payload, source_uri=source_uri,
    )
    nodes.extend(native_nodes)
    edges.extend(native_edges)
    assertions.extend(native_assertions)
    # Ф2.6c: which of the just-emitted Feature nodes a BuildOperation may
    # legally point a realizes edge at — never a dangling reference, and
    # never anything the native builder itself did not already create.
    known_feature_ids = {item.id for item in native_nodes if item.type == "Feature"}
    title_block = spec_payload.get("title_block")
    material_designation = (
        title_block.get("material") if isinstance(title_block, dict) else None
    )
    if isinstance(material_designation, str) and material_designation.strip():
        nodes.append(GraphNode(
            id="material:primary",
            type="Material",
            name=material_designation.strip(),
        ))
        edges.append(GraphEdge(
            id="applies:material:primary",
            type="applies_to",
            source_id="material:primary",
            target_id="product:legacy-spec",
        ))
        material_assertion_id = "assertion:material:designation"
        assertions.append(Assertion(
            id=material_assertion_id,
            subject_id="material:primary",
            predicate=PREDICATE.MATERIAL_DESIGNATION,
            value=ExactValue(kind="exact", value=material_designation.strip()),
            origin="derived",
            assurance="proposed",
            confidence=0.25,
            impacts=["material_quantity", "manufacturing_safety"],
        ))
        operation_assertion_ids.append(material_assertion_id)
    provenance_origin = {
        "measured": "observed",
        "stated": "observed",
        "standard": "standard",
        "guessed": "assumed",
        "propagated": "derived",
        # Ф2 нового CAD-редактора: a feature added whole in the editor
        # (add_feature_to_graph) stamps ParamProvenance.origin="human" on
        # every one of its own params — previously unmapped here (nothing
        # produced this value on THIS path before), so it silently fell
        # through to the "assumed" default and rendered as a guess despite
        # being exactly what the engineer typed. "human" is already a valid
        # graph Assertion.origin (used by correction assertions) — map it
        # straight through instead of losing it.
        "human": "human",
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
        for feature_id in dict.fromkeys(feature.source_feature_ids):
            target_id = f"feature:{feature_id}"
            if target_id not in known_feature_ids:
                continue
            edges.append(GraphEdge(
                id=f"realizes:{operation_id}:{feature_id}",
                type="realizes",
                source_id=operation_id,
                target_id=target_id,
            ))
        kind_id = f"assertion:{operation_id}:kind"
        assertions.append(Assertion(
            id=kind_id,
            subject_id=operation_id,
            predicate=PREDICATE.OPERATION_KIND,
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
            predicate=PREDICATE.OPERATION_SEQUENCE,
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
                predicate=f"{OPERATION_PARAM_PREFIX}{name}",
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
            predicate=f"{BUILD_UNRESOLVED_PREFIX}{index}",
            value=UnknownValue(kind="unknown", reason=str(item)),
            origin="derived",
            assurance="proposed",
            evidence_ids=["evidence:whole-sheet"] if source_uri else [],
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
        # Same gap as spec_feature_tree_as_graph's own copy of this map —
        # see its comment. This is the copy add_feature_to_graph's rebuild
        # actually goes through (feature_tree_revision_patch).
        "human": "human",
    }
    active_operation_ids = compile_build_plan(graph, "preview").operation_node_ids
    # Ф2.6c: same fail-closed rule as the first build — only realize Feature
    # nodes that already exist in this graph. A newly-added spec element has
    # no Feature node yet (this function does not rebuild the native Ф1.2
    # graph), so it legitimately gets no realizes edge, same as any other
    # id-tagged reference this codebase refuses to invent.
    known_feature_ids = {item.id for item in graph.nodes if item.type == "Feature"}

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
        for feature_id in dict.fromkeys(feature.source_feature_ids):
            target_id = f"feature:{feature_id}"
            if target_id not in known_feature_ids:
                continue
            add_edges.append(GraphEdge(
                id=f"realizes:{operation_id}:{feature_id}",
                type="realizes",
                source_id=operation_id,
                target_id=target_id,
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
                PREDICATE.OPERATION_KIND if name == "kind"
                else PREDICATE.OPERATION_SEQUENCE if name == "sequence"
                else f"{OPERATION_PARAM_PREFIX}{name}"
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

    # A2: candidate.missing_data (e.g. "0:outer:4: длина ступени Ø29.5 не
    # указана — построено с предположением...") used to be dropped
    # entirely on a rebuild — this function had no equivalent of
    # spec_feature_tree_as_graph's own build.unresolved.<n> persistence, so
    # feature_tree_from_graph had nothing but opaque "critical assertion
    # <id>" text to report after the SAME round trip every rebuild goes
    # through. Superseded by index against whatever was active before, best
    # effort — the count can legitimately shift between revisions (a
    # correction can resolve or introduce different notes), which this does
    # not perfectly reconcile, but a stale note surviving one extra revision
    # is a display nuisance, not a build-safety gap.
    old_unresolved_ids = sorted(
        item.id for item in graph.assertions
        if item.state == "active"
        and item.subject_id == "product:legacy-spec"
        and item.predicate.startswith(BUILD_UNRESOLVED_PREFIX)
    )
    for index, item in enumerate(candidate.missing_data):
        assertion_id = f"assertion:{prefix}:unresolved:{index}"
        add_assertions.append(Assertion(
            id=assertion_id,
            subject_id="product:legacy-spec",
            predicate=f"{BUILD_UNRESOLVED_PREFIX}{index}",
            value=UnknownValue(kind="unknown", reason=str(item)),
            origin="derived",
            assurance="proposed",
            confidence=0.5,
            hypothesis_id=hypothesis_id,
            supersedes_assertion_id=(
                old_unresolved_ids[index] if index < len(old_unresolved_ids) else None
            ),
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
            predicate=f"{BUILD_REMOVED_ASSERTION_PREFIX}{index}",
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
