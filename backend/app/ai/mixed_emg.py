"""Composition of immutable profile graphs into one mixed EMG revision."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.emg_predicates import PREDICATE
from app.domain.engineering_model_graph import (
    Assertion,
    BuildTarget,
    EngineeringModelGraph,
    Evidence,
    ExactValue,
    GraphEdge,
    GraphNode,
    GraphSource,
    Impact,
    Requirement,
)

CrossProfileEdge = Literal["depends_on", "connects_to", "located_in", "part_of", "maps_to_topology"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MixedGraphMember(_StrictModel):
    alias: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    graph_id: str = Field(min_length=1, max_length=255)
    revision: int = Field(ge=0)
    canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CrossProfileLink(_StrictModel):
    id: str = Field(min_length=1, max_length=160)
    type: CrossProfileEdge
    source_member: str
    source_node_id: str
    target_member: str
    target_node_id: str
    impact: Impact
    description: str | None = Field(default=None, max_length=500)


class MixedModel(_StrictModel):
    name: str = Field(min_length=1, max_length=300)
    members: list[MixedGraphMember] = Field(min_length=2, max_length=20)
    links: list[CrossProfileLink] = Field(default_factory=list, max_length=10_000)

    @model_validator(mode="after")
    def validate_aliases_and_links(self) -> MixedModel:
        aliases = [item.alias for item in self.members]
        if len(aliases) != len(set(aliases)):
            raise ValueError("duplicate mixed graph member aliases")
        identities = [(item.graph_id, item.revision) for item in self.members]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate mixed graph member revisions")
        link_ids = [item.id for item in self.links]
        if len(link_ids) != len(set(link_ids)):
            raise ValueError("duplicate cross-profile link ids")
        alias_set = set(aliases)
        for link in self.links:
            if link.source_member not in alias_set or link.target_member not in alias_set:
                raise ValueError(f"cross-profile link {link.id} references an unknown member")
            if link.source_member == link.target_member:
                raise ValueError(f"cross-profile link {link.id} must connect distinct members")
        return self


def _qualified(alias: str, identifier: str) -> str:
    return f"member:{alias}:{identifier}"


def compose_mixed_graph(
    *,
    graph_id: str,
    model: MixedModel,
    member_graphs: dict[str, EngineeringModelGraph],
    source_revision_id: str,
    source_approved: bool,
) -> EngineeringModelGraph:
    """Namespace and compose exact member revisions without flattening provenance."""
    expected = {item.alias for item in model.members}
    if set(member_graphs) != expected:
        raise ValueError("member graph aliases do not match mixed model")
    root_id = "document-set:mixed"
    nodes = [GraphNode(id=root_id, type="DocumentSet", name=model.name)]
    sources: list[GraphSource] = []
    edges: list[GraphEdge] = []
    assertions: list[Assertion] = []
    evidence: list[Evidence] = []
    hypothesis_options = []
    hypothesis_sets = []
    requirements: list[Requirement] = []
    member_required_assertions: list[str] = []
    extension_registry = []

    for member in sorted(model.members, key=lambda item: item.alias):
        graph = member_graphs[member.alias]
        if graph.graph_id != member.graph_id:
            raise ValueError(f"member {member.alias} graph_id mismatch")
        if graph.revision != member.revision:
            raise ValueError(f"member {member.alias} revision mismatch")
        if graph.canonical_sha256 != member.canonical_sha256:
            raise ValueError(f"member {member.alias} canonical SHA mismatch")
        if graph.profile == "mixed":
            raise ValueError("nested mixed graphs are not supported in emg/1.0")
        graph_source_id = f"graph-revision:{member.alias}"
        sources.append(
            GraphSource(
                id=graph_source_id,
                uri=f"emg://{member.graph_id}/r{member.revision}",
                sha256=member.canonical_sha256,
                revision=str(member.revision),
                media_type="application/vnd.engineering-model-graph+json",
            )
        )
        for item in graph.sources:
            sources.append(item.model_copy(update={"id": _qualified(member.alias, item.id)}))
        for item in graph.nodes:
            nodes.append(item.model_copy(update={"id": _qualified(member.alias, item.id)}))
        for item in graph.edges:
            edges.append(
                item.model_copy(
                    update={
                        "id": _qualified(member.alias, item.id),
                        "source_id": _qualified(member.alias, item.source_id),
                        "target_id": _qualified(member.alias, item.target_id),
                    }
                )
            )
        for item in graph.evidence:
            evidence.append(
                item.model_copy(
                    update={
                        "id": _qualified(member.alias, item.id),
                        "source_id": (
                            _qualified(member.alias, item.source_id)
                            if item.source_id
                            else graph_source_id
                        ),
                        "source_region_id": (
                            _qualified(member.alias, item.source_region_id)
                            if item.source_region_id
                            else None
                        ),
                    }
                )
            )
        for item in graph.assertions:
            value = item.value
            if value.kind == "expression":
                value = value.model_copy(
                    update={
                        "variable_assertion_ids": [
                            _qualified(member.alias, assertion_id)
                            for assertion_id in value.variable_assertion_ids
                        ]
                    }
                )
            assertions.append(
                item.model_copy(
                    update={
                        "id": _qualified(member.alias, item.id),
                        "subject_id": _qualified(member.alias, item.subject_id),
                        "evidence_ids": [
                            _qualified(member.alias, evidence_id)
                            for evidence_id in item.evidence_ids
                        ],
                        "supersedes_assertion_id": (
                            _qualified(member.alias, item.supersedes_assertion_id)
                            if item.supersedes_assertion_id
                            else None
                        ),
                        "value": value,
                    }
                )
            )
        for item in graph.hypothesis_options:
            hypothesis_options.append(
                item.model_copy(
                    update={
                        "id": _qualified(member.alias, item.id),
                        "assertion_ids": [
                            _qualified(member.alias, assertion_id)
                            for assertion_id in item.assertion_ids
                        ],
                    }
                )
            )
        for item in graph.hypothesis_sets:
            hypothesis_sets.append(
                item.model_copy(
                    update={
                        "id": _qualified(member.alias, item.id),
                        "option_ids": [
                            _qualified(member.alias, option_id) for option_id in item.option_ids
                        ],
                        "selected_option_id": (
                            _qualified(member.alias, item.selected_option_id)
                            if item.selected_option_id
                            else None
                        ),
                    }
                )
            )
        requirement_map = {}
        for item in graph.requirements:
            mapped = item.model_copy(
                update={
                    "id": _qualified(member.alias, item.id),
                    "target_node_ids": [
                        _qualified(member.alias, node_id) for node_id in item.target_node_ids
                    ],
                    "assertion_ids": [
                        _qualified(member.alias, assertion_id)
                        for assertion_id in item.assertion_ids
                    ],
                }
            )
            requirements.append(mapped)
            requirement_map[item.id] = mapped
        production_targets = [item for item in graph.build_targets if item.id == "production"]
        if len(production_targets) != 1:
            raise ValueError(f"member {member.alias} must expose exactly one production target")
        for requirement_id in production_targets[0].requirement_ids:
            mapped = requirement_map.get(requirement_id)
            if mapped is None:
                raise ValueError(f"member {member.alias} has a broken production requirement")
            member_required_assertions.extend(mapped.assertion_ids)
        member_roots = sorted(
            {node_id for target in graph.build_targets for node_id in target.root_node_ids}
        )
        for node_id in member_roots:
            edges.append(
                GraphEdge(
                    id=_qualified(member.alias, f"contained-root:{node_id}"),
                    type="contains",
                    source_id=root_id,
                    target_id=_qualified(member.alias, node_id),
                )
            )
        for registration in graph.extension_registry:
            if registration not in extension_registry:
                extension_registry.append(registration)

    approval = "human_approved" if source_approved else "observed"
    link_evidence = Evidence(
        id="evidence:mixed-links",
        kind="human_decision",
        payload={
            "engineering_revision_id": source_revision_id,
            "approved": source_approved,
        },
        sha256=hashlib.sha256(source_revision_id.encode()).hexdigest(),
    )
    evidence.append(link_evidence)
    cross_assertions = []
    node_ids = {item.id for item in nodes}
    for link in sorted(model.links, key=lambda item: item.id):
        source_id = _qualified(link.source_member, link.source_node_id)
        target_id = _qualified(link.target_member, link.target_node_id)
        if source_id not in node_ids or target_id not in node_ids:
            raise ValueError(f"cross-profile link {link.id} references an unknown node")
        constraint_id = f"constraint:mixed:{hashlib.sha256(link.id.encode()).hexdigest()[:16]}"
        nodes.append(
            GraphNode(id=constraint_id, type="Constraint", name=link.description or link.id)
        )
        edges.append(
            GraphEdge(
                id=f"cross:{hashlib.sha256(link.id.encode()).hexdigest()[:20]}",
                type=link.type,
                source_id=source_id,
                target_id=target_id,
            )
        )
        assertion_id = f"assertion:mixed-link:{hashlib.sha256(link.id.encode()).hexdigest()[:16]}"
        assertions.append(
            Assertion(
                id=assertion_id,
                subject_id=constraint_id,
                predicate=PREDICATE.CROSS_PROFILE_LINK,
                value=ExactValue(
                    kind="exact",
                    value={
                        "id": link.id,
                        "type": link.type,
                        "source": source_id,
                        "target": target_id,
                    },
                ),
                origin="human",
                assurance=approval,
                evidence_ids=[link_evidence.id],
                confidence=1.0,
                impacts=[link.impact],
            )
        )
        cross_assertions.append(assertion_id)

    release_requirement = Requirement(
        id="requirement:mixed-release",
        kind="domain",
        target_node_ids=[root_id],
        assertion_ids=sorted(set(member_required_assertions + cross_assertions)),
    )
    requirements.append(release_requirement)
    return EngineeringModelGraph(
        graph_id=graph_id,
        profile="mixed",
        sources=sources,
        nodes=nodes,
        edges=edges,
        assertions=assertions,
        evidence=evidence,
        hypothesis_options=hypothesis_options,
        hypothesis_sets=hypothesis_sets,
        requirements=requirements,
        build_targets=[
            BuildTarget(id="preview", kind="pdf", root_node_ids=[root_id], critical_impacts=[]),
            BuildTarget(
                id="production",
                kind="pdf",
                root_node_ids=[root_id],
                requirement_ids=[release_requirement.id],
            ),
        ],
        extension_registry=extension_registry,
    ).sealed()
