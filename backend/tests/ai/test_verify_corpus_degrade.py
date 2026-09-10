"""Эталон обязан указывать туда, где предмет ОКАЗАЛСЯ после деградации.

Эталон, не пересчитанный вместе с растром, молча мерил бы проверяльщика не в
том месте — и порог измеримости (E2) вышел бы выдуманным. Проверка прямая:
чёрная метка в известной точке чистого листа, и после каждой ступени самое
тёмное место растра обязано быть там, куда эталон её перенёс.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.ai.verify_corpus.degrade import LADDER, degrade


def _sheet_with_mark(x: float, y: float) -> tuple[bytes, dict]:
    image = Image.new("RGB", (1200, 850), "white")
    # Метка крупная: при 75 dpi она сжимается вчетверо и обязана остаться меткой.
    ImageDraw.Draw(image).ellipse([x - 24, y - 24, x + 24, y + 24], fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    truth = {
        "image_size_px": [1200, 850],
        "px_per_part_mm": 11.81,
        "labels": [
            {"kind": "text", "text": "•", "bbox_px": [x - 24, y - 24, x + 24, y + 24]},
            {"kind": "dimension", "anchors_px": [[x, y], [x + 1, y]], "label": None},
        ],
    }
    return buffer.getvalue(), truth


def _darkest_blob_centre(png: bytes) -> tuple[float, float]:
    grey = np.asarray(Image.open(io.BytesIO(png)).convert("L")).astype(float)
    # Фото кладёт лист на тёмный стол — ищем метку только внутри светлого листа.
    mask = grey < 100
    paper = grey > 150
    from scipy import ndimage  # noqa: PLC0415

    labels, count = ndimage.label(mask)
    best = None
    for index in range(1, count + 1):
        blob = labels == index
        size = blob.sum()
        if not 20 <= size <= 20000:
            continue
        ys, xs = np.nonzero(blob)
        # Метка окружена бумагой; пятна стола и переплёта — нет.
        y0, y1 = max(0, ys.min() - 6), min(grey.shape[0], ys.max() + 7)
        x0, x1 = max(0, xs.min() - 6), min(grey.shape[1], xs.max() + 7)
        ring = paper[y0:y1, x0:x1].mean()
        if best is None or ring > best[0]:
            best = (ring, xs.mean(), ys.mean())
    assert best is not None, "метка не найдена"
    return best[1], best[2]


@pytest.mark.parametrize("step", LADDER, ids=[step["name"] for step in LADDER])
def test_the_truth_follows_the_mark_through_every_step(step):
    png, truth = _sheet_with_mark(700.0, 400.0)
    degraded, moved = degrade(png, truth, step, seed=3)

    expected = moved["labels"][1]["anchors_px"][0]
    found = _darkest_blob_centre(degraded)
    # Допуск — пара пикселей после перевода: интерполяция и JPEG размывают метку.
    assert abs(found[0] - expected[0]) <= 3.0 and abs(found[1] - expected[1]) <= 3.0, (
        step["name"],
        found,
        expected,
    )


def test_the_image_size_in_the_truth_matches_the_raster():
    png, truth = _sheet_with_mark(700.0, 400.0)
    for step in LADDER:
        degraded, moved = degrade(png, truth, step, seed=1)
        size = Image.open(io.BytesIO(degraded)).size
        assert list(size) == moved["image_size_px"], step["name"]


def test_a_resize_scales_the_part_scale_and_a_photo_drops_it():
    """После перспективы масштаб неравномерен — одного числа не существует."""
    png, truth = _sheet_with_mark(700.0, 400.0)
    _png, half = degrade(png, truth, {"name": "x", "scale": 0.5}, seed=1)
    _png, photo = degrade(png, truth, {"name": "photo", "photo": True}, seed=1)

    assert half["px_per_part_mm"] == pytest.approx(11.81 * 0.5)
    assert photo["px_per_part_mm"] is None
    assert photo["homography_from_clean"]


def test_the_clean_truth_is_not_mutated():
    png, truth = _sheet_with_mark(700.0, 400.0)
    before = repr(truth)
    degrade(png, truth, {"name": "x", "scale": 0.5}, seed=1)
    assert repr(truth) == before
