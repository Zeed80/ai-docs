"""Ф9 Evidence Engine: resolve validation-issue citations against ingested
NormativeDocument rows."""

from __future__ import annotations

import pytest

from app.ai.cad_ir.schema import ValidationIssueIR
from app.ai.norm_citation import resolve_norm_citations
from app.db.models import NormativeDocument


@pytest.mark.asyncio
async def test_resolves_citation_when_document_ingested(db_session):
    doc = NormativeDocument(code="ГОСТ 2.303-68", title="Линии", document_type="ГОСТ")
    db_session.add(doc)
    await db_session.flush()

    issue = ValidationIssueIR(code="ESKD_LINE_WEIGHT", message_ru="...", norm_ref="ГОСТ 2.303-68")
    out = await resolve_norm_citations([issue], db_session)
    assert out[0].norm_clause_text == "ГОСТ 2.303-68 — Линии"


@pytest.mark.asyncio
async def test_matches_document_regardless_of_ingested_edition(db_session):
    """Cited "ГОСТ 2.303-68" should match an ingested "ГОСТ 2.303-2018" —
    same standard number, a newer edition still ingested in the corpus."""
    doc = NormativeDocument(code="ГОСТ 2.303-2018", title="Линии (нов. ред.)", document_type="ГОСТ")
    db_session.add(doc)
    await db_session.flush()

    issue = ValidationIssueIR(code="ESKD_LINE_WEIGHT", message_ru="...", norm_ref="ГОСТ 2.303-68")
    out = await resolve_norm_citations([issue], db_session)
    assert out[0].norm_clause_text == "ГОСТ 2.303-2018 — Линии (нов. ред.)"


@pytest.mark.asyncio
async def test_stays_none_when_standard_not_ingested(db_session):
    issue = ValidationIssueIR(code="ESKD_LINE_WEIGHT", message_ru="...", norm_ref="ГОСТ 2.303-68")
    out = await resolve_norm_citations([issue], db_session)
    assert out[0].norm_clause_text is None
    assert out[0].norm_ref == "ГОСТ 2.303-68"  # the plain citation survives regardless


@pytest.mark.asyncio
async def test_issues_without_norm_ref_are_untouched(db_session):
    issue = ValidationIssueIR(code="GEOM_DEGENERATE", message_ru="...")
    out = await resolve_norm_citations([issue], db_session)
    assert out[0].norm_ref is None
    assert out[0].norm_clause_text is None


@pytest.mark.asyncio
async def test_already_resolved_issues_are_not_requeried(db_session):
    issue = ValidationIssueIR(
        code="ESKD_LINE_WEIGHT", message_ru="...", norm_ref="ГОСТ 2.303-68",
        norm_clause_text="already resolved",
    )
    out = await resolve_norm_citations([issue], db_session)
    assert out[0].norm_clause_text == "already resolved"
