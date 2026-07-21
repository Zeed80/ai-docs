from app.ai.model_registry import ModelRegistry
from app.ai.schemas import AITask
from app.ai.task_routing import TaskRouting
from app.api.providers_api import _apply_slot_assignment


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


def test_drawing_graph_reader_has_independent_assignment(monkeypatch):
    registry = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
    current = TaskRouting(task="cad_drawing_graph_read", models=[])
    saved = {}

    monkeypatch.setattr(
        "app.ai.task_routing.get_routing_for",
        lambda _task: current,
    )
    monkeypatch.setattr(
        "app.ai.task_routing.save_task_routing",
        lambda task, routing: saved.update(task=task, routing=routing),
    )

    _apply_slot_assignment(
        "cad_drawing_graph_read", "qwen3_6_35b_apex_ollama", registry
    )

    assert saved["task"].value == "cad_drawing_graph_read"
    assert saved["routing"].models == [
        "qwen3_6_35b_apex_ollama",
        "gemma4_e4b_ollama",
    ]


def test_drawing_graph_reader_defaults_to_no_thinking(monkeypatch):
    from app.ai import task_routing

    monkeypatch.setattr(task_routing, "_defaults_cache", None)
    monkeypatch.setattr(task_routing, "_redis_get", lambda: None)

    routing = task_routing.get_routing_for(AITask.CAD_DRAWING_GRAPH_READ)

    assert routing.thinking is False
