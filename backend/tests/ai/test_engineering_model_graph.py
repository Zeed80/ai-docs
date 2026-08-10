import pytest
from pydantic import ValidationError

from app.ai.cad_emg_compat import (
    feature_tree_revision_patch,
    feature_tree_from_graph,
    legacy_spec_as_low_assurance,
    spec_feature_tree_as_graph,
)
from app.ai.cad_ir.feature_tree import Feature3D, FeatureTreeCandidate, ParamProvenance
from app.domain.engineering_model_graph import (
    Assertion,
    BuildTarget,
    DeterministicTraceChecks,
    EngineeringModelGraph,
    ExactValue,
    GraphEdge,
    GraphNode,
    GraphPatch,
    HypothesisOption,
    PatchMergeError,
    ReaderManifest,
    ReaderProgress,
    Requirement,
    TracePrimitive,
    TraceProposal,
    VisualVerification,
    apply_graph_patch,
    assertion_impact_report,
    compile_build_plan,
    critical_assertion_ids,
    evaluate_trace_admission,
    graph_contract_upgrade_patch,
    next_reader_manifest,
    plan_next_reader_pass,
    rank_trace_proposals,
    select_hypothesis,
)
from app.services.engineering_model_graph import verify_graph


def _graph(*, critical_assurance="proposed") -> EngineeringModelGraph:
    return EngineeringModelGraph(
        graph_id="emg:test",
        profile="mechanical",
        nodes=[
            GraphNode(id="docs", type="DocumentSet"),
            GraphNode(id="product", type="Product"),
            GraphNode(id="op", type="BuildOperation"),
            GraphNode(id="decor", type="Geometry"),
        ],
        edges=[
            GraphEdge(id="e1", type="contains", source_id="docs", target_id="product"),
            GraphEdge(id="e2", type="depends_on", source_id="op", target_id="product"),
        ],
        assertions=[
            Assertion(
                id="a-envelope", subject_id="product", predicate="envelope.width",
                value=ExactValue(kind="exact", value=40.0), unit="mm",
                origin="assumed", assurance=critical_assurance, confidence=0.5,
                impacts=["envelope"],
            ),
            Assertion(
                id="a-decor", subject_id="decor", predicate="local.radius",
                value=ExactValue(kind="exact", value=0.5), unit="mm",
                origin="traced", assurance="corroborated", confidence=0.8,
                impacts=["visual_only"],
            ),
            Assertion(
                id="a-kind", subject_id="op", predicate="operation.kind",
                value=ExactValue(kind="exact", value="extrude"),
                origin="human", assurance="human_approved", confidence=1.0,
            ),
            Assertion(
                id="a-depth", subject_id="op", predicate="operation.param.depth_mm",
                value=ExactValue(kind="exact", value=12.0), unit="mm",
                origin="observed", assurance="constraint_validated", confidence=0.95,
            ),
        ],
        requirements=[
            Requirement(id="r-envelope", kind="envelope", target_node_ids=["product"], assertion_ids=["a-envelope"])
        ],
        build_targets=[
            BuildTarget(
                id="production", kind="production_step", root_node_ids=["product"],
                requirement_ids=["r-envelope"],
            )
        ],
    ).sealed()


def test_canonical_hash_is_stable_and_broken_refs_fail_closed():
    graph = _graph()
    assert graph.calculated_sha256() == graph.canonical_sha256
    assert graph.sealed().canonical_sha256 == graph.canonical_sha256
    payload = graph.model_dump(mode="json")
    payload["edges"][0]["target_id"] = "missing"
    with pytest.raises(ValidationError, match="broken edge reference"):
        EngineeringModelGraph.model_validate(payload)


def test_legacy_consensus_provenance_becomes_assertion_level_source_region():
    graph = legacy_spec_as_low_assurance(
        {
            "main_view": {"outer": [{"diameter_mm": 30}]},
            "value_provenance": {
                "main_view/outer/0/diameter_mm": {
                    "evidence": [{
                        "source_bbox": [10, 20, 110, 70],
                        "raw_text": "Ø30",
                        "image_index": 1,
                        "pass": 2,
                    }],
                },
            },
        },
        graph_id="image-generation:test",
        source_sha256="a" * 64,
        source_uri="image-gen/test/normalized.png",
    )

    assertion = next(
        item for item in graph.assertions
        if item.predicate == "main_view.outer[0].diameter_mm"
    )
    assert assertion.origin == "derived"
    assert assertion.assurance == "proposed"
    assert len(assertion.evidence_ids) == 1
    evidence = next(item for item in graph.evidence if item.id in assertion.evidence_ids)
    assert evidence.source_region_id is not None
    assert evidence.payload["bbox"] == {"x0": 10.0, "y0": 20.0, "x1": 110.0, "y1": 70.0}
    assert evidence.payload["fallback"] is False
    assert evidence.payload["raw_text"] == "Ø30"
    assert any(
        item.id == evidence.source_region_id and item.type == "SourceRegion"
        for item in graph.nodes
    )
    assert not any(
        item.predicate.startswith("value_provenance") for item in graph.assertions
    )


def test_graph_patch_is_atomic_revision_safe_and_model_cannot_approve():
    graph = _graph()
    patch = GraphPatch(
        patch_id="p1", base_revision=0, base_sha256=graph.canonical_sha256,
        producer="reader", pass_id="pass-1", idempotency_key="key-1",
        add_nodes=[GraphNode(id="feature", type="Feature")],
        add_assertions=[Assertion(
            id="a-feature", subject_id="feature", predicate="feature.kind",
            value=ExactValue(kind="exact", value="hole"), origin="observed",
            assurance="observed", confidence=0.8,
        )],
    )
    merged = apply_graph_patch(graph, patch)
    assert merged.revision == 1
    assert merged.parent_revision == 0
    assert merged.canonical_sha256 == merged.calculated_sha256()
    with pytest.raises(PatchMergeError, match="stale_base_revision"):
        apply_graph_patch(merged, patch)
    with pytest.raises(PatchMergeError, match="duplicate_idempotency_key"):
        apply_graph_patch(graph, patch, applied_idempotency_keys={"key-1"})

    forbidden = patch.model_copy(update={
        "patch_id": "p2", "idempotency_key": "key-2",
        "add_assertions": [patch.add_assertions[0].model_copy(update={
            "id": "a-forbidden", "assurance": "constraint_validated"
        })],
    })
    with pytest.raises(PatchMergeError, match="producer_cannot_validate_or_approve"):
        apply_graph_patch(graph, forbidden)


def test_contract_upgrade_patch_adds_gates_without_removing_existing_contract():
    current = _graph()
    payload = current.model_dump(mode="json")
    payload["canonical_sha256"] = ""
    payload["assertions"].append({
        "id": "a-required-view",
        "subject_id": "product",
        "predicate": "drawing.required_views_complete",
        "value": {"kind": "unknown", "reason": "not generated"},
        "origin": "derived",
        "assurance": "proposed",
        "confidence": 0.0,
        "impacts": ["required_view"],
    })
    payload["requirements"].append({
        "id": "r-view", "kind": "view", "target_node_ids": ["product"],
        "assertion_ids": ["a-required-view"], "mandatory": True,
    })
    payload["build_targets"][0]["requirement_ids"].append("r-view")
    desired = EngineeringModelGraph.model_validate(payload).sealed()

    patch = graph_contract_upgrade_patch(
        current, desired, patch_prefix="contract-upgrade:test",
    )

    assert patch is not None
    upgraded = apply_graph_patch(current, patch)
    assert {item.id for item in current.requirements} <= {
        item.id for item in upgraded.requirements
    }
    assert "r-view" in upgraded.build_targets[0].requirement_ids
    assert "a-required-view" in compile_build_plan(
        upgraded, "production"
    ).critical_assumption_ids
    with pytest.raises(PatchMergeError, match="contract_replacement_requires"):
        apply_graph_patch(current, patch.model_copy(update={"producer": "reader"}))


def test_confirmed_assertion_can_only_be_superseded_by_human_with_replacement():
    graph = _graph(critical_assurance="human_approved")
    replacement = graph.assertions[0].model_copy(update={
        "id": "a-envelope-2", "supersedes_assertion_id": "a-envelope",
        "value": ExactValue(kind="exact", value=42.0),
    })
    patch = GraphPatch(
        patch_id="p", base_revision=0, base_sha256=graph.canonical_sha256,
        producer="system", pass_id="pass", idempotency_key="k",
        add_assertions=[replacement], supersede_assertion_ids=["a-envelope"],
    )
    with pytest.raises(PatchMergeError, match="confirmed_assertions_require_human"):
        apply_graph_patch(graph, patch)
    merged = apply_graph_patch(graph, patch.model_copy(update={"producer": "human"}))
    assert next(item for item in merged.assertions if item.id == "a-envelope").state == "superseded"


def test_criticality_is_target_dependency_based_and_release_stays_blocked():
    graph = _graph()
    assert critical_assertion_ids(graph, "production") == {"a-envelope"}
    plan = compile_build_plan(graph, "production")
    assert plan.provisional is True
    assert plan.production_export_allowed is False
    assert plan.critical_assumption_ids == ["a-envelope"]
    assert compile_build_plan(graph, "production").artifact_hash == plan.artifact_hash

    approved = _graph(critical_assurance="human_approved")
    approved_plan = compile_build_plan(approved, "production")
    assert approved_plan.provisional is False
    assert approved_plan.production_export_allowed is True


def test_assertion_impact_reports_target_criticality_and_downstream_rebuilds():
    graph = _graph()
    payload = graph.model_dump(mode="json")
    payload["nodes"].extend([
        {"id": "artifact", "type": "Artifact"},
        {"id": "face", "type": "TopologyElement"},
    ])
    payload["edges"].extend([
        {
            "id": "e-artifact", "type": "generated_by",
            "source_id": "artifact", "target_id": "op",
        },
        {
            "id": "e-topology", "type": "maps_to_topology",
            "source_id": "artifact", "target_id": "face",
        },
    ])
    graph = EngineeringModelGraph.model_validate(payload).sealed()

    report = assertion_impact_report(graph, "a-envelope", "production")

    assert report.critical_for_target is True
    assert report.classification == "critical_for_target"
    assert report.affected_build_operation_ids == ["op"]
    assert report.affected_artifact_ids == ["artifact"]
    assert report.affected_topology_element_ids == ["face"]
    assert report.dependency_paths["face"] == [
        "product", "op", "artifact", "face",
    ]

    decorative = assertion_impact_report(graph, "a-decor", "production")
    assert decorative.classification == "non_critical_for_target"
    assert decorative.affected_build_operation_ids == []


def test_hypothesis_selection_uses_fixed_scoring_and_stable_tie_break():
    options = [
        HypothesisOption(id="b", assertion_ids=[], hard_constraints_satisfied=True, evidence_coverage=0.8),
        HypothesisOption(id="a", assertion_ids=[], hard_constraints_satisfied=True, evidence_coverage=0.8),
        HypothesisOption(id="c", assertion_ids=[], hard_constraints_satisfied=False, evidence_coverage=1.0),
    ]
    assert select_hypothesis(options) == "a"


def _trace(*, critical=False, verdict="match"):
    proposal = TraceProposal(
        id="tp1", source_region_id="roi-1", hypothesis_id="h1",
        primitives=[TracePrimitive(kind="polyline", parameters={"points": [0.0, 0.0, 1.0, 1.0]})],
        source_bbox=(10, 20, 30, 40), uncertainty=0.1,
        checks=DeterministicTraceChecks(
            connected=True, closed=True, no_self_intersections=True,
            no_dangling_ends=True, anchors_satisfied=True,
            dimensions_satisfied=True, forbidden_geometry_clear=True,
            pixel_precision=0.95, pixel_recall=0.9,
            critical_impact_detected=critical,
        ),
    )
    visual = VisualVerification(
        proposal_id="tp1", verdict=verdict, element_count=1,
        shape_matches=True, position_matches=True, orientation_matches=True,
        connectivity_matches=True, confidence=0.9, raw_output="{}",
        verifier_model="independent-vlm",
    )
    return proposal, visual


def test_hybrid_trace_requires_all_gates_and_returns_critical_to_reader():
    proposal, visual = _trace()
    accepted = evaluate_trace_admission(
        proposal, visual, assertion_is_non_critical=True, conflicts_with_validated=False
    )
    assert accepted.accepted is True
    assert accepted.score > 0.8
    critical, visual = _trace(critical=True)
    rejected = evaluate_trace_admission(
        critical, visual, assertion_is_non_critical=True, conflicts_with_validated=False
    )
    assert rejected.accepted is False
    assert "critical_dependency" in rejected.reason_codes
    proposal, mismatch = _trace(verdict="mismatch")
    assert not evaluate_trace_admission(
        proposal, mismatch, assertion_is_non_critical=True, conflicts_with_validated=False
    ).accepted


def test_reader_stops_after_two_no_progress_passes_and_preserves_defaults():
    manifest = ReaderManifest()
    manifest = next_reader_manifest(manifest, ReaderProgress(), call_elapsed_seconds=10)
    assert manifest.stop_reason is None
    manifest = next_reader_manifest(manifest, ReaderProgress(), call_elapsed_seconds=10)
    assert manifest.stop_reason == "fixed_point_no_progress"
    assert manifest.calls_used == 2
    assert manifest.max_wall_seconds == 900
    assert manifest.max_model_calls == 32


def test_reader_prioritizes_critical_unknown_before_hybrid_trace():
    graph = _graph()
    payload = graph.model_dump(mode="json")
    payload["canonical_sha256"] = ""
    payload["assertions"][0]["value"] = {"kind": "unknown", "reason": "unreadable"}
    graph = EngineeringModelGraph.model_validate(payload).sealed()
    plan = plan_next_reader_pass(graph, target_id="production", ordinary_attempts={"a-envelope": 2})
    assert plan.kind == "read_critical"
    assert plan.assertion_ids == ["a-envelope"]


def test_trace_ranking_limits_region_to_three_proposals():
    proposal, visual = _trace()
    ranked = rank_trace_proposals(
        [(proposal, visual)], assertion_is_non_critical=True, conflicts_with_validated=False
    )
    assert ranked[0][1].accepted
    with pytest.raises(ValueError, match="at most three"):
        rank_trace_proposals(
            [(proposal.model_copy(update={"id": str(i)}), visual.model_copy(update={"proposal_id": str(i)})) for i in range(4)],
            assertion_is_non_critical=True,
            conflicts_with_validated=False,
        )


def test_verifier_runs_twelve_levels_and_requires_trace_evidence():
    state, issues = verify_graph(_graph())
    assert state.checked_levels == list(range(1, 13))
    assert state.release == "blocked"
    assert "trace_verification_incomplete" in state.issue_codes
    assert any(item["level"] == 10 for item in issues)


def _mechanical_graph_with_feature(*, linked: bool) -> EngineeringModelGraph:
    """Minimal sealed mechanical graph with one Feature node, optionally
    linked to a View by represented_by — Фаза 1.4's fail-closed contract:
    unlinked is the honest default until the reader populates
    features_shown, so it must warn, not block release outright."""
    nodes = [
        GraphNode(id="docs", type="DocumentSet"),
        GraphNode(id="product", type="Product"),
        GraphNode(id="op", type="BuildOperation"),
        GraphNode(id="feature:0:chamfers:0", type="Feature"),
    ]
    edges = [
        GraphEdge(id="e1", type="contains", source_id="docs", target_id="product"),
        GraphEdge(id="e2", type="depends_on", source_id="op", target_id="product"),
    ]
    if linked:
        nodes.append(GraphNode(id="view:front", type="View"))
        edges.append(GraphEdge(
            id="shown", type="represented_by",
            source_id="feature:0:chamfers:0", target_id="view:front",
        ))
    return EngineeringModelGraph(
        graph_id="emg:mech-feature-test", profile="mechanical",
        nodes=nodes, edges=edges,
        assertions=[
            Assertion(
                id="a-kind", subject_id="op", predicate="operation.kind",
                value=ExactValue(kind="exact", value="extrude"),
                origin="human", assurance="human_approved", confidence=1.0,
            ),
            Assertion(
                id="a-material", subject_id="product", predicate="material.designation",
                value=ExactValue(kind="exact", value="Steel"),
                origin="human", assurance="human_approved", confidence=1.0,
            ),
        ],
    ).sealed()


def test_unlinked_mechanical_feature_warns_but_does_not_block_release():
    graph = _mechanical_graph_with_feature(linked=False)
    state, issues = verify_graph(graph)
    assert "mechanical_feature_without_view" in state.issue_codes
    detail = next(item for item in issues if item["code"] == "mechanical_feature_without_view")
    assert detail["severity"] == "warning"
    assert detail["node_ids"] == ["feature:0:chamfers:0"]


def test_linked_mechanical_feature_clears_the_warning():
    graph = _mechanical_graph_with_feature(linked=True)
    _, issues = verify_graph(graph)
    assert not any(item["code"] == "mechanical_feature_without_view" for item in issues)


def test_mechanical_graph_without_feature_nodes_is_silent_on_the_new_rule():
    # legacy-passthrough-only graphs (no Feature nodes at all) must not be
    # caught by this rule retroactively — _graph() has none.
    _, issues = verify_graph(_graph(critical_assurance="human_approved"))
    assert not any(item["code"] == "mechanical_feature_without_view" for item in issues)


def test_feature_tree_is_deterministic_projection_of_graph_revision():
    graph = _graph(critical_assurance="human_approved")
    candidate = feature_tree_from_graph(graph, target_id="production")
    assert candidate.features[0].kind == "extrude"
    assert candidate.features[0].params == {"depth_mm": 12.0}
    assert graph.canonical_sha256[:12] in candidate.label
    assert feature_tree_from_graph(graph, target_id="production") == candidate


def test_legacy_spec_is_only_low_assurance_compatibility_view():
    graph = legacy_spec_as_low_assurance(
        {"kind": "shaft", "diameter_mm": 20.0, "depth_mm": None},
        graph_id="legacy:1",
    )
    assert graph.schema_version == "emg/1.0"
    assert all(item.assurance == "proposed" for item in graph.assertions)
    assert all(item.origin == "derived" for item in graph.assertions)


def test_legacy_spec_links_unknowns_to_normalized_source_without_trace_permission():
    graph = legacy_spec_as_low_assurance(
        {"kind": "shaft", "diameter_mm": None},
        graph_id="legacy:source",
        source_sha256="a" * 64,
        source_uri="image-gen/test/normalized.png",
    )

    assert graph.sources[0].uri == "image-gen/test/normalized.png"
    assert graph.sources[0].media_type == "image/png"
    evidence = graph.evidence[0]
    assert evidence.payload["fallback"] is True
    unknown = next(item for item in graph.assertions if item.value.kind == "unknown")
    assert unknown.evidence_ids == [evidence.id]
    assert unknown.impacts == ["base_topology"]


def test_spec_candidate_round_trips_through_sealed_graph_before_kernel():
    candidate = FeatureTreeCandidate(
        features=[Feature3D(
            kind="chamfer",
            params={
                "size_mm": 1.0,
                "edge_selector": {"curve": "Circle", "at_z_mm": 20.0},
            },
            param_provenance={
                "size_mm": ParamProvenance(origin="stated", detail="callout"),
                "edge_selector": ParamProvenance(origin="propagated", detail="profile"),
            },
            confidence=0.9,
        )],
        score=0.9,
        label="source candidate",
    )
    graph = spec_feature_tree_as_graph(
        {"part": "test", "main_view": {"type": "shaft"}},
        candidate,
        graph_id="emg:roundtrip",
    )
    projected = feature_tree_from_graph(graph, target_id="preview")
    assert projected.features[0].params == candidate.features[0].params
    assert projected.missing_data == []
    assert compile_build_plan(graph, "production").production_export_allowed is False


def test_human_spec_rebuild_is_an_atomic_patch_and_old_operations_are_not_rebuilt():
    original = FeatureTreeCandidate(
        features=[Feature3D(
            kind="extrude", params={"depth_mm": 10.0},
            param_provenance={
                "depth_mm": ParamProvenance(origin="stated", detail="source"),
            }, confidence=0.8,
        )],
        score=0.8,
        label="original",
    )
    graph = spec_feature_tree_as_graph(
        {"part": "plate", "depth_mm": 10.0}, original, graph_id="emg:edit",
    )
    corrected = original.model_copy(deep=True)
    corrected.features[0].params["depth_mm"] = 12.0
    patch = feature_tree_revision_patch(
        graph,
        {"part": "plate", "depth_mm": 12.0},
        corrected,
        producer="human",
        pass_id="editor:1",
        idempotency_key="edit:1",
    )
    revised = apply_graph_patch(graph, patch)

    assert revised.revision == 1
    assert any(item.state == "superseded" for item in revised.assertions)
    assert compile_build_plan(revised, "preview").operation_node_ids == [
        "operation:r1:0000"
    ]
    projected = feature_tree_from_graph(revised, target_id="preview")
    assert projected.features[0].params["depth_mm"] == 12.0
    assert projected.features[0].param_provenance["depth_mm"].origin == "stated"

    corrected.features[0].params["depth_mm"] = 14.0
    second = feature_tree_revision_patch(
        revised,
        {"part": "plate", "depth_mm": 14.0},
        corrected,
        producer="human",
        pass_id="editor:2",
        idempotency_key="edit:2",
    )
    revised_again = apply_graph_patch(revised, second)
    assert compile_build_plan(revised_again, "preview").operation_node_ids == [
        "operation:r2:0000"
    ]
    assert feature_tree_from_graph(
        revised_again, target_id="preview"
    ).features[0].params["depth_mm"] == 14.0


def test_feature_tree_operation_sequence_is_numeric_beyond_nine_features():
    candidate = FeatureTreeCandidate(
        features=[Feature3D(
            kind="hole",
            params={"diameter_mm": float(index + 1), "through": True},
            confidence=0.9,
        ) for index in range(12)],
        score=0.9,
        label="twelve holes",
    )
    graph = spec_feature_tree_as_graph(
        {"part": "pattern"}, candidate, graph_id="emg:sequence",
    )

    projected = feature_tree_from_graph(graph, target_id="preview")

    assert [item.params["diameter_mm"] for item in projected.features] == [
        float(index + 1) for index in range(12)
    ]
