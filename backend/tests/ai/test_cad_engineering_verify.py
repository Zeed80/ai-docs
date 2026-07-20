from app.ai.cad_engineering_graph import build_engineering_graph
from app.ai.cad_engineering_verify import verify_engineering_ir
from app.ai.cad_ir.schema import CadIR, Circle, Point, SourceInfo


def _ir(*, assurance: str = "inferred", dxf_reopens: bool | None = True) -> CadIR:
    ir = CadIR(
        scale=1.0,
        scale_source="manual",
        source=SourceInfo(kind="scan", image_width=200, image_height=100),
        entities=[
            Circle(
                id="circle-1",
                center=Point(x=50, y=50),
                radius=10,
                confidence=1.0,
                assurance=assurance,
            )
        ],
    )
    ir.validation.dxf_reopens = dxf_reopens
    return ir


def test_inferred_geometry_can_never_be_exact_ready():
    result = verify_engineering_ir(_ir())

    assert result.exact_ready is False
    assert result.checks["entities_verified"] is False
    assert "ENTITIES_NOT_VERIFIED" in {finding.code for finding in result.findings}


def test_invalid_graph_reference_fails_closed():
    ir = _ir(assurance="human_approved")
    graph = build_engineering_graph(ir)
    graph.views[0].entity_ids.append("missing")

    result = verify_engineering_ir(ir, graph=graph)

    assert result.exact_ready is False
    assert result.checks["references_valid"] is False


def test_missing_dxf_reopen_proof_fails_closed():
    result = verify_engineering_ir(
        _ir(assurance="human_approved", dxf_reopens=None)
    )

    assert result.exact_ready is False
    assert result.checks["dxf_reopens"] is False


def test_validation_does_not_call_inferred_geometry_exact():
    from app.ai.cad_validate import validate_ir

    ir = _ir()
    validate_ir(ir)

    assert ir.digitization_status == "review_required"
