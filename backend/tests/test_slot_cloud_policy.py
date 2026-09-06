"""Облако доступно любому слоту — но только по явному решению оператора.

Раньше здесь было две несогласованные половины. Экран предлагал облачные
модели для слотов, читающих содержимое документов (`cad_spec_read` и соседние),
а бэкенд такое назначение отвергал: `_enforce_confidential` возвращал маршрут в
local_only, после чего `_validate` падал с ValueError, и «Применить» отвечало
400. Выбор был, решения не было.

Теперь решение принимает оператор, и оно доходит до вызова: назначение пишет в
маршрут `cloud_override`, а роутер признаёт его сильнее, чем `confidential=True`
у места вызова. Проверяем всю цепочку — от валидации черновика до политики
роутера, — потому что рвалась она ровно между этими концами.
"""

from __future__ import annotations

import asyncio

import pytest

from app.ai.schemas import AIRequest, AITask, ModelStatus
from app.ai.task_routing import CONFIDENTIAL_TASKS, TaskRouting, _enforce_confidential, _validate
from app.api import providers_api as pa

ALL_SLOTS = [slot for slot, *_ in pa._SLOTS]


def _confidential_values() -> set[str]:
    return {t.value for t in CONFIDENTIAL_TASKS}


# ---------------------------------------------------------------------------
# Признак «наружу уходит само содержимое»
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slot", ALL_SLOTS)
def test_document_content_flag_matches_task_list(slot: str) -> None:
    """Признак выводится из CONFIDENTIAL_TASKS, а не из отдельного списка."""
    expected = any(item.split(" ")[0] in _confidential_values() for item in pa._slot_affected(slot))
    assert pa._slot_hard_confidential(slot) is expected


def test_document_slots_are_marked_and_agent_slots_are_not() -> None:
    for slot in ("cad_spec_read", "cad_text_ocr", "ocr_fast", "structured_extraction"):
        assert pa._slot_hard_confidential(slot) is True, slot
    for slot in ("agent_orchestrator", "agent_auditor", "agent_email", "agent_compression"):
        assert pa._slot_hard_confidential(slot) is False, slot


# ---------------------------------------------------------------------------
# Слот остаётся локальным, пока облако не разрешили
# ---------------------------------------------------------------------------


def test_slot_is_local_until_cloud_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pa, "_cloud_allowed_slots", lambda: set())
    assert pa._slot_effective_local_only("cad_spec_read") is True


def test_any_slot_can_be_opened_to_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    """Включая те, что читают чертежи: это решение оператора, а не системы."""
    monkeypatch.setattr(pa, "_cloud_allowed_slots", lambda: {"cad_spec_read"})
    assert pa._slot_effective_local_only("cad_spec_read") is False
    out = pa._build_slot_out(
        *pa._slot_meta("cad_spec_read"),
        model=None,
        registry=None,
        cloud_slots={"cad_spec_read"},
        current_model=None,
    )
    assert out.cloud_optionable is True
    assert out.cloud_allowed is True
    assert out.local_only is False
    # Разница с planner'ом — не в запрете, а в том, что именно уходит наружу.
    assert out.document_content is True


# ---------------------------------------------------------------------------
# Валидация черновика
# ---------------------------------------------------------------------------


class _Cap:
    provider_model = "vendor/model:free"
    local_only = False
    status = ModelStatus.PRODUCTION
    capability_source = "curated"
    capabilities_unknown = False
    modalities: tuple = ()

    class provider:  # noqa: N801 — имитация enum-поля каталога
        value = "openrouter"


class _Registry:
    def __init__(self) -> None:
        self.models = {"openrouter_test": _Cap()}


def _validate_draft(cloud_overrides):
    return asyncio.run(
        pa._validate_assignment_draft(
            _Registry(),
            {"cad_spec_read": "openrouter_test"},
            loaded={},
            cloud_overrides=cloud_overrides,
        )
    )


def test_cloud_without_permission_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pa, "_assignment_snapshot", lambda registry: {"slots": {}})
    monkeypatch.setattr(pa, "_cloud_allowed_slots", lambda: set())
    _diff, _warnings, errors = _validate_draft({"cad_spec_read": False})
    assert "cloud_for_confidential" in {e.code for e in errors}


def test_cloud_with_permission_passes_but_says_what_leaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pa, "_assignment_snapshot", lambda registry: {"slots": {}})
    _diff, warnings, errors = _validate_draft({"cad_spec_read": True})
    assert not [e for e in errors if e.code == "cloud_for_confidential"]
    # Молча выпускать содержимое чертежа наружу нельзя даже по решению
    # оператора: решение должно быть названо.
    assert "document_content_leaves_perimeter" in {w.code for w in warnings}


# ---------------------------------------------------------------------------
# Решение доходит до маршрута и до вызова
# ---------------------------------------------------------------------------


def test_enforce_confidential_respects_an_explicit_override() -> None:
    granted = TaskRouting(
        task="cad_spec_read",
        models=["openrouter_x"],
        local_only=False,
        allow_cloud=True,
        cloud_override=True,
    )
    assert _enforce_confidential(AITask.CAD_SPEC_READ, granted) is granted

    silent = granted.model_copy(update={"cloud_override": False})
    forced = _enforce_confidential(AITask.CAD_SPEC_READ, silent)
    assert forced.local_only is True and forced.allow_cloud is False


def test_validate_accepts_a_cloud_model_only_with_the_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.ai.task_routing.known_model_keys", lambda: {"openrouter_x"})
    monkeypatch.setattr("app.ai.task_routing._is_local_key", lambda key: False)
    routing = TaskRouting(
        task="cad_spec_read",
        models=["openrouter_x"],
        local_only=False,
        allow_cloud=True,
        cloud_override=True,
    )
    _validate(AITask.CAD_SPEC_READ, routing)  # не должно бросать

    with pytest.raises(ValueError, match="cannot use non-local"):
        _validate(AITask.CAD_SPEC_READ, routing.model_copy(update={"cloud_override": False}))


def test_router_lets_the_operator_decision_win_over_the_call_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Место вызова ставит confidential=True всегда — иначе решение не сработает.

    Читатели чертежей (`spec_fragments`, `system_reader`, `cad_trace`) передают
    ``confidential=True`` жёстко. Пока это перекрывало политику маршрута,
    назначенная облачная модель отвергалась уже на вызове — тем самым отказом,
    который не считается сбоем модели и не даёт откатиться на локальную.
    """
    from app.ai import router as router_mod

    routing = TaskRouting(
        task="cad_spec_read",
        models=["openrouter_x"],
        local_only=False,
        allow_cloud=True,
        cloud_override=True,
    )
    monkeypatch.setattr("app.ai.task_routing.get_routing_for", lambda task: routing)

    seen: dict = {}

    def _capture(self, request, model, resolved=None):
        seen["confidential"] = request.confidential
        seen["allow_cloud"] = request.allow_cloud
        raise RuntimeError("stop")  # дальше провайдера дёргать незачем

    monkeypatch.setattr(router_mod.AIRouter, "_enforce_policy", _capture)
    monkeypatch.setattr(
        router_mod.AIRouter,
        "_resolve_provider",
        lambda self, model: (None, None),
    )
    monkeypatch.setattr(
        router_mod.ai_router.registry,
        "get_model",
        lambda key: _Cap(),
        raising=False,
    )

    request = AIRequest(
        task=AITask.CAD_SPEC_READ,
        prompt="x",
        confidential=True,
        allow_cloud=False,
    )
    with pytest.raises(Exception):
        asyncio.run(router_mod.ai_router.run(request))

    assert seen == {"confidential": False, "allow_cloud": True}


def test_router_keeps_the_block_without_an_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без решения оператора всё как было: облако для такой задачи закрыто."""
    from app.ai import router as router_mod

    routing = TaskRouting(
        task="cad_spec_read",
        models=["openrouter_x"],
        local_only=True,
        allow_cloud=False,
        cloud_override=False,
    )
    monkeypatch.setattr("app.ai.task_routing.get_routing_for", lambda task: routing)

    seen: dict = {}

    def _capture(self, request, model, resolved=None):
        seen["confidential"] = request.confidential
        raise RuntimeError("stop")

    monkeypatch.setattr(router_mod.AIRouter, "_enforce_policy", _capture)
    monkeypatch.setattr(router_mod.AIRouter, "_resolve_provider", lambda self, model: (None, None))
    monkeypatch.setattr(
        router_mod.ai_router.registry, "get_model", lambda key: _Cap(), raising=False
    )

    with pytest.raises(Exception):
        asyncio.run(
            router_mod.ai_router.run(
                AIRequest(task=AITask.CAD_SPEC_READ, prompt="x", confidential=False)
            )
        )
    assert seen == {"confidential": True}


# ---------------------------------------------------------------------------
# Роли агента: модель живёт в agent_config, а не в маршруте задачи
# ---------------------------------------------------------------------------


def _run_planning(monkeypatch, *, routing, allow_cloud, task=AITask.ORCHESTRATOR_PLANNING):
    from app.ai import router as router_mod

    monkeypatch.setattr("app.ai.task_routing.get_routing_for", lambda t: routing)
    seen: dict = {}

    def _capture(self, request, model, resolved=None):
        seen["confidential"] = request.confidential
        seen["allow_cloud"] = request.allow_cloud
        raise RuntimeError("stop")

    monkeypatch.setattr(router_mod.AIRouter, "_enforce_policy", _capture)
    monkeypatch.setattr(router_mod.AIRouter, "_resolve_provider", lambda self, m: (None, None))
    monkeypatch.setattr(
        router_mod.ai_router.registry, "get_model", lambda key: _Cap(), raising=False
    )
    with pytest.raises(Exception):
        asyncio.run(
            router_mod.ai_router.run(
                AIRequest(
                    task=task,
                    prompt="x",
                    confidential=False,
                    allow_cloud=allow_cloud,
                    preferred_model="openrouter_test",
                )
            )
        )
    return seen


def test_cloud_model_for_an_agent_role_is_not_blocked_by_the_shared_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Слоты «Быстрая» и «Оркестратор» делят одну задачу orchestrator_planning.

    Облачную модель, назначенную только быстрой роли, нельзя выразить в
    маршруте задачи: оркестратор остаётся локальным, маршрут — local_only. Пока
    место вызова не могло заявить свой выбор, такая модель отвергалась
    политикой, а отказ политики — жёсткий стоп на весь ход агента, без отката
    на локальную модель. То есть выбор облачной «быстрой» модели ломал ответы
    целиком.
    """
    local_routing = TaskRouting(
        task="orchestrator_planning",
        models=["ollama_local"],
        local_only=True,
        allow_cloud=False,
    )
    assert _run_planning(monkeypatch, routing=local_routing, allow_cloud=True) == {
        "confidential": False,
        "allow_cloud": True,
    }


def test_a_local_role_model_keeps_the_local_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    local_routing = TaskRouting(
        task="orchestrator_planning",
        models=["ollama_local"],
        local_only=True,
        allow_cloud=False,
    )
    assert _run_planning(monkeypatch, routing=local_routing, allow_cloud=False) == {
        "confidential": True,
        "allow_cloud": False,
    }


def test_a_call_site_cannot_open_a_confidential_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """Послабление доступно только задачам вне CONFIDENTIAL_TASKS.

    Иначе любое место вызова могло бы обойти политику, назвав модель поимённо.
    """
    routing = TaskRouting(
        task="cad_spec_read", models=["ollama_local"], local_only=True, allow_cloud=False
    )
    assert _run_planning(
        monkeypatch, routing=routing, allow_cloud=True, task=AITask.CAD_SPEC_READ
    ) == {"confidential": True, "allow_cloud": False}


def test_turn_router_recognises_a_cloud_model_by_catalog() -> None:
    from app.ai.turn_router import _is_cloud_model

    assert _is_cloud_model(None) is False
    assert _is_cloud_model("не-существующая-модель") is False
