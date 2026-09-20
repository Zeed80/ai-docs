# E05.3.2 — technology async job и закрытие E05.3

Дата: 20 сентября 2026. Статус: **REVIEWED, production verified**.

## Принятый срез

E05.3.2 добавляет ровно `tech.generate_tp_from_drawing` через точную пару
`POST /api/agent/cap/tech` + исходный `action`. Прямой business route и прочие
`tech` actions остаются fail-closed и сохраняют legacy-контракт.

Допустимый ответ содержит непустые `task_id`, `plan_id` и `status: "queued"`.
Он нормализуется в ToolResult v1 `partial`/`job_queued`, `retryable: false`, с
полным raw payload в `data`, `task_id`/`plan_id`/`queued` в checkpoint и exact
operation/recipient acceptance в evidence. Общий E05.3 adapter использует карту
identity: document jobs требуют `document_id`, technology job — `plan_id`.

Корректный versioned nonterminal сохраняется без double wrapping;
`succeeded` с queued job и неверная legacy-форма отклоняются. Выполняется ровно
одна HTTP-попытка: 4xx — `failed`, 3xx/5xx или ошибка после начала dispatch —
`outcome_unknown`, ошибка до dispatch — `failed`. Публичный recipient, RBAC,
approval policy, аргументы и порядок broker publish → DB commit не менялись.

## Закрывающий реестр E05.3

E03 содержит 9 `db-async-enqueue` operations. Четыре имеют доказанный строгий
queue receipt и мигрированы: `documents.classify`, `documents.extract`,
`documents.reprocess`, `tech.generate_tp_from_drawing`. Остальные пять явно
отложены:

- `documents.ingest` — несколько внешних/async эффектов, только
  `pipeline_queued`, task ID отсутствует;
- `documents.update` — enqueue условен, исключение broker проглатывается,
  ответ не является queue receipt;
- `email.label` — enqueue условен, task ID игнорируется, broker failure
  проглатывается;
- `invoices.export_excel` — возвращаемый ID принадлежит DB `ExportJob`, а
  broker failure проглатывается; `pending` не доказывает enqueue;
- `tool_catalog.pause_parsing` — pause не ставит задачу, resume игнорирует task
  ID и возвращает только progress.

Пропущенных operations с доказанным строгим queue-acceptance contract нет.
Поэтому E05.3 закрыта как **SCOPED COMPLETE / REVIEWED**; это не делает
отложенные операции безопасными и не меняет их legacy-поведение.

## Проверка и следующий шаг

Независимо прошли 252 focused/catalog и 43 boundary/router теста; сохранено
известное предупреждение pytest о `asyncio_loop_scope`. Ruff check, format check
и `git diff --check` прошли.

Старший агент выполнил `make prod-build`. Backend и Celery-контейнеры
пересозданы; `https://localhost/health` вернул `{"status":"ok"}`, backend и
workers healthy. SHA-256 `tool_result.py` и `tool_transport.py` в checkout и
production backend-контейнере совпали.

E05 остаётся **IN PROGRESS**. Следующий этап — E05.4 external/MCP handlers.
