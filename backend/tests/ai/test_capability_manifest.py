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


def test_tech_capability_declares_material_domain():
    """Ф8.2: the tech capability's declared domain must match what
    tp_generator.material_group_with_confidence actually recognizes —
    documentation drift here would defeat the point of declaring it."""
    manifest = load_capability_manifest()
    domain = manifest.by_name["tech"].domain
    assert domain is not None
    assert set(domain["materials"]) == {
        "steel_carbon", "steel_alloy", "stainless", "aluminum", "cast_iron",
    }


def test_capability_without_domain_defaults_to_none(tmp_path: Path):
    path = tmp_path / "capabilities.yml"
    path.write_text("version: 2\ncapabilities:\n  - name: plain\n", encoding="utf-8")
    clear_capability_manifest_cache()
    manifest = load_capability_manifest(path)
    assert manifest.by_name["plain"].domain is None


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
