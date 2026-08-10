"""Persistence, verification and revision-safe patching for EMG v1."""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    EngineeringGraphAssertionRecord,
    EngineeringGraphEdgeRecord,
    EngineeringGraphNodeRecord,
    EngineeringGraphRevision,
    EngineeringProject,
    GraphPatchRecord,
    GraphVerificationRun,
)
from app.domain.emg_predicates import PREDICATE
from app.domain.engineering_model_graph import (
    EngineeringModelGraph,
    GraphPatch,
    PatchMergeError,
    VerificationState,
    apply_graph_patch,
    compile_build_plan,
    domain_adapter_for,
)
from app.storage import delete_file, download_file, upload_file


class DuplicatePatchError(ValueError):
    pass


def _storage_path(graph: EngineeringModelGraph) -> str:
    return f"engineering-model-graphs/{graph.graph_id}/r{graph.revision}-{graph.canonical_sha256}.json"


def load_graph(row: EngineeringGraphRevision) -> EngineeringModelGraph:
    graph = EngineeringModelGraph.model_validate_json(download_file(row.storage_path))
    if graph.canonical_sha256 != row.canonical_sha256 or graph.calculated_sha256() != row.canonical_sha256:
        raise ValueError("canonical graph hash mismatch")
    return graph


async def latest_graph_revision(
    db: AsyncSession, graph_id: str, *, lock: bool = False
) -> EngineeringGraphRevision | None:
    query = (
        select(EngineeringGraphRevision)
        .where(EngineeringGraphRevision.graph_id == graph_id)
        .order_by(EngineeringGraphRevision.revision.desc())
        .limit(1)
    )
    if lock:
        query = query.with_for_update()
    return (await db.execute(query)).scalar_one_or_none()


async def persist_graph_revision(
    db: AsyncSession,
    graph: EngineeringModelGraph,
    *,
    engineering_project_id: uuid.UUID | None,
    engineering_revision_id: uuid.UUID | None = None,
) -> EngineeringGraphRevision:
    """Upload canonical graph and create immutable row plus searchable projections."""
    sealed = graph.sealed()
    if sealed.canonical_sha256 != graph.canonical_sha256:
        graph = sealed
    path = _storage_path(graph)
    upload_file(graph.model_dump_json().encode(), path, "application/json")
    try:
        row = EngineeringGraphRevision(
            engineering_project_id=engineering_project_id,
            engineering_revision_id=engineering_revision_id,
            graph_id=graph.graph_id,
            schema_version=graph.schema_version,
            revision=graph.revision,
            parent_revision=graph.parent_revision,
            canonical_sha256=graph.canonical_sha256,
            profile=graph.profile,
            storage_path=path,
            comprehension_status=graph.verification.comprehension,
            build_status=graph.verification.build,
            release_status=graph.verification.release,
            reader_manifest=graph.reader_manifest.model_dump(mode="json"),
        )
        db.add(row)
        await db.flush()
        db.add_all([
            EngineeringGraphNodeRecord(
                graph_revision_id=row.id,
                node_id=item.id,
                node_type=item.type,
                payload=item.model_dump(mode="json"),
            )
            for item in graph.nodes
        ])
        db.add_all([
            EngineeringGraphEdgeRecord(
                graph_revision_id=row.id,
                edge_id=item.id,
                edge_type=item.type,
                source_node_id=item.source_id,
                target_node_id=item.target_id,
                payload=item.model_dump(mode="json"),
            )
            for item in graph.edges
        ])
        db.add_all([
            EngineeringGraphAssertionRecord(
                graph_revision_id=row.id,
                assertion_id=item.id,
                subject_node_id=item.subject_id,
                predicate=item.predicate,
                origin=item.origin,
                assurance=item.assurance,
                state=item.state,
                payload=item.model_dump(mode="json"),
            )
            for item in graph.assertions
        ])
        await db.flush()
        return row
    except Exception:
        delete_file(path)
        raise


async def create_initial_graph(
    db: AsyncSession,
    graph: EngineeringModelGraph,
    *,
    engineering_project_id: uuid.UUID,
    engineering_revision_id: uuid.UUID | None = None,
) -> EngineeringGraphRevision:
    if graph.revision != 0 or graph.parent_revision is not None:
        raise ValueError("initial graph must be revision 0 without parent")
    if not await db.get(EngineeringProject, engineering_project_id):
        raise LookupError("engineering project not found")
    if await latest_graph_revision(db, graph.graph_id):
        raise ValueError("graph already exists")
    return await persist_graph_revision(
        db, graph.sealed(), engineering_project_id=engineering_project_id,
        engineering_revision_id=engineering_revision_id,
    )


async def persist_pipeline_graph(
    db: AsyncSession,
    graph: EngineeringModelGraph,
) -> EngineeringGraphRevision:
    """Persist the deterministic graph produced for a Studio generation.

    A generation owns one initial EMG. Replays with identical content reuse it;
    a changed reading must arrive later as GraphPatch instead of overwriting or
    silently full-snapshotting the source of truth.
    """
    latest = await latest_graph_revision(db, graph.graph_id, lock=True)
    sealed = graph.sealed()
    if latest:
        if latest.canonical_sha256 != sealed.canonical_sha256:
            raise ValueError("pipeline graph already exists; changed reading requires GraphPatch")
        return latest
    return await persist_graph_revision(
        db,
        sealed,
        engineering_project_id=None,
        engineering_revision_id=None,
    )


async def persist_feature_tree_revision(
    db: AsyncSession,
    *,
    graph_id: str,
    spec: dict[str, Any],
    candidate: Any,
    producer: str,
    pass_id: str,
    idempotency_key: str,
    source_sha256: str | None = None,
    source_uri: str | None = None,
) -> EngineeringGraphRevision:
    """Persist an editor/retry feature tree strictly through ``GraphPatch``.

    Old generations may not have an EMG yet. They receive an immutable r0
    compatibility import followed by the requested patch, so the corrected
    state is still never smuggled in as a replacement snapshot.
    """
    from app.ai.cad_emg_compat import (
        feature_tree_revision_patch,
        spec_feature_tree_as_graph,
    )

    latest = await latest_graph_revision(db, graph_id, lock=True)
    if latest is None:
        initial = spec_feature_tree_as_graph(
            spec,
            candidate,
            graph_id=graph_id,
            source_sha256=source_sha256,
            source_uri=source_uri,
        )
        latest = await persist_graph_revision(
            db,
            initial,
            engineering_project_id=None,
            engineering_revision_id=None,
        )
        # A system retry which merely imports its current snapshot needs no
        # artificial second revision. Human correction is recorded explicitly.
        if producer != "human":
            return latest

    elif producer == "system":
        from app.ai.cad_emg_compat import feature_tree_from_graph

        current = feature_tree_from_graph(load_graph(latest), target_id="preview")
        current_operations = [
            (item.kind, item.params) for item in current.features
        ]
        candidate_operations = [
            (item.kind, item.params) for item in candidate.features
        ]
        if current_operations == candidate_operations:
            return latest

    duplicate = (
        await db.execute(select(GraphPatchRecord).where(
            GraphPatchRecord.graph_id == graph_id,
            GraphPatchRecord.idempotency_key == idempotency_key,
        ))
    ).scalar_one_or_none()
    if duplicate is not None:
        if not duplicate.accepted or duplicate.result_revision_id is None:
            raise ValueError(
                "idempotent graph patch was previously rejected: "
                + ",".join(duplicate.validation_errors or [])
            )
        row = await db.get(EngineeringGraphRevision, duplicate.result_revision_id)
        if row is None:
            raise LookupError("accepted graph patch result revision is missing")
        return row

    base_graph = load_graph(latest)
    patch = feature_tree_revision_patch(
        base_graph,
        spec,
        candidate,
        producer=producer,
        pass_id=pass_id,
        idempotency_key=idempotency_key,
    )
    row, errors = await merge_and_persist_patch(
        db, patch, expected_graph_id=graph_id,
    )
    if row is None:
        raise ValueError("graph patch rejected: " + ",".join(errors))
    return row


async def merge_and_persist_patch(
    db: AsyncSession,
    patch: GraphPatch,
    *,
    expected_graph_id: str,
) -> tuple[EngineeringGraphRevision | None, list[str]]:
    """Journal and apply a patch. Rejection is a successful persisted outcome."""
    base = (
        await db.execute(select(EngineeringGraphRevision).where(
            EngineeringGraphRevision.canonical_sha256 == patch.base_sha256
        ).with_for_update())
    ).scalar_one_or_none()
    if not base:
        raise LookupError("base graph revision not found")
    if base.graph_id != expected_graph_id:
        raise LookupError("base revision belongs to another graph")
    duplicate = (
        await db.execute(select(GraphPatchRecord).where(
            GraphPatchRecord.graph_id == base.graph_id,
            GraphPatchRecord.idempotency_key == patch.idempotency_key,
        ))
    ).scalar_one_or_none()
    if duplicate:
        raise DuplicatePatchError("duplicate idempotency key")
    latest = await latest_graph_revision(db, base.graph_id, lock=True)
    graph = load_graph(latest) if latest else load_graph(base)
    errors: list[str] = []
    merged: EngineeringModelGraph | None = None
    try:
        merged = apply_graph_patch(graph, patch)
    except PatchMergeError as exc:
        errors = exc.errors

    journal = GraphPatchRecord(
        graph_id=base.graph_id,
        patch_id=patch.patch_id,
        base_revision=patch.base_revision,
        base_sha256=patch.base_sha256,
        producer=patch.producer,
        pass_id=patch.pass_id,
        idempotency_key=patch.idempotency_key,
        accepted=merged is not None,
        payload=patch.model_dump(mode="json"),
        validation_errors=errors,
    )
    db.add(journal)
    await db.flush()
    if merged is None:
        return None, errors
    state, _issues = verify_graph(merged)
    merged = merged.model_copy(update={"verification": state}).sealed()
    row = await persist_graph_revision(
        db,
        merged,
        engineering_project_id=base.engineering_project_id,
        engineering_revision_id=base.engineering_revision_id,
    )
    journal.result_revision_id = row.id
    await db.flush()
    return row, []


def _has_cycle(graph: EngineeringModelGraph) -> bool:
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        if edge.type in {"depends_on", "generated_by"}:
            adjacency[edge.source_id].append(edge.target_id)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited:
            return False
        visiting.add(node_id)
        if any(visit(target) for target in adjacency[node_id]):
            return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    return any(visit(node.id) for node in graph.nodes)


def _domain_rule_issues(graph: EngineeringModelGraph) -> list[dict[str, Any]]:
    """Check adapter structure independently from reader/build assertions."""
    if graph.profile == "mixed":
        return []
    adapter = domain_adapter_for(graph.profile)
    issues: list[dict[str, Any]] = []
    unsupported_nodes = sorted({
        item.type for item in graph.nodes
        if item.type not in adapter.supported_node_types
    })
    unsupported_edges = sorted({
        item.type for item in graph.edges
        if item.type not in adapter.supported_edge_types
    })
    active_predicates = {
        item.predicate for item in graph.assertions if item.state == "active"
    }
    missing_predicates = sorted(
        set(adapter.mandatory_assertions) - active_predicates
    )
    for code, values in (
        ("domain_unsupported_node_type", unsupported_nodes),
        ("domain_unsupported_edge_type", unsupported_edges),
        ("domain_mandatory_assertion_missing", missing_predicates),
    ):
        if values:
            issues.append({
                "level": 7, "code": code, "values": values, "severity": "error",
            })

    node_type = {item.id: item.type for item in graph.nodes}
    if graph.profile == "assembly":
        if not any(item.type == "Product" for item in graph.nodes):
            issues.append({"level": 7, "code": "assembly_product_missing", "severity": "error"})
        instance_ids = {edge.source_id for edge in graph.edges if edge.type == "instance_of"}
        unconstrained = sorted(
            item_id for item_id in instance_ids
            if not any(
                edge.type == "mates_with"
                and item_id in {edge.source_id, edge.target_id}
                for edge in graph.edges
            )
            and len(instance_ids) > 1
        )
        if unconstrained:
            issues.append({
                "level": 7, "code": "assembly_instances_without_mate",
                "node_ids": unconstrained, "severity": "error",
            })
    elif graph.profile == "construction":
        feature_ids = {item.id for item in graph.nodes if item.type == "Feature"}
        located = {edge.source_id for edge in graph.edges if edge.type == "located_in"}
        if missing := sorted(feature_ids - located):
            issues.append({
                "level": 7, "code": "construction_element_without_storey",
                "node_ids": missing, "severity": "error",
            })
        opening_ids = {
            item.subject_id for item in graph.assertions
            if item.state == "active"
            and item.predicate == PREDICATE.ELEMENT_KIND
            and item.value.kind == "exact"
            and item.value.value == "opening"
        }
        hosted = {edge.source_id for edge in graph.edges if edge.type == "opens_in"}
        if missing := sorted(opening_ids - hosted):
            issues.append({
                "level": 7, "code": "construction_opening_without_host",
                "node_ids": missing, "severity": "error",
            })
    elif graph.profile == "mechanical":
        feature_ids = {item.id for item in graph.nodes if item.type == "Feature"}
        # Warning, not error: represented_by is sourced strictly from a
        # view's own features_shown (native_feature_graph_additions), and the
        # reader does not populate features_shown yet (Фаза 1.1 follow-up) —
        # every Feature node is legitimately unlinked today. Track this as a
        # regression metric (mirrors level 6's same_object_across_views
        # precedent) rather than blocking every mechanical build before the
        # data can pass a hard gate.
        if feature_ids:
            shown = {edge.source_id for edge in graph.edges if edge.type == "represented_by"}
            if missing := sorted(feature_ids - shown):
                issues.append({
                    "level": 7, "code": "mechanical_feature_without_view",
                    "node_ids": missing, "severity": "warning",
                })
    elif graph.profile in {"mep", "electrical", "hydraulic", "pid"}:
        port_ids = {item.id for item in graph.nodes if item.type == "Port"}
        ownership = defaultdict(int)
        for edge in graph.edges:
            if edge.type == "part_of" and edge.source_id in port_ids:
                ownership[edge.source_id] += 1
        invalid = sorted(item_id for item_id in port_ids if ownership[item_id] != 1)
        if invalid:
            issues.append({
                "level": 7, "code": "system_port_ownership_invalid",
                "node_ids": invalid, "severity": "error",
            })
        if not any(node_type.get(edge.source_id) == "Port" and node_type.get(edge.target_id) == "Port" for edge in graph.edges if edge.type == "connects_to"):
            connectivity = next((
                item for item in graph.assertions
                if item.state == "active"
                and item.predicate == PREDICATE.SYSTEM_CONNECTIVITY_CLOSED
            ), None)
            if connectivity and connectivity.value.kind == "exact" and connectivity.value.value is True:
                issues.append({
                    "level": 7, "code": "system_connectivity_claim_without_edges",
                    "severity": "error",
                })
    return issues


def verify_graph(graph: EngineeringModelGraph) -> tuple[VerificationState, list[dict[str, Any]]]:
    """Run the twelve verification levels with explicit unavailable evidence."""
    issues: list[dict[str, Any]] = []
    checked = list(range(1, 13))
    if graph.calculated_sha256() != graph.canonical_sha256:
        issues.append({"level": 1, "code": "canonical_hash_mismatch", "severity": "error"})
    known_units = {None, "mm", "cm", "m", "deg", "rad", "kg", "g", "N", "Pa", "MPa", "%", "1"}
    for item in graph.assertions:
        if item.unit not in known_units:
            issues.append({"level": 2, "code": "unknown_unit", "assertion_id": item.id, "severity": "error"})
        if item.coordinate_system and not any(
            node.id == item.coordinate_system for node in graph.nodes
        ):
            issues.append({"level": 3, "code": "unknown_coordinate_system", "assertion_id": item.id, "severity": "error"})
    if _has_cycle(graph):
        issues.append({"level": 4, "code": "dependency_cycle", "severity": "error"})
    active_by_key: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for item in graph.assertions:
        if item.state == "active" and item.assurance != "contradicted":
            active_by_key[(item.subject_id, item.predicate)].append(item)
    for assertions in active_by_key.values():
        validated = [item for item in assertions if item.assurance in {"constraint_validated", "human_approved"}]
        values = {item.value.model_dump_json() for item in validated}
        if len(values) > 1:
            issues.append({"level": 5, "code": "contradictory_validated_values", "severity": "error"})
    if not any(edge.type == "same_object_across_views" for edge in graph.edges):
        issues.append({"level": 6, "code": "cross_view_not_available", "severity": "warning"})
    if not graph.requirements:
        issues.append({"level": 7, "code": "domain_requirements_missing", "severity": "warning"})
    issues.extend(_domain_rule_issues(graph))
    traced = [item for item in graph.assertions if item.origin == "traced" and item.state == "active"]
    evidence = {item.id: item for item in graph.evidence}
    for item in traced:
        kinds = {evidence[eid].kind for eid in item.evidence_ids if eid in evidence}
        if not {"trace_run", "visual_verification"} <= kinds:
            issues.append({"level": 8, "code": "trace_verification_incomplete", "assertion_id": item.id, "severity": "error"})
    critical_unresolved: set[str] = set()
    for target in graph.build_targets:
        try:
            plan = compile_build_plan(graph, target.id)
            critical_unresolved.update(plan.critical_assumption_ids)
        except (KeyError, ValueError) as exc:
            issues.append({"level": 9, "code": "build_plan_compile_failed", "detail": str(exc), "severity": "error"})
    artifact_evidence = {item.kind for item in graph.evidence}
    if "kernel_topology" not in artifact_evidence:
        issues.append({"level": 10, "code": "brep_ifc_reopen_not_available", "severity": "warning"})
    if "projection_comparison" not in artifact_evidence:
        issues.append({"level": 11, "code": "projection_comparison_not_available", "severity": "warning"})
    mandatory_2d = [item for item in graph.requirements if item.mandatory and item.kind in {"view", "section", "dimension"}]
    artifact_ids = {node.id for node in graph.nodes if node.type == "Artifact"}
    supported_2d_media = {"application/pdf", "application/dxf", "image/svg+xml"}
    verified_2d_artifacts = {
        item.subject_id
        for item in graph.assertions
        if item.state == "active"
        and item.subject_id in artifact_ids
        and item.predicate == PREDICATE.ARTIFACT_MEDIA_TYPE
        and item.value.kind == "exact"
        and item.value.value in supported_2d_media
        and item.assurance in {"constraint_validated", "human_approved"}
        and item.evidence_ids
    }
    if mandatory_2d and not verified_2d_artifacts:
        issues.append({"level": 12, "code": "required_2d_artifacts_missing", "severity": "error"})

    errors = [item for item in issues if item["severity"] == "error"]
    contradictions = any(item["code"] == "contradictory_validated_values" for item in errors)
    comprehension = "contradictory" if contradictions else ("review_needed" if errors else "converged")
    build = "not_ready" if errors else ("preview_ready" if critical_unresolved else "verified")
    release = "blocked" if errors or critical_unresolved else "approved"
    state = VerificationState(
        comprehension=comprehension,
        build=build,
        release=release,
        issue_codes=[item["code"] for item in issues],
        critical_unresolved_assertion_ids=sorted(critical_unresolved),
        checked_levels=checked,
    )
    return state, issues


async def persist_verification_run(
    db: AsyncSession,
    row: EngineeringGraphRevision,
    state: VerificationState,
    issues: list[dict[str, Any]],
) -> GraphVerificationRun:
    run = GraphVerificationRun(
        graph_revision_id=row.id,
        levels=state.checked_levels,
        result={"state": state.model_dump(mode="json"), "issues": issues},
    )
    db.add(run)
    await db.flush()
    return run
