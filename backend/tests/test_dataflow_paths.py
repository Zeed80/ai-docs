"""A broken ${steps...} reference must tell the replan what is there.

Live 2026-10-10: a supplier search found nothing, the plan referenced
results[0].id, and the replan saw only "list index out of range" — three
revisions repeated the same search and the order blocked.
"""

from __future__ import annotations

import pytest

from app.domain.work_planning import DataflowPathError, _path_get

OUTPUT = {"result": {"results": [], "total": 0}, "executor": "capability"}


def test_an_empty_list_says_the_earlier_step_found_nothing():
    with pytest.raises(DataflowPathError, match="has 0 items — the earlier step found nothing"):
        _path_get(OUTPUT, "result.results[0].id")


def test_a_missing_field_lists_the_fields_that_exist():
    with pytest.raises(DataflowPathError, match=r"no field 'supplier_id'.*\['results', 'total'\]"):
        _path_get(OUTPUT, "result.supplier_id")


def test_a_present_path_still_resolves():
    assert _path_get({"result": {"results": [{"id": 7}]}}, "result.results[0].id") == 7
