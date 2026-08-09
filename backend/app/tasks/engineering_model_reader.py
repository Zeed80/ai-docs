"""Celery entrypoint for the budgeted EngineeringModelGraph reader."""

from __future__ import annotations

from app.tasks.async_runner import run_async
from app.tasks.celery_app import celery_app


@celery_app.task(
    bind=True,
    name="engineering_model_reader.run",
    max_retries=0,
    soft_time_limit=930,
    time_limit=960,
)
def run_engineering_model_reader(
    self,
    graph_id: str,
    target_id: str,
) -> dict:
    return run_async(_run_engineering_model_reader(graph_id, target_id))


async def _run_engineering_model_reader(
    graph_id: str,
    target_id: str,
) -> dict:
    from sqlalchemy import select

    from app.ai.engineering_hybrid_trace import (
        HybridTracePassResult,
        run_hybrid_trace_pass,
    )
    from app.ai.engineering_model_reader import read_focused_assertions
    from app.db.models import (
        EngineeringGraphRevision,
        TraceProposalRecord,
        VisualVerificationRun,
    )
    from app.db.session import _get_session_factory
    from app.services.engineering_model_reader import run_persisted_reader

    factory = _get_session_factory()
    async with factory() as db:

        async def observe_trace_result(graph, plan, result) -> None:
            if not isinstance(result, HybridTracePassResult):
                return
            revision = (
                await db.execute(
                    select(EngineeringGraphRevision).where(
                        EngineeringGraphRevision.canonical_sha256 == graph.canonical_sha256
                    )
                )
            ).scalar_one()
            for rank, evaluation in enumerate(result.evaluations, start=1):
                existing = (
                    await db.execute(
                        select(TraceProposalRecord).where(
                            TraceProposalRecord.graph_revision_id == revision.id,
                            TraceProposalRecord.proposal_id == evaluation.proposal.id,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    continue
                proposal_row = TraceProposalRecord(
                    graph_revision_id=revision.id,
                    proposal_id=evaluation.proposal.id,
                    source_region_id=evaluation.proposal.source_region_id,
                    assertion_id=plan.assertion_ids[0] if plan.assertion_ids else None,
                    rank=rank,
                    status=(
                        "accepted"
                        if evaluation.proposal.id == result.selected_proposal_id
                        else "eligible"
                        if evaluation.admission.accepted
                        else "rejected"
                    ),
                    payload=evaluation.proposal.model_dump(mode="json"),
                    score=evaluation.admission.score,
                )
                db.add(proposal_row)
                await db.flush()
                db.add(
                    VisualVerificationRun(
                        trace_proposal_id=proposal_row.id,
                        verifier_model=evaluation.visual.verifier_model,
                        verdict=evaluation.visual.verdict,
                        result=evaluation.visual.model_dump(
                            mode="json",
                            exclude={"raw_output"},
                        ),
                        raw_output=evaluation.visual.raw_output,
                    )
                )

        outcome = await run_persisted_reader(
            db,
            graph_id=graph_id,
            target_id=target_id,
            read_pass=read_focused_assertions,
            hybrid_trace_pass=run_hybrid_trace_pass,
            observe_pass_result=observe_trace_result,
        )
    return outcome.model_dump(mode="json")
