"""Облачной модели схему тоже надо передавать, а не только просить словами.

Найдено на живом чтении чертежа z4-r4.jpg. Назначенная облачная модель лист
прочитала прекрасно: узнала ступенчатый вал, обозначение ПЭ-137.04.10.02.008.09,
сталь 45, резьбы M18×1.5 и M24×1.5, шпоночные пазы, разрезы A-A и Б-Б. Но
ответила markdown-отчётом, потому что схему ей никто не передал: Ollama-провайдер
кладёт её в ``format``, а OpenAI-совместимый игнорировал и ``response_schema``, и
``metadata["json_schema"]``. Все 29 запросов чтения были отброшены как «не JSON»,
и оцифровка упала на проверке типа детали — с формулировкой, будто модель не
поняла, что перед ней вал.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.ai.providers.openai_compatible import _response_format
from app.ai.schemas import AIRequest, AITask


class _Answer(BaseModel):
    kind: str
    diameter: float


def _request(**kw) -> AIRequest:
    return AIRequest(task=AITask.CAD_SPEC_READ, prompt="читай", **kw)


def test_no_schema_no_format() -> None:
    """Свободный ответ остаётся свободным — формат навязывается только по просьбе."""
    assert _response_format(_request()) == {}


def test_pydantic_schema_without_declared_support_asks_for_plain_json() -> None:
    """Строгую схему принимает не каждая модель; валидный JSON — почти каждая.

    У OpenRouter это отдельный параметр ``structured_outputs``, и у бесплатных
    вариантов его обычно нет. Просить схему там — получить 400 вместо ответа.
    """
    out = _response_format(_request(response_schema=_Answer))
    assert out == {"response_format": {"type": "json_object"}}


def test_declared_support_gets_the_real_schema() -> None:
    out = _response_format(
        _request(
            response_schema=_Answer,
            metadata={"structured_output_supported": True},
        )
    )
    fmt = out["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert set(fmt["json_schema"]["schema"]["properties"]) == {"kind", "diameter"}


def test_raw_json_schema_from_metadata_is_honoured() -> None:
    """Читатель чертежа передаёт схему именно так — как готовый JSON-schema."""
    schema = {"type": "object", "properties": {"kind": {"type": "string"}}}
    out = _response_format(
        _request(metadata={"json_schema": schema, "structured_output_supported": True})
    )
    assert out["response_format"]["json_schema"]["schema"] == schema


def test_metadata_schema_without_support_still_forces_json() -> None:
    out = _response_format(_request(metadata={"json_schema": {"type": "object"}}))
    assert out == {"response_format": {"type": "json_object"}}


def test_chat_payload_carries_the_format() -> None:
    """Проверка на уровне запроса, а не только helper'а."""
    import asyncio

    import httpx

    from app.ai.providers.openai_compatible import OpenAICompatibleProvider
    from app.ai.schemas import ProviderConfig, ProviderKind

    captured: dict = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):  # noqa: D401 - заглушка httpx
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"kind":"вал","diameter":25}'}}]}

    async def _post(self, url, **kw):
        captured.update(kw.get("json") or {})
        return _Resp()

    provider = OpenAICompatibleProvider(
        ProviderConfig(
            kind=ProviderKind.OPENROUTER,
            base_url="https://openrouter.ai/api/v1",
            is_local=False,
        )
    )
    original = httpx.AsyncClient.post
    httpx.AsyncClient.post = _post
    try:
        asyncio.run(
            provider.chat(
                _request(response_schema=_Answer, metadata={"structured_output_supported": True}),
                "vendor/model",
            )
        )
    finally:
        httpx.AsyncClient.post = original

    assert captured["response_format"]["type"] == "json_schema"


def test_schema_is_spelled_out_when_strict_mode_is_unavailable() -> None:
    """Модель без structured_outputs узнаёт форму единственным доступным путём.

    Без этого читатель чертежа получал валидный, но чужой JSON: он ждёт
    ``{"frames": [...]}``, а модель отвечала голым массивом — и слой
    геометрических допусков терялся целиком, на каждом проходе.
    """
    from app.ai.providers.openai_compatible import _schema_hint

    schema = {"type": "object", "properties": {"frames": {"type": "array"}}}
    hint = _schema_hint(_request(metadata={"json_schema": schema}))
    assert "frames" in hint
    assert "ОДНИМ объектом JSON" in hint

    # Со строгим режимом схема уходит параметром, повторять её в тексте незачем.
    assert (
        _schema_hint(
            _request(metadata={"json_schema": schema, "structured_output_supported": True})
        )
        == ""
    )
    assert _schema_hint(_request()) == ""


def test_openrouter_probe_separates_json_object_from_real_schema() -> None:
    """`response_format` и `structured_outputs` — разные обещания.

    Каталог записывал первое как второе, и читатель слал строгую схему модели,
    которая её молча игнорирует.
    """
    from app.ai.provider_catalog_probes import _openrouter_capability
    from app.ai.schemas import ProviderKind

    def cap(params):
        return _openrouter_capability(
            "k",
            ProviderKind.OPENROUTER,
            {"id": "vendor/m", "supported_parameters": params, "architecture": {}},
            "2026-09-06",
        )

    assert cap(["response_format"]).supports_structured_output is False
    assert cap(["response_format", "structured_outputs"]).supports_structured_output is True


def test_only_the_assigned_model_runs_and_its_own_error_is_reported(monkeypatch) -> None:
    """Автоматического запаса нет: работает только назначенная модель.

    Раньше здесь проверялось обратное — что перебор доходит до третьего
    кандидата. Такая цепочка и была источником целого класса дефектов: диагноз
    называл чужую модель («Model gemma4:e4b is not served» при том, что
    отвечала голова), проба возможностей мерила ответ запасного, политика
    обрывала ход из-за облачной записи в хвосте. И главное — подмена шла
    молча: в настройках одна модель, в работе другая, и по результату этого
    не видно.

    Решение принято по итогам разбора: конвейер работает только на моделях,
    которые выбрал оператор. Осознанный второй проход (слот «Повторное
    извлечение») остаётся — он читает свою модель сам и вызывает её напрямую,
    это шаг конвейера, а не тихая замена при сбое.
    """
    import asyncio

    from app.ai import router as router_mod
    from app.ai.schemas import AIResponse, ProviderKind
    from app.ai.task_routing import TaskRouting

    routing = TaskRouting(
        task="cad_spec_read",
        models=["cloud_over_quota", "local_missing", "local_working"],
        local_only=False,
        allow_cloud=True,
        cloud_override=True,
    )
    monkeypatch.setattr("app.ai.task_routing.get_routing_for", lambda task: routing)

    class _Cap:
        def __init__(self, key: str) -> None:
            self.name = key
            self.provider_model = key
            self.local_only = key != "cloud_over_quota"
            self.supports_structured_output = False
            self.thinking_supported = False
            self.thinking_enabled = False
            self.thinking_levels = []
            self.thinking_level_default = None
            self.modalities = set()
            self.provider = type("P", (), {"value": "openrouter"})

    monkeypatch.setattr(
        router_mod.ai_router.registry, "get_model", lambda key: _Cap(key), raising=False
    )

    def _resolve(self, model):
        if model.provider_model == "local_missing":
            raise RuntimeError("Model local_missing is not served by any enabled ollama node")
        return object(), None

    async def _dispatch(self, provider, request, model):
        if model.provider_model == "cloud_over_quota":
            raise RuntimeError("429 Rate limit exceeded: daily limit reached")
        return AIResponse(
            task=request.task,
            provider=ProviderKind.OLLAMA,
            model=model.provider_model,
            text="ok",
        )

    monkeypatch.setattr(router_mod.AIRouter, "_resolve_provider", _resolve)
    monkeypatch.setattr(router_mod.AIRouter, "_enforce_policy", lambda *a, **kw: None)
    monkeypatch.setattr(router_mod.AIRouter, "_dispatch", _dispatch)

    import pytest

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(
            router_mod.ai_router.run(
                AIRequest(task=AITask.CAD_SPEC_READ, prompt="x", confidential=False)
            )
        )

    # Причина — своя у назначенной модели, а не «третий кандидат не сработал».
    assert "daily limit" in str(exc.value)
    assert "local_working" not in str(exc.value)


# ── Сэмплирующие параметры по видам шлюзов ───────────────────────────────────


def test_nonstandard_sampling_params_do_not_reach_a_strict_gateway():
    """`top_k`/`min_p` в спецификации OpenAI отсутствуют.

    Локальные серверы принимают их как расширение, а строгий облачный шлюз
    отвечает 400 — то есть параметр, отправленный «на всякий случай», стоит
    целого кандидата цепочки.
    """
    from app.ai.providers.openai_compatible import _inference_params
    from app.ai.schemas import AIRequest, AITask

    request = AIRequest(
        task=AITask.CAD_SPEC_READ,
        metadata={"inference_params": {"temperature": 0, "top_k": 1, "min_p": 0.05}},
    )

    strict = _inference_params(request, provider_kind="openai")
    assert "top_k" not in strict
    assert "min_p" not in strict

    lenient = _inference_params(request, provider_kind="vllm")
    assert lenient["top_k"] == 1
    assert lenient["min_p"] == 0.05


def test_the_output_limit_is_sent_to_every_gateway():
    from app.ai.providers.openai_compatible import _inference_params
    from app.ai.schemas import AIRequest, AITask

    request = AIRequest(task=AITask.CAD_SPEC_READ, metadata={"num_predict": 6000})
    assert _inference_params(request, provider_kind="openai")["max_tokens"] == 6000
