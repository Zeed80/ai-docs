"""A missing value is a question, not the end of the sheet.

But a follow-up may only RECOVER what the drawing carries: an answer that
matches no callout is the model filling in a blank, and filling in blanks
silently is the one thing this pipeline must never do.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.ai.cad_recognize.spec_followup import (
    _missing_values,
    _neighbour_context,
    resolve_missing_dimensions,
)


def _sheet_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (1200, 800), "white").save(buffer, format="PNG")
    return buffer.getvalue()


_SPEC = {
    "part": "Вал",
    "main_view": {
        "type": "тело вращения (вал)",
        "outer": [
            {"diameter_mm": 40.0, "length_mm": 150.0},
            {"diameter_mm": 60.0, "length_mm": None},
            {"diameter_mm": 40.0, "length_mm": 120.0},
        ],
    },
    "dimensions": [
        {"value": "Ø40"}, {"value": "Ø60"},
        {"value": "150"}, {"value": "200"}, {"value": "120"}, {"value": "470"},
    ],
}


def test_the_missing_value_is_found_and_named():
    gaps = _missing_values(_SPEC)
    assert [(path, field) for path, field, _n, _b in gaps] == [
        ("main_view.outer.1", "length_mm")
    ]


def test_the_question_points_at_a_place_on_the_sheet():
    """"Step 3" means nothing; "between Ø40 and Ø60" is somewhere to look."""
    context = _neighbour_context(_SPEC["main_view"], "outer", 1)
    assert "Ø40" in context and "справа" in context


@pytest.mark.asyncio
async def test_an_answer_the_sheet_carries_is_accepted(monkeypatch):
    asked: list[str] = []

    async def fake_ask(prompt, _image, **_kw):
        asked.append(prompt)
        return {"length_mm": 200.0}

    monkeypatch.setattr("app.ai.cad_recognize.spec_fragments._ask", fake_ask)
    spec, log = await resolve_missing_dimensions(
        _sheet_bytes(), _SPEC, router=object()
    )

    assert spec["main_view"]["outer"][1]["length_mm"] == 200.0
    assert log[0]["accepted"] is True
    # The question names the step by its neighbours and lists the sheet's own
    # axial callouts — that is what makes it an easier question than the first.
    assert "Ø60" in asked[0] and "150" in asked[0]
    # And the value carries where it came from.
    assert spec["provenance"]["main_view.outer.1.length_mm"]["origin"] == "vlm_followup"
    # The input spec is untouched: the pair is the audit.
    assert _SPEC["main_view"]["outer"][1]["length_mm"] is None


@pytest.mark.asyncio
async def test_an_answer_no_callout_supports_is_refused(monkeypatch):
    async def fake_ask(*_a, **_kw):
        return {"length_mm": 173.0}  # a number that is nowhere on this sheet

    monkeypatch.setattr("app.ai.cad_recognize.spec_fragments._ask", fake_ask)
    spec, log = await resolve_missing_dimensions(
        _sheet_bytes(), _SPEC, router=object()
    )

    assert spec["main_view"]["outer"][1]["length_mm"] is None
    assert log[0]["accepted"] is False
    assert "выноской" in log[0]["reason"]
    assert log[0]["answer_mm"] == 173.0  # recorded, not hidden


@pytest.mark.asyncio
async def test_a_reader_that_cannot_answer_leaves_the_gap(monkeypatch):
    async def fake_ask(*_a, **_kw):
        return {"length_mm": None}

    monkeypatch.setattr("app.ai.cad_recognize.spec_fragments._ask", fake_ask)
    spec, log = await resolve_missing_dimensions(
        _sheet_bytes(), _SPEC, router=object()
    )

    assert spec["main_view"]["outer"][1]["length_mm"] is None
    assert log[0]["accepted"] is False
    assert "provenance" not in spec


@pytest.mark.asyncio
async def test_followup_without_independent_callouts_is_refused(monkeypatch):
    async def fake_ask(*_a, **_kw):
        return {"length_mm": 200.0}

    monkeypatch.setattr("app.ai.cad_recognize.spec_fragments._ask", fake_ask)
    no_callouts = {
        "main_view": {"outer": [{"diameter_mm": 60.0, "length_mm": None}]},
        "dimensions": [],
    }
    spec, log = await resolve_missing_dimensions(
        _sheet_bytes(), no_callouts, router=object()
    )
    assert spec["main_view"]["outer"][0]["length_mm"] is None
    assert log[0]["accepted"] is False
    assert "нет независимо прочитанных выносок" in log[0]["reason"]


@pytest.mark.asyncio
async def test_a_complete_spec_asks_nothing(monkeypatch):
    async def fake_ask(*_a, **_kw):  # pragma: no cover — must not run
        raise AssertionError("a complete spec must not be questioned")

    monkeypatch.setattr("app.ai.cad_recognize.spec_fragments._ask", fake_ask)
    complete = {
        "main_view": {"outer": [{"diameter_mm": 40.0, "length_mm": 150.0}]},
    }
    spec, log = await resolve_missing_dimensions(
        _sheet_bytes(), complete, router=object()
    )
    assert spec is complete and log == []


def test_a_recovered_value_stops_blocking_the_build():
    """The contract APPENDS its codes, so unresolved has to be re-derived.

    Without this, a length a follow-up just recovered keeps its stale
    "length-missing" entry and blocks the part it was meant to unblock.
    """
    from app.tasks.cad_trace import _revalidated_spec

    spec = {
        "schema_version": 1,
        "part": "Вал",
        "main_view": {
            "type": "тело вращения (вал)",
            "outer": [
                {"diameter_mm": 40.0, "length_mm": 150.0},
                {"diameter_mm": 60.0, "length_mm": 200.0},
            ],
        },
        "unresolved": [
            "body:0:outer:1:length-missing",
            "расточка: диаметров нет среди прочитанных выносок",
        ],
        "provenance": {"main_view.outer.1.length_mm": {"origin": "vlm_followup"}},
        "consensus": {"passes": 3},
    }
    revalidated = _revalidated_spec(spec)

    # The generated code is gone — the value it complained about now exists.
    assert revalidated["unresolved"] == [
        "расточка: диаметров нет среди прочитанных выносок"
    ]
    # Everything the contract does not know about survives the round trip.
    assert revalidated["provenance"] == spec["provenance"]
    assert revalidated["consensus"] == spec["consensus"]


@pytest.mark.asyncio
async def test_a_plate_is_asked_for_its_thickness(monkeypatch):
    asked: list[str] = []

    async def fake_ask(prompt, _image, **_kw):
        asked.append(prompt)
        return {"thickness_mm": 20.0}

    monkeypatch.setattr("app.ai.cad_recognize.spec_fragments._ask", fake_ask)
    plate = {
        "main_view": {
            "type": "фланец",
            "profile": {"shape": "circle", "diameter_mm": 560.0},
        },
        "dimensions": [{"value": "Ø560"}, {"value": "20"}],
    }
    spec, log = await resolve_missing_dimensions(
        _sheet_bytes(), plate, router=object()
    )
    assert spec["main_view"]["profile"]["thickness_mm"] == 20.0
    assert log[0]["accepted"] is True
    assert "ТОЛЩИНА" in asked[0]
