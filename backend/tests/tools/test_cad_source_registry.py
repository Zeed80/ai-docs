from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tools" / "cad-dataset" / "acquire_open_sources.py"
SPEC = importlib.util.spec_from_file_location("acquire_open_sources", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_registry_never_approves_noncommercial_source() -> None:
    sources = MODULE._read_registry(SCRIPT.with_name("source_registry.json"))
    approved = [source for source in sources.values() if source["status"].startswith("approved")]
    assert approved
    assert all(source["commercial_training"] for source in approved)
    assert sources["floorplancad"]["status"] == "quarantined"
    assert sources["archcad_400k"]["status"] == "quarantined"
    assert sources["sketchgraphs"]["status"] == "quarantined"
    assert sources["freecad_parts_library"]["license"] == "CC BY 3.0"
    assert sources["buildingsmart_sample_test_files"]["license"] == "CC BY 4.0"


def test_source_split_is_group_stable() -> None:
    assert MODULE._stable_split("source:part-1") == MODULE._stable_split("source:part-1")
    assert MODULE._stable_split("source:part-1") in {"train", "val", "holdout"}


def test_qcad_assets_require_allowlisted_sidecar_license(tmp_path: Path) -> None:
    public_domain = tmp_path / "part.rdf"
    public_domain.write_text(
        '<dcterms:license>http://creativecommons.org/publicdomain/mark/1.0/</dcterms:license>'
    )
    assert MODULE._rdf_license(public_domain) in MODULE.ALLOWED_RDF_LICENSES

    unknown = tmp_path / "unknown.rdf"
    unknown.write_text("<rdf>no license</rdf>")
    assert MODULE._rdf_license(unknown) is None


def test_registry_is_machine_readable() -> None:
    payload = json.loads(SCRIPT.with_name("source_registry.json").read_text())
    assert payload["schema_version"] == 1
    assert len(payload["sources"]) >= 8


def test_step_geometry_requires_real_step_topology() -> None:
    valid = (
        b"ISO-10303-21;\n"
        + b"#1=ADVANCED_FACE();" * 4
        + b"#2=EDGE_CURVE();" * 4
        + b"#3=ORIENTED_EDGE();" * 4
    )
    assert MODULE._step_geometry_count(valid) == 12
    assert MODULE._step_geometry_count(b"not a STEP file") == 0
