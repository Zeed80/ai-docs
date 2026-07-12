"""Assurance ladder: schema v2 migration, transition rules, provenance flags."""

from __future__ import annotations

import pytest

from app.ai.cad_ir import CadIR, SourceInfo
from app.ai.cad_ir.assurance import (
    AssuranceTransitionError,
    can_set,
    sanitize_incoming,
    set_assurance,
)
from app.ai.cad_ir.schema import SCHEMA_VERSION, Alternative, Point, Segment, TextEntity
from app.ai.cad_validate import validate_ir


def _seg(**kw) -> Segment:
    return Segment(p1=Point(x=0, y=0), p2=Point(x=100, y=0), **kw)


# ── v1 → v2 migration ─────────────────────────────────────────────────────────


def test_v1_payload_migrates_assurance_from_origin() -> None:
    v1 = {
        "schema_version": 1,
        "source": {"image_width": 100, "image_height": 100},
        "entities": [
            {"type": "segment", "p1": {"x": 0, "y": 0}, "p2": {"x": 10, "y": 0}, "origin": "cv"},
            {"type": "segment", "p1": {"x": 0, "y": 5}, "p2": {"x": 10, "y": 5}, "origin": "human"},
            {"type": "segment", "p1": {"x": 0, "y": 9}, "p2": {"x": 10, "y": 9}, "origin": "spec"},
        ],
    }
    ir = CadIR.model_validate(v1)
    # migration always lands on the CURRENT schema version (was pinned to 2
    # and silently went stale when CAD IR v3 bumped SCHEMA_VERSION)
    assert ir.schema_version == SCHEMA_VERSION
    assert ir.entities[0].assurance == "inferred"
    assert ir.entities[1].assurance == "human_approved"
    assert ir.entities[2].assurance == "constraint_validated"


def test_v2_roundtrip_with_hypotheses_and_provenance() -> None:
    ir = CadIR(
        source=SourceInfo(image_width=200, image_height=100),
        entities=[
            TextEntity(
                position=Point(x=10, y=20),
                text="Ø18",
                alternatives=[Alternative(value="Ø16", p=0.31), Alternative(value="M18", p=0.08)],
                source_region={"x0": 5, "y0": 10, "x1": 60, "y1": 30},
                evidence=["ocr:conf=61"],
            )
        ],
    )
    restored = CadIR.model_validate_json(ir.model_dump_json())
    assert restored == ir
    assert restored.entities[0].alternatives[0].value == "Ø16"


# ── transition rules ──────────────────────────────────────────────────────────


def test_recognizer_cannot_approve() -> None:
    e = _seg()
    assert not can_set("recognizer", e.assurance, "human_approved")
    with pytest.raises(AssuranceTransitionError):
        set_assurance(e, "human_approved", "recognizer")


def test_solver_can_validate_but_not_approve() -> None:
    e = _seg()
    set_assurance(e, "constraint_validated", "solver")
    assert e.assurance == "constraint_validated"
    with pytest.raises(AssuranceTransitionError):
        set_assurance(e, "human_approved", "solver")


def test_nobody_but_human_touches_approved() -> None:
    e = _seg(assurance="human_approved")
    with pytest.raises(AssuranceTransitionError):
        set_assurance(e, "inferred", "solver")
    set_assurance(e, "inferred", "human")  # человек может понизить
    assert e.assurance == "inferred"


def test_sanitize_strips_unearned_assurance() -> None:
    fixed = sanitize_incoming({"type": "segment", "assurance": "human_approved"}, actor="recognizer")
    assert fixed["assurance"] == "inferred"
    human = sanitize_incoming({"type": "segment", "assurance": "inferred"}, actor="human")
    assert human["assurance"] == "human_approved"


# ── review interplay ──────────────────────────────────────────────────────────


def test_approved_entities_skip_review_queue() -> None:
    ir = CadIR(
        source=SourceInfo(image_width=100, image_height=100),
        entities=[_seg(confidence=0.55, assurance="human_approved")],
    )
    validate_ir(ir)
    assert not [r for r in ir.review if not r.resolved]


def test_diffusion_modified_review_is_sticky_across_revalidation() -> None:
    from app.ai.cad_ir.schema import ReviewItem

    seg = _seg(confidence=0.95)
    ir = CadIR(source=SourceInfo(image_width=100, image_height=100), entities=[seg])
    ir.review = [ReviewItem(entity_id=seg.id, reason="diffusion_modified")]
    validate_ir(ir)
    pending = [r for r in ir.review if not r.resolved]
    assert len(pending) == 1
    assert pending[0].reason == "diffusion_modified"


def test_sticky_diffusion_issue_survives_revalidation() -> None:
    from app.ai.cad_ir.schema import ValidationIssueIR

    seg = _seg()
    ir = CadIR(source=SourceInfo(image_width=100, image_height=100), entities=[seg])
    ir.validation.issues.append(ValidationIssueIR(
        code="DIFFUSION_ADDED_INK", severity="warn", entity_ids=[seg.id], message_ru="x"
    ))
    validate_ir(ir)
    assert any(i.code == "DIFFUSION_ADDED_INK" for i in ir.validation.issues)
    # once the flagged entity is gone, the sticky issue is dropped
    ir.entities = []
    validate_ir(ir)
    assert not any(i.code == "DIFFUSION_ADDED_INK" for i in ir.validation.issues)
