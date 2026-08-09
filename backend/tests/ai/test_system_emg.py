import pytest
from pydantic import ValidationError

from app.ai.system_emg import EngineeringSystemModel, system_as_graph
from app.domain.engineering_model_graph import (
    Assertion,
    Evidence,
    ExactValue,
    GraphNode,
    GraphPatch,
    apply_graph_patch,
    compile_build_plan,
)
from app.services.engineering_model_graph import verify_graph


def _model(*, connected: bool = True) -> EngineeringSystemModel:
    return EngineeringSystemModel.model_validate({
        "profile": "hydraulic",
        "name": "Hydraulic power unit",
        "system_kind": "hydraulic_power",
        "equipment": [
            {"id": "pump", "name": "Pump", "equipment_type": "pump"},
            {"id": "tank", "name": "Tank", "equipment_type": "reservoir"},
        ],
        "ports": [
            {
                "id": "pump-out",
                "equipment_id": "pump",
                "kind": "pressure",
                "direction": "out",
                "medium": "hydraulic_oil",
                "nominal_size_mm": 20,
            },
            {
                "id": "tank-in",
                "equipment_id": "tank",
                "kind": "return",
                "direction": "in",
                "medium": "hydraulic_oil",
                "nominal_size_mm": 20,
            },
        ],
        "connections": (
            [{
                "id": "line-1",
                "first_port_id": "pump-out",
                "second_port_id": "tank-in",
            }]
            if connected else []
        ),
    })


def test_system_graph_release_depends_on_actual_connectivity_and_approval():
    approved = system_as_graph(
        graph_id="system:test",
        model=_model(),
        source_revision_id="revision-1",
        source_approved=True,
    )

    assert approved.profile == "hydraulic"
    assert sum(node.type == "Port" for node in approved.nodes) == 2
    assert sum(edge.type == "connects_to" for edge in approved.edges) == 1
    approved_plan = compile_build_plan(approved, "production")
    assert approved_plan.production_export_allowed is False
    assert "assertion:system:required-diagram" in approved_plan.critical_assumption_ids

    unapproved = system_as_graph(
        graph_id="system:test",
        model=_model(),
        source_revision_id="revision-1",
        source_approved=False,
    )
    assert compile_build_plan(unapproved, "production").production_export_allowed is False


def test_verified_system_pdf_is_required_before_production_release():
    graph = system_as_graph(
        graph_id="system:test",
        model=_model(),
        source_revision_id="revision-1",
        source_approved=True,
    )
    required = next(
        item for item in graph.assertions
        if item.predicate == "system.required_diagram_complete"
    )
    evidence = Evidence(
        id="evidence:system-pdf",
        kind="projection_comparison",
        payload={"artifact_sha256": "a" * 64, "required_views_complete": True},
        sha256="a" * 64,
    )
    released = apply_graph_patch(graph, GraphPatch(
        patch_id="system-pdf:test",
        base_revision=graph.revision,
        base_sha256=graph.canonical_sha256,
        producer="system",
        pass_id="system-pdf:r1",
        idempotency_key="system-pdf:test",
        add_nodes=[GraphNode(id="artifact:system-pdf", type="Artifact")],
        add_evidence=[evidence],
        add_assertions=[
            Assertion(
                id="assertion:system-pdf-media",
                subject_id="artifact:system-pdf",
                predicate="artifact.media_type",
                value=ExactValue(kind="exact", value="application/pdf"),
                origin="derived",
                assurance="constraint_validated",
                evidence_ids=[evidence.id],
                confidence=1.0,
            ),
            Assertion(
                id="assertion:system-pdf-complete",
                subject_id="system:root",
                predicate="system.required_diagram_complete",
                value=ExactValue(kind="exact", value=True),
                origin="derived",
                assurance="constraint_validated",
                evidence_ids=[evidence.id],
                confidence=1.0,
                impacts=["required_view"],
                supersedes_assertion_id=required.id,
            ),
        ],
        supersede_assertion_ids=[required.id],
    ))

    assert compile_build_plan(released, "production").production_export_allowed is True
    state, issues = verify_graph(released)
    assert "required_2d_artifacts_missing" not in state.issue_codes
    assert not [item for item in issues if item["severity"] == "error"]


def test_unconnected_required_ports_are_critical_unknowns():
    graph = system_as_graph(
        graph_id="system:test",
        model=_model(connected=False),
        source_revision_id="revision-1",
        source_approved=True,
    )

    assertion = next(
        item for item in graph.assertions
        if item.predicate == "system.connectivity_closed"
    )
    plan = compile_build_plan(graph, "production")
    assert assertion.value.kind == "unknown"
    assert assertion.id in plan.critical_assumption_ids
    assert plan.production_export_allowed is False


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("medium", "water", "incompatible media"),
        ("direction", "out", "incompatible directions"),
    ],
)
def test_connection_rejects_incompatible_ports(field, value, message):
    payload = _model().model_dump(mode="json")
    payload["ports"][1][field] = value

    with pytest.raises(ValidationError, match=message):
        EngineeringSystemModel.model_validate(payload)


def test_connection_cardinality_is_enforced():
    payload = _model().model_dump(mode="json")
    payload["equipment"].append({
        "id": "filter",
        "name": "Filter",
        "equipment_type": "filter",
    })
    payload["ports"].append({
        "id": "filter-in",
        "equipment_id": "filter",
        "kind": "inlet",
        "direction": "in",
        "medium": "hydraulic_oil",
        "required_connection": True,
        "max_connections": 1,
    })
    payload["connections"].append({
        "id": "line-2",
        "first_port_id": "pump-out",
        "second_port_id": "filter-in",
    })

    with pytest.raises(ValidationError, match="cardinality exceeded"):
        EngineeringSystemModel.model_validate(payload)
