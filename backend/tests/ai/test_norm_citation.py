"""Ф9 Evidence Engine: resolve validation-issue citations against ingested
NormativeDocument rows."""

from __future__ import annotations

import pytest

from app.ai.cad_ir.schema import ValidationIssueIR
from app.ai.norm_citation import _doc_code_base, resolve_norm_citations
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


# ── _doc_code_base (fix #16: multi-dash ГОСТ numbers) ───────────────────────


def test_doc_code_base_strips_only_the_trailing_year():
    assert _doc_code_base("ГОСТ 2.303-68") == "ГОСТ 2.303"
    assert _doc_code_base("ГОСТ 2.303-2018") == "ГОСТ 2.303"


def test_doc_code_base_preserves_multi_dash_standard_numbers():
    """Regression: a naive split("-")[0] mangled "ГОСТ Р 50-123-2018" into
    "ГОСТ Р 50", which would then match ANY "ГОСТ Р 50-*" document instead
    of specifically 50-123."""
    assert _doc_code_base("ГОСТ Р 50-123-2018") == "ГОСТ Р 50-123"


def test_doc_code_base_no_dash_is_unchanged():
    assert _doc_code_base("ГОСТ 2789") == "ГОСТ 2789"


@pytest.mark.asyncio
async def test_multi_dash_standard_number_matches_the_correct_document_only(db_session):
    correct = NormativeDocument(code="ГОСТ Р 50-123-2018", title="Correct", document_type="ГОСТ")
    wrong = NormativeDocument(code="ГОСТ Р 50-999-2020", title="Wrong", document_type="ГОСТ")
    db_session.add_all([correct, wrong])
    await db_session.flush()

    issue = ValidationIssueIR(code="ESKD_LINE_WEIGHT", message_ru="...", norm_ref="ГОСТ Р 50-123-2018")
    out = await resolve_norm_citations([issue], db_session)
    assert out[0].norm_clause_text == "ГОСТ Р 50-123-2018 — Correct"
