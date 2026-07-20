"""vlm_dimensions.py: response parsing + crop extraction (pure functions,
no live model needed)."""

from __future__ import annotations

import pytest

pytest.importorskip("PIL")

from app.ai.vlm_dimensions import _parse_response, crop_bytes_for_region


def test_parse_response_single_unambiguous_reading():
    raw = '{"readings": [{"text": "Ø18H7", "value_mm": 18.0, "kind": "diameter", "tolerance": "H7", "confidence": 0.95}]}'
    out = _parse_response(raw)
    assert len(out) == 1
    assert out[0]["text"] == "Ø18H7"
    assert out[0]["value_mm"] == 18.0
    assert out[0]["kind"] == "diameter"


def test_parse_response_multiple_readings_sorted_by_confidence():
    raw = """{"readings": [
        {"text": "Ø16", "value_mm": 16.0, "kind": "diameter", "confidence": 0.2},
        {"text": "Ø18", "value_mm": 18.0, "kind": "diameter", "confidence": 0.7},
        {"text": "M18", "value_mm": null, "kind": "thread", "confidence": 0.1}
    ]}"""
    out = _parse_response(raw)
    assert [r["text"] for r in out] == ["Ø18", "Ø16", "M18"]
    assert out[2]["value_mm"] is None


def test_parse_response_handles_markdown_fences():
    raw = '```json\n{"readings": [{"text": "R8", "kind": "radius", "confidence": 1.0}]}\n```'
    out = _parse_response(raw)
    assert len(out) == 1
    assert out[0]["text"] == "R8"
    assert out[0]["value_mm"] is None


def test_parse_response_clamps_confidence_and_ignores_garbage():
    raw = '{"readings": [{"text": "x", "confidence": 5.0}, {"confidence": 0.5}, "not-a-dict"]}'
    out = _parse_response(raw)
    assert len(out) == 1  # second entry has no "text", third isn't a dict
    assert out[0]["confidence"] == 1.0  # clamped


def test_parse_response_non_dict_or_missing_readings_is_empty():
    assert _parse_response("not json at all {{{") == []
    assert _parse_response('{"other_key": []}') == []
    assert _parse_response("[1, 2, 3]") == []


def test_crop_bytes_for_region_extracts_and_upscales():
    import io

    from PIL import Image

    img = Image.new("RGB", (400, 300), "white")
    for x in range(100, 150):
        img.putpixel((x, 100), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    image_bytes = buf.getvalue()

    class _Region:
        x0, y0, x1, y1 = 100, 90, 150, 110

    crop = crop_bytes_for_region(image_bytes, _Region())
    assert crop is not None
    cropped_img = Image.open(io.BytesIO(crop))
    # padding=12 on each side of a 50x20 region -> 74x44 before upscaling
    assert cropped_img.width >= 74
    assert cropped_img.height >= 44


def test_crop_bytes_for_region_returns_none_on_degenerate_region():
    import io

    from PIL import Image

    img = Image.new("RGB", (100, 100), "white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    class _BadRegion:
        x0, y0, x1, y1 = 500, 500, 500, 500  # entirely outside the image

    assert crop_bytes_for_region(buf.getvalue(), _BadRegion()) is None


def test_parse_json_array_tolerates_fences_and_trailing_text():
    from app.ai.vlm_dimensions import _parse_json_array

    assert _parse_json_array('[{"text": "A", "bbox": [1, 2, 3, 4]}]') == [
        {"text": "A", "bbox": [1, 2, 3, 4]}
    ]
    fenced = '```json\n[{"text": "M20", "bbox": [0, 0, 5, 5]}]\n```'
    assert _parse_json_array(fenced)[0]["text"] == "M20"
    # An object (not an array) is not a grounding response.
    assert _parse_json_array('{"text": "A"}') == []
    assert _parse_json_array("no json here") == []


def test_read_sheet_text_entities_maps_tile_boxes_to_sheet(monkeypatch):
    import asyncio
    import io

    from PIL import Image

    import app.ai.vlm_dimensions as vd

    class _Resp:
        text = '[{"text": "A1", "bbox": [10, 20, 30, 40]}]'

    class _Router:
        async def run(self, request):
            return _Resp()

    # A sheet small enough to be a single tile, so the reported box needs no
    # tile offset but is divided back by the legibility upscale factor.
    image = Image.new("L", (600, 400), color=255)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    entities = asyncio.get_event_loop().run_until_complete(
        vd.read_sheet_text_entities(buffer.getvalue(), router=_Router())
    )

    assert len(entities) == 1
    entity = entities[0]
    assert entity.text == "A1"
    assert entity.origin == "vlm"
    # qwen3-vl grounds in a per-axis 0..1000 space: map by the tile's own
    # dimensions (600×400 single tile). The blank sheet has no ink, so the
    # ink-snap leaves the mapped position untouched.
    assert entity.position.x == pytest.approx(10 / 1000 * 600, abs=1.0)  # baseline-left x
    assert entity.position.y == pytest.approx(40 / 1000 * 400, abs=1.0)  # baseline y (box bottom)
    assert entity.source_region is not None


def test_snap_text_to_ink_tightens_coarse_box_onto_glyph():
    pytest.importorskip("cv2")
    import io

    from PIL import Image, ImageDraw, ImageFont

    from app.ai.cad_ir.schema import Point, SourceRegion, TextEntity
    from app.ai.vlm_dimensions import _snap_text_to_ink

    gray = Image.new("L", (300, 200), color=255)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 30)
    ImageDraw.Draw(gray).text((150, 90), "7", fill=0, font=font)
    # Bounding box of the ink itself (black glyph), not the white background.
    ink_box = gray.point(lambda v: 255 if v < 128 else 0).getbbox()

    # A VLM read whose box is ~18px up-left of the real glyph.
    entity = TextEntity(
        position=Point(x=132, y=105),
        text="7",
        height=30,
        source_region=SourceRegion(x0=132, y0=75, x1=150, y1=105),
    )
    _snap_text_to_ink([entity], gray)

    # Snapped baseline-left now sits on the glyph ink, not the coarse box.
    assert entity.position.x == pytest.approx(ink_box[0], abs=3)
    assert entity.position.y == pytest.approx(ink_box[3], abs=3)


def test_normalize_text_sizes_clamps_giants_and_drops_monsters():
    from app.ai.cad_ir.schema import Point, SourceRegion, TextEntity
    from app.ai.vlm_dimensions import _normalize_text_sizes

    def _t(text, h):
        return TextEntity(
            position=Point(x=100, y=200), text=text, height=h,
            source_region=SourceRegion(x0=100, y0=200 - h, x1=100 + 5 * h, y1=200),
        )

    normal = [_t(f"d{i}", 20) for i in range(9)]  # median height 20
    giant = _t("Ø80js6", 60)     # 3x median -> clamped to 40
    monster = _t("garbage", 200)  # 10x median -> dropped

    out = _normalize_text_sizes_wrapper(normal + [giant, monster])
    texts = {e.text: e for e in out}
    assert "garbage" not in texts            # monster dropped
    assert texts["Ø80js6"].height == 40.0    # giant clamped to 2x median
    assert all(e.height <= 40.0 for e in out)


def _normalize_text_sizes_wrapper(entities):
    from app.ai.vlm_dimensions import _normalize_text_sizes

    _normalize_text_sizes(entities)
    return entities
