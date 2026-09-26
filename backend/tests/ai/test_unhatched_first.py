"""Виды вала для элементов лицом: без штриховки — первыми (корпус v10: паз
на разрезе полого вала опровергал верное чтение, не дойдя до вида под ним)."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageDraw

from app.ai.cad_recognize.verifiers.stage import unhatched_first


def test_the_view_without_hatching_goes_first():
    image = Image.new("L", (1400, 900), 255)
    draw = ImageDraw.Draw(image)
    # Разрез сверху: контур и штриховка 45°; вид снизу — контур без штриховки.
    draw.rectangle([100, 100, 1300, 300], outline=0, width=6)
    for offset in range(-200, 1400, 14):
        draw.line([(100 + offset, 300), (100 + offset + 200, 100)], fill=0, width=2)
    draw.rectangle([0, 0, 99, 900], fill=255)
    draw.rectangle([1301, 0, 1400, 900], fill=255)
    draw.rectangle([100, 100, 1300, 300], outline=0, width=6)
    draw.rectangle([100, 500, 1300, 700], outline=0, width=6)
    gray = np.asarray(image)
    section = SimpleNamespace(bbox_px=(95, 95, 1305, 305))
    plain = SimpleNamespace(bbox_px=(95, 495, 1305, 705))

    assert unhatched_first(gray, [section, plain]) == [plain, section]
    assert unhatched_first(gray, [plain, section]) == [plain, section]
