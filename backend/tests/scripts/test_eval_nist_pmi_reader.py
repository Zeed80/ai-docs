from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

from eval_nist_pmi_reader import candidates_from_spec, page_truth_index, select_pages


def _truth():
    return {
        "semantic_id": "nist:ftc:06:atc:52",
        "evidence": {
            "drawing_page_candidates": [
                {"asset": "suite/nist_ftc_06_asme1_rd.pdf", "page": 1}
            ]
        },
    }


def test_page_truth_index_uses_official_page_membership():
    assert page_truth_index([_truth()]) == {
        "nist_ftc_06_asme1_rd_p01.png": [_truth()]
    }


def test_select_pages_never_substitutes_missing_images(tmp_path):
    assert select_pages([_truth()], tmp_path, None) == []
    image = tmp_path / "nist_ftc_06_asme1_rd_p01.png"
    image.write_bytes(b"PNG")
    assert select_pages([_truth()], tmp_path, 1) == [(image, [_truth()])]


def test_candidate_adapter_keeps_observation_unverified():
    spec = {
        "dimensions": [{"value": "2X S⌀ 1.250 ± .008", "evidence": [{"bbox": [1, 2, 3, 4]}]}],
        "annotations": [{"kind": "datum", "text": "A-B", "evidence": []}],
    }
    candidates = candidates_from_spec(spec, "ftc", "06")
    assert candidates[0]["category"] == "Directly Toleranced Dimensions & Dimension Symbols"
    assert candidates[0]["assurance"] == {
        "semantic_status": "observed",
        "geometry_linked": False,
        "drawing_located": True,
    }
    assert candidates[1]["category"] == "Datum Features, Datum Targets, Datum Reference Frames"
    assert candidates[1]["assurance"]["drawing_located"] is False
