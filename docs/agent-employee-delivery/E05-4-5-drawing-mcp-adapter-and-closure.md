# E05.4.5 — drawing MCP read adapter и закрытие E05.4

Дата: 21 сентября 2026. Статус: **REVIEWED / DEPLOYED**.

## Адаптер

ToolResult v1 применяется только к `POST /api/agent/cap/mcp`,
`action=drawing_analysis_mcp`, mapping `arguments` с непустым `drawing_id` и
`reanalyze`, который отсутствует либо равен exact boolean `false`.

Успех требует mapping без domain error, `drawing.id`, совпадающий с запросом,
непустые `filename`/`format`, строковый `status`, `features:list` и exact
non-bool `total_features == len(features)`. Выбранные dimensions/surfaces/GDT
поля feature должны быть list-compatible. Raw payload сохраняется.

Malformed 2xx и 4xx дают `failed`; pre-dispatch failure — `failed`; 3xx/5xx,
timeout и exception после dispatch — `outcome_unknown`. Выполняется одна
попытка. Корректный versioned nonterminal сохраняется, versioned success
повторно валидируется.

## Scoped closure E05.4 / E05

- `drawing_analysis_mcp(reanalyze=true)` остаётся legacy/fail-closed: recipient
  имеет multiple-commit + enqueue boundary и не выдаёт строгую квитанцию через
  MCP wrapper.
- Dynamic external MCP остаётся legacy: schemas/effects/results задаются
  runtime-конфигурацией, per-tool effect attestation и receipt отсутствуют.
- Все 11 `computer_use.*` имеют no-retry boundary, но не получают success
  adapter: grant/audit/budget и browser/file effects не являются receipts.
- `email.send` подтверждает только queue acceptance, не SMTP delivery.

Безопасные точные операции E05.4 мигрированы, а остаток явно классифицирован
без массовой нормализации произвольных external payloads. E05.4 и E05 —
**SCOPED COMPLETE / REVIEWED**.

## Проверка

Исполнитель: 64 focused tests. Независимый расширенный MCP/transport/result/
router/policy/catalog набор: 394 passed; сохранено известное предупреждение
`asyncio_loop_scope`. Ruff check, format check и `git diff --check` прошли.

Production-стек пересобран и перезапущен через `make prod-build`.
`https://localhost/health` вернул `{"status":"ok"}`; backend и Celery beat,
обычный, GPU- и LoRA-workers запущены, health-enabled сервисы healthy. SHA-256
трёх изменённых runtime-модулей совпали с backend-контейнером.

Следующая карточка — E06: consumers не должны повторять `partial/job_queued`,
продолжать после `outcome_unknown` или терять точный `waiting_approval`.
