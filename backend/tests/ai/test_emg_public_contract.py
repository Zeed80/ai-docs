import json
from pathlib import Path

from app.domain.engineering_model_graph import EngineeringModelGraph, GraphPatch

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_checked_in_emg_examples_match_public_contract():
    minimal = EngineeringModelGraph.model_validate_json(
        (REPO_ROOT / "examples/emg/minimal-mechanical.emg.json").read_text()
    )
    full = EngineeringModelGraph.model_validate_json(
        (REPO_ROOT / "examples/emg/full-mechanical.emg.json").read_text()
    )
    patch = GraphPatch.model_validate_json(
        (REPO_ROOT / "examples/emg/human-correction.emg-patch.json").read_text()
    )

    assert minimal.canonical_sha256 == minimal.calculated_sha256()
    assert full.canonical_sha256 == full.calculated_sha256()
    assert patch.schema_version == "emg-patch/1.0"
    assert patch.base_sha256 == full.canonical_sha256


def test_checked_in_json_schemas_are_draft_2020_12_and_versioned():
    graph_schema = json.loads(
        (REPO_ROOT / "schemas/engineering-model-graph-1.0.schema.json").read_text()
    )
    patch_schema = json.loads((REPO_ROOT / "schemas/graph-patch-1.0.schema.json").read_text())

    assert graph_schema["$schema"].endswith("2020-12/schema")
    assert graph_schema["properties"]["schema_version"]["const"] == "emg/1.0"
    assert patch_schema["properties"]["schema_version"]["const"] == "emg-patch/1.0"
