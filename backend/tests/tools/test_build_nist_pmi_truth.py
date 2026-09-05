from __future__ import annotations

import importlib.util
import pathlib

import pytest
from openpyxl import Workbook

SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parents[3] / "tools/cad-dataset/build_nist_pmi_truth.py"
)
SPEC = importlib.util.spec_from_file_location("build_nist_pmi_truth", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _workbook(path: pathlib.Path, suite: str = "CTC") -> pathlib.Path:
    directory = path / f"NIST_MBE_PMI_{suite}_Definitions"
    directory.mkdir(parents=True)
    workbook_path = directory / f"NIST-{suite}-PMI-Definitions.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append([])
    sheet.append(
        [
            f"Primary {suite}",
            "Primary ATC",
            "PMI Category",
            "Description",
            "Specification",
            "Measurand",
            "Comments",
            "Standards",
        ]
    )
    sheet.append(
        [
            1,
            7,
            "Geometric Tolerances",
            "Profile tolerance",
            "⌓ | 0.5 | A",
            "surface",
            "official",
            "Y14.5",
        ]
    )
    workbook.save(workbook_path)
    return workbook_path


def test_parse_workbook_preserves_source_spelling_and_does_not_invent_links(tmp_path):
    workbook = _workbook(tmp_path)
    (workbook.parent / "nist_ctc_01_asme1_rd.stp").write_bytes(b"STEP")
    (workbook.parent / "nist_ctc_01_asme1_rd.pdf").write_bytes(b"PDF")
    records = MODULE.parse_workbook(workbook, source_root=tmp_path)

    assert len(records) == 1
    record = records[0]
    assert record["semantic_id"] == "nist:ctc:01:atc:7"
    assert record["specification"] == "⌓ | 0.5 | A"
    assert record["normalized"]["specification"] == "⌓ | 0.5 | a"
    assert record["assurance"]["semantic_status"] == "source_defined"
    assert record["assurance"]["geometry_linked"] is False
    assert record["assurance"]["drawing_located"] is False
    assert record["evidence"]["step_assets"] == [
        "NIST_MBE_PMI_CTC_Definitions/nist_ctc_01_asme1_rd.stp"
    ]
    assert record["evidence"]["step_scopes"].popitem()[1] == "unspecified"


def test_validator_fails_closed_on_duplicate_ids(tmp_path):
    workbook = _workbook(tmp_path)
    record = MODULE.parse_workbook(workbook, source_root=tmp_path)[0]
    with pytest.raises(ValueError, match="duplicate PMI semantic_id"):
        MODULE.validate_records([record, record])


def test_validator_rejects_claimed_geometry_link_without_targets(tmp_path):
    workbook = _workbook(tmp_path)
    record = MODULE.parse_workbook(workbook, source_root=tmp_path)[0]
    record["assurance"]["geometry_linked"] = True
    with pytest.raises(ValueError, match="claims geometry link"):
        MODULE.validate_records([record])


def test_summary_is_explicitly_not_promotion_ready_without_associations(tmp_path):
    workbook = _workbook(tmp_path)
    records = MODULE.parse_workbook(workbook, source_root=tmp_path)
    summary = MODULE.summarize(records)
    assert summary["records"] == 1
    assert summary["geometry_linked"] == 0
    assert summary["drawing_located"] == 0
    assert summary["promotion_ready"] is False
