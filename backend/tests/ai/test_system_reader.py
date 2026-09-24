"""Tests for app.ai.system_reader (Ф5.3 — P&ID/MEP/electrical/hydraulic reader).

system_read_as_model is pure/deterministic given a read sheet; the
VLM-facing half is only smoke-tested against a fake router (see the
module's own docstring on why: no real schematic corpus in this repo yet).
"""

from __future__ import annotations

import pytest

from app.ai.system_reader import (
    SystemSheetRead,
    read_system_diagram,
    system_read_as_model,
)


def _sheet(**overrides) -> SystemSheetRead:
    base = {
        "name": "Схема отопления",
        "system_kind": "отопление",
        "equipment": [
            {"id": "e1", "name": "Котёл", "equipment_type": "boiler"},
            {"id": "e2", "name": "Радиатор", "equipment_type": "radiator"},
        ],
        "ports": [
            {
                "id": "p1",
                "equipment_id": "e1",
                "kind": "supply",
                "direction": "out",
                "medium": "вода",
            },
            {
                "id": "p2",
                "equipment_id": "e2",
                "kind": "inlet",
                "direction": "in",
                "medium": "вода",
            },
        ],
        "connections": [
            {"id": "c1", "first_port_id": "p1", "second_port_id": "p2"},
        ],
    }
    base.update(overrides)
    return SystemSheetRead.model_validate(base)


def test_compatible_connection_builds():
    model, report = system_read_as_model(_sheet(), profile="mep")
    assert model is not None, report
    assert report["equipment_built"] == 2
    assert report["ports_built"] == 2
    assert report["connections_built"] == 1
    assert report["skipped"] == []
    assert report["unresolved_required_ports"] == []


def test_incompatible_medium_connection_is_excluded_individually():
    sheet = _sheet(
        ports=[
            {
                "id": "p1",
                "equipment_id": "e1",
                "kind": "supply",
                "direction": "out",
                "medium": "вода",
            },
            {
                "id": "p2",
                "equipment_id": "e2",
                "kind": "inlet",
                "direction": "in",
                "medium": "пар",
            },
        ],
    )
    model, report = system_read_as_model(sheet, profile="mep")
    assert model is not None, report
    assert report["connections_built"] == 0
    skipped = report["skipped"][0]
    assert skipped["id"] == "c1"
    assert skipped["reason"] == "incompatible_or_overloaded"
    # The rest of the model (equipment/ports) still builds despite the bad connection.
    assert report["ports_built"] == 2


def test_port_on_unknown_equipment_is_excluded():
    sheet = _sheet(
        ports=[
            {
                "id": "p1",
                "equipment_id": "does-not-exist",
                "kind": "supply",
                "direction": "out",
                "medium": "вода",
            },
            {
                "id": "p2",
                "equipment_id": "e2",
                "kind": "inlet",
                "direction": "in",
                "medium": "вода",
            },
        ],
        connections=[],
    )
    model, report = system_read_as_model(sheet, profile="mep")
    assert model is not None, report
    assert report["ports_built"] == 1
    assert {"id": "p1", "kind": "port", "reason": "unknown_equipment"} in report["skipped"]


def test_required_unconnected_port_is_reported_unresolved():
    sheet = _sheet(
        ports=[
            {
                "id": "p1",
                "equipment_id": "e1",
                "kind": "supply",
                "direction": "out",
                "medium": "вода",
                "required_connection": True,
            },
            {
                "id": "p2",
                "equipment_id": "e2",
                "kind": "inlet",
                "direction": "in",
                "medium": "вода",
                "required_connection": True,
            },
        ],
        connections=[],
    )
    model, report = system_read_as_model(sheet, profile="mep")
    assert model is not None, report
    assert sorted(report["unresolved_required_ports"]) == ["p1", "p2"]


def test_empty_equipment_blocks_with_reason():
    sheet = _sheet(equipment=[], ports=[], connections=[])
    model, report = system_read_as_model(sheet, profile="mep")
    assert model is None
    assert report["blocked"] is True
    assert report["blocked_reason"] == "system_model_validation_failed"


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeRouter:
    def __init__(self, text: str) -> None:
        self._text = text
        self.requests: list = []

    async def run(self, request):
        self.requests.append(request)
        return _FakeResponse(self._text)


@pytest.mark.asyncio
async def test_read_system_diagram_wires_vlm_json_through_to_a_model():
    payload = """{
      "name": "Схема", "system_kind": "отопление",
      "equipment": [{"id": "e1", "name": "Котёл", "equipment_type": "boiler"}],
      "ports": [
        {"id": "p1", "equipment_id": "e1", "kind": "supply", "direction": "out", "medium": "вода"}
      ],
      "connections": []
    }"""
    router = _FakeRouter(payload)
    model, report = await read_system_diagram(b"fake-image-bytes", profile="mep", router=router)
    assert model is not None, report
    assert len(model.equipment) == 1
    assert router.requests, "the VLM router was never called"


@pytest.mark.asyncio
async def test_read_system_diagram_fails_closed_on_garbage_response():
    router = _FakeRouter("не могу прочитать схему")
    model, report = await read_system_diagram(b"fake-image-bytes", profile="pid", router=router)
    assert model is None
    assert report == {"read_failed": True}


def test_the_same_line_is_one_medium_whatever_the_model_appends_to_it():
    """Живой P&ID (бак T001 → насос P001, Wikimedia): модель писала у концов
    одной линии «растворитель (solvent)» и «растворитель (solvent), 5 m³/h» —
    строки разные, и ни одна связь на трёх реальных листах не построилась."""
    sheet = _sheet(
        ports=[
            {
                "id": "p1",
                "equipment_id": "e1",
                "kind": "outlet",
                "direction": "out",
                "medium": "растворитель (solvent)",
            },
            {
                "id": "p2",
                "equipment_id": "e2",
                "kind": "suction",
                "direction": "in",
                "medium": "растворитель (solvent), 5 m³/h",
                "line": "01-100-PE-N",
            },
        ],
    )

    model, report = system_read_as_model(sheet, profile="pid")

    assert model is not None, report
    assert report["connections_built"] == 1
    assert {port.medium for port in model.ports} == {"растворитель"}
    # Сказанное моделью не теряется — оно в отчёте.
    assert report["medium_details"]["p2"] == "растворитель (solvent), 5 m³/h; 01-100-PE-N"


def test_a_nozzle_that_branches_through_a_tee_keeps_both_lines():
    """Живой P&ID RI Sample: нижний штуцер бака через тройник питает два
    насоса; порт принимал одну связь, и вторая ветвь отбрасывалась молча."""
    sheet = _sheet(
        equipment=[
            {"id": "e1", "name": "Бак", "equipment_type": "tank"},
            {"id": "e2", "name": "Насос 1", "equipment_type": "pump"},
            {"id": "e3", "name": "Насос 2", "equipment_type": "pump"},
        ],
        ports=[
            {
                "id": "p1",
                "equipment_id": "e1",
                "kind": "outlet",
                "direction": "out",
                "medium": "нафта",
                "branches": 2,
            },
            {
                "id": "p2",
                "equipment_id": "e2",
                "kind": "suction",
                "direction": "in",
                "medium": "нафта",
            },
            {
                "id": "p3",
                "equipment_id": "e3",
                "kind": "suction",
                "direction": "in",
                "medium": "нафта",
            },
        ],
        connections=[
            {"id": "c1", "first_port_id": "p1", "second_port_id": "p2"},
            {"id": "c2", "first_port_id": "p1", "second_port_id": "p3"},
        ],
    )

    model, report = system_read_as_model(sheet, profile="pid")

    assert model is not None, report
    assert report["connections_built"] == 2
