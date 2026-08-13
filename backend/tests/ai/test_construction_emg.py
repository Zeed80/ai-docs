import pytest
from pydantic import ValidationError

from app.ai.construction_emg import (
    ConstructionModel,
    build_construction_sheets_svg,
    construction_as_graph,
    construction_sheets_patch,
)
from app.domain.engineering_model_graph import (
    Assertion,
    ExactValue,
    GraphPatch,
    apply_graph_patch,
    compile_build_plan,
    domain_adapter_for,
)
from app.services.engineering_model_graph import evaluate_build_admission, verify_graph


def _model(*, material: str | None = "Concrete C30/37") -> ConstructionModel:
    return ConstructionModel.model_validate({
        "site_name": "Test site",
        "building_name": "Test building",
        "storeys": [{"id": "level-1", "name": "Level 1", "elevation_mm": 0}],
        "elements": [
            {
                "id": "wall-1",
                "kind": "wall",
                "name": "External wall",
                "storey_id": "level-1",
                "material": material,
                "load_bearing": True,
                "box": {
                    "x_mm": 0,
                    "y_mm": 0,
                    "z_mm": 0,
                    "width_mm": 5000,
                    "depth_mm": 200,
                    "height_mm": 3000,
                },
            },
            {
                "id": "opening-1",
                "kind": "opening",
                "name": "Door opening",
                "storey_id": "level-1",
                "host_id": "wall-1",
                "box": {
                    "x_mm": 1000,
                    "y_mm": 0,
                    "z_mm": 0,
                    "width_mm": 900,
                    "depth_mm": 200,
                    "height_mm": 2100,
                },
            },
            {
                "id": "space-1",
                "kind": "space",
                "name": "Room 101",
                "storey_id": "level-1",
                "box": {
                    "x_mm": 200,
                    "y_mm": 200,
                    "z_mm": 0,
                    "width_mm": 4600,
                    "depth_mm": 4000,
                    "height_mm": 3000,
                },
            },
        ],
    })


def test_construction_graph_projects_spatial_hierarchy_and_opening_host():
    graph = construction_as_graph(
        graph_id="construction:test",
        model=_model(),
        source_revision_id="revision-1",
        source_approved=True,
    )

    assert graph.profile == "construction"
    assert sum(edge.type == "located_in" for edge in graph.edges) == 3
    assert sum(edge.type == "opens_in" for edge in graph.edges) == 1
    assert sum(node.type == "BuildOperation" for node in graph.nodes) == 3
    assert compile_build_plan(graph, "preview").production_export_allowed is True
    production = compile_build_plan(graph, "production")
    assert production.production_export_allowed is False
    assert "assertion:construction:ifc-reopen" in production.critical_assumption_ids
    assert "BuildOperation" in domain_adapter_for("construction").supported_node_types

    reopen = next(
        assertion
        for assertion in graph.assertions
        if assertion.predicate == "construction.ifc_reopen_valid"
    )
    built = apply_graph_patch(
        graph,
        GraphPatch(
            patch_id="construction-build:test",
            base_revision=graph.revision,
            base_sha256=graph.canonical_sha256,
            producer="system",
            pass_id="construction-build:r1",
            idempotency_key="construction-build:test",
            add_assertions=[Assertion(
                id="assertion:construction-ifc-reopen:test",
                subject_id="product:building",
                predicate="construction.ifc_reopen_valid",
                value=ExactValue(kind="exact", value=True),
                origin="derived",
                assurance="constraint_validated",
                confidence=1.0,
                supersedes_assertion_id=reopen.id,
            )],
            supersede_assertion_ids=[reopen.id],
        ),
    )
    built_plan = compile_build_plan(built, "production")
    assert built_plan.production_export_allowed is False
    assert "assertion:construction:required-sheets" in built_plan.critical_assumption_ids


def test_deterministic_construction_sheets_cover_plan_section_and_elements():
    model = _model()
    first_svg, first_report = build_construction_sheets_svg(model)
    second_svg, second_report = build_construction_sheets_svg(model)

    assert first_svg == second_svg
    assert first_report == second_report
    assert first_report["valid"] is True
    assert first_report["views"] == ["plan:level-1", "section"]
    assert first_report["element_occurrences"] == 6

    graph = construction_as_graph(
        graph_id="construction:test",
        model=model,
        source_revision_id="revision-1",
        source_approved=True,
    )
    patched = apply_graph_patch(
        graph,
        construction_sheets_patch(graph, svg=first_svg, report=first_report),
    )
    state, issues = verify_graph(patched)
    assert "required_2d_artifacts_missing" not in state.issue_codes
    assert not [item for item in issues if item["severity"] == "error"]
    assert "assertion:construction:required-sheets" not in compile_build_plan(
        patched, "production"
    ).critical_assumption_ids


def test_tampered_construction_sheets_cannot_be_admitted():
    svg, report = build_construction_sheets_svg(_model())
    graph = construction_as_graph(
        graph_id="construction:test",
        model=_model(),
        source_revision_id="revision-1",
        source_approved=True,
    )

    with pytest.raises(ValueError, match="does not validate"):
        construction_sheets_patch(graph, svg=svg + b" ", report=report)


def test_construction_release_keeps_unapproved_source_values_critical():
    graph = construction_as_graph(
        graph_id="construction:test",
        model=_model(),
        source_revision_id="revision-1",
        source_approved=False,
    )

    production = compile_build_plan(graph, "production")
    assert production.production_export_allowed is False
    assert any(
        assertion.id in production.critical_assumption_ids
        and assertion.origin == "human"
        for assertion in graph.assertions
    )


def test_construction_model_rejects_opening_outside_host():
    payload = _model().model_dump(mode="json")
    opening = next(item for item in payload["elements"] if item["kind"] == "opening")
    opening["box"]["x_mm"] = 4800

    with pytest.raises(ValidationError, match="lies outside host"):
        ConstructionModel.model_validate(payload)


def test_domain_verifier_rejects_construction_element_without_storey():
    graph = construction_as_graph(
        graph_id="construction:test",
        model=_model(),
        source_revision_id="revision-1",
        source_approved=True,
    )
    payload = graph.model_dump(mode="json")
    payload["canonical_sha256"] = ""
    # Select by the opening assertion rather than relying on a fixture hash.
    opening_id = next(
        item["subject_id"] for item in payload["assertions"]
        if item["predicate"] == "element.kind" and item["value"]["value"] == "opening"
    )
    payload["edges"] = [
        edge for edge in payload["edges"]
        if not (edge["type"] == "located_in" and edge["source_id"] == opening_id)
    ]
    broken = type(graph).model_validate(payload).sealed()

    state, _ = verify_graph(broken)

    assert "construction_element_without_storey" in state.issue_codes


def test_construction_material_gap_blocks_production():
    graph = construction_as_graph(
        graph_id="construction:test",
        model=_model(material=None),
        source_revision_id="revision-1",
        source_approved=True,
    )

    production = compile_build_plan(graph, "production")
    missing = next(
        assertion
        for assertion in graph.assertions
        if assertion.predicate == "element.material"
    )
    assert missing.value.kind == "unknown"
    assert missing.id in production.critical_assumption_ids

    pending = {
        item.id for item in graph.assertions
        if item.predicate in {
            "construction.ifc_reopen_valid",
            "construction.required_sheets_complete",
        }
    }
    admission = evaluate_build_admission(
        graph,
        "production",
        "construction_ifc",
        pending_output_assertion_ids=pending,
    )
    assert admission.allowed is False
    blocker = next(
        item for item in admission.blockers
        if item.assertion_id == missing.id
    )
    assert blocker.code == "critical_parameter_unknown"
    question = next(
        item for item in admission.questions
        if item.assertion_id == missing.id
    )
    assert question.predicate == "element.material"
    assert question.reason == "construction material is missing"


def test_construction_generator_rejects_wrong_domain():
    graph = construction_as_graph(
        graph_id="construction:test",
        model=_model(),
        source_revision_id="revision-1",
        source_approved=True,
    )

    report = evaluate_build_admission(graph, "production", "assembly_step")

    assert report.allowed is False
    assert "generator_profile_incompatible" in {
        item.code for item in report.blockers
    }
    assert "generator_target_incompatible" in {
        item.code for item in report.blockers
    }
