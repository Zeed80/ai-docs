from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

from eval_reader_fields import corpus_provenance


def test_corpus_provenance_does_not_call_synthetic_or_unknown_real() -> None:
    provenance = corpus_provenance([
        {"source": "synthetic"},
        {"source": "synthetic"},
        {},
    ])

    assert provenance == {
        "sheets": 3,
        "real_sheets": 0,
        "synthetic_sheets": 2,
        "unknown_source_sheets": 1,
        "by_source": {"synthetic": 2, "unknown": 1},
    }


def test_corpus_provenance_counts_only_explicit_real_source_labels() -> None:
    provenance = corpus_provenance([
        {"source": "hand_checked_real"},
        {"source": "public_real"},
        {"source": "realistic"},
    ])

    assert provenance["real_sheets"] == 2
    assert provenance["by_source"]["realistic"] == 1
