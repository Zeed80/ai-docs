# E21.2b6 — AIRouter attempts долговечного оркестратора

Дата: 5 октября 2026. Исходный commit: `bd65013c`.
Исполнитель: `gpt-5.6-sol`, high; root — независимая приёмка и выпуск.
Статус: TESTED / REVIEWED / DEPLOYED.

## Контракт ограниченного среза

Долговечный runtime связывает AIRouter с тем же authoritative WorkBudgetContext,
который использует AgentSession. Контекст задаётся сервером на время исполнения
через ContextVar; request metadata не может заменить ledger. Снимок фиксирует
owner, ledger, active plan и revision. После завершения или исключения контекст
сбрасывается; новая работа не наследует бюджет предыдущей.

В срез входят Ollama text/structured вызовы ORCHESTRATOR_PLANNING (решение и
план), CLASSIFICATION (аудит), EMAIL_DRAFTING (refinement), CODE_GENERATION
(условный draft). Каждый повтор AIRouter для исправления формата резервирует
отдельный llm_calls slot до provider dispatch и сохраняет расход при ошибке,
timeout или cancellation. Postflight проверяет актуальность до применения
ответа или повторной попытки. BudgetExecutionStopped проходит мимо обычных
Exception recovery handlers.

Неподдерживаемые provider/task/image paths внутри bound durable исполнения
fail-closed. Это включает nested AIRouter embedding/rerank, если такой путь
достигается в ходе работы. Вне bound durable исполнения старые AIRouter callers
не мигрируются этим срезом. Генерация постоянных skills не активируется.

Конечные token/cost caps без доказанного upper bound продолжают блокировать
вызов. Full E21, остальные entry adapters, direct model callers, token/cost
accounting, legacy reconciliation и active-time/replan остаются незавершёнными.

## Независимые критерии приёмки

- Physical retries/format degradation используют общий ledger с AgentSession.
- Ноль dispatch при исчерпании бюджета, finite caps и неподдерживаемом маршруте.
- Смена owner/ledger/active plan, cancellation и lease loss блокируют ответ.
- Reservation/settlement failure не превращается в обычный model fallback.
- Concurrent last-slot и crash/replay не разрешают лишнюю попытку.
- Контекст из metadata не принимается; scope сбрасывается и изолирован по task.
- Новые и соседние профили проходят на отдельной testcontainers PostgreSQL.
- Принятый diff проходит Ruff, production rebuild и HTTPS `/health`.

## Проверки

До окончательного diff root проверил исходные соседние границы: provider/headless/
channel parity — 20 passed; router/turn router/orchestrator — 57 passed;
durable chat/checkpoints/nested tools — 104 passed. Эти исходные прогоны не
подменяют повторную проверку принятого кода.

Root независимо проверил frozen diff и callers, отрицательные пути и настоящий
OllamaProvider с подставным HTTP. Замечания в одном цикле review: frozen ledger ID,
явный image guard, pre-server bounds, physical HTTP regression, точная injection
physical reserve failure, durable worker binding и task isolation. Все закрыты
кодом и тестами. Исполнитель: новый профиль 18 passed, provider/durable 48 passed,
router/orchestrator 62 passed; эти числа не добавляются к root-приёмке.

Окончательные независимые команды (каждая — отдельный процесс с новой
testcontainers PostgreSQL, `POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=1`):

```bash
python3 -m pytest backend/tests/test_work_budget_airouter.py backend/tests/test_work_budget_provider.py backend/tests/test_work_budget_headless.py backend/tests/test_agent_channel_parity.py backend/tests/test_durable_chat.py backend/tests/test_chat_checkpoints.py backend/tests/test_work_budget_tools.py backend/tests/test_router_format_recovery.py backend/tests/test_router_dispatch.py backend/tests/test_router_inference_params.py backend/tests/test_turn_router.py backend/tests/ai/test_router.py backend/tests/ai/test_orchestrator.py -q --tb=short
python3 -m pytest backend/tests/test_work_budget_ledger.py backend/tests/test_work_order_lease.py backend/tests/test_agent_execution_boundary.py backend/tests/test_agent_delegations.py -q --tb=short
```

Первый профиль — **204 passed**, второй — **62 passed**, всего **266 passed**.
В каждом одно существующее предупреждение pytest об `asyncio_loop_scope`.
Ruff check/format и `git diff --check` прошли. Crash/replay и parent/child
last-slot покрыты прежними ledger/provider tests в окончательном профиле;
это не отдельная живая аварийная проверка production.

## Production-проверка

`make prod-build` завершился успешно; backend, Celery workers и beat пересозданы.
Backend/frontend/worker/GPU-worker/LoRA-worker healthy; beat running.
HTTPS `curl -ksS https://localhost/health` → `{"status":"ok"}`.
SHA256 runtime-файлов совпадает на хосте, в backend и celery-worker:

- `router.py`: `078b7190d0496f1cee920768c1bb1ba51cb2b0fbfe58aedb77bb6cef8f34dd47`
- `work_budget_context.py`: `8747305da27db262a2905387db70eb40e2e38a65f5142313cb9305a742dcc488`
- `durable_chat.py`: `d0e7a41013bea9d0bac2679ffea799b48b6567adc43fe63d4e5dfeb2ab557d92`

Новых миграций и переноса production-данных не было. Это health/code deployment
validation, не живая модельная приёмка и не расширение платного rollout.
Живая LLM и внешние получатели не вызывались. Экономия модели не измерена.
