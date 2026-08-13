from __future__ import annotations

import json
import pathlib
import sys

from pydantic import BaseModel

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

import live_emg_stack_regression as regression


class NestedEvidence(BaseModel):
    value: str


def test_evidence_serializer_handles_nested_pydantic_models():
    payload = json.loads(regression._evidence_bytes({"nested": NestedEvidence(value="ok")}))
    assert payload == {"nested": {"value": "ok"}}


def test_evidence_bundle_is_content_addressed(tmp_path):
    regression.ARTIFACT_DIR = tmp_path
    receipt = regression._write_evidence_bundle(
        "case-a", {"source.json": {"id": "source-a"}, "model.step": b"STEP"}
    )

    manifest = json.loads((tmp_path / "case-a" / "manifest.json").read_text())
    assert receipt["saved"] is True
    assert receipt["file_count"] == 2
    assert manifest["complete"] is True
    assert {item["path"] for item in manifest["files"]} == {"source.json", "model.step"}
