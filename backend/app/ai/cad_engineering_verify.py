"""Independent fail-closed verification of an engineering interpretation.

Recognition and graph construction are proposal stages.  This module is the
only place that decides whether those proposals contain enough independent
evidence to be considered an exact digitization candidate.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.ai.cad_engineering_graph import EngineeringGraph, build_engineering_graph
from app.ai.cad_ir.schema import CadIR
from app.ai.cad_validate import validate_ir


class VerificationFinding(BaseModel):
    code: str
    severity: Literal["error", "warn"]
    message: str
    entity_ids: list[str] = Field(default_factory=list)


class EngineeringVerification(BaseModel):
    exact_ready: bool
    findings: list[VerificationFinding] = Field(default_factory=list)
    checks: dict[str, bool] = Field(default_factory=dict)
    graph: EngineeringGraph


def verify_engineering_ir(
    ir: CadIR,
    *,
    graph: EngineeringGraph | None = None,
    profile: Literal["mechanical", "construction", "auto"] = "auto",
) -> EngineeringVerification:
    """Verify references and engineering evidence without trusting a model.

    Pixel coverage alone is intentionally insufficient.  A scan-origin IR is
    exact-ready only when every exported entity has been constraint-validated
    or human-approved, the interpretation graph has no unresolved/inferred
    claims, deterministic CAD validation passes, and the DXF reopened.
    """

    graph = graph or build_engineering_graph(ir, profile=profile)
    findings: list[VerificationFinding] = []
    entity_ids = {entity.id for entity in ir.entities}

    referenced_ids = {
        entity_id
        for view in graph.views
        for entity_id in view.entity_ids
    } | {
        entity_id
        for feature in graph.features
        for entity_id in feature.entity_ids
    } | {
        entity_id
        for relation in graph.dimensions
        for entity_id in relation.target_entity_ids
    }
    missing = sorted(referenced_ids - entity_ids)
    if missing:
        findings.append(
            VerificationFinding(
                code="GRAPH_REFERENCE_INVALID",
                severity="error",
                message="Инженерный граф ссылается на отсутствующие CAD-сущности.",
                entity_ids=missing,
            )
        )

    inferred = [feature for feature in graph.features if feature.status == "inferred"]
    if inferred:
        findings.append(
            VerificationFinding(
                code="FEATURES_NOT_CONSTRAINT_VALIDATED",
                severity="error",
                message=(
                    "Инженерные признаки являются гипотезами и не подтверждены "
                    "ограничениями или человеком."
                ),
                entity_ids=sorted(
                    {entity_id for feature in inferred for entity_id in feature.entity_ids}
                ),
            )
        )

    if graph.unresolved:
        findings.append(
            VerificationFinding(
                code="ENGINEERING_GRAPH_UNRESOLVED",
                severity="error",
                message="В инженерном графе остались неразрешённые связи.",
            )
        )

    unverified = [
        entity.id
        for entity in ir.entities
        if entity.assurance not in ("constraint_validated", "human_approved")
        and not entity.construction
    ]
    if unverified:
        findings.append(
            VerificationFinding(
                code="ENTITIES_NOT_VERIFIED",
                severity="error",
                message=(
                    "Часть экспортируемой геометрии основана только на распознавании "
                    "и не имеет независимого подтверждения."
                ),
                entity_ids=unverified,
            )
        )

    validation = validate_ir(ir)
    if validation.blocking:
        findings.append(
            VerificationFinding(
                code="CAD_VALIDATION_FAILED",
                severity="error",
                message=f"Детерминированная проверка CAD IR: ошибок {len(validation.blocking)}.",
            )
        )
    if ir.validation.dxf_reopens is not True:
        findings.append(
            VerificationFinding(
                code="DXF_REOPEN_NOT_PROVEN",
                severity="error",
                message="Нет подтверждения независимого повторного открытия DXF.",
            )
        )

    checks = {
        "references_valid": not missing,
        "features_validated": not inferred,
        "graph_resolved": not graph.unresolved,
        "entities_verified": not unverified,
        "cad_validation_passed": not validation.blocking,
        "dxf_reopens": ir.validation.dxf_reopens is True,
    }
    exact_ready = all(checks.values())
    # The verifier is authoritative; graph construction alone cannot promote.
    graph = graph.model_copy(update={"exact_ready": exact_ready})
    return EngineeringVerification(
        exact_ready=exact_ready,
        findings=findings,
        checks=checks,
        graph=graph,
    )
