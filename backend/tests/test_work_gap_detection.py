"""Б12: repeated same-shape WorkStepAttempt failures become a draft
CapabilityProposal for a human to review — never auto-promoted, never
created synchronously per failure (batched, periodic).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.models import CapabilityProposal
from app.domain.work_gap_detection import (
    _error_signature,
    create_gap_proposals,
    detect_capability_gaps,
)
from app.domain.work_orders import (
    claim_ready_step,
    create_single_step_plan,
    create_work_order,
    fail_attempt,
)


def test_error_signature_prefers_code_over_type():
    assert _error_signature({"code": "boom", "type": "RuntimeError"}) == "boom"
    assert _error_signature({"type": "RuntimeError"}) == "RuntimeError"
    assert _error_signature({}) == "unknown"
    assert _error_signature(None) == "unknown"


async def _make_failed_attempt(db, *, capability: str, action: str, error: dict) -> None:
    order = await create_work_order(db, owner_key="tester", objective="Doomed call")
    await create_single_step_plan(
        db,
        order,
        kind="capability",
        title="Call",
        input_data={},
        capability=capability,
        action=action,
        max_attempts=1,
    )
    claimed = await claim_ready_step(db, worker_id="w", work_order_id=order.id)
    assert claimed is not None
    c_order, c_step, c_attempt = claimed
    await fail_attempt(
        db,
        order=c_order,
        step=c_step,
        attempt=c_attempt,
        error=error,
        retryable=False,
        actor="w",
    )
    await db.flush()


@pytest.mark.asyncio
async def test_no_gap_below_threshold(db_session):
    for _ in range(4):
        await _make_failed_attempt(
            db_session,
            capability="documents",
            action="ingest",
            error={"code": "storage_unreachable"},
        )

    gaps = await detect_capability_gaps(db_session, threshold=5)

    assert gaps == []


@pytest.mark.asyncio
async def test_gap_at_threshold_is_detected_and_proposal_created(db_session):
    for _ in range(5):
        await _make_failed_attempt(
            db_session,
            capability="documents",
            action="ingest",
            error={"code": "storage_unreachable"},
        )

    gaps = await detect_capability_gaps(db_session, threshold=5)
    assert len(gaps) == 1
    assert gaps[0]["capability"] == "documents"
    assert gaps[0]["action"] == "ingest"
    assert gaps[0]["error_signature"] == "storage_unreachable"
    assert gaps[0]["count"] == 5

    created = await create_gap_proposals(db_session, threshold=5)
    assert created == 1

    proposal = (await db_session.execute(select(CapabilityProposal))).scalar_one()
    assert proposal.status == "draft"
    assert proposal.missing_capability == "documents"
    assert "documents.ingest" in proposal.title
    assert proposal.metadata_["auto_detected"] is True
    assert proposal.metadata_["gap_signature"] == "documents.ingest:storage_unreachable"
    assert len(proposal.metadata_["attempt_ids"]) == 5


@pytest.mark.asyncio
async def test_different_error_signatures_do_not_merge(db_session):
    for _ in range(3):
        await _make_failed_attempt(
            db_session,
            capability="documents",
            action="ingest",
            error={"code": "storage_unreachable"},
        )
    for _ in range(3):
        await _make_failed_attempt(
            db_session,
            capability="documents",
            action="ingest",
            error={"code": "invalid_mime_type"},
        )

    gaps = await detect_capability_gaps(db_session, threshold=3)

    assert len(gaps) == 2
    assert {g["error_signature"] for g in gaps} == {"storage_unreachable", "invalid_mime_type"}


@pytest.mark.asyncio
async def test_gap_detection_ignores_failures_outside_window(db_session):
    for _ in range(5):
        await _make_failed_attempt(
            db_session,
            capability="documents",
            action="ingest",
            error={"code": "storage_unreachable"},
        )

    gaps = await detect_capability_gaps(db_session, threshold=5, window=timedelta(seconds=-1))

    assert gaps == []


@pytest.mark.asyncio
async def test_create_gap_proposals_does_not_duplicate_an_open_proposal(db_session):
    for _ in range(5):
        await _make_failed_attempt(
            db_session,
            capability="documents",
            action="ingest",
            error={"code": "storage_unreachable"},
        )
    first = await create_gap_proposals(db_session, threshold=5)
    assert first == 1

    # More failures of the exact same shape arrive before anyone decides the
    # first proposal — the next periodic tick must not spam a second one.
    for _ in range(5):
        await _make_failed_attempt(
            db_session,
            capability="documents",
            action="ingest",
            error={"code": "storage_unreachable"},
        )
    second = await create_gap_proposals(db_session, threshold=5)

    assert second == 0
    count = (await db_session.execute(select(CapabilityProposal))).scalars().all()
    assert len(count) == 1


@pytest.mark.asyncio
async def test_create_gap_proposals_creates_a_new_one_once_prior_is_decided(db_session):
    for _ in range(5):
        await _make_failed_attempt(
            db_session,
            capability="documents",
            action="ingest",
            error={"code": "storage_unreachable"},
        )
    await create_gap_proposals(db_session, threshold=5)
    existing = (await db_session.execute(select(CapabilityProposal))).scalar_one()
    existing.status = "accepted"  # a human decided it
    await db_session.flush()

    for _ in range(5):
        await _make_failed_attempt(
            db_session,
            capability="documents",
            action="ingest",
            error={"code": "storage_unreachable"},
        )
    created = await create_gap_proposals(db_session, threshold=5)

    assert created == 1
