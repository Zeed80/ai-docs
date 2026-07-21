"""Revision-bound, fail-closed CAD certification and normalized projection."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.cad_engineering_verify import EngineeringVerification, verify_engineering_ir
from app.ai.cad_ir.schema import CadIR
from app.db.models import (
    CadCertification,
    CadElementRecord,
    CadIrRevision,
    CadRelationRecord,
    ImageGeneration,
)


class CertificationBlocked(ValueError):
    pass


def _verification_payload(result: EngineeringVerification) -> dict:
    return result.model_dump(mode="json", exclude={"graph"})


async def _certificate(
    db: AsyncSession, revision: CadIrRevision, *, profile: str
) -> CadCertification:
    row = (
        await db.execute(
            select(CadCertification)
            .where(CadCertification.cad_ir_revision_id == revision.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        row = CadCertification(cad_ir_revision_id=revision.id, profile=profile)
        db.add(row)
        await db.flush()
    return row


def verify_for_certification(ir: CadIR, profile: str = "auto") -> EngineeringVerification:
    verifier_profile = profile if profile in {"mechanical", "construction"} else "auto"
    result = verify_engineering_ir(ir, profile=verifier_profile)
    if not result.exact_ready:
        codes = ", ".join(finding.code for finding in result.findings) or "UNKNOWN"
        raise CertificationBlocked(f"Ревизия не готова к точной сертификации: {codes}")
    return result


async def approve_by_drafter(
    db: AsyncSession,
    revision: CadIrRevision,
    ir: CadIR,
    *,
    actor_sub: str,
    profile: str = "auto",
) -> CadCertification:
    result = verify_for_certification(ir, profile)
    row = await _certificate(db, revision, profile=profile)
    if row.status == "certified":
        return row
    row.profile = profile
    row.status = "drafter_approved"
    row.verification = _verification_payload(result)
    row.drafter_approved_by = actor_sub
    row.drafter_approved_at = datetime.now(timezone.utc)
    row.normcontrol_approved_by = None
    row.normcontrol_approved_at = None
    row.manifest_hash = None
    await db.flush()
    return row


async def _project_revision(
    db: AsyncSession,
    revision: CadIrRevision,
    ir: CadIR,
    result: EngineeringVerification,
) -> None:
    await db.execute(delete(CadElementRecord).where(CadElementRecord.cad_ir_revision_id == revision.id))
    await db.execute(delete(CadRelationRecord).where(CadRelationRecord.cad_ir_revision_id == revision.id))
    for entity in ir.entities:
        payload = entity.model_dump(mode="json")
        db.add(
            CadElementRecord(
                cad_ir_revision_id=revision.id,
                element_id=entity.id,
                element_type=entity.type,
                assurance=entity.assurance,
                payload=payload,
                source_region=payload.get("source_region"),
                evidence=list(entity.evidence),
            )
        )
    for relation in ir.relations:
        db.add(CadRelationRecord(
            cad_ir_revision_id=revision.id,
            relation_id=relation.id,
            relation_type=f"source_graph:{relation.kind}",
            source_element_id=relation.source_entity_id,
            target_element_ids=relation.target_entity_ids,
            payload=relation.model_dump(mode="json"),
            evidence=relation.evidence,
        ))
    graph = result.graph
    for view in graph.views:
        db.add(CadRelationRecord(
            cad_ir_revision_id=revision.id,
            relation_id=view.id,
            relation_type="view",
            target_element_ids=view.entity_ids,
            payload=view.model_dump(mode="json"),
            evidence=view.evidence,
        ))
    for feature in graph.features:
        db.add(CadRelationRecord(
            cad_ir_revision_id=revision.id,
            relation_id=feature.id,
            relation_type=f"feature:{feature.kind}",
            target_element_ids=feature.entity_ids,
            payload=feature.model_dump(mode="json"),
            evidence=feature.evidence,
        ))
    for dimension in graph.dimensions:
        db.add(CadRelationRecord(
            cad_ir_revision_id=revision.id,
            relation_id=f"dimension:{dimension.dimension_id}",
            relation_type=f"dimension:{dimension.relation}",
            source_element_id=dimension.dimension_id,
            target_element_ids=dimension.target_entity_ids,
            payload=dimension.model_dump(mode="json"),
            evidence=dimension.evidence,
        ))
    await db.flush()
    projected = await db.scalar(
        select(func.count(CadElementRecord.id)).where(CadElementRecord.cad_ir_revision_id == revision.id)
    )
    if projected != len(ir.entities):
        raise CertificationBlocked(
            f"Проекция в БД неполна: {projected or 0} из {len(ir.entities)} элементов."
        )


async def approve_by_normcontroller(
    db: AsyncSession,
    generation: ImageGeneration,
    revision: CadIrRevision,
    ir: CadIR,
    *,
    actor_sub: str,
) -> CadCertification:
    row = await _certificate(db, revision, profile="auto")
    if row.status != "drafter_approved" or not row.drafter_approved_by:
        raise CertificationBlocked("Сначала требуется подпись чертёжника для этой ревизии.")
    if row.drafter_approved_by == actor_sub:
        raise CertificationBlocked("Чертёжник и нормоконтролёр должны быть разными пользователями.")
    result = verify_for_certification(ir, row.profile)
    await _project_revision(db, revision, ir, result)
    signed_at = datetime.now(timezone.utc)
    row.status = "certified"
    row.verification = _verification_payload(result)
    row.normcontrol_approved_by = actor_sub
    row.normcontrol_approved_at = signed_at
    digest = f"{revision.ir_sha256}:{row.drafter_approved_by}:{actor_sub}:{revision.revision}"
    row.manifest_hash = hashlib.sha256(digest.encode()).hexdigest()
    revision.approved_by = actor_sub
    revision.approved_at = signed_at
    generation.accepted = True
    generation.accepted_by = actor_sub
    generation.accepted_at = signed_at
    generation.accepted_revision = revision.revision
    await db.flush()
    return row


def certification_out(row: CadCertification, revision: CadIrRevision) -> dict:
    return {
        "id": str(row.id),
        "revision": revision.revision,
        "profile": row.profile,
        "status": row.status,
        "verification": row.verification,
        "drafter_approved_by": row.drafter_approved_by,
        "drafter_approved_at": row.drafter_approved_at,
        "normcontrol_approved_by": row.normcontrol_approved_by,
        "normcontrol_approved_at": row.normcontrol_approved_at,
        "manifest_hash": row.manifest_hash,
    }
