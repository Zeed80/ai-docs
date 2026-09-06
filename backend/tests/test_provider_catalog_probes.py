"""Подтянутая из облака модель описывается фактами, а не догадками.

`refresh_models` регистрировал каждую модель одинаково: modalities={TEXT,
TOOL_CALLING}, supports_tool_calling=True, supports_structured_output=True —
вслепую, для любого провайдера. Дальше эти выдуманные значения участвовали в
валидации назначения как факт: система утверждала о модели то, чего не
проверяла.
"""

from app.ai.provider_catalog_probes import capability_from_listing
from app.ai.schemas import ProviderKind

# Ответ OpenRouter в том виде, в каком он приходит из /v1/models.
OPENROUTER_ITEM = {
    "id": "anthropic/claude-sonnet-4",
    "context_length": 200000,
    "architecture": {"input_modalities": ["text", "image"]},
    "supported_parameters": ["tools", "response_format", "structured_outputs", "reasoning"],
    "pricing": {"prompt": "0.000003", "completion": "0.000015"},
}


def test_openrouter_metadata_is_used_instead_of_guessing():
    cap = capability_from_listing("k", ProviderKind.OPENROUTER, OPENROUTER_ITEM)

    assert cap.max_context_tokens == 200000
    assert {m.value for m in cap.modalities} == {"text", "vision", "tool_calling"}
    assert cap.supports_tool_calling is True
    assert cap.supports_structured_output is True
    assert cap.thinking_supported is True
    assert cap.capabilities_unknown is False


def test_price_is_converted_from_per_token_to_per_thousand():
    """Провайдер публикует цену за один токен, каталог хранит за тысячу."""
    cap = capability_from_listing("k", ProviderKind.OPENROUTER, OPENROUTER_ITEM)
    assert cap.cost_per_1k_input == 0.003
    assert round(cap.cost_per_1k_output, 6) == 0.015


def test_json_object_alone_is_not_structured_output():
    """`response_format` — «ответь валидным JSON», `structured_outputs` — схема.

    Мы записывали первое как второе, и читатель чертежа слал строгую схему
    модели, которая её молча игнорирует: ответ приходил вольным текстом.
    """
    item = {
        "id": "vendor/free-model",
        "architecture": {"input_modalities": ["text", "image"]},
        "supported_parameters": ["response_format", "reasoning"],
    }
    cap = capability_from_listing("k", ProviderKind.OPENROUTER, item)
    assert cap.supports_structured_output is False


def test_a_text_only_model_gets_no_invented_abilities():
    item = {
        "id": "some/text-model",
        "context_length": 32768,
        "architecture": {"input_modalities": ["text"]},
        "supported_parameters": ["temperature"],
    }
    cap = capability_from_listing("k", ProviderKind.OPENROUTER, item)

    assert {m.value for m in cap.modalities} == {"text"}
    assert cap.supports_tool_calling is False
    assert cap.supports_structured_output is False
    assert cap.thinking_supported is False


def test_missing_price_stays_missing_rather_than_zero():
    item = {"id": "x", "pricing": {"prompt": "0", "completion": None}}
    cap = capability_from_listing("k", ProviderKind.OPENROUTER, item)
    assert cap.cost_per_1k_input is None
    assert cap.cost_per_1k_output is None


def test_a_provider_without_metadata_is_marked_unknown_not_capable():
    cap = capability_from_listing("k", ProviderKind.DEEPSEEK, {"id": "deepseek-chat"})

    assert cap.capabilities_unknown is True
    assert cap.modalities == set()
    # Главное: возможности больше не выдумываются.
    assert cap.supports_tool_calling is False
    assert cap.supports_structured_output is False


def test_a_broken_response_degrades_to_unknown_instead_of_crashing():
    """Формат ответа провайдера может измениться — обновление каталога не
    должно падать целиком из-за одной странной записи."""
    cap = capability_from_listing(
        "k", ProviderKind.OPENROUTER, {"id": "x", "architecture": "не-объект"}
    )
    assert cap.capabilities_unknown is True
    assert cap.provider_model == "x"


# ── Семейства, добавленные после аудита оцифровки ────────────────────────────
#
# Разбирался только OpenRouter, поэтому у всех остальных облаков терялось даже
# то, что они публикуют прямым текстом. Теперь три уровня честности вместо
# двух: полный разбор, «известно только окно контекста», «не знаем ничего».


def test_mistral_capabilities_are_read_not_guessed():
    item = {
        "id": "pixtral-large-latest",
        "max_context_length": 131072,
        "capabilities": {
            "completion_chat": True,
            "vision": True,
            "function_calling": True,
            "structured_outputs": True,
        },
    }
    cap = capability_from_listing("k", ProviderKind.MISTRAL, item)

    assert cap.capabilities_unknown is False
    assert {m.value for m in cap.modalities} == {"text", "vision", "tool_calling"}
    assert cap.supports_tool_calling is True
    assert cap.supports_structured_output is True
    assert cap.max_context_tokens == 131072


def test_mistral_without_vision_does_not_claim_it():
    item = {"id": "mistral-small", "capabilities": {"function_calling": True}}
    cap = capability_from_listing("k", ProviderKind.MISTRAL, item)

    assert {m.value for m in cap.modalities} == {"text", "tool_calling"}
    assert cap.supports_structured_output is False


def test_anthropic_listing_with_capabilities_is_parsed():
    item = {
        "id": "claude-opus-5",
        "max_input_tokens": 1000000,
        "capabilities": {"vision": True, "extended_thinking": True},
    }
    cap = capability_from_listing("k", ProviderKind.ANTHROPIC, item)

    assert cap.capabilities_unknown is False
    assert cap.max_context_tokens == 1000000
    assert "vision" in {m.value for m in cap.modalities}
    assert cap.supports_structured_output is True
    assert cap.thinking_supported is True


def test_anthropic_old_listing_keeps_capabilities_unknown():
    """До марта 2026 листинг нёс только id и display_name.

    Провайдер за прокси может отдавать ровно эту форму до сих пор — и тогда
    честный ответ «не проверяли», а не набор возможностей, угаданный по имени.
    """
    cap = capability_from_listing(
        "k", ProviderKind.ANTHROPIC, {"id": "claude-opus-5", "display_name": "Claude Opus 5"}
    )
    assert cap.capabilities_unknown is True
    assert cap.modalities == set()


def test_context_window_survives_where_capabilities_do_not():
    """Groq не описывает возможности, но окно публикует — терять его незачем."""
    item = {"id": "llama-3.3-70b", "context_window": 131072}
    cap = capability_from_listing("k", ProviderKind.GROQ, item)

    assert cap.max_context_tokens == 131072
    # Окно — измеримый факт; возможности по-прежнему не подтверждены.
    assert cap.capabilities_unknown is True
    assert cap.modalities == set()
    assert cap.supports_tool_calling is False


def test_openai_stays_unknown_because_its_listing_says_nothing():
    """Листинг OpenAI несёт id/created/owned_by — догадка по имени запрещена."""
    cap = capability_from_listing("k", ProviderKind.OPENAI, {"id": "gpt-4o", "owned_by": "openai"})
    assert cap.capabilities_unknown is True
    assert cap.max_context_tokens is None
