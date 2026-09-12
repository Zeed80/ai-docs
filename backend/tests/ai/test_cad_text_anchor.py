"""Подпись размера: привязка центром и поворот — одинаково во всех рендерах.

Соглашение IR: `rotation` — градусы по часовой на листе (ось y вниз), как в
SVG; вертикальная подпись по ГОСТ 2.307 читается снизу вверх — это -90.
`anchor="middle"` — точка стоит в середине базовой линии.

До этого PNG-рендер поворачивал в обратную сторону (PIL крутит против часовой)
и вставлял плитку углом, а все рендеры читали центр подписи как её левый край.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

from app.ai.cad_ir.schema import CadIR, Point, SourceInfo, TextEntity


def _ir(**text) -> CadIR:
    return CadIR(
        source=SourceInfo(image_width=800, image_height=600),
        entities=[
            TextEntity(position=Point(x=400, y=300), text="Ø125", height=30, **text),
        ],
    )


def _ink_box(ir: CadIR) -> tuple[int, int, int, int]:
    from app.ai.cad_ir.png_render import render_ir_to_png

    rendered = render_ir_to_png(ir, draw_text=True)
    if isinstance(rendered, (bytes, bytearray)):
        grey = np.asarray(Image.open(io.BytesIO(rendered)).convert("L"))
    else:
        grey = np.asarray(rendered)
        if grey.ndim == 3:
            grey = grey.mean(axis=2)
    ys, xs = np.nonzero(grey < 128)
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def test_png_a_centred_label_is_centred():
    x0, _y0, x1, _y1 = _ink_box(_ir(anchor="middle"))
    assert abs((x0 + x1) / 2 - 400) <= 4


def test_png_vertical_text_reads_bottom_to_top_around_its_anchor():
    x0, y0, x1, y1 = _ink_box(_ir(anchor="middle", rotation=-90.0))

    assert y1 - y0 > x1 - x0  # стоит вертикально
    assert abs((y0 + y1) / 2 - 300) <= 4  # по центру вокруг точки
    # Снизу вверх — верх букв смотрит влево, значит чернила левее базовой линии.
    assert x1 <= 404 and x0 >= 360


def test_svg_carries_the_anchor_and_the_rotation():
    from app.ai.cad_ir.svg_render import render_ir_to_svg

    svg = render_ir_to_svg(_ir(anchor="middle", rotation=-90.0)).decode("utf-8")

    assert 'text-anchor="middle"' in svg
    assert "rotate(-90 400 300)" in svg


def test_dxf_centres_the_text_and_turns_it_counter_clockwise():
    import ezdxf
    from ezdxf.enums import TextEntityAlignment

    from app.ai.cad_ir.dxf_render import render_ir_to_dxf

    document = ezdxf.read(
        io.StringIO(render_ir_to_dxf(_ir(anchor="middle", rotation=-90.0)).decode("utf-8"))
    )
    text = next(entity for entity in document.modelspace() if entity.dxftype() == "TEXT")

    assert text.get_placement()[0] == TextEntityAlignment.BOTTOM_CENTER
    # DXF: ось y вверх, угол против часовой — снизу вверх это +90.
    assert text.dxf.rotation == 90.0


def test_a_start_anchored_text_is_unchanged():
    """Штамп и надписи — от левого края, как и были."""
    x0, _y0, _x1, _y1 = _ink_box(_ir())
    assert 398 <= x0 <= 406
