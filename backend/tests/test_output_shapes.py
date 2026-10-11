"""T2: the planner reads what actions answer instead of guessing paths."""

from __future__ import annotations


def test_shapes_name_the_fields_live_runs_guessed_wrong():
    from app.ai.output_shapes import action_output_shapes

    shapes = action_output_shapes()
    # Live 2026-10-10: result.supplier_id instead of result.results[0].id,
    # "results" instead of "items" for invoices.list.
    assert shapes["suppliers.search"].startswith("{results:[{id,")
    assert shapes["invoices.list"].startswith("{items:[{id,")
    assert "text" in shapes["documents.read"]


def test_every_shape_belongs_to_a_catalog_action_and_most_actions_have_one():
    from app.ai.output_shapes import action_output_shapes
    from app.ai.tool_catalog import TOOLS

    shapes = action_output_shapes()
    assert set(shapes) <= set(TOOLS)
    assert len(shapes) >= 200


def test_planner_catalog_carries_outputs():
    from app.domain.work_planning import _planner_catalog

    suppliers = next(c for c in _planner_catalog() if c["name"] == "suppliers")
    assert suppliers["outputs"]["search"].startswith("{results:")


def test_describe_wraps_list_answers_as_the_gateway_does():
    from pydantic import BaseModel

    from app.ai.output_shapes import describe

    class Item(BaseModel):
        id: str
        name: str

    assert describe(list[Item]) == "{items:[{id,name}],total}"
    assert describe(None) is None
