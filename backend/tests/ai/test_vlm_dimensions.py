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
