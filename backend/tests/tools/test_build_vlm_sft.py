"""VLM SFT converter: CadIR -> isotropic 0..1000 primitive DSL."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tools" / "cad-dataset" / "build_vlm_sft.py"
sys.path.insert(0, str(ROOT / "backend"))
SPEC = importlib.util.spec_from_file_location("build_vlm_sft", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _ir():
    from app.ai.cad_ir.schema import (
        Arc, CadIR, Circle, Point, Polyline, Segment, SourceInfo, TextEntity,
    )

    # 2000x1000 sheet -> isotropic scale = 1000/2000 = 0.5.
    return CadIR(
        source=SourceInfo(image_width=2000, image_height=1000),
        entities=[
            Segment(p1=Point(x=0, y=0), p2=Point(x=200, y=100)),
            Circle(center=Point(x=400, y=200), radius=80),
            Arc(center=Point(x=600, y=200), radius=40, start_angle=0, end_angle=90),
            Polyline(points=[Point(x=0, y=0), Point(x=100, y=0), Point(x=100, y=100)], closed=True),
            TextEntity(position=Point(x=10, y=10), text="Ø50", height=20),  # ignored
        ],
    )


def test_ir_to_dsl_normalizes_isotropically_and_drops_text():
    dsl = MODULE.ir_to_dsl(_ir())
    assert dsl["lines"] == [[0, 0, 100, 50]]          # coords * 0.5
    assert dsl["circles"] == [[200, 100, 40]]         # radius scaled too (isotropic)
    assert dsl["arcs"] == [[300, 100, 20, 0, 90]]
    assert dsl["polylines"] == [{"pts": [[0, 0], [50, 0], [50, 50]], "closed": 1}]
    assert "texts" not in dsl  # geometry-only DSL


def test_sft_record_is_qwen_vl_conversation():
    record = MODULE._sft_record("/x/img.png", {"lines": [[1, 2, 3, 4]]})
    assert record["images"] == ["/x/img.png"]
    assert record["messages"][0]["role"] == "user"
    assert "<image>" in record["messages"][0]["content"]
    assert record["messages"][1]["role"] == "assistant"
    import json

    assert json.loads(record["messages"][1]["content"])["lines"] == [[1, 2, 3, 4]]
