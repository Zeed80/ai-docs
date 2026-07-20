import io

import pytest

pytest.importorskip("cv2")


def test_inner_a4_frame_produces_true_paper_scale():
    import cv2
    import numpy as np

    from app.tasks.cad_trace import _scale_from_quad

    # A4 portrait inner frame: 185 × 287 mm. At 4 px/mm it is 740×1148 px.
    quad = np.array([[[0, 0]], [[739, 0]], [[739, 1147]], [[0, 1147]]], dtype=np.int32)

    scale, sheet_format = _scale_from_quad(quad, 740, 1148, "A4")

    assert sheet_format == "A4"
    assert scale == pytest.approx(0.25, rel=0.002)
    x, y, width, height = cv2.boundingRect(quad)
    assert (x, y, width, height) == (0, 0, 740, 1148)


def test_pdf_page_render_is_a_real_png():
    import fitz
    from PIL import Image

    from app.tasks.cad_trace import _pdf_page_to_png

    document = fitz.open()
    page = document.new_page(width=200, height=100)
    page.draw_line((10, 10), (190, 90))
    pdf = document.tobytes()
    document.close()

    png = _pdf_page_to_png(pdf, page_index=0, dpi=144)

    image = Image.open(io.BytesIO(png))
    assert image.format == "PNG"
    assert image.size == (400, 200)


def test_pdf_page_render_rejects_missing_page():
    import fitz

    from app.tasks.cad_trace import _pdf_page_to_png

    document = fitz.open()
    document.new_page()
    pdf = document.tobytes()
    document.close()

    with pytest.raises(ValueError, match="всего страниц: 1"):
        _pdf_page_to_png(pdf, page_index=1)
