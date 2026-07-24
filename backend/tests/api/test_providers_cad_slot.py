import pytest

from app.ai.model_registry import ModelRegistry
from app.ai.schemas import AITask
from app.ai.task_routing import TaskRouting
from app.api.providers_api import _SLOTS, _apply_slot_assignment


def test_cad_reader_assignment_drops_legacy_generic_vlm_tail(monkeypatch):
    registry = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
    current = TaskRouting(
        task="cad_spec_read",
        models=[
            "qwen3_6_27b_qwopus_ollama",
            "qwen3_vl_32b_ollama",
            "gemma4_e4b_ollama",
        ],
    )
    saved = {}

    monkeypatch.setattr(
        "app.ai.task_routing.get_routing_for",
        lambda _task: current,
    )
    monkeypatch.setattr(
        "app.ai.task_routing.save_task_routing",
        lambda task, routing: saved.update(task=task, routing=routing),
    )

    _apply_slot_assignment("cad_spec_read", "qwen3_6_35b_apex_ollama", registry)

    assert saved["routing"].models == [
        "qwen3_6_35b_apex_ollama",
        "gemma4_e4b_ollama",
    ]


def test_digitize_group_exposes_only_the_two_working_spec_slots():
    """The experimental whole-sheet graph pipeline (layout / fragment /
    evidence-verify / legacy read) is deliberately NOT surfaced as user slots —
    it runs opt-in on its model_registry.yaml fallback defaults. Only the two
    production «По описанию» slots remain configurable."""
    digitize_slots = {slot for slot, group, *_ in _SLOTS if group == "Оцифровка"}
    assert digitize_slots == {"cad_spec_read", "cad_spec_draft"}
    assert not any(slot.startswith("cad_drawing_graph") for slot, *_ in _SLOTS)


def test_drawing_graph_reader_defaults_to_no_thinking(monkeypatch):
    """The graph tasks still exist for the opt-in experiment; their routing
    default (thinking off) is unchanged even though no UI slot targets them."""
    from app.ai import task_routing

    monkeypatch.setattr(task_routing, "_defaults_cache", None)
    monkeypatch.setattr(task_routing, "_redis_get", lambda: None)

    routing = task_routing.get_routing_for(AITask.CAD_DRAWING_GRAPH_READ)

    assert routing.thinking is False
