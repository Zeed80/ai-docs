import pytest

from app.ai.mixed_emg import MixedModel, compose_mixed_graph
from app.domain.engineering_model_graph import (
    Assertion,
    BuildTarget,
    EngineeringModelGraph,
    ExactValue,
    GraphNode,
    Requirement,
    UnknownValue,
    compile_build_plan,
)


def _member(graph_id: str, profile: str, *, ready: bool = True):
    assertion = Assertion(
        id="assertion:ready",
        subject_id="product:root",
        predicate="member.ready",
        value=(
            ExactValue(kind="exact", value=True)
            if ready else UnknownValue(kind="unknown", reason="member unresolved")
        ),
        origin="human",
        assurance="human_approved" if ready else "proposed",
        confidence=1.0 if ready else 0.0,
        impacts=["connectivity"],
    )
    requirement = Requirement(
        id="requirement:release",
        kind="domain",
        target_node_ids=["product:root"],
        assertion_ids=[assertion.id],
    )
    return EngineeringModelGraph(
        graph_id=graph_id,
        profile=profile,
        nodes=[GraphNode(id="product:root", type="Product", name=graph_id)],
        assertions=[assertion],
        requirements=[requirement],
        build_targets=[
            BuildTarget(id="preview", kind="pdf", root_node_ids=["product:root"], critical_impacts=[]),
            BuildTarget(
                id="production",
                kind="pdf",
                root_node_ids=["product:root"],
                requirement_ids=[requirement.id],
            ),
        ],
    ).sealed()


def _model(left, right, *, approved_order=True):
    members = [
        {
            "alias": "mechanical",
            "graph_id": left.graph_id,
            "revision": left.revision,
            "canonical_sha256": left.canonical_sha256,
        },
        {
            "alias": "systems",
            "graph_id": right.graph_id,
            "revision": right.revision,
            "canonical_sha256": right.canonical_sha256,
        },
    ]
    if not approved_order:
        members.reverse()
    return MixedModel.model_validate({
        "name": "Machine with hydraulics",
        "members": members,
        "links": [{
            "id": "pump-mounted-on-machine",
            "type": "depends_on",
            "source_member": "systems",
            "source_node_id": "product:root",
            "target_member": "mechanical",
            "target_node_id": "product:root",
            "impact": "connectivity",
        }],
    })


def test_mixed_graph_namespaces_members_and_gates_cross_profile_links():
    mechanical = _member("mechanical:1", "mechanical")
    systems = _member("system:1", "hydraulic")
    model = _model(mechanical, systems)

    graph = compose_mixed_graph(
        graph_id="mixed:test",
        model=model,
        member_graphs={"mechanical": mechanical, "systems": systems},
        source_revision_id="revision-1",
        source_approved=True,
    )

    assert graph.profile == "mixed"
    assert len([node for node in graph.nodes if node.id.endswith("product:root")]) == 2
    assert len(graph.sources) == 2
    assert any(edge.type == "depends_on" for edge in graph.edges)
    assert compile_build_plan(graph, "production").production_export_allowed is True

    unapproved = compose_mixed_graph(
        graph_id="mixed:test",
        model=model,
        member_graphs={"mechanical": mechanical, "systems": systems},
        source_revision_id="revision-1",
        source_approved=False,
    )
    assert compile_build_plan(unapproved, "production").production_export_allowed is False


def test_mixed_graph_is_order_independent_and_propagates_member_blockers():
    mechanical = _member("mechanical:1", "mechanical")
    systems = _member("system:1", "hydraulic")
    first = compose_mixed_graph(
        graph_id="mixed:test",
        model=_model(mechanical, systems),
        member_graphs={"mechanical": mechanical, "systems": systems},
        source_revision_id="revision-1",
        source_approved=True,
    )
    second = compose_mixed_graph(
        graph_id="mixed:test",
        model=_model(mechanical, systems, approved_order=False),
        member_graphs={"systems": systems, "mechanical": mechanical},
        source_revision_id="revision-1",
        source_approved=True,
    )
    assert first.canonical_sha256 == second.canonical_sha256

    blocked_system = _member("system:blocked", "hydraulic", ready=False)
    blocked = compose_mixed_graph(
        graph_id="mixed:blocked",
        model=_model(mechanical, blocked_system),
        member_graphs={"mechanical": mechanical, "systems": blocked_system},
        source_revision_id="revision-1",
        source_approved=True,
    )
    assert compile_build_plan(blocked, "production").production_export_allowed is False


def test_mixed_graph_rejects_sha_and_cross_node_mismatches():
    mechanical = _member("mechanical:1", "mechanical")
    systems = _member("system:1", "hydraulic")
    model = _model(mechanical, systems)
    tampered = model.model_copy(deep=True)
    tampered.members[0].canonical_sha256 = "0" * 64
    with pytest.raises(ValueError, match="canonical SHA mismatch"):
        compose_mixed_graph(
            graph_id="mixed:test",
            model=tampered,
            member_graphs={"mechanical": mechanical, "systems": systems},
            source_revision_id="revision-1",
            source_approved=True,
        )

    broken_link = model.model_copy(deep=True)
    broken_link.links[0].target_node_id = "missing"
    with pytest.raises(ValueError, match="unknown node"):
        compose_mixed_graph(
            graph_id="mixed:test",
            model=broken_link,
            member_graphs={"mechanical": mechanical, "systems": systems},
            source_revision_id="revision-1",
            source_approved=True,
        )
