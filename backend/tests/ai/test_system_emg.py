import pytest
from pydantic import ValidationError

from app.ai.system_emg import EngineeringSystemModel, system_as_graph
from app.domain.engineering_model_graph import compile_build_plan


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
    assert compile_build_plan(approved, "production").production_export_allowed is True

    unapproved = system_as_graph(
        graph_id="system:test",
        model=_model(),
        source_revision_id="revision-1",
        source_approved=False,
    )
    assert compile_build_plan(unapproved, "production").production_export_allowed is False


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
