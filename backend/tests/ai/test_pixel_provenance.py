"""Pixel provenance: diffusion-added/removed ink detection and flags."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("cv2")

import cv2  # noqa: E402

from app.ai.cad_ir.schema import Point, Segment
from app.ai.pixel_provenance import (
    diffusion_change_masks,
    entities_in_mask,
    removed_regions,
)


def _png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def _sheet(extra_line: bool, missing_line: bool) -> np.ndarray:
    """White sheet with base geometry; optionally an added or removed stroke."""
    img = np.full((400, 500), 255, dtype=np.uint8)
    cv2.line(img, (50, 60), (450, 60), 0, 3)
    cv2.line(img, (50, 60), (50, 340), 0, 3)
    cv2.circle(img, (250, 200), 70, 0, 2)
    if not missing_line:
        cv2.line(img, (50, 340), (450, 340), 0, 3)
    if extra_line:
        cv2.line(img, (150, 120), (350, 120), 0, 3)  # hallucinated stroke
    return img


def test_masks_catch_added_and_removed_ink() -> None:
    source_png = _png(_sheet(extra_line=False, missing_line=False))
    result = _sheet(extra_line=True, missing_line=True)
    result_ink = cv2.bitwise_not(result)

    masks = diffusion_change_masks(result_ink, source_png)
    assert masks is not None
    added, removed = masks
    # the hallucinated line at y≈120 is in `added`
    assert added[115:126, 200:300].any()
    # the erased bottom line at y≈340 is in `removed`
    assert removed[334:347, 150:350].any()
    # untouched geometry is in neither mask
    assert not added[55:66, 100:400].any()

    boxes = removed_regions(removed)
    assert boxes, "стёртый участок должен дать bbox"
    x0, y0, x1, y1 = boxes[0]
    assert y0 > 300 and (x1 - x0) > 200


def test_entities_in_mask_flags_only_hallucinated_geometry() -> None:
    source_png = _png(_sheet(extra_line=False, missing_line=False))
    result = _sheet(extra_line=True, missing_line=False)
    result_ink = cv2.bitwise_not(result)
    added, _removed = diffusion_change_masks(result_ink, source_png)

    real = Segment(p1=Point(x=50, y=60), p2=Point(x=450, y=60))
    fake = Segment(p1=Point(x=150, y=120), p2=Point(x=350, y=120))
    flagged = entities_in_mask([real, fake], added, thin_px=2, thick_px=3)
    assert fake.id in flagged
    assert real.id not in flagged


def test_identical_images_produce_empty_masks() -> None:
    source_png = _png(_sheet(extra_line=False, missing_line=False))
    result_ink = cv2.bitwise_not(_sheet(extra_line=False, missing_line=False))
    added, removed = diffusion_change_masks(result_ink, source_png)
    assert int(added.sum()) == 0
    assert int(removed.sum()) == 0
