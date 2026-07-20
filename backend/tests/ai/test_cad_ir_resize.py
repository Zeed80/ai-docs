from app.ai.cad_ir.resize import fit_ir_to_long_side, resize_ir
from app.ai.cad_ir.schema import (
    CadIR,
    Circle,
    HatchRegion,
    Point,
    SourceInfo,
    SourceRegion,
    TextEntity,
    UnresolvedRegion,
)


def _ir() -> CadIR:
    return CadIR(
        source=SourceInfo(image_width=1000, image_height=500),
        scale=0.2,
        entities=[
            Circle(
                center=Point(x=500, y=250),
                radius=100,
                source_region=SourceRegion(x0=400, y0=150, x1=600, y1=350),
            ),
            TextEntity(position=Point(x=100, y=100), text="M20", height=20),
            HatchRegion(
                boundary=[
                    Point(x=10, y=10),
                    Point(x=90, y=10),
                    Point(x=90, y=90),
                ],
                holes=[[
                    Point(x=20, y=20),
                    Point(x=30, y=20),
                    Point(x=30, y=30),
                ]],
            ),
        ],
        unresolved_regions=[
            UnresolvedRegion(
                region=SourceRegion(x0=800, y0=400, x1=900, y1=450),
                ink_pixels=12,
            )
        ],
    )


def test_resize_ir_preserves_physical_dimensions() -> None:
    out = resize_ir(_ir(), 500, 250)
    circle = out.entities[0]

    assert out.scale == 0.4
    assert circle.center == Point(x=250, y=125)
    assert circle.radius == 50
    assert circle.radius * out.scale == 20
    assert out.entities[1].height == 10
    assert out.entities[2].holes[0][2] == Point(x=15, y=15)
    assert out.unresolved_regions[0].region.x0 == 400


def test_fit_ir_to_long_side_never_upscales() -> None:
    original = _ir()
    out = fit_ir_to_long_side(original, 1600)

    assert out.source.image_width == 1000
    assert out is not original
