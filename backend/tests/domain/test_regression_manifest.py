from __future__ import annotations

from pathlib import Path

try:
    from scripts.regression_manifest_check import validate_manifest
except ImportError:
    import pytest
    pytest.skip("scripts.regression_manifest_check not yet implemented", allow_module_level=True)


def test_example_invoices_manifest_is_valid() -> None:
    assert validate_manifest(Path("example-invoices/manifest.json")) == []


def test_drawing_samples_manifest_is_valid_placeholder() -> None:
    assert validate_manifest(Path("docs/drawing-samples-manifest.json")) == []


def test_technology_regression_manifest_is_valid() -> None:
    assert validate_manifest(Path("docs/technology-regression-manifest.json")) == []
