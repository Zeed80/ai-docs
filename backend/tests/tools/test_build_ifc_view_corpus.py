from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tools" / "cad-dataset" / "build_ifc_view_corpus.py"
SPEC = importlib.util.spec_from_file_location("build_ifc_view_corpus", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_schema_variants_select_one_canonical_asset():
    rows = [
        {"source_group_id": "scene", "relative_path": "IFC4/scene.ifc", "split": "holdout"},
        {"source_group_id": "scene", "relative_path": "IFC4X3/scene.ifc", "split": "holdout"},
    ]
    selected, splits = MODULE._canonical_assets_and_splits(rows)
    assert len(selected) == 1
    assert selected[0]["relative_path"] == "IFC4X3/scene.ifc"
    assert splits == {"scene": "holdout"}


def test_group_split_preserves_canonical_source_manifest():
    rows = [
        {
            "source_group_id": f"scene-{index}",
            "relative_path": f"IFC4/{index}.ifc",
            "split": "train" if index < 10 else "val" if index < 12 else "holdout",
        }
        for index in range(14)
    ]
    selected, splits = MODULE._canonical_assets_and_splits(rows)
    assert len(selected) == 14
    assert list(splits.values()).count("train") == 10
    assert list(splits.values()).count("val") == 2
    assert list(splits.values()).count("holdout") == 2


def test_ifc_line_styles_preserve_visibility_section_and_semantic_weight():
    assert MODULE._line_style("IfcWall", "section", "visible") == ("contour", "main")
    assert MODULE._line_style("IfcWall", "feature", "hidden") == ("hidden", "thin")
    assert MODULE._line_style("IfcGrid", "feature", "visible") == ("axis", "thin")
    assert MODULE._line_style("IfcSpace", "boundary", "visible") == ("thin", "thin")
    assert MODULE._line_style("IfcDistributionElement", "feature", "visible") == ("thin", "thin")
    assert MODULE._line_style("IfcPipeSegment", "feature", "visible") == ("thin", "thin")
