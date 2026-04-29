# Модельно-независимый AI-слой

## Назначение

AI-слой изолирует бизнес-логику от конкретных моделей и провайдеров. Код домена должен вызывать `AIRouter`, а не Ollama, vLLM или внешний API напрямую.

Базовые модели в конфиге сейчас:

- `gemma4_e4b_ollama` для быстрых локальных multimodal/OCR задач;
- `gemma4_26b_ollama` для локального reasoning;
- `local_embedding_vllm` как пример embedding-модели через vLLM;
- `future_reasoning_cloud` как выключенный кандидат для будущего внешнего провайдера.

## Основные файлы

- `backend/app/ai/schemas.py` — Pydantic-схемы запросов, ответов, моделей, маршрутов и eval.
- `backend/app/ai/model_registry.py` — загрузка и управление registry.
- `backend/app/ai/router.py` — выбор модели по задаче, policy enforcement, structured validation.
- `backend/app/ai/providers/` — адаптеры Ollama, vLLM и OpenAI-compatible endpoints.
- `backend/app/ai/config/model_registry.yaml` — стартовый registry моделей и task routing.
- `backend/app/ai/evals/` — минимальный harness и benchmark cases.

## Добавление новой модели

1. Добавить модель в `backend/app/ai/config/model_registry.yaml`.
2. Указать provider, provider_model, modalities, context, status, local_only, quality_score.
3. Добавить модель в `fallback_chain` нужной задачи.
4. Запустить regression:

```bash
python scripts/ai_eval.py --model new_model_name --task engineering_reasoning
```

5. После успешных проверок перевести статус: `candidate` → `staging` → `production`.

## Правила безопасности

- Конфиденциальные задачи по умолчанию `confidential=true`.
- Если route имеет `local_only=true`, cloud-модель запрещена даже для неконфиденциального запроса.
- Cloud provider требует `allow_cloud=true`.
- Tool calling не исполняет действия. Модель возвращает только proposed tool calls, затем backend/OpenClaw policy решает, можно ли выполнять действие.
- Structured output всегда валидируется Pydantic-схемой через `response_schema`.
- OCR fallback для document processing использует только `AIRouter` и route `AITask.INVOICE_OCR`; прямые вызовы Ollama/vLLM из domain/tasks кода запрещены.

## Пример использования

```python
from backend.app.ai import AIRouter, ModelRegistry
from backend.app.ai.schemas import AIRequest, AITask, ChatMessage

registry = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
router = AIRouter(registry)

response = await router.run(
    AIRequest(
        task=AITask.ENGINEERING_REASONING,
        messages=[ChatMessage(role="user", content="Draft a process plan for this shaft")],
        confidential=True,
    )
)
```
