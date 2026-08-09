"""Deterministic MEP/P&ID/electrical/hydraulic connectivity projection."""

from __future__ import annotations

import hashlib
import html
import json
from collections import Counter
from typing import Literal
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


def build_system_diagram_svg(model: EngineeringSystemModel) -> tuple[bytes, dict]:
    """Render and independently reopen a deterministic semantic system diagram."""
    width = max(420, 120 + len(model.equipment) * 220)
    height = max(300, 180 + max((
        sum(port.equipment_id == item.id for port in model.ports)
        for item in model.equipment
    ), default=1) * 34)
    equipment_positions: dict[str, tuple[float, float]] = {}
    port_positions: dict[str, tuple[float, float]] = {}
    equipment_ports = {
        item.id: [port for port in model.ports if port.equipment_id == item.id]
        for item in model.equipment
    }
    groups = []
    for index, item in enumerate(model.equipment):
        x = 70.0 + index * 220.0
        y = 90.0
        equipment_positions[item.id] = (x, y)
        ports = equipment_ports[item.id]
        port_items = []
        for port_index, port in enumerate(ports):
            port_y = y + 35.0 + port_index * 30.0
            port_x = x if port.direction == "in" else x + 140.0
            if port.direction == "bidirectional":
                port_x = x + 70.0
            port_positions[port.id] = (port_x, port_y)
            port_items.append(
                f'<circle data-port-id="{html.escape(port.id)}" '
                f'cx="{port_x:.1f}" cy="{port_y:.1f}" r="5" />'
            )
        groups.append(
            f'<g data-equipment-id="{html.escape(item.id)}">'
            f'<rect x="{x:.1f}" y="{y:.1f}" width="140" height="120" rx="8" />'
            f'<text x="{x + 70:.1f}" y="{y + 24:.1f}" text-anchor="middle">'
            f'{html.escape(item.name)}</text>{"".join(port_items)}</g>'
        )
    connections = []
    for connection in model.connections:
        first = port_positions[connection.first_port_id]
        second = port_positions[connection.second_port_id]
        mid_x = (first[0] + second[0]) / 2
        points = (
            f"{first[0]:.1f},{first[1]:.1f} {mid_x:.1f},{first[1]:.1f} "
            f"{mid_x:.1f},{second[1]:.1f} {second[0]:.1f},{second[1]:.1f}"
        )
        connections.append(
            f'<polyline data-connection-id="{html.escape(connection.id)}" '
            f'data-first-port-id="{html.escape(connection.first_port_id)}" '
            f'data-second-port-id="{html.escape(connection.second_port_id)}" '
            f'points="{points}" />'
        )
    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        '<style>rect{fill:#fff;stroke:#111;stroke-width:2}'
        'circle{fill:#fff;stroke:#111;stroke-width:2}'
        'polyline{fill:none;stroke:#075985;stroke-width:3}'
        'text{font:14px sans-serif;fill:#111}</style>'
        f'<text x="20" y="30">{html.escape(model.name)} — {html.escape(model.system_kind)}</text>'
        f'<g id="connections">{"".join(connections)}</g>'
        f'<g id="equipment">{"".join(groups)}</g>'
        '</svg>'
    ).encode()
    reopened = ElementTree.fromstring(svg)
    namespace = {"svg": "http://www.w3.org/2000/svg"}
    rendered_equipment = {
        item.attrib["data-equipment-id"]
        for item in reopened.findall(".//svg:g[@data-equipment-id]", namespace)
    }
    rendered_ports = {
        item.attrib["data-port-id"]
        for item in reopened.findall(".//svg:circle[@data-port-id]", namespace)
    }
    rendered_connections = {
        item.attrib["data-connection-id"]
        for item in reopened.findall(".//svg:polyline[@data-connection-id]", namespace)
    }
    report = {
        "media_type": "image/svg+xml",
        "equipment_ids": sorted(rendered_equipment),
        "port_ids": sorted(rendered_ports),
        "connection_ids": sorted(rendered_connections),
        "required_views_complete": True,
        "unresolved_port_ids": model.unresolved_port_ids(),
    }
    report["valid"] = bool(
        rendered_equipment == {item.id for item in model.equipment}
        and rendered_ports == {item.id for item in model.ports}
        and rendered_connections == {item.id for item in model.connections}
        and not report["unresolved_port_ids"]
    )
    report["artifact_sha256"] = hashlib.sha256(svg).hexdigest()
    report["canonical_report_sha256"] = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return svg, report


def system_diagram_patch(
    graph: EngineeringModelGraph,
    *,
    svg: bytes,
    report: dict,
) -> GraphPatch:
    """Admit a reopened complete SVG diagram without granting human assurance."""
    artifact_sha = hashlib.sha256(svg).hexdigest()
    if (
        not report.get("valid")
        or report.get("media_type") != "image/svg+xml"
        or report.get("artifact_sha256") != artifact_sha
        or not report.get("required_views_complete")
    ):
        raise ValueError("system diagram report does not validate the supplied SVG")
    required = next(
        item for item in graph.assertions
        if item.state == "active"
        and item.predicate == "system.required_diagram_complete"
    )
    suffix = artifact_sha[:16]
    artifact_id = f"artifact:system-diagram:{suffix}"
    operation_id = f"operation:system-diagram:{suffix}"
    evidence_id = f"evidence:system-diagram:{suffix}"
    evidence = Evidence(
        id=evidence_id,
        kind="projection_comparison",
        payload=report,
        sha256=report["canonical_report_sha256"],
    )
    return GraphPatch(
        patch_id=f"system-diagram:{suffix}",
        base_revision=graph.revision,
        base_sha256=graph.canonical_sha256,
        producer="system",
        pass_id=f"system-diagram:r{graph.revision + 1}",
        idempotency_key=f"system-diagram:{artifact_sha}",
        add_nodes=[
            GraphNode(id=operation_id, type="BuildOperation", name="Build system diagram"),
            GraphNode(id=artifact_id, type="Artifact", name="System diagram SVG"),
        ],
        add_edges=[GraphEdge(
            id=f"generated-by:system-diagram:{suffix}",
            type="generated_by",
            source_id=artifact_id,
            target_id=operation_id,
        )],
        add_evidence=[evidence],
        add_assertions=[
            Assertion(
                id=f"assertion:system-diagram-media:{suffix}",
                subject_id=artifact_id,
                predicate="artifact.media_type",
                value=ExactValue(kind="exact", value="image/svg+xml"),
                origin="derived",
                assurance="constraint_validated",
                evidence_ids=[evidence_id],
                confidence=1.0,
            ),
            Assertion(
                id=f"assertion:system-diagram-complete:{suffix}",
                subject_id="system:root",
                predicate="system.required_diagram_complete",
                value=ExactValue(kind="exact", value=True),
                origin="derived",
                assurance="constraint_validated",
                evidence_ids=[evidence_id],
                confidence=1.0,
                impacts=["required_view"],
                supersedes_assertion_id=required.id,
            ),
        ],
        supersede_assertion_ids=[required.id],
    )
