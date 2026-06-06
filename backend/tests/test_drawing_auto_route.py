"""Regression: only unambiguous vector/CAD formats auto-route to the heavy
drawing-analysis pipeline on ingest. Raster/PDF (invoices, letters, scans) must
NOT — auto-routing every raster/PDF funnelled all uploaded invoices into VLM
drawing analysis, which monopolised the single-flight GPU lane and stalled OCR.
"""

from app.api.documents import DRAWING_AUTO_ROUTE_EXTENSIONS


def test_cad_vector_formats_auto_route():
    for ext in ("dwg", "dxf", "step", "stp", "iges", "svg"):
        assert ext in DRAWING_AUTO_ROUTE_EXTENSIONS


def test_raster_and_pdf_do_not_auto_route():
    # These are classified first; a drawing is created only if classify_document
    # detects doc_type=drawing.
    for ext in ("pdf", "jpg", "jpeg", "png", "bmp", "tiff", "webp"):
        assert ext not in DRAWING_AUTO_ROUTE_EXTENSIONS
