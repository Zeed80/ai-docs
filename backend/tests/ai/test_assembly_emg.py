from app.ai.assembly_emg import (
    assembly_as_graph,
    assembly_drawing_patch,
    assembly_revision_patch,
    build_assembly_drawing_svg,
)
from app.domain.assembly import analyze_assembly_dof
from app.domain.engineering_model_graph import (
    Assertion,
    ExactValue,
    GraphNode,
    GraphPatch,
    apply_graph_patch,
    compile_build_plan,
)
from app.services.engineering_model_graph import evaluate_build_admission, verify_graph


def _assembly_graph(*, shaft_quantity: int = 1):
    components = [
        {
            "instance_key": "housing",
            "designation": "Housing",
            "quantity": 1,
            "metadata_": {"grounded": True},
            "transform": {},
        },
        {
            "instance_key": "shaft",
            "designation": "Shaft",
            "quantity": shaft_quantity,
            "metadata_": {},
            "transform": {"x": 12.0},
        },
    ]
    mates = [{
        "id": "mate-1",
        "mate_type": "fixed",
        "first_instance_key": "housing",
        "second_instance_key": "shaft",
        "parameters": {},
    }]
    return assembly_as_graph(
        graph_id="assembly:test",
        name="Test assembly",
        designation="ASM-001",
        components=components,
        mates=mates,
        dof=analyze_assembly_dof(components, mates),
        collisions=[],
        exact_checked=["housing", "shaft"],
        interference_degraded=None,
    )


def test_assembly_dof_requires_a_ground_path_for_every_instance():
    components = [
        {
            "instance_key": "housing",
            "quantity": 1,
            "metadata_": {"grounded": True},
        },
        {"instance_key": "shaft", "quantity": 1},
    ]
    report = analyze_assembly_dof(
        components,
        [{
            "id": "mate-1",
            "mate_type": "fixed",
            "first_instance_key": "housing",
            "second_instance_key": "shaft",
            "parameters": {},
        }],
    )

    assert report.degrees_of_freedom == 0
    assert report.floating_instances == []
    assert report.constraint_rank_estimate == 6
    assert report.fully_constrained is True


def test_assembly_dof_reports_floating_grouped_and_unknown_mates():
    components = [
        {
            "instance_key": "base",
            "quantity": 1,
            "metadata_": {"grounded": True},
        },
        {"instance_key": "bolt-group", "quantity": 4},
    ]
    report = analyze_assembly_dof(
        components,
        [{
            "id": "mate-unknown",
            "mate_type": "custom_magic",
            "first_instance_key": "base",
            "second_instance_key": "bolt-group",
        }],
    )

    assert report.degrees_of_freedom == 6
    assert report.floating_instances == ["bolt-group"]
    assert report.grouped_quantity_instances == ["bolt-group"]
    assert report.unsupported_mates == ["mate-unknown"]
    assert report.fully_constrained is False


def test_assembly_dof_detects_overconstraint():
    components = [
        {
            "instance_key": "base",
            "quantity": 1,
            "metadata_": {"grounded": True},
        },
        {"instance_key": "part", "quantity": 1},
    ]
    mates = [
        {
            "id": f"fixed-{index}",
            "mate_type": "fixed",
            "first_instance_key": "base",
            "second_instance_key": "part",
        }
        for index in range(2)
    ]

    report = analyze_assembly_dof(components, mates)

    assert report.degrees_of_freedom == 0
    assert report.overconstrained is True
    assert report.fully_constrained is False


def test_assembly_graph_projects_instances_bom_mates_and_release_gate():
    graph = _assembly_graph()

    assert graph.profile == "assembly"
    assert sum(node.type == "BuildOperation" for node in graph.nodes) == 2
    assert sum(edge.type == "instance_of" for edge in graph.edges) == 2
    assert sum(edge.type == "mates_with" for edge in graph.edges) == 1
    assert any(
        assertion.predicate == "component.quantity"
        and assertion.value.kind == "exact"
        and assertion.value.value == 1
        for assertion in graph.assertions
    )
    assert compile_build_plan(graph, "preview").production_export_allowed is True
    production = compile_build_plan(graph, "production")
    assert production.production_export_allowed is False
    assert "assertion:assembly:artifact-reopen" in production.critical_assumption_ids
    assert "assertion:assembly:required-2d" in production.critical_assumption_ids


def test_assembly_generator_admission_allows_only_declared_pending_outputs():
    graph = _assembly_graph()
    pending = {
        item.id for item in graph.assertions
        if item.predicate in {
            "assembly.artifact_reopen_valid",
            "assembly.required_2d_complete",
        }
    }

    report = evaluate_build_admission(
        graph,
        "production",
        "assembly_step",
        pending_output_assertion_ids=pending,
    )

    assert report.allowed is True
    assert report.review_required is True
    assert report.pending_output_assertion_ids == sorted(pending)
    bypass = evaluate_build_admission(
        graph,
        "production",
        "assembly_step",
        pending_output_assertion_ids={next(iter(graph.assertions)).id},
    )
    assert bypass.allowed is False
    assert "invalid_pending_output_assertion" in {
        item.code for item in bypass.blockers
    }


def test_deterministic_assembly_drawing_covers_instances_mates_and_views():
    components = [
        {
            "key": "housing",
            "shape": {"kind": "box", "width_mm": 100, "height_mm": 80, "depth_mm": 40},
            "transform": {},
        },
        {
            "key": "shaft",
            "shape": {"kind": "cylinder", "diameter_mm": 30, "height_mm": 120},
            "transform": {"translate": [120, 25, 0]},
        },
    ]
    mates = [{
        "id": "mate-1",
        "first_instance_key": "housing",
        "second_instance_key": "shaft",
    }]
    first_svg, first_report = build_assembly_drawing_svg(
        components=components, mates=mates, name="Test assembly"
    )
    second_svg, second_report = build_assembly_drawing_svg(
        components=components, mates=mates, name="Test assembly"
    )

    assert first_svg == second_svg
    assert first_report == second_report
    assert first_report["valid"] is True
    assert first_report["views"] == ["assembled", "exploded"]
    assert first_report["instance_occurrences"] == 4
    assert first_report["mate_occurrences"] == 2

    graph = _assembly_graph()
    patched = apply_graph_patch(
        graph,
        assembly_drawing_patch(graph, svg=first_svg, report=first_report),
    )
    state, issues = verify_graph(patched)
    assert "required_2d_artifacts_missing" not in state.issue_codes
    assert not [item for item in issues if item["severity"] == "error"]
    assert "assertion:assembly:required-2d" not in compile_build_plan(
        patched, "production"
    ).critical_assumption_ids


def test_tampered_assembly_drawing_cannot_be_admitted():
    svg, report = build_assembly_drawing_svg(
        components=[{
            "key": "base",
            "shape": {"kind": "box", "width_mm": 100, "height_mm": 80, "depth_mm": 20},
            "transform": {},
        }],
        mates=[],
        name="Base",
    )

    with pytest.raises(ValueError, match="does not validate"):
        assembly_drawing_patch(_assembly_graph(), svg=svg + b" ", report=report)


def test_assembly_revision_patch_is_deterministic_and_supersedes_bom_value():
    current = _assembly_graph()
    desired = _assembly_graph(shaft_quantity=4)

    first = assembly_revision_patch(current, desired)
    second = assembly_revision_patch(current, desired)

    assert first is not None
    assert second is not None
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    merged = apply_graph_patch(current, first)
    assert merged.revision == 1
    assert any(
        assertion.state == "active"
        and assertion.predicate == "component.quantity"
        and assertion.value.kind == "exact"
        and assertion.value.value == 4
        for assertion in merged.assertions
    )
    assert assembly_revision_patch(merged, desired) is None


def test_assembly_revision_patch_upgrades_legacy_release_contract():
    desired = _assembly_graph()
    payload = desired.model_dump(mode="json")
    payload["canonical_sha256"] = ""
    payload["assertions"] = [
        item for item in payload["assertions"]
        if item["id"] != "assertion:assembly:required-2d"
    ]
    payload["requirements"] = [
        item for item in payload["requirements"]
        if item["id"] != "requirement:assembly-2d"
    ]
    payload["requirements"][0]["assertion_ids"] = [
        item for item in payload["requirements"][0]["assertion_ids"]
        if item != "assertion:assembly:required-2d"
    ]
    payload["build_targets"][1]["requirement_ids"] = [
        "requirement:assembly-release"
    ]
    legacy = type(desired).model_validate(payload).sealed()

    patch = assembly_revision_patch(legacy, desired)

    assert patch is not None
    upgraded = apply_graph_patch(legacy, patch)
    assert any(
        item.predicate == "assembly.required_2d_complete"
        for item in upgraded.assertions if item.state == "active"
    )
    assert "requirement:assembly-2d" in upgraded.build_targets[1].requirement_ids


def test_assembly_sync_preserves_reopen_only_for_unchanged_snapshot():
    current = _assembly_graph()
    reopen = next(
        assertion
        for assertion in current.assertions
        if assertion.predicate == "assembly.artifact_reopen_valid"
    )
    built = apply_graph_patch(
        current,
        GraphPatch(
            patch_id="assembly-build:test",
            base_revision=current.revision,
            base_sha256=current.canonical_sha256,
            producer="system",
            pass_id="assembly-build:r1",
            idempotency_key="assembly-build:test",
            add_nodes=[GraphNode(id="artifact:test", type="Artifact")],
            add_assertions=[
                Assertion(
                    id="assertion:assembly-reopen:test",
                    subject_id="product:assembly",
                    predicate="assembly.artifact_reopen_valid",
                    value=ExactValue(kind="exact", value=True),
                    origin="derived",
                    assurance="constraint_validated",
                    confidence=1.0,
                    supersedes_assertion_id=reopen.id,
                ),
                Assertion(
                    id="assertion:artifact-sha:test",
                    subject_id="artifact:test",
                    predicate="artifact.sha256",
                    value=ExactValue(kind="exact", value="abc"),
                    origin="derived",
                    assurance="constraint_validated",
                    confidence=1.0,
                ),
            ],
            supersede_assertion_ids=[reopen.id],
        ),
    )

    assert assembly_revision_patch(built, _assembly_graph()) is None

    changed = assembly_revision_patch(built, _assembly_graph(shaft_quantity=4))
    assert changed is not None
    assert "assertion:artifact-sha:test" in changed.retract_assertion_ids
    merged = apply_graph_patch(built, changed)
    active_reopen = next(
        assertion
        for assertion in merged.assertions
        if assertion.state == "active"
        and assertion.predicate == "assembly.artifact_reopen_valid"
    )
    assert active_reopen.value.kind == "unknown"
import pytest
