"""Deterministic assembly/BOM/mate projection into EngineeringModelGraph."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.domain.assembly import AssemblyDofReport
from app.domain.engineering_model_graph import (
    Assertion,
    BuildTarget,
    EngineeringModelGraph,
    Evidence,
    ExactValue,
    GraphEdge,
    GraphNode,
    GraphPatch,
    Requirement,
    UnknownValue,
)


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _stable(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode()).hexdigest()[:16]
    return f"{prefix}:{digest}"


def assembly_as_graph(
    *,
    graph_id: str,
    name: str,
    designation: str | None,
    components: list[Any],
    mates: list[Any],
    dof: AssemblyDofReport,
    collisions: list[tuple[str, str]],
    exact_checked: list[str],
    interference_degraded: str | None,
) -> EngineeringModelGraph:
    active = [item for item in components if not bool(_value(item, "suppressed", False))]
    nodes = [
        GraphNode(id="document-set:root", type="DocumentSet", name="Assembly document set"),
        GraphNode(id="product:assembly", type="Product", name=name),
    ]
    edges = [GraphEdge(
        id="contains:root-assembly",
        type="contains",
        source_id="document-set:root",
        target_id="product:assembly",
    )]
    assertions: list[Assertion] = []
    required: list[str] = []
    for predicate, value, impacts in (
        ("product.name", name, ["interchangeability"]),
        ("product.designation", designation, ["interchangeability"]),
    ):
        if value is None:
            continue
        assertion_id = _stable("assertion", f"product:assembly:{predicate}")
        assertions.append(Assertion(
            id=assertion_id,
            subject_id="product:assembly",
            predicate=predicate,
            value=ExactValue(kind="exact", value=value),
            origin="human",
            assurance="human_approved",
            confidence=1.0,
            impacts=impacts,
        ))
        required.append(assertion_id)
    definitions: dict[str, str] = {}
    instance_nodes: dict[str, str] = {}
    for index, component in enumerate(active):
        key = str(_value(component, "instance_key"))
        component_designation = str(_value(component, "designation"))
        definition_id = definitions.setdefault(
            component_designation,
            _stable("component-definition", component_designation),
        )
        if not any(item.id == definition_id for item in nodes):
            nodes.append(GraphNode(
                id=definition_id,
                type="Component",
                name=component_designation,
            ))
        instance_id = _stable("component-instance", key)
        instance_nodes[key] = instance_id
        operation_id = _stable("operation-instance", key)
        nodes.extend([
            GraphNode(id=instance_id, type="Component", name=key),
            GraphNode(id=operation_id, type="BuildOperation", name=f"Instance {key}"),
        ])
        edges.extend([
            GraphEdge(
                id=_stable("contains-instance", key),
                type="contains",
                source_id="product:assembly",
                target_id=instance_id,
            ),
            GraphEdge(
                id=_stable("instance-of", key),
                type="instance_of",
                source_id=instance_id,
                target_id=definition_id,
            ),
            GraphEdge(
                id=_stable("operation-depends", key),
                type="depends_on",
                source_id=operation_id,
                target_id=instance_id,
            ),
        ])
        values = [
            ("component.instance_key", key, None, ["component_count"]),
            (
                "component.designation",
                component_designation,
                None,
                ["interchangeability"],
            ),
            (
                "component.quantity",
                int(_value(component, "quantity", 1)),
                "1",
                ["component_count", "mass"],
            ),
            (
                "component.transform",
                _value(component, "transform", {}) or {},
                None,
                ["assembly_interface", "envelope"],
            ),
            (
                "component.grounded",
                key in dof.grounded_instances,
                None,
                ["mate"],
            ),
        ]
        for predicate, value, unit, impacts in values:
            assertion_id = _stable("assertion", f"{instance_id}:{predicate}")
            assertions.append(Assertion(
                id=assertion_id,
                subject_id=instance_id,
                predicate=predicate,
                value=ExactValue(kind="exact", value=value),
                unit=unit,
                origin="human",
                assurance="human_approved",
                confidence=1.0,
                impacts=impacts,
            ))
            required.append(assertion_id)
        for predicate, value in (
            ("operation.kind", "assembly_instance"),
            ("operation.sequence", index),
            ("operation.param.instance_key", key),
            ("operation.param.transform", _value(component, "transform", {}) or {}),
        ):
            assertion_id = _stable("assertion", f"{operation_id}:{predicate}")
            assertions.append(Assertion(
                id=assertion_id,
                subject_id=operation_id,
                predicate=predicate,
                value=ExactValue(kind="exact", value=value),
                origin="derived",
                assurance="constraint_validated",
                confidence=1.0,
                impacts=["component_count", "assembly_interface"],
            ))
            required.append(assertion_id)

    for index, mate in enumerate(mates):
        first = str(_value(mate, "first_instance_key"))
        second = str(_value(mate, "second_instance_key"))
        if first not in instance_nodes or second not in instance_nodes or first == second:
            continue
        mate_key = str(_value(mate, "id", f"{index}:{first}:{second}"))
        constraint_id = _stable("constraint-mate", mate_key)
        nodes.append(GraphNode(id=constraint_id, type="Constraint", name=mate_key))
        edges.extend([
            GraphEdge(
                id=_stable("mate-edge", mate_key),
                type="mates_with",
                source_id=instance_nodes[first],
                target_id=instance_nodes[second],
            ),
            GraphEdge(
                id=_stable("mate-applies-first", mate_key),
                type="applies_to",
                source_id=constraint_id,
                target_id=instance_nodes[first],
            ),
            GraphEdge(
                id=_stable("mate-applies-second", mate_key),
                type="applies_to",
                source_id=constraint_id,
                target_id=instance_nodes[second],
            ),
        ])
        for predicate, value in (
            ("mate.type", str(_value(mate, "mate_type"))),
            ("mate.parameters", _value(mate, "parameters", {}) or {}),
        ):
            assertion_id = _stable("assertion", f"{constraint_id}:{predicate}")
            assertions.append(Assertion(
                id=assertion_id,
                subject_id=constraint_id,
                predicate=predicate,
                value=ExactValue(kind="exact", value=value),
                origin="human",
                assurance="human_approved",
                confidence=1.0,
                impacts=["mate", "assembly_interface"],
            ))
            required.append(assertion_id)

    dof_payload = dof.model_dump(mode="json")
    dof_hash = hashlib.sha256(
        json.dumps(dof_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    evidence = [Evidence(
        id=f"evidence:assembly-dof:{dof_hash[:16]}",
        kind="calculation",
        payload={"solver": "declared-mate-rank-v1", "report": dof_payload},
        sha256=dof_hash,
    )]
    dof_assertion_id = "assertion:assembly:dof"
    assertions.append(Assertion(
        id=dof_assertion_id,
        subject_id="product:assembly",
        predicate="assembly.degrees_of_freedom",
        value=ExactValue(kind="exact", value=dof.degrees_of_freedom),
        unit="1",
        origin="derived",
        assurance="constraint_validated" if dof.fully_constrained else "proposed",
        evidence_ids=[evidence[0].id],
        confidence=1.0,
        impacts=["mate", "operational_safety"],
    ))
    required.append(dof_assertion_id)

    interference_payload = {
        "solver": "exact-brep-with-aabb-fallback-v1",
        "collisions": collisions,
        "exact_checked": sorted(exact_checked),
        "degraded": interference_degraded,
    }
    interference_hash = hashlib.sha256(
        json.dumps(
            interference_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    interference_evidence = Evidence(
        id=f"evidence:assembly-interference:{interference_hash[:16]}",
        kind="calculation",
        payload=interference_payload,
        sha256=interference_hash,
    )
    evidence.append(interference_evidence)
    exact_complete = (
        not interference_degraded
        and not collisions
        and set(exact_checked) == set(dof.active_instances)
    )
    interference_id = "assertion:assembly:interference"
    assertions.append(Assertion(
        id=interference_id,
        subject_id="product:assembly",
        predicate="assembly.interference_clear",
        value=(
            ExactValue(kind="exact", value=True)
            if exact_complete
            else UnknownValue(
                kind="unknown",
                reason=interference_degraded
                or "exact interference is incomplete or collisions exist",
            )
        ),
        origin="derived",
        assurance="constraint_validated" if exact_complete else "proposed",
        evidence_ids=[interference_evidence.id],
        confidence=1.0 if exact_complete else 0.0,
        impacts=["assembly_interface", "operational_safety"],
    ))
    reopen_id = "assertion:assembly:artifact-reopen"
    assertions.append(Assertion(
        id=reopen_id,
        subject_id="product:assembly",
        predicate="assembly.artifact_reopen_valid",
        value=UnknownValue(kind="unknown", reason="assembly builder has not reopened an artifact"),
        origin="derived",
        assurance="proposed",
        impacts=["base_topology", "operational_safety"],
    ))
    required.extend([interference_id, reopen_id])
    requirement = Requirement(
        id="requirement:assembly-release",
        kind="interface",
        target_node_ids=["product:assembly"],
        assertion_ids=required,
    )
    return EngineeringModelGraph(
        graph_id=graph_id,
        profile="assembly",
        nodes=nodes,
        edges=edges,
        assertions=assertions,
        evidence=evidence,
        requirements=[requirement],
        build_targets=[
            BuildTarget(
                id="preview",
                kind="preview_brep",
                root_node_ids=["product:assembly"],
                critical_impacts=[],
            ),
            BuildTarget(
                id="production",
                kind="production_step",
                root_node_ids=["product:assembly"],
                requirement_ids=[requirement.id],
            ),
        ],
    ).sealed()


def assembly_revision_patch(
    current: EngineeringModelGraph,
    desired: EngineeringModelGraph,
) -> GraphPatch | None:
    """Create an append/supersede/retract patch for a changed assembly snapshot."""
    current_nodes = {item.id for item in current.nodes}
    desired_nodes = {item.id for item in desired.nodes}
    current_edges = {item.id for item in current.edges}
    desired_edges = {item.id for item in desired.edges}
    active = {
        (item.subject_id, item.predicate): item
        for item in current.assertions
        if item.state == "active"
    }
    desired_by_key = {
        (item.subject_id, item.predicate): item for item in desired.assertions
    }
    existing_evidence = {item.id for item in current.evidence}
    add_evidence = [
        item for item in desired.evidence if item.id not in existing_evidence
    ]
    reopen_key = ("product:assembly", "assembly.artifact_reopen_valid")
    extra_subject_ids = current_nodes - desired_nodes

    def _same_value(existing: Assertion | None, wanted: Assertion) -> bool:
        return bool(
            existing
            and existing.model_dump(
                exclude={"id", "supersedes_assertion_id", "state"}
            ) == wanted.model_dump(
                exclude={"id", "supersedes_assertion_id", "state"}
            )
        )

    # A successful artifact patch is derived from the current editable
    # snapshot.  Re-syncing that unchanged snapshot must preserve the reopen
    # proof and remain idempotent.  Any BOM/mate/transform/interference change
    # invalidates it and restores the unknown release gate below.
    snapshot_changed = bool(
        desired_nodes - current_nodes
        or desired_edges - current_edges
        or add_evidence
        or any(
            key != reopen_key and not _same_value(active.get(key), wanted)
            for key, wanted in desired_by_key.items()
        )
        or any(
            key != reopen_key
            and item.subject_id not in extra_subject_ids
            and key not in desired_by_key
            for key, item in active.items()
        )
    )
    add_assertions = []
    supersede = []
    for key, wanted in desired_by_key.items():
        existing = active.get(key)
        if (
            key == reopen_key
            and not snapshot_changed
            and existing is not None
            and existing.value.kind == "exact"
            and existing.value.value is True
            and existing.assurance == "constraint_validated"
        ):
            continue
        if _same_value(existing, wanted):
            continue
        suffix = hashlib.sha256(f"{key}:{wanted.model_dump_json()}".encode()).hexdigest()[:16]
        replacement = wanted.model_copy(update={
            "id": f"assertion:r{current.revision + 1}:{suffix}",
            "supersedes_assertion_id": existing.id if existing else None,
        })
        add_assertions.append(replacement)
        if existing:
            supersede.append(existing.id)
    retract = [
        item.id
        for key, item in active.items()
        if key not in desired_by_key
        and (snapshot_changed or item.subject_id not in extra_subject_ids)
    ]
    payload = {
        "nodes": [item.model_dump(mode="json") for item in desired.nodes],
        "edges": [item.model_dump(mode="json") for item in desired.edges],
        "assertions": [item.model_dump(mode="json") for item in desired.assertions],
        "evidence": [item.model_dump(mode="json") for item in desired.evidence],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if not any((
        set(item.id for item in desired.nodes) - current_nodes,
        set(item.id for item in desired.edges) - current_edges,
        add_assertions,
        retract,
        add_evidence,
    )):
        return None
    return GraphPatch(
        patch_id=f"assembly-sync:{digest[:20]}",
        base_revision=current.revision,
        base_sha256=current.canonical_sha256,
        # The assembly snapshot is composed from records entered through the
        # authenticated engineering editor.  Mark the patch as human so the
        # merger may explicitly supersede those approved source assertions;
        # the deterministic DOF/interference assertions remain derived.
        producer="human",
        pass_id=f"assembly-sync:r{current.revision + 1}",
        idempotency_key=f"assembly-sync:{digest}",
        add_nodes=[item for item in desired.nodes if item.id not in current_nodes],
        add_edges=[item for item in desired.edges if item.id not in current_edges],
        add_assertions=add_assertions,
        add_evidence=add_evidence,
        supersede_assertion_ids=supersede,
        retract_assertion_ids=retract,
    )
