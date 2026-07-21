import pytest

from app.ai.cad_ir.schema import CadIR, Circle, Point, SourceInfo
from app.services.cad_certification import CertificationBlocked, verify_for_certification


def _ir(assurance: str, *, dxf_reopens: bool = True) -> CadIR:
    ir = CadIR(
        scale=1.0,
        scale_source="manual",
        source=SourceInfo(kind="scan", image_width=100, image_height=100),
        entities=[
            Circle(
                id="circle-1",
                center=Point(x=50, y=50),
                radius=10,
                assurance=assurance,
            )
        ],
    )
    ir.validation.dxf_reopens = dxf_reopens
    return ir


def test_certification_rejects_model_only_geometry():
    with pytest.raises(CertificationBlocked, match="ENTITIES_NOT_VERIFIED"):
        verify_for_certification(_ir("inferred"))


def test_certification_accepts_independently_verified_geometry():
    result = verify_for_certification(_ir("human_approved"))

    assert result.exact_ready is True
    assert all(result.checks.values())


def test_experimental_profile_still_uses_fail_closed_verifier():
    with pytest.raises(CertificationBlocked):
        verify_for_certification(_ir("human_approved", dxf_reopens=False), "electrical")
