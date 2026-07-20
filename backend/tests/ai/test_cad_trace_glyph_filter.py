"""Post-trace removal of in-glyph segments (pure function, no DB/model)."""

from __future__ import annotations

from app.ai.cad_ir.schema import Point, Segment, SourceRegion, TextEntity
from app.tasks.cad_trace import _drop_in_glyph_segments


def _text(x0, y0, x1, y1) -> TextEntity:
    return TextEntity(
        position=Point(x=x0, y=y1),
        text="12",
        height=y1 - y0,
        source_region=SourceRegion(x0=x0, y0=y0, x1=x1, y1=y1),
    )


def test_drops_stroke_fully_inside_glyph_but_keeps_crossing_line():
    label = _text(100, 100, 130, 120)
    glyph_stroke = Segment(p1=Point(x=105, y=105), p2=Point(x=125, y=115))
    crossing_line = Segment(p1=Point(x=0, y=110), p2=Point(x=400, y=110))
    body_line = Segment(p1=Point(x=200, y=300), p2=Point(x=600, y=300))

    kept = _drop_in_glyph_segments([glyph_stroke, crossing_line, body_line], [label])

    kept_ids = {id(e) for e in kept}
    assert id(glyph_stroke) not in kept_ids   # entirely inside the label box
    assert id(crossing_line) in kept_ids       # extends beyond the box
    assert id(body_line) in kept_ids           # far from any text


def test_no_text_keeps_everything():
    seg = Segment(p1=Point(x=1, y=1), p2=Point(x=2, y=2))
    assert _drop_in_glyph_segments([seg], []) == [seg]


def test_oversized_box_and_long_line_are_never_deleted():
    # A mis-snapped label box that swallowed geometry (huge height vs the
    # sheet's typical text) must not delete the shaft body line inside it.
    normal = [_text(100 + 20 * i, 100, 115 + 20 * i, 118) for i in range(5)]  # h=18 each
    huge = _text(200, 200, 700, 460)  # h=260, ~14x the median — a mis-snap
    body = Segment(p1=Point(x=220, y=330), p2=Point(x=680, y=330))  # long line inside it
    glyph = Segment(p1=Point(x=104, y=104), p2=Point(x=112, y=114))  # real glyph stroke

    kept = _drop_in_glyph_segments([body, glyph], normal + [huge])
    kept_ids = {id(e) for e in kept}
    assert id(body) in kept_ids       # long line survives (guarded box + length)
    assert id(glyph) not in kept_ids  # a genuine short stroke is still removed
