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
        "qwen3_vl_30b_a3b_ollama",
        "gemma4_e4b_ollama",
    ]


def test_digitize_group_exposes_only_working_spec_and_text_slots():
    """The experimental whole-sheet graph pipeline (layout / fragment /
    evidence-verify / legacy read) is deliberately NOT surfaced as user slots —
    it runs opt-in on its model_registry.yaml fallback defaults. Only the two
    production «По описанию» slots and its OCR text layer remain configurable."""
    digitize_slots = {slot for slot, group, *_ in _SLOTS if group == "Оцифровка"}
    assert digitize_slots == {"cad_spec_read", "cad_text_ocr", "cad_spec_draft"}
    assert not any(slot.startswith("cad_drawing_graph") for slot, *_ in _SLOTS)


def test_drawing_graph_reader_defaults_to_no_thinking(monkeypatch):
    """The graph tasks still exist for the opt-in experiment; their routing
    default (thinking off) is unchanged even though no UI slot targets them."""
    from app.ai import task_routing

    monkeypatch.setattr(task_routing, "_defaults_cache", None)
    monkeypatch.setattr(task_routing, "_redis_get", lambda: None)

    routing = task_routing.get_routing_for(AITask.CAD_DRAWING_GRAPH_READ)

    assert routing.thinking is False


# ── Выключение слота текстового слоя ─────────────────────────────────────────


def test_only_slots_the_pipeline_survives_without_can_be_switched_off():
    from app.api.providers_api import _OPTIONAL_SLOTS

    # Текстовый слой читает надписи отдельным проходом; способная vision-модель
    # делает это сама, и тогда второй проход только тратит время.
    assert "cad_text_ocr" in _OPTIONAL_SLOTS
    # А без чтения чертежа метода «по описанию» просто нет.
    assert "cad_spec_read" not in _OPTIONAL_SLOTS
    assert "embedding" not in _OPTIONAL_SLOTS


def test_disabling_a_required_slot_is_refused():
    import pytest
    from fastapi import HTTPException

    from app.api.providers_api import _set_slot_disabled

    with pytest.raises(HTTPException) as exc:
        _set_slot_disabled("cad_spec_read", True)
    assert exc.value.status_code == 400


def test_disable_is_written_to_the_route_not_as_a_reset(monkeypatch):
    """«Не использовать» и «сбросить к дефолту» — разные вещи.

    Сброс (`reset_task_routing`) снимает оверрайд и возвращает цепочку из
    model_registry.yaml, где у `cad_text_ocr` стоит glm-ocr, — то есть снова
    ВКЛЮЧАЕТ стадию. Поэтому выключение хранится отдельным полем маршрута.
    """
    from app.api.providers_api import _set_slot_disabled

    current = TaskRouting(task="cad_text_ocr", models=["glm_ocr_ollama"])
    saved = {}
    monkeypatch.setattr("app.ai.task_routing.get_routing_for", lambda _t: current)
    monkeypatch.setattr(
        "app.ai.task_routing.save_task_routing",
        lambda task, routing: saved.update(task=task, routing=routing),
    )

    _set_slot_disabled("cad_text_ocr", True)

    assert saved["task"] == AITask.CAD_TEXT_OCR
    assert saved["routing"].disabled is True
    # Модель остаётся на месте: включить слот обратно можно, ничего не выбирая заново.
    assert saved["routing"].models == ["glm_ocr_ollama"]


def test_disabled_state_survives_a_restart_path():
    """Поле едет в Postgres вместе с маршрутом, а не только в Redis.

    `save_task_routing` пишет лишь в Redis, а старт восстанавливает ключ из
    Postgres — настройка, сохранённая мимо `persist_task_routing`, откатилась бы
    при первом же перезапуске.
    """
    from app.api.providers_api import _slot_affected

    routing = TaskRouting(task="cad_text_ocr", models=["glm_ocr_ollama"], disabled=True)
    assert "disabled" in routing.model_dump(mode="json")
    # Слот объявляет свою задачу, поэтому _persist_slot_durable её найдёт.
    assert "cad_text_ocr" in _slot_affected("cad_text_ocr")


# ── Выход из облачного назначения ────────────────────────────────────────────


def test_assigning_a_local_model_drops_the_cloud_fallbacks_it_forbids():
    """Живая ошибка: «Назначения не применены».

        Confidential task cad_text_ocr cannot use non-local models:
        ollama_cloud_deepseek-v3_1_671b, openrouter_minimax_minimax-m3_free

    Назначение модели решает и политику задачи: локальная модель делает
    конфиденциальную задачу local_only. Хвост цепочки при этом переносился
    как есть, поэтому после облачного назначения в нём оставались облачные
    модели — и попытка вернуть слот на ЛОКАЛЬНУЮ модель отвергалась целиком,
    с перечислением ровно тех моделей, которые оператор и хотел убрать.
    Выйти из облачного назначения через экран было нельзя вообще.

    Эти записи не просто «невалидны на бумаге»: при local_only роутер отвергает
    каждую из них на вызове, то есть они мертвы в любом случае.
    """
    from app.ai.task_routing import policy_filtered_tail

    tail = policy_filtered_tail(
        AITask.CAD_TEXT_OCR,
        "glm_ocr_ollama",
        ["ollama_cloud_deepseek-v3_1_671b", "openrouter_minimax_minimax-m3_free"],
    )
    assert tail == []


def test_local_fallbacks_survive_the_same_assignment():
    from app.ai.task_routing import policy_filtered_tail

    tail = policy_filtered_tail(
        AITask.CAD_TEXT_OCR,
        "glm_ocr_ollama",
        ["qwen3_5_9b_ollama", "openrouter_minimax_minimax-m3_free"],
    )
    assert tail == ["qwen3_5_9b_ollama"]


def test_a_deliberate_cloud_assignment_keeps_its_chain():
    """Облачную модель первой оператор ставит осознанно — чистить нечего."""
    from app.ai.task_routing import policy_filtered_tail

    chain = ["openrouter_minimax_minimax-m3_free", "glm_ocr_ollama"]
    assert (
        policy_filtered_tail(AITask.CAD_TEXT_OCR, "ollama_cloud_deepseek-v3_1_671b", chain) == chain
    )


def test_a_non_confidential_task_keeps_cloud_fallbacks():
    from app.ai.task_routing import policy_filtered_tail

    chain = ["claude_sonnet_anthropic"]
    assert policy_filtered_tail(AITask.ORCHESTRATOR_PLANNING, "qwen3_5_9b_ollama", chain) == chain
