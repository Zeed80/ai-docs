from pathlib import Path

import pytest

from app.ai.capability_manifest import (
    CapabilityManifest,
    capability_schema_hash,
    clear_capability_manifest_cache,
    load_capability_manifest,
)


def test_live_capability_manifest_is_typed_and_has_gates():
    manifest = load_capability_manifest()

    assert isinstance(manifest, CapabilityManifest)
    assert manifest.by_name["invoices"].method == "POST"
    assert manifest.is_gated("invoices", "approve") is True
    assert manifest.is_gated("invoices", "list") is False


def test_capability_manifest_rejects_duplicate_names(tmp_path: Path):
    path = tmp_path / "capabilities.yml"
    path.write_text(
        "version: 2\ncapabilities:\n"
        "  - name: invoices\n"
        "  - name: invoices\n",
        encoding="utf-8",
    )

    clear_capability_manifest_cache()
    with pytest.raises(ValueError, match="Duplicate capability names"):
        load_capability_manifest(path)


def test_capability_schema_hash_changes_with_contract(tmp_path: Path):
    path = tmp_path / "capabilities.yml"
    path.write_text("version: 2\ncapabilities: []\n", encoding="utf-8")
    first = capability_schema_hash(path)

    path.write_text("version: 2\nmode: capabilities\ncapabilities: []\n", encoding="utf-8")
    second = capability_schema_hash(path)

    assert first != second
