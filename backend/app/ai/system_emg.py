"""Deterministic MEP/P&ID/electrical/hydraulic connectivity projection."""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.engineering_model_graph import (
    Assertion,
    BuildTarget,
    EngineeringModelGraph,
    Evidence,
    ExactValue,
    GraphEdge,
    GraphNode,
    Requirement,
    UnknownValue,
)


SystemProfile = Literal["mep", "electrical", "hydraulic", "pid"]
PortDirection = Literal["in", "out", "bidirectional"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SystemEquipment(_StrictModel):
    id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=300)
    equipment_type: str = Field(min_length=1, max_length=160)


class SystemPort(_StrictModel):
    id: str = Field(min_length=1, max_length=160)
    equipment_id: str = Field(min_length=1, max_length=160)
    kind: str = Field(min_length=1, max_length=160)
    direction: PortDirection
    medium: str = Field(min_length=1, max_length=160)
    nominal_size_mm: float | None = Field(default=None, gt=0)
    required_connection: bool = True
    max_connections: int = Field(default=1, ge=1, le=100)


class SystemConnection(_StrictModel):
    id: str = Field(min_length=1, max_length=160)
    first_port_id: str = Field(min_length=1, max_length=160)
    second_port_id: str = Field(min_length=1, max_length=160)


class EngineeringSystemModel(_StrictModel):
    profile: SystemProfile
    name: str = Field(min_length=1, max_length=300)
    system_kind: str = Field(min_length=1, max_length=160)
    equipment: list[SystemEquipment] = Field(min_length=1, max_length=10_000)
    ports: list[SystemPort] = Field(min_length=1, max_length=50_000)
    connections: list[SystemConnection] = Field(default_factory=list, max_length=100_000)

    @model_validator(mode="after")
    def validate_references_and_compatibility(self) -> "EngineeringSystemModel":
        equipment_ids = [item.id for item in self.equipment]
        port_ids = [item.id for item in self.ports]
        connection_ids = [item.id for item in self.connections]
        for label, values in (
            ("equipment", equipment_ids),
            ("port", port_ids),
            ("connection", connection_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate system {label} ids")
        equipment_set = set(equipment_ids)
        if missing := sorted({item.equipment_id for item in self.ports} - equipment_set):
            raise ValueError("unknown port equipment: " + ", ".join(missing))
        ports = {item.id: item for item in self.ports}
        pairs = set()
        degree: Counter[str] = Counter()
        for connection in self.connections:
            first = ports.get(connection.first_port_id)
            second = ports.get(connection.second_port_id)
            if first is None or second is None:
                raise ValueError(f"connection {connection.id} references an unknown port")
            if first.id == second.id:
                raise ValueError(f"connection {connection.id} loops a port to itself")
            pair = tuple(sorted((first.id, second.id)))
            if pair in pairs:
                raise ValueError(f"duplicate connection between {pair[0]} and {pair[1]}")
            pairs.add(pair)
            if first.medium.casefold() != second.medium.casefold():
                raise ValueError(f"connection {connection.id} joins incompatible media")
            if first.direction == second.direction and first.direction != "bidirectional":
                raise ValueError(f"connection {connection.id} joins incompatible directions")
            degree[first.id] += 1
            degree[second.id] += 1
        overloaded = [item.id for item in self.ports if degree[item.id] > item.max_connections]
        if overloaded:
            raise ValueError("port connection cardinality exceeded: " + ", ".join(overloaded))
        return self

    def unresolved_port_ids(self) -> list[str]:
        degree = Counter(
            port_id
            for connection in self.connections
            for port_id in (connection.first_port_id, connection.second_port_id)
        )
        return sorted(
            item.id
            for item in self.ports
            if item.required_connection and degree[item.id] == 0
        )


def _stable(prefix: str, value: str) -> str:
    return f"{prefix}:{hashlib.sha256(value.encode()).hexdigest()[:16]}"


def system_as_graph(
    *,
    graph_id: str,
    model: EngineeringSystemModel,
    source_revision_id: str,
    source_approved: bool,
) -> EngineeringModelGraph:
    assurance = "human_approved" if source_approved else "observed"
    evidence = Evidence(
        id=_stable("evidence:system-source", source_revision_id),
        kind="human_decision",
        payload={
            "engineering_revision_id": source_revision_id,
            "approved": source_approved,
        },
        sha256=hashlib.sha256(source_revision_id.encode()).hexdigest(),
    )
    system_id = "system:root"
    nodes = [GraphNode(id=system_id, type="System", name=model.name)]
    edges: list[GraphEdge] = []
    assertions: list[Assertion] = []
    required: list[str] = []
    equipment_nodes = {}
    port_nodes = {}

    system_kind_id = "assertion:system:kind"
    assertions.append(Assertion(
        id=system_kind_id,
        subject_id=system_id,
        predicate="system.kind",
        value=ExactValue(kind="exact", value=model.system_kind),
        origin="human",
        assurance=assurance,
        evidence_ids=[evidence.id],
        confidence=1.0,
        impacts=["connectivity", "regulatory_check"],
    ))
    required.append(system_kind_id)

    for item in model.equipment:
        node_id = _stable("component:system", item.id)
        equipment_nodes[item.id] = node_id
        nodes.append(GraphNode(id=node_id, type="Component", name=item.name))
        edges.append(GraphEdge(
            id=_stable("contains:system", item.id),
            type="contains",
            source_id=system_id,
            target_id=node_id,
        ))
        assertion_id = _stable("assertion:equipment-type", item.id)
        assertions.append(Assertion(
            id=assertion_id,
            subject_id=node_id,
            predicate="equipment.type",
            value=ExactValue(kind="exact", value=item.equipment_type),
            origin="human",
            assurance=assurance,
            evidence_ids=[evidence.id],
            confidence=1.0,
            impacts=["connectivity", "envelope"],
        ))
        required.append(assertion_id)

    for item in model.ports:
        node_id = _stable("port:system", item.id)
        port_nodes[item.id] = node_id
        nodes.append(GraphNode(id=node_id, type="Port", name=item.id))
        edges.append(GraphEdge(
            id=_stable("part-of:port", item.id),
            type="part_of",
            source_id=node_id,
            target_id=equipment_nodes[item.equipment_id],
        ))
        values = [
            ("port.kind", item.kind, None),
            ("port.direction", item.direction, None),
            ("port.medium", item.medium, None),
        ]
        if item.nominal_size_mm is not None:
            values.append(("port.nominal_size", item.nominal_size_mm, "mm"))
        for predicate, value, unit in values:
            assertion_id = _stable(f"assertion:{predicate}", item.id)
            assertions.append(Assertion(
                id=assertion_id,
                subject_id=node_id,
                predicate=predicate,
                value=ExactValue(kind="exact", value=value),
                unit=unit,
                origin="human",
                assurance=assurance,
                evidence_ids=[evidence.id],
                confidence=1.0,
                impacts=["connectivity", "connection_opening", "operational_safety"],
            ))
            required.append(assertion_id)

    for connection in model.connections:
        edges.append(GraphEdge(
            id=_stable("connects:system", connection.id),
            type="connects_to",
            source_id=port_nodes[connection.first_port_id],
            target_id=port_nodes[connection.second_port_id],
        ))

    unresolved = model.unresolved_port_ids()
    connectivity_id = "assertion:system:connectivity-closed"
    required_2d_id = "assertion:system:required-diagram"
    assertions.append(Assertion(
        id=connectivity_id,
        subject_id=system_id,
        predicate="system.connectivity_closed",
        value=(
            ExactValue(kind="exact", value=True)
            if not unresolved
            else UnknownValue(
                kind="unknown",
                reason="unconnected required ports: " + ", ".join(unresolved),
            )
        ),
        origin="derived",
        assurance="constraint_validated" if not unresolved else "proposed",
        confidence=1.0 if not unresolved else 0.0,
        impacts=["connectivity", "operational_safety"],
    ))
    required.append(connectivity_id)
    assertions.append(Assertion(
        id=required_2d_id,
        subject_id=system_id,
        predicate="system.required_diagram_complete",
        value=UnknownValue(
            kind="unknown",
            reason="P&ID, schematic or system diagram has not been verified",
        ),
        origin="derived",
        assurance="proposed",
        impacts=["required_view"],
    ))
    required.append(required_2d_id)
    requirement = Requirement(
        id="requirement:system-release",
        kind="connectivity",
        target_node_ids=[system_id],
        assertion_ids=required,
    )
    diagram_requirement = Requirement(
        id="requirement:system-2d",
        kind="view",
        target_node_ids=[system_id],
        assertion_ids=[required_2d_id],
    )
    return EngineeringModelGraph(
        graph_id=graph_id,
        profile=model.profile,
        nodes=nodes,
        edges=edges,
        assertions=assertions,
        evidence=[evidence],
        requirements=[requirement, diagram_requirement],
        build_targets=[
            BuildTarget(id="preview", kind="dxf", root_node_ids=[system_id], critical_impacts=[]),
            BuildTarget(
                id="production",
                kind="pdf",
                root_node_ids=[system_id],
                requirement_ids=[requirement.id, diagram_requirement.id],
            ),
        ],
    ).sealed()
