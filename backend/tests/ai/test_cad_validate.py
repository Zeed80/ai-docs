"""Typed validation checks over the CAD IR."""

from __future__ import annotations

import pytest

from app.ai.cad_ir import CadIR, SourceInfo
from app.ai.cad_ir.schema import Circle, DimensionEntity, Point, Polyline, ReviewItem, Segment, SheetInfo, TextEntity
from app.ai.cad_validate import CadCheckCode, run_llm_review_levels, validate_ir


def _ir(entities, scale=0.5) -> CadIR:
    return CadIR(
        source=SourceInfo(image_width=400, image_height=300),
        scale=scale,
        entities=entities,
    )


def _codes(report) -> set[str]:
    return {i.code for i in report.issues}


def test_clean_ir_passes() -> None:
    report = validate_ir(_ir([
        Segment(p1=Point(x=0, y=0), p2=Point(x=100, y=0)),
        Circle(center=Point(x=50, y=50), radius=20),
    ]))
    assert report.issues == []
    assert report.blocking == []


def test_scale_unknown_flagged() -> None:
    report = validate_ir(_ir([Segment(p1=Point(x=0, y=0), p2=Point(x=100, y=0))], scale=None))
    assert CadCheckCode.SCALE_UNKNOWN.value in _codes(report)


def test_degenerate_and_duplicate_segments() -> None:
    seg = Segment(p1=Point(x=10, y=10), p2=Point(x=110, y=10))
    dup = Segment(p1=Point(x=110.5, y=10.5), p2=Point(x=10.5, y=10.5))  # reversed, within tol
    tiny = Segment(p1=Point(x=0, y=0), p2=Point(x=1, y=1))
    report = validate_ir(_ir([seg, dup, tiny]))
    codes = _codes(report)
    assert CadCheckCode.GEOM_DUPLICATE.value in codes
    assert CadCheckCode.GEOM_DEGENERATE.value in codes


def test_self_intersection_is_error() -> None:
    pytest.importorskip("shapely")
    bowtie = Polyline(
        points=[Point(x=0, y=0), Point(x=100, y=100), Point(x=100, y=0), Point(x=0, y=100)],
        closed=True,
    )
    report = validate_ir(_ir([bowtie]))
    assert CadCheckCode.GEOM_SELF_INTERSECTION.value in _codes(report)
    assert report.blocking


def test_eskd_line_weight() -> None:
    axis_thick = Segment(
        p1=Point(x=0, y=0), p2=Point(x=100, y=0), line_class="axis", width_class="main"
    )
    report = validate_ir(_ir([axis_thick]))
    assert CadCheckCode.ESKD_LINE_WEIGHT.value in _codes(report)


def test_eskd_checks_carry_a_plain_norm_citation() -> None:
    """Ф9: every ЕСКД-formatting issue cites the standard it enforces, even
    before any resolve_norm_citations lookup against the ingested corpus."""
    axis_thick = Segment(
        p1=Point(x=0, y=0), p2=Point(x=100, y=0), line_class="axis", width_class="main"
    )
    report = validate_ir(_ir([axis_thick]))
    line_weight = next(i for i in report.issues if i.code == "ESKD_LINE_WEIGHT")
    assert line_weight.norm_ref == "ГОСТ 2.303-68"
    assert line_weight.norm_clause_text is None  # not resolved against the corpus here


def test_geometry_checks_have_no_norm_citation() -> None:
    """GEOM_* issues are sanity checks, not standard-mandated — they
    shouldn't fabricate a citation that doesn't exist."""
    tiny = Segment(p1=Point(x=0, y=0), p2=Point(x=1, y=1))
    report = validate_ir(_ir([tiny]))
    degenerate = next(i for i in report.issues if i.code == "GEOM_DEGENERATE")
    assert degenerate.norm_ref is None


def test_low_confidence_entities_enter_review_queue() -> None:
    ir = _ir([
        Segment(p1=Point(x=0, y=0), p2=Point(x=100, y=0), confidence=0.55),
        Segment(p1=Point(x=0, y=10), p2=Point(x=100, y=10), confidence=0.95),
    ])
    validate_ir(ir)
    pending = [r for r in ir.review if not r.resolved]
    assert len(pending) == 1
    assert pending[0].entity_id == ir.entities[0].id
    assert pending[0].reason == "low_confidence"


def test_resolved_review_items_survive_revalidation() -> None:
    seg = Segment(p1=Point(x=0, y=0), p2=Point(x=100, y=0), confidence=0.55)
    ir = _ir([seg])
    ir.review = [ReviewItem(entity_id=seg.id, reason="low_confidence", resolved=True)]
    validate_ir(ir)
    assert all(r.resolved for r in ir.review if r.entity_id == seg.id)


def test_low_coverage_is_blocking() -> None:
    ir = _ir([Segment(p1=Point(x=0, y=0), p2=Point(x=100, y=0))])
    ir.validation.coverage_recall = 0.6
    ir.validation.coverage_precision = 0.9
    report = validate_ir(ir)
    assert CadCheckCode.COVERAGE_LOW.value in _codes(report)
    assert report.blocking


# ── Ф7.1: formalized 7-level pipeline ───────────────────────────────────────


def test_every_issue_carries_an_assurance_level() -> None:
    report = validate_ir(_ir([
        Segment(p1=Point(x=0, y=0), p2=Point(x=100, y=0), line_class="axis", width_class="main"),
    ], scale=None))
    assert report.issues  # SCALE_UNKNOWN + ESKD_LINE_WEIGHT at minimum
    for issue in report.issues:
        assert issue.level > 0


def test_by_level_groups_issues_correctly() -> None:
    report = validate_ir(_ir([
        Segment(p1=Point(x=0, y=0), p2=Point(x=100, y=0), line_class="axis", width_class="main"),
    ], scale=None))
    grouped = report.by_level()
    assert 1 in grouped  # SCALE_UNKNOWN
    assert 4 in grouped  # ESKD_LINE_WEIGHT (+ ESKD_NO_CONTOUR_GEOMETRY, also level 4)
    assert all(i.code == "SCALE_UNKNOWN" for i in grouped[1])
    assert {i.code for i in grouped[4]} == {"ESKD_LINE_WEIGHT", "ESKD_NO_CONTOUR_GEOMETRY"}


def test_geometry_issues_are_level_2() -> None:
    tiny = Segment(p1=Point(x=0, y=0), p2=Point(x=1, y=1))
    report = validate_ir(_ir([tiny]))
    degenerate = next(i for i in report.issues if i.code == "GEOM_DEGENERATE")
    assert degenerate.level == 2


def test_dim_chain_mismatch_is_level_3() -> None:
    from unittest.mock import patch

    with patch("app.ai.cad_hypothesis.check_dimension_chains", return_value=["сумма не сходится"]):
        report = validate_ir(_ir([Segment(p1=Point(x=0, y=0), p2=Point(x=100, y=0))]))
    mismatch = next(i for i in report.issues if i.code == "DIM_CHAIN_MISMATCH")
    assert mismatch.level == 3


def test_nonstandard_ra_is_flagged() -> None:
    ir = _ir([TextEntity(position=Point(x=10, y=10), text="Ra 1.4", height=10)])
    report = validate_ir(ir)
    ra_issues = [i for i in report.issues if i.code == "RA_INVALID"]
    assert len(ra_issues) == 1
    assert ra_issues[0].level == 3
    assert "1.6" in ra_issues[0].message_ru  # nearest standard value


def test_standard_ra_not_flagged() -> None:
    ir = _ir([TextEntity(position=Point(x=10, y=10), text="Ra 1.6", height=10)])
    report = validate_ir(ir)
    assert not [i for i in report.issues if i.code == "RA_INVALID"]


def test_ra_on_dimension_entity_also_checked() -> None:
    ir = _ir([DimensionEntity(p1=Point(x=0, y=0), p2=Point(x=10, y=0), text="Ra 3.0")])
    report = validate_ir(ir)
    assert any(i.code == "RA_INVALID" for i in report.issues)


def test_nonstandard_scale_flagged_when_stated_in_title_block() -> None:
    ir = _ir([Segment(p1=Point(x=0, y=0), p2=Point(x=10, y=0))])
    ir.sheet = SheetInfo(title_block={"scale": "1:3"})
    report = validate_ir(ir)
    scale_issues = [i for i in report.issues if i.code == "ESKD_SCALE_NONSTANDARD"]
    assert len(scale_issues) == 1
    assert scale_issues[0].level == 4


def test_standard_scale_not_flagged() -> None:
    ir = _ir([Segment(p1=Point(x=0, y=0), p2=Point(x=10, y=0))])
    ir.sheet = SheetInfo(title_block={"scale": "1:2"})
    report = validate_ir(ir)
    assert not [i for i in report.issues if i.code == "ESKD_SCALE_NONSTANDARD"]


def test_no_stated_scale_is_not_flagged() -> None:
    ir = _ir([Segment(p1=Point(x=0, y=0), p2=Point(x=10, y=0))])
    report = validate_ir(ir)
    assert not [i for i in report.issues if i.code == "ESKD_SCALE_NONSTANDARD"]


# ── Ф7.3: full ЕСКД checker (sheet format / title block / contour presence) ──


def test_sheet_format_unknown_only_when_framed() -> None:
    ir = _ir([Segment(p1=Point(x=0, y=0), p2=Point(x=10, y=0))])
    ir.sheet = SheetInfo(frame=False, format=None)
    report = validate_ir(ir)
    assert not [i for i in report.issues if i.code == "ESKD_SHEET_FORMAT_UNKNOWN"]


def test_sheet_format_unknown_flagged_when_framed_and_unset() -> None:
    ir = _ir([Segment(p1=Point(x=0, y=0), p2=Point(x=10, y=0))])
    ir.sheet = SheetInfo(frame=True, format=None)
    report = validate_ir(ir)
    issues = [i for i in report.issues if i.code == "ESKD_SHEET_FORMAT_UNKNOWN"]
    assert len(issues) == 1
    assert issues[0].severity == "info"
    assert issues[0].level == 4


def test_sheet_format_standard_not_flagged() -> None:
    ir = _ir([Segment(p1=Point(x=0, y=0), p2=Point(x=10, y=0))])
    ir.sheet = SheetInfo(frame=True, format="A3")
    report = validate_ir(ir)
    assert not [i for i in report.issues if i.code == "ESKD_SHEET_FORMAT_UNKNOWN"]


def test_title_block_incomplete_when_region_empty_of_text() -> None:
    ir = _ir([Segment(p1=Point(x=0, y=0), p2=Point(x=10, y=0))])
    ir.sheet = SheetInfo(
        frame=True,
        title_block={"detected": True, "region": {"x0": 100, "y0": 100, "x1": 200, "y1": 150}},
    )
    report = validate_ir(ir)
    issues = [i for i in report.issues if i.code == "ESKD_TITLE_BLOCK_INCOMPLETE"]
    assert len(issues) == 1
    assert issues[0].level == 4


def test_title_block_complete_when_text_inside_region() -> None:
    ir = _ir([
        Segment(p1=Point(x=0, y=0), p2=Point(x=10, y=0)),
        TextEntity(position=Point(x=150, y=120), text="АБВГ.001", height=10),
    ])
    ir.sheet = SheetInfo(
        frame=True,
        title_block={"detected": True, "region": {"x0": 100, "y0": 100, "x1": 200, "y1": 150}},
    )
    report = validate_ir(ir)
    assert not [i for i in report.issues if i.code == "ESKD_TITLE_BLOCK_INCOMPLETE"]


def test_title_block_not_checked_without_frame() -> None:
    ir = _ir([Segment(p1=Point(x=0, y=0), p2=Point(x=10, y=0))])
    ir.sheet = SheetInfo(frame=False, title_block={"detected": True, "region": {"x0": 0, "y0": 0, "x1": 1, "y1": 1}})
    report = validate_ir(ir)
    assert not [i for i in report.issues if i.code == "ESKD_TITLE_BLOCK_INCOMPLETE"]


def test_no_contour_geometry_flagged_for_dimension_only_sheet() -> None:
    ir = _ir([DimensionEntity(p1=Point(x=0, y=0), p2=Point(x=10, y=0), text="40")])
    report = validate_ir(ir)
    issues = [i for i in report.issues if i.code == "ESKD_NO_CONTOUR_GEOMETRY"]
    assert len(issues) == 1
    assert issues[0].level == 4


def test_no_contour_geometry_not_flagged_when_present() -> None:
    ir = _ir([Segment(p1=Point(x=0, y=0), p2=Point(x=10, y=0))])  # default contour/main
    report = validate_ir(ir)
    assert not [i for i in report.issues if i.code == "ESKD_NO_CONTOUR_GEOMETRY"]


def test_empty_sheet_not_flagged_as_missing_contour() -> None:
    report = validate_ir(_ir([]))
    assert not [i for i in report.issues if i.code == "ESKD_NO_CONTOUR_GEOMETRY"]


@pytest.mark.asyncio
async def test_llm_review_parses_issues_with_correct_levels() -> None:
    from unittest.mock import AsyncMock

    from app.ai.schemas import AIResponse, AITask, ProviderKind

    fake_router = AsyncMock()
    fake_router.run.return_value = AIResponse(
        task=AITask.DRAWING_ANALYSIS_VLM, provider=ProviderKind.OLLAMA, model="test",
        text='{"issues": [{"level": 6, "severity": "warn", "message": "нет обозначения базы"}, '
             '{"level": 7, "severity": "info", "message": "странная пропорция"}]}',
    )
    issues = await run_llm_review_levels(b"fake-png", router=fake_router)
    assert len(issues) == 2
    assert {i.level for i in issues} == {6, 7}
    codes = {i.code for i in issues}
    assert codes == {"NORMCONTROL_LLM", "VLM_CRITIC"}


@pytest.mark.asyncio
async def test_llm_review_degrades_to_empty_on_failure() -> None:
    from unittest.mock import AsyncMock

    fake_router = AsyncMock()
    fake_router.run.side_effect = RuntimeError("model unavailable")
    issues = await run_llm_review_levels(b"fake-png", router=fake_router)
    assert issues == []


@pytest.mark.asyncio
async def test_llm_review_empty_issues_when_drawing_looks_fine() -> None:
    from unittest.mock import AsyncMock

    from app.ai.schemas import AIResponse, AITask, ProviderKind

    fake_router = AsyncMock()
    fake_router.run.return_value = AIResponse(
        task=AITask.DRAWING_ANALYSIS_VLM, provider=ProviderKind.OLLAMA, model="test",
        text='{"issues": []}',
    )
    issues = await run_llm_review_levels(b"fake-png", router=fake_router)
    assert issues == []
