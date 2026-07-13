"""Reproducible release manifest (C5)."""

from __future__ import annotations

import hashlib

import pytest

from app.ai.cad_ir import CadIR, SourceInfo
from app.ai.cad_ir.dxf_render import render_ir_to_dxf
from app.ai.cad_ir.png_render import render_ir_to_png
from app.ai.cad_ir.schema import AnnotationEntity, Circle, Point, Segment, TextEntity
from app.ai.cad_ir.svg_render import render_ir_to_svg
from app.ai.cad_validate import validate_ir
from app.services.cad_release import ReleaseBlocked, build_release_manifest


def _ir():
    ir = CadIR(
        source=SourceInfo(image_width=400, image_height=300),
        scale=0.5,
        scale_source="manual",
        entities=[
            Segment(p1=Point(x=0, y=0), p2=Point(x=100, y=0), line_class="contour", width_class="main"),
            Circle(center=Point(x=50, y=50), radius=20, line_class="contour", width_class="main"),
            TextEntity(position=Point(x=10, y=10), text="Вал"),
            AnnotationEntity(position=Point(x=5, y=5), kind="roughness", value="3.2"),
        ],
    )
    validate_ir(ir)
    return ir


def _stored_hashes(ir):
    def h(b):
        return hashlib.sha256(b).hexdigest()

    return h(render_ir_to_png(ir)), h(render_ir_to_svg(ir)), h(render_ir_to_dxf(ir))


def test_dxf_render_is_byte_deterministic():
    # C5 reproducibility hinges on this.
    ir = _ir()
    hashes = {hashlib.sha256(render_ir_to_dxf(ir)).hexdigest() for _ in range(3)}
    assert len(hashes) == 1


def _manifest(ir, *, accepted=True, revision=0, accepted_revision=0):
    png, svg, dxf = _stored_hashes(ir)
    ir_sha = hashlib.sha256(ir.model_dump_json().encode()).hexdigest()
    return build_release_manifest(
        generation_id="gen-1",
        revision=revision,
        ir=ir,
        stored_ir_sha256=ir_sha,
        stored_artifact_hashes={"png": png, "svg": svg, "dxf": dxf},
        accepted=accepted,
        accepted_by="user-1",
        accepted_at="2026-07-13T00:00:00+00:00",
        accepted_revision=accepted_revision,
        approved_by="user-1",
        approved_at="2026-07-13T00:00:00+00:00",
    )


def test_manifest_reports_full_reproducibility():
    m = _manifest(_ir())
    assert m["fully_reproducible"] is True
    assert m["cad_ir"]["reproducible"] is True
    assert all(a["reproducible"] for a in m["artifacts"].values())
    assert m["dxf_version"] == "R2010"
    assert m["manifest_sha256"]
    assert m["validation"]["eskd_profile_version"]


def test_manifest_hash_is_stable():
    ir = _ir()
    assert _manifest(ir)["manifest_sha256"] == _manifest(ir)["manifest_sha256"]


def test_release_blocked_when_not_accepted():
    with pytest.raises(ReleaseBlocked):
        _manifest(_ir(), accepted=False)


def test_release_blocked_when_revision_mismatch():
    with pytest.raises(ReleaseBlocked):
        _manifest(_ir(), revision=2, accepted_revision=1)


def test_release_blocked_on_blocking_issue():
    # A degenerate segment is an error-severity (blocking) geometry issue.
    ir = CadIR(
        source=SourceInfo(image_width=400, image_height=300),
        scale=0.5, scale_source="manual",
        entities=[Segment(p1=Point(x=0, y=0), p2=Point(x=1, y=1))],
    )
    validate_ir(ir)
    assert ir.validation.blocking
    with pytest.raises(ReleaseBlocked):
        _manifest(ir)


def test_manifest_flags_tampered_artifact():
    ir = _ir()
    png, svg, dxf = _stored_hashes(ir)
    ir_sha = hashlib.sha256(ir.model_dump_json().encode()).hexdigest()
    m = build_release_manifest(
        generation_id="gen-1", revision=0, ir=ir,
        stored_ir_sha256=ir_sha,
        stored_artifact_hashes={"png": png, "svg": svg, "dxf": "deadbeef"},  # tampered
        accepted=True, accepted_by="u", accepted_at=None, accepted_revision=0,
        approved_by="u", approved_at=None,
    )
    assert m["artifacts"]["dxf"]["reproducible"] is False
    assert m["fully_reproducible"] is False
