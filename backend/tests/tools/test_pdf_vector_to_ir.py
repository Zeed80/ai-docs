from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tools" / "cad-dataset" / "pdf_vector_to_ir.py"
SPEC = importlib.util.spec_from_file_location("pdf_vector_to_ir", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_vector_pdf_produces_independent_raster_and_ir(tmp_path: Path) -> None:
    pdf_path = tmp_path / "drawing.pdf"
    document = fitz.open()
    page = document.new_page(width=300, height=200)
    shape = page.new_shape()
    for offset in range(6):
        shape.draw_rect(fitz.Rect(20 + offset * 5, 20 + offset * 4, 260 - offset * 3, 170))
    shape.finish(width=0.4, color=(0, 0, 0))
    shape.commit()
    page.insert_text((40, 100), "MECHANICAL TEST", fontsize=12)
    document.save(pdf_path)
    document.close()

    summary = MODULE.convert([pdf_path], tmp_path / "out", 144, "test_source")
    assert summary["pages"] == 1
    assert summary["entities"] >= 20
    assert len(list((tmp_path / "out" / "clean").glob("*.png"))) == 1
    assert len(list((tmp_path / "out" / "ir").glob("*.json"))) == 1
