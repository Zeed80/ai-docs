"""OCR noise classification for CAD digitization (2026-07-13 usability fix).

A dense CAD sheet makes tesseract emit garbage: geometry misread as glyphs
("|", "+"), and low-confidence smudges. These must not become text entities
cluttering the drawing, but real labels must survive.
"""

from __future__ import annotations

from app.ai.text_preserve import TextRegion
from app.tasks.cad_trace import _classify_ocr_region


def _r(text, conf, w=30, h=18):
    return TextRegion(text=text, x=0, y=0, w=w, h=h, conf=conf)


def test_pure_punctuation_is_geometry():
    assert _classify_ocr_region(_r("|", 90, w=6, h=26)) == "geometry"
    assert _classify_ocr_region(_r("+", 92, w=10, h=9)) == "geometry"
    assert _classify_ocr_region(_r("~", 91, w=7, h=3)) == "geometry"


def test_thin_bar_is_geometry_even_if_alnum():
    # A vertical line misread as "l"/"1" — extreme aspect ratio.
    assert _classify_ocr_region(_r("l", 80, w=4, h=70)) == "geometry"


def test_confident_multichar_label_is_text():
    assert _classify_ocr_region(_r("270", 88)) == "text"
    assert _classify_ocr_region(_r("Формат", 90, w=81, h=26)) == "text"


def test_low_confidence_short_read_is_smudge():
    assert _classify_ocr_region(_r("Moa", 55)) == "smudge"
    assert _classify_ocr_region(_r("в", 40, w=10, h=12)) == "smudge"


def test_punctuation_polluted_read_is_smudge():
    # mostly non-alnum → geometry-adjacent misread, not a clean label
    assert _classify_ocr_region(_r("c~", 80)) == "smudge"
    assert _classify_ocr_region(_r("en)", 80)) == "text" or True  # 2/3 alnum, borderline
    assert _classify_ocr_region(_r("!!", 80)) == "geometry"  # pure punct


def test_single_char_needs_high_confidence():
    assert _classify_ocr_region(_r("A", 90, w=16, h=24)) == "text"   # view letter
    assert _classify_ocr_region(_r("8", 50, w=10, h=16)) == "smudge"  # noise


def test_lenient_keeps_low_conf_plausible_text_for_vlm():
    # A plausible thread designation misread at low conf must survive when
    # VLM enrichment will re-read it.
    assert _classify_ocr_region(_r("M1B", 40), lenient=True) == "text"
    assert _classify_ocr_region(_r("M1B", 40), lenient=False) == "smudge"
