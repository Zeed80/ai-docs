# E21.2b7 — Direct Ollama text helpers долговечного исполнения

Дата: 5 октября 2026. Исходный commit: `d57dc4a6`.
Исполнитель: `gpt-5.6-sol`, high; root — независимая приёмка и выпуск.
Статус: TESTED / REVIEWED / DEPLOYED.

## Контракт ограниченного среза

Direct text helpers `generate`, `reasoning_generate`, `generate_json` и `chat`
внутри server-owned bound durable execution используют тот же WorkBudgetContext,
что AIRouter и AgentSession. Каждый physical Ollama POST, включая transport/JSON
retry, требует отдельного slot общего ledger. Ошибка, timeout или cancellation
не возвращают зарезервированный slot. После settlement актуальность frozen
owner/ledger/plan/revision проверяется до выдачи ответа и повторной попытки.

Finite token/cost caps без доказанного upper bound, исчерпанный бюджет и
неподдерживаемые direct provider routes блокируют GPU/client/HTTP preparation.
Legacy Claude fallback и default Ollama при другом configured provider в bound
`reasoning_generate` запрещены. BudgetExecutionStopped не маскируется обычными
Exception recovery handlers или optional title fallback.

Явный detached verifier/planner context сохраняет самостоятельный lifecycle.
Одновременный ambient durable context и explicit context блокируются: одинаковые
ID не заменяют доказательство execution fence. Контекст не принимается из
request metadata, HTTP headers или аргументов модели.

## Caller inventory и границы

| Caller | Граница | Состояние этого среза |
|---|---|---|
| Durable worker → Orchestrator → AIRouter | Bound asyncio execution | Ранее принят E21.2b6 |
| Direct helper в том же bound asyncio execution | Ollama HTTP leaf | Подключается этим срезом |
| AIRouter legacy convenience methods | generate_json/reasoning_generate | Наследуют ambient context только в том же процессе/ходе |
| table_sql_pipeline generate SQL/title | reasoning_generate | Наследует ambient только при in-process вызове |
| workspace.sql_table HTTP tool | API worker → table_sql_pipeline | HTTP не переносит ContextVar; этот caller не мигрирован |
| Detached work planner/verifier | explicit snapshot/once-fence | Сохраняется E21.2b4/E21.2b3; collision блокируется |
| Standalone email compose/triage | AgentSession/direct helpers вне durable scope | Не мигрирован |
| Telegram voice preprocessing | AIRouter до intake | Не мигрирован |
| CAD/document processing и chat_with_images | Separate/VLM callers | Вне employee scope этого среза |

Наличие context-aware helper не доказывает миграцию всех его callers.
Непроверенные entry adapters, HTTP recipient model calls, прочие provider paths,
token/cost accounting и bounds, legacy reconciliation и active-time/replan
остаются остатком E21. Full E21 остаётся IN_PROGRESS.

## Независимая приёмка

До изменений root проверил detached planner: 23 passed; detached verifier:
17 passed, каждый в отдельной testcontainers PostgreSQL. Исходные прогоны
не заменяют повторную приёмку окончательного diff.

Root проверил реальный frozen diff, callers, preflight, reserve/settle и все
исходы HTTP leaf. Один цикл review закрыл: postflight для любого provider error,
sticky collision, postflight после awaited client close, reserve до создания
client, разрешённый Ollama при включённой legacy Claude настройке, успешный chat
и optional table-title handler. Защитные тесты не ослаблялись.

Исполнитель: 20 passed, 0 failed, 0 skipped. Диагностический запуск 18 passed /
2 failed выявил невалидный fixture SQL без таблицы; fixture исправлен на
`SELECT id FROM invoices`, execute_sql в этом тесте подставлен и БД не читает.
Эти числа не добавляются к независимой приёмке.

Окончательные независимые команды (каждый профиль — отдельный процесс и новая
testcontainers PostgreSQL; `POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=1`):

```bash
python3 -m pytest backend/tests/test_work_budget_direct_text.py backend/tests/test_work_budget_airouter.py backend/tests/test_work_budget_provider.py backend/tests/test_work_budget_tools.py backend/tests/test_durable_chat.py backend/tests/test_chat_checkpoints.py backend/tests/test_work_budget_headless.py backend/tests/test_agent_channel_parity.py backend/tests/test_router_format_recovery.py backend/tests/test_router_dispatch.py backend/tests/test_router_inference_params.py backend/tests/test_turn_router.py backend/tests/ai/test_router.py backend/tests/ai/test_orchestrator.py backend/tests/ai/test_ollama_inference_options.py -q --tb=short
python3 -m pytest backend/tests/test_work_budget_planner.py -q --tb=short
python3 -m pytest backend/tests/test_work_budget_verifier.py -q --tb=short
```

Совместный профиль — **236 passed**, planner — **23 passed**, verifier —
**17 passed**, всего **276 passed**. Каждый профиль сообщает одно существующее
pytest warning об `asyncio_loop_scope`. Ruff check/format и `git diff --check`
прошли. Транспорт всех новых model tests подставной; живая LLM не использовалась.

## Production-проверка

`make prod-build` завершился успешно; backend, Celery workers и beat пересозданы.
Backend/frontend/worker/GPU-worker/LoRA-worker healthy, beat running.
HTTPS `curl -ksS https://localhost/health` → `{"status":"ok"}`.
SHA256 двух runtime-файлов совпадает на хосте, в backend и celery-worker:

- `ollama_client.py`: `e2b627edfc3be4869b2ae92862e9e6a934a6340e7b4fc4f5bebc979e233ee124`
- `work_budget_context.py`: `8a4a1f389851473d588f6cd2d5bf0fbbb9b0577164f78d8dfc2fd8c2e69a3d58`

Новых миграций и переноса production-данных не выполнялось. Health/hash
подтверждают выпуск кода, а не live model quality или расширение платного rollout.
Живая LLM, платные providers и внешние получатели не вызываются.
Экономия модели не измерена.
