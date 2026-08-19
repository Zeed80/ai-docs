# План доработки агентской системы: секретарь-слой + WorkOrder-слой

Дата: 2026-08-19. Источники: внешняя критика (qwen3.8:27b) секретарь-оркестратора,
верифицированная построчно по коду; собственный построчный разбор секций 11–20
`AGENT_SYSTEM_DEVELOPMENT_PLAN.md`. Каждый пункт подтверждён конкретным
файлом/строкой на момент написания.

Обозначения те же, что в `AGENT_SYSTEM_DEVELOPMENT_PLAN.md`: `[x]` сделано,
`[~]` частично, `[ ]` не начато. У каждого пункта приоритет `P0` (риск/
безопасность, первым) / `P1` (структура) / `P2` (фичи) / `P3` (качество/
эксплуатация). У каждого пункта — «Шаги» (с чего начать → чем закончить,
без оценок времени) и «Готово, когда» (acceptance).

Часть **А** — секретарь-оркестратор (`orchestrator.py`, `agent_loop.py`,
`routes.yml`) — стабильный код, не затрагивается текущим WorkOrder-
рефакторингом. Часть **Б** — durable-runtime (`work_orders.py`,
`work_planning.py`, `work_learning.py`) — активно строится, секции 11–20
плана. Можно вести параллельно.

---

## Часть А — Секретарь-оркестратор

### A1. Единый enforcement-слой для fast-path — `P0` — `[x]` сделано (2026-08-19)

- **Итог**: при реализации нашлось на 2 дыры больше, чем в изначальном
  плане — тот же паттерн (`httpx` напрямую + `_agent_headers()`, без
  `check_tool_execution`) был ещё в `_try_sheet_edit_directly` (соседний
  fast-path, вызывается прямо перед spec-table-patch) и **дважды** в
  `_reconcile_spec_table` (автоматическая пост-обработка после публикации
  worker'ом spec-table — category-error correction и grouping/sort reconcile).
  Итого 4 точки, не 1. Все 4 теперь проходят через `check_tool_execution`
  (три — inline-гейт, `_try_spec_table_patch_directly` — делегированием в
  уже policy-gated `_execute_workspace_spec`). Регрессионные тесты
  (`test_gated_patch_never_applies_directly`,
  `test_gated_sheet_edit_never_applies_directly`) подтверждены в обе
  стороны: падают на исходном коде (gated skill реально уходил в сеть),
  зелёные на исправленном. Полный `tests/ai/` прогнан — все провалы
  предсуществующие (недоступность Postgres/сети/event-loop изоляция
  тестов в песочнице), не regressions.

- **Факт**: `_try_spec_table_patch_directly` (`orchestrator.py:1108`) не
  вызывает `check_tool_execution` вообще — идёт прямым `httpx.AsyncClient`
  POST'ом с самодельными заголовками `_agent_headers()`. Рядом в том же
  файле уже есть правильный образец —
  `_execute_workspace_tool_directly` (~`orchestrator.py:2540`) вызывает
  `check_tool_execution(...)` и блокирует, если `tool_name in
  approval_gates`, **до** похода в сеть. То есть паттерн для копирования уже
  в кодовой базе, просто не применён ко второму fast-path.
- **Шаги**:
  1. Открыть `_execute_workspace_tool_directly` (`orchestrator.py:2540`) —
     это референс: `policy = check_tool_execution(skill_name=tool_name,
     args=..., config=config, approval_gates=approval_gates)`, затем проверка
     `if not policy.allowed or tool_name in approval_gates: ... blocked`.
  2. В `_try_spec_table_patch_directly` (`orchestrator.py:1108-1167`) после
     `parsed = parse_patch_command(...)` и до `await self._outer_send(...)`
     вставить тот же вызов `check_tool_execution` с
     `skill_name="workspace.spec_table_patch"` (завести это имя в
     `gateway.yml`, если его там ещё нет — см. `capability_action_map()` в
     `capability_router.py:374`, там должна появиться запись).
  3. Если `policy.allowed` ложно или skill в `approval_gates` — вернуть
     `False` из функции (турн уходит в обычный dispatch через LLM/worker, как
     и при «команда не распознана»), не отправлять `orchestrator.status`
     «применяю мгновенно».
  4. Прогнать существующий `backend/tests/ai/test_agent_fastpath.py` — не
     должен сломаться (это presentation-only путь, останется разрешён).
  5. Добавить новый тест: временно пометить `workspace.spec_table_patch`
     approval-gated в тестовом `gateway.yml`-фикстуре → fast-path должен
     отказаться от прямого исполнения и провалиться в обычный dispatch.
  6. Повторить шаги 2–5 для flow-status fast-path, если у него тот же паттерн
     (проверить отдельно — искать вызовы `_agent_headers()` без соседнего
     `check_tool_execution` по всему `orchestrator.py`: `grep -n
     "_agent_headers()" orchestrator.py` и для каждого вхождения проверить
     контекст).
- **Готово, когда**: `grep -n "_agent_headers()" orchestrator.py` — каждое
  вхождение либо в уже policy-gated функции, либо получило свою проверку;
  новый тест из шага 5 зелёный.

### A2. Синхронизировать `_GATED_SKILL_MARKERS` с `gateway.yml` — `P0`

- **Факт**: `_GATED_SKILL_MARKERS` (`orchestrator.py:3334-3339`) — ручной
  tuple из 4 пар маркеров (`email.send`, `invoice.approve`,
  `anomaly.resolve`, `table.apply_diff`). `gateway.yml:262-276` содержит 15
  approval gates — 11 из них в tuple не отражены (`invoice.reject`,
  `payment.mark_paid`, `procurement.send_rfq`, `warehouse.confirm_receipt`,
  `norm.activate_rule`, `bom.approve`, `bom.create_purchase_request`,
  `compare.decide`, `doc.batch_ntd_check`, `doc.bulk_delete` и др.) — то есть
  `risk_class()` уже сегодня недооценивает риск для 11 из 15 gates.
- **Шаги**:
  1. В `orchestrator.py` рядом с `risk_class()` (`:3342`) заменить
     статический tuple на загрузку из `gateway.yml` при старте модуля:
     `_GATED_SKILL_MARKERS = tuple(g.replace(".", "__") for g in
     load_gateway_config().approval_gates) + tuple(load_gateway_config
     ().approval_gates)` (оба варианта разделителя, как в текущем tuple —
     `"email.send", "email__send"`).
  2. Найти, откуда `gateway.yml` уже парсится в этом модуле (искать
     `gateway.yml` или `route_table.py` в импортах `orchestrator.py`) —
     переиспользовать тот же loader/кэш, не писать второй парсер.
  3. Добавить startup-assert (в месте, где приложение поднимается, или в
     `validate_capability_catalog()` `capability_router.py:389`, если она уже
     проходит по всем capability): каждый `approval_gates`-пункт из
     `gateway.yml` присутствует в результирующем `_GATED_SKILL_MARKERS`.
  4. Тест: добавить фиктивный gate в тестовый `gateway.yml`, не трогая
     `orchestrator.py` → `risk_class()` на plan с этим skill возвращает
     `"gated"`.
- **Готово, когда**: тест из шага 4 зелёный; статический tuple из кода исчез.

### A3. Разрезать orchestrator.py / agent_loop.py — `P1`

- **Факт**: `orchestrator.py:45` — `from app.ai.agent_loop import
  AgentSession, _execute_skill, _extract_list_count` — два приватных
  (`_`-префикс) символа импортируются напрямую.
- **Шаги**:
  1. `grep -n "^def _execute_skill\|^def _extract_list_count"
     backend/app/ai/agent_loop.py` — найти определения, прочитать сигнатуры и
     все места использования внутри `agent_loop.py` самого.
  2. Переименовать оба (убрать `_`-префикс) в `agent_loop.py`, экспортировать
     через явный `__all__` в начале файла вместе с `AgentSession`.
  3. В `orchestrator.py:45` обновить импорт на публичные имена.
  4. `grep -rn "_execute_skill\|_extract_list_count"` по всему `backend/app`
     — обновить все остальные вызовы (если есть, помимо orchestrator.py и
     самого agent_loop.py).
  5. Прогнать полный `ai/`-тестовый пакет (`pytest backend/tests/ai
     backend/tests/test_agent_*.py`) — переименование не должно ничего
     сломать по смыслу, только имена.
  6. (Отдельным шагом, не обязательно сразу) выписать в docstring на верху
     `orchestrator.py` и `agent_loop.py` 3-5 строк «что публично, что нет» —
     минимальный контракт без выноса в protocol/dataclass, если полный
     рефакторинг границы — это уже отдельная более крупная задача, которую
     можно отложить.
- **Готово, когда**: `grep -rn "from app.ai.agent_loop import" backend/app`
  не содержит имён с `_`-префиксом.

### A4. Пороги/бюджеты из кода в конфиг — `P1`

- **Факт**: `_response_budget_for` (`orchestrator.py:89-99`, значения
  4096/8192) и `aux_quality_budget` (`model_tier.py:178`) — числа хардкожены
  в Python.
- **Шаги**:
  1. Открыть `aiagent/config/routes.yml`, посмотреть текущую структуру
     верхнего уровня (какие секции уже есть) — новый блок добавлять по
     аналогии, не с нуля придумывать формат.
  2. Добавить в `routes.yml` (или отдельный `aiagent/config/budgets.yml`,
     если `routes.yml` тематически про роутинг, не бюджеты — решить по месту)
     секцию вида:
     ```yaml
     response_budgets:
       table: 8192
       document: 8192
       chart: 8192
       tier_large_default: 8192
       default: 4096
     aux_quality_budgets:
       below_large: 1
       large_and_above: 2
     ```
  3. В `orchestrator.py` `_response_budget_for` заменить хардкод на чтение из
     загруженного конфига (переиспользовать существующий loader для
     `routes.yml`, не писать новый).
  4. В `model_tier.py` `aux_quality_budget` — то же самое.
  5. Прогнать `backend/tests/ai/test_agent_*` — значения бюджетов не должны
     измениться по факту (те же цифры, просто источник другой), тесты не
     должны требовать правки логики, только импортов конфига в фикстурах,
     если они на них завязаны напрямую.
- **Готово, когда**: `grep -n "8192\|4096" orchestrator.py model_tier.py` —
  числа отсутствуют в этих файлах, только в YAML.

### A5. Актуализировать статус `ai_config` legacy store — `P2`

- **Факт**: `model_resolver.py:10` — «no longer reads legacy ai_config
  store», но 15 файлов ссылаются на `ai_config` (`router.py`,
  `embeddings.py`, `ollama_client.py`, `task_routing.py`,
  `assignment_groups.py`, `policy_engine.py`, `agent_loop.py`, `main.py`,
  `extraction.py`, `memory.py`, `providers_api.py`, `capability_router.py`,
  `ai_settings.py` и ещё 2).
- **Шаги**:
  1. `grep -n "ai_config" <файл>` по каждому из 15 файлов — выписать
     конкретную строку использования (не просто факт наличия).
  2. Для каждого использования определить категорию: (а) читает из
     `task_routing` под именем `ai_config` по историческим причинам — просто
     переименовать переменную/импорт; (б) реально читает отдельную таблицу
     `ai_config` в БД для чего-то, чего в `task_routing` нет — задокументировать
     назначение; (в) мёртвый код (не вызывается) — удалить.
  3. Составить таблицу «файл → категория → действие» — сохранить как
     подраздел в этом же файле или отдельным issue, прежде чем трогать код.
  4. Только после таблицы — выполнять миграцию категории (а) файл за файлом,
     с прогоном локальных тестов каждого модуля.
  5. Обновить абзац в `CLAUDE.md` про `ai_config` под итоговое состояние.
- **Готово, когда**: таблица закрыта (каждая строка — «мигрировано» или
  «оставлено намеренно, см. почему»); `CLAUDE.md` соответствует коду.

### A6. Наблюдаемость решения о маршруте — `P3`

- **Шаги**:
  1. Проверить `backend/app/ai/audit.py` `AuditCode` — есть ли уже коды вида
     `ROUTE_*`; если нет, добавить `AuditCode.ROUTE_FAST_PATH`,
     `AuditCode.ROUTE_TIER_LLM`, `AuditCode.ROUTE_RECIPE_REPLAY`.
  2. В каждом месте `orchestrator.py`, где ход уходит в конкретный путь
     (fast-path успешен, tier-LLM выбран, recipe replay сработал) —
     записывать соответствующий audit-код через существующий
     `_record_orchestrator_tool_event`/audit-механизм.
  3. Один тест: прогнать 3 разных типа запроса (под fast-path, под recipe,
     под обычный LLM-путь) → проверить, что в audit-логе появился ожидаемый
     код.
- **Готово, когда**: по любому ходу в audit-логе виден маршрут без чтения
  кода.

### A7. Сценарные тесты ядра секретаря — `P3`

- **Шаги**:
  1. Собрать 5-10 реальных фраз пользователей по ядру (счета/аномалии/
     approval) — можно взять из существующих `backend/tests/ai/
     test_agent_routing_golden.py`, если там уже есть заготовки, расширить,
     не дублировать файл.
  2. Найти в `routes.yml` два маркера, которые пересекаются по подстроке
     (например «отсортируй», как отмечено в критике — проверить актуальность
     этого конкретного примера, возможно уже не пересекается) — написать
     тест, фиксирующий, какой побеждает и почему это осознанно (комментарий
     в тесте, не только assert).
  3. Один golden e2e: счёт → аномалия → approval → ответ агента — как
     integration-тест поверх mock/fixture-БД, не поверх продакшен-сервисов.
- **Готово, когда**: новые тесты в `backend/tests/ai/` зелёные и
  задокументированы как «golden», не удаляются при следующих правках
  routes.yml без ревью.

---

## Часть Б — WorkOrder durable-runtime (секции 11–20 плана)

### Б11. Команды агентов — `P2` — решено: делаем сейчас

- **Факт**: `AgentTeam` (`models.py:1525`) — `name/status/purpose/metadata` +
  relationship на `AgentTask`. `WorkOrder.parent_id` принимается API
  (`api/work_orders.py:89,142,204`), но decomposition-логики нет нигде.
- **Шаги** — минимальный team-manager:
  1. В `domain/work_planning.py` (`:112` уже строит план из budgets) —
     добавить шаг типа `kind="decompose"`, который при выполнении создаёт N
     дочерних `WorkOrder` через уже существующую `create_work_order(...,
     parent_id=...)` (`domain/work_orders.py:222`).
  2. Родительский `WorkOrder` переходит в новое состояние ожидания детей
     (проверить текущий FSM в `domain/work_orders.py` — искать
     `status: Mapped[str]` переходы) — добавить `waiting_children` в набор
     статусов.
  3. Beat-джоба (аналог `work.dispatch_ready`) при пересборке родителя
     проверяет: все дети `completed`/`failed` → агрегирует
     `result_summary`.
  4. Решить границы ролей на этом первом заходе минимально: каждый дочерний
     `WorkOrder` наследует `owner_key` и `risk_level` родителя (не выше), но
     получает собственный `budgets`-срез (родительский бюджет делится или
     каждый ребёнок получает явный лимит из плана decompose-шага) — без
     этого дети могут суммарно превысить бюджет, который согласовывался на
     родителя.
  5. Approval: если хотя бы один дочерний шаг approval-gated, approval
     запрашивается на него отдельно (как обычно для WorkStep), родитель не
     получает отдельный «групповой» approval на этом первом заходе —
     упрощение, которое можно расширить позже.
  6. Тест: один WorkOrder с decompose-шагом → 2 дочерних WorkOrder создаются,
     оба завершаются → родитель переходит в `completed` с агрегированным
     summary.
  7. Тест на бюджет: один дочерний WorkOrder превышает унаследованный лимит
     → уходит в `blocked` (переиспользовать механизм из Б15, если Б15 уже
     сделан к этому моменту; если нет — временно без hard-limit, но с явным
     TODO-комментарием на месте, не молчаливым пропуском).
- **Готово, когда**: тесты из шагов 6-7 зелёные.

### Б12. Gap detection для саморасширения — `P2`

- **Факт**: `CapabilityProposal` создаётся только вручную через
  `_create_capability_proposal` (`agent_control_plane.py:548`).
- **Шаги**:
  1. В `domain/work_orders.py` найти место, где пишется
     `WorkStepAttempt.error` при провале (искать `last_error=` или
     `error=` в контексте `WorkStepAttempt`).
  2. Добавить агрегирующий запрос (можно как отдельная функция в
     `domain/work_orders.py` или новый `domain/work_gap_detection.py`):
     группировка по `(capability, action, error.get("type"))` за скользящее
     окно (например 7 дней), порог — N провалов одного типа.
  3. При превышении порога — вызвать существующий путь создания
     `CapabilityProposal` (переиспользовать модель/функцию из
     `agent_control_plane.py`, не дублировать схему), с `evidence` =
     ссылки на конкретные `WorkStepAttempt.id`.
  4. Повесить на Celery beat как периодическую задачу (по аналогии с
     `work.dispatch_ready` в `tasks/celery_app.py`) — не на каждый провал
     синхронно, отдельным батчем.
  5. Тест: 5 провалов одного типа за окно → появляется черновой
     `CapabilityProposal`; approval — как для ручного (без автопромоушена).
- **Готово, когда**: тест из шага 5 зелёный; proposal остаётся draft до
  явного решения человека (не автопромоутится).

### Б13. Интерфейс оператора — `P1`

- **Факт**: `frontend/app/work-orders/page.tsx` (125 строк) — плоский
  список, poll 5с (`:59-60`), кнопка «Запустить следующий шаг» (`:110`),
  `WorkToolCall` не отображается вовсе, approve — только на отдельной
  странице `/approvals`.
- **Шаги** (независимые подпункты, можно по одному):
  1. **Убрать/промаркировать ручной запуск**: строка `:110` — заменить
     кнопку «Запустить следующий шаг» на кнопку с явным лейблом
     «Форсировать шаг вручную (debug)» + confirm-диалог, чтобы отличалась от
     штатного автономного пути; либо спрятать за feature-флагом для
     операторов с повышенными правами.
  2. **Показать WorkToolCall**: добавить эндпоинт (или расширить уже
     существующий `GET /api/work-orders/{id}/plan`, `api/work_orders.py`) —
     отдавать `tool_calls` вместе с шагом; в `page.tsx` рядом с блоком «План»
     (`:113`) добавить раскрывающийся список tool calls на шаг: args, digest,
     result/error.
  3. **Встроить approve/reject**: в блоке шага (`:113`), если
     `step.state === "waiting_approval"` — добавить кнопки, дергающие тот же
     `/api/approvals/{id}/decide`, что использует страница `/approvals`
     (переиспользовать существующий API-клиент оттуда, не писать новый).
  4. **Граф вместо текста зависимостей**: минимально — заменить строку
     `depends: {step.depends_on.join(", ")}` (`:113`) на визуальную
     подсветку (например обводка шагов одного уровня одним цветом), не
     обязательно полноценный DAG-виджет с нуля; полноценный граф — если
     останется время, отдельным пунктом.
  5. **WebSocket вместо poll**: посмотреть, как подключается WS в
     `frontend/app/*` для чата (искать `useWebSocket`/`new WebSocket` в
     `frontend/`), завести аналогичный канал `/api/work-orders/{id}/stream`
     на бэкенде (переиспользовать событийную таблицą, которая уже пишется —
     просто транслировать вместо/вместе с poll), заменить `setInterval` в
     `page.tsx:59-60`.
- **Готово, когда**: каждый подпункт закрыт независимым PR/коммитом с своим
  e2e-тестом в `frontend/tests/e2e/work-orders.spec.ts`.

### Б14. Наблюдаемость — `P3`

- **Шаги**:
  1. Найти таблицу событий, которая уже пишется (`Event`/`WorkOrderEvent` —
     см. `event_type`/`payload` в `page.tsx:17` typing, найти backend-модель
     по имени).
  2. Добавить один агрегирующий эндпоинт `GET /api/work-orders/metrics`:
     counts по `status` за последние 24ч, p50/p95 `finished_at - started_at`
     по `WorkStep.kind`/`capability` — обычный SQL `GROUP BY`, без внешних
     систем метрик.
  3. Показать эти цифры в шапке `/work-orders` (небольшой блок над списком).
- **Готово, когда**: цифры видны в UI, обновляются вместе с общим poll/WS.

### Б15. Бюджеты — `P1`

- **Факт**: `budgets: JSON` читается по двум ключам —
  `max_replans` (`domain/work_orders.py:538,628`), `timeout_seconds`
  (`domain/work_planning.py:154`).
- **Шаги**:
  1. Проверить, отдают ли уже используемые провайдеры (Ollama/Anthropic
     клиенты в `backend/app/ai/`) usage/token-counts в ответе — искать
     `usage` в ответах `ollama_client.py`/anthropic-клиенте.
  2. В `WorkStepAttempt` (`models.py`) добавить колонки
     `tokens_used: int | None`, `cost_usd: numeric | None` — новая alembic
     миграция по аналогии с `20260817_0006_work_order_learning.py`.
  3. В месте, где `WorkStepAttempt` завершается успешно/ошибкой
     (`domain/work_orders.py`, искать `finished_at=` присвоение) — писать
     туда фактический usage из ответа провайдера.
  4. Добавить `token_budget`/`cost_budget` как читаемые ключи `budgets` (как
     сейчас `max_replans`) — перед запуском следующего шага проверять
     накопленную сумму по `work_order_id` (`SUM` по `WorkStepAttempt`), если
     превышен — перевести `WorkOrder` в `blocked` с понятной причиной в
     `blocker` (поле уже есть в модели, `models.py`).
  5. Тест: WorkOrder с низким `token_budget`, шаги расходуют больше → на
     очередном шаге получает `blocked`, а не продолжает исполнение.
- **Готово, когда**: тест из шага 5 зелёный; `budgets` содержит минимум 4
  ключа (было 2).

### Б16. Каналы — `P2`

- **Факт**: 0 файлов email/telegram ссылаются на `WorkOrder`. Важно не
  путать с `email_triage.py` — это отдельный, уже существующий детерминированный
  pipeline обработки счетов («Scenario 1 degraded mode»), не канал поручений
  агенту. Задача здесь — новый путь «письмо с поручением агенту» → `WorkOrder`,
  не переиспользование invoice-пайплайна.
- **Шаги**:
  1. Выбрать один канал для эталона — рекомендация: email, т.к. `imap_client.py`
     уже умеет читать почту, и в `agent.py:150` уже есть прецедент
     Telegram↔WebSocket моста (переиспользовать этот паттерн для email тоже
     возможно после).
  2. В `imap_client.py` (или новом соседнем модуле `agent_email_ingress.py`,
     если мешать в существующий pipeline не стоит по чистоте) — добавить
     правило: письма в отдельную под-папку/с отдельным маркером темы
     трактуются как поручение, не как счёт на обработку.
  3. Создание `WorkOrder` тем же `create_work_order(...)`
     (`domain/work_orders.py:222`) с `source="email"`, `owner_key` = адрес
     отправителя (сверить с существующей auth-моделью — есть ли уже
     маппинг email→user).
  4. Reply-to-source: при `completed`/`blocked` — отправить ответ через уже
     существующий `email_sender.py`, письмом в тот же тред (`In-Reply-To`
     заголовок).
  5. Тест: письмо с маркером → создаётся WorkOrder → фейковый провайдер
     завершает его → приходит ответ на исходный адрес.
- **Готово, когда**: тест из шага 5 зелёный на одном канале (email); telegram
  — отдельным пунктом позже по тому же образцу, не в этом заходе.

### Б17. MCP-интеграция — `P0`

- **Факт**: `mcp_client.py` (475 строк, `stdio`+`HTTP`, auto-reconnect)
  подключён только к `AgentSession` в `agent_loop.py`. В
  `capabilities.generated.yml` — 0 MCP-записей. План ошибочно помечает как
  `[ ]`.
- **Шаги**:
  1. Сразу: в `AGENT_SYSTEM_DEVELOPMENT_PLAN.md` секция 17 — исправить
     формулировку с «MCP/connectors» как единого `[ ]` на явное «MCP-клиент
     реализован для чат-агента (`ai/mcp_client.py`), не подключён к WorkOrder
     capability gateway — `[~]`».
  2. Решено: подключаем MCP-инструменты к WorkOrder-планам. Интеграция
     **только** через `capability_router.py`: каждый MCP-tool должен
     появиться в `capabilities.generated.yml` как обычная capability (с
     `approval_gates`, если внешний), проходить `_enforce_capability_policy`
     (`capability_router.py:585`) наравне со штатными skills — **не**
     отдельный прямой путь из `work_planning.py` к `mcp_client.py`.
  3. Конкретно: в `agent_loop.py` найти, где сейчас `load_mcp_tools(servers)`
     превращает MCP-инструменты в OpenAI function-calling схему для чат-
     `AgentSession` — на старте приложения (там же, где сегодня грузится
     `capabilities.generated.yml`) прогнать тот же `load_mcp_tools` и
     смёржить результат в манифест, который видит `work_planning.py`
     planner, помечая каждый MCP-tool `source: "mcp"` в metadata записи —
     чтобы отличать от штатных skills при аудите.
  4. Approval gates для MCP-tools по умолчанию: пока нет способа заранее
     знать, насколько конкретный внешний MCP-сервер безопасен — **все**
     MCP-инструменты по умолчанию approval-gated при первом подключении
     (whitelist послаблений — отдельным решением позже, не в этом заходе).
  5. `WorkToolCall` для MCP-вызовов пишется тем же механизмом, что и для
     штатных capability (write-ahead, digest, idempotency key) — не отдельным
     кодовым путём, иначе evidence/verifier не увидят вызов.
  6. Тест: MCP-инструмент из манифеста (approval-gated по умолчанию, шаг 4)
     → шаг плана на него уходит в `waiting_approval`, не исполняется
     напрямую — тот же тест-паттерн, что в A1.
  7. Тест на смешанный план: шаг с обычной capability + шаг с MCP-tool в
     одном WorkPlan → оба видны в `/api/work-orders/{id}/plan` одинаково
     (UI из Б13.2 не должен знать про разницу между ними).
- **Готово, когда**: формулировка плана исправлена (шаг 1); тесты из шагов
  6-7 зелёные.

### Б18. Тестирование — `P1`

- **Факт**: весь домен (3049 строк) прикрыт одним файлом
  `test_work_orders.py` (555 строк).
- **Шаги**:
  1. Прочитать текущий `test_work_orders.py` целиком, выписать смысловые
     блоки (по `class Test*`/`def test_*` группам) — вероятно уже неявно
     сгруппированы по темам (lease, verifier, replanning, computer_use).
  2. Разбить механически по этим темам на файлы:
     `test_work_order_lease.py`, `test_work_order_verifier.py`,
     `test_work_order_replanning.py`, `test_computer_use_grants.py`,
     оставив в `test_work_orders.py` только CRUD/API-уровень. Ничего не
     переписывать по смыслу на этом шаге — чистый split.
  3. Добавить новый тест на race condition: два асинхронных воркера
     одновременно вызывают дозреватель `SKIP LOCKED`-запроса (искать
     функцию дозревания ready-шагов в `domain/work_orders.py`, вероятно
     рядом с `work.dispatch_ready` логикой) над одним `WorkStep` — assert,
     что только один получает lease, второй видит шаг уже занятым.
  4. Добавить тест на просроченный lease: `lease_expires_at` в прошлом →
     следующий дозреватель считает шаг доступным для повторного захвата.
- **Готово, когда**: файлы из шага 2 существуют и проходят; тесты из шагов
  3-4 зелёные.

### Б19. Production rollout — `P3`

- **Факт**: в `infra/` нет staging/canary compose-профиля.
- **Шаги**:
  1. Скопировать `infra/docker-compose.prod.yml` в
     `infra/docker-compose.staging.yml`, заменить только домены/секреты на
     staging-эквиваленты (искать существующие env-паттерны в
     `docker-compose.prod.yml`, не выдумывать новую схему конфигурации).
  2. Один параграф в `docs/`/`AGENT_SYSTEM_DEVELOPMENT_PLAN.md` — процедура
     отката: `git checkout <previous tag> && docker compose -f
     docker-compose.prod.yml up -d --build` + что проверить после (health-
     эндпоинт, smoke-тест).
- **Готово, когда**: `docker compose -f infra/docker-compose.staging.yml
  config` проходит без ошибок валидации.

### Б20. Документация — `P3`

- **Шаги**:
  1. После Б13 (не раньше — иначе описывать то, что тут же меняется) — один
     файл `docs/work-orders-operator-guide.md`: что означает каждый статус
     из `statusClass` (`page.tsx:22-27`), когда использовать ручной запуск
     (теперь явный debug-флаг из Б13.1), как читать evidence/tool calls.
- **Готово, когда**: файл существует, ссылка на него — в
  `AGENT_SYSTEM_DEVELOPMENT_PLAN.md` секция 20.

---

## Рекомендуемый порядок исполнения

1. **P0 сначала, обе части параллельно**: A1 → A2 → Б17.1 (формулировка,
   тривиально) → Б17 (полная интеграция, шаги 2-7). Все три — один класс
   риска (обход policy/approval); Б17 теперь решён на «да», так что делаем
   его целиком в этом же проходе, не только формулировку.
2. **P1**: A3 → A4 → Б13 (по подпунктам, независимо) → Б15 → Б18.
   Б15 (бюджеты) стоит подвинуть перед Б11, т.к. Б11.4/Б11.7 (наследование
   бюджета детьми) на неё ссылается — без Б15 у Б11 нет hard-limit, только
   TODO-заглушка.
3. **P2**: A5 → Б11 (после Б15) → Б12 → Б16.
4. **P3**: A6 → A7 → Б14 → Б19 → Б20.

Оба стоп-пункта решены пользователем: **Б11 — да** (декомпозиция нужна
сейчас), **Б17.2 — да** (MCP-инструменты подключаются к WorkOrder-планам).
Оставшиеся зависимости между пунктами: Б11 логически проще делать после Б15
(бюджет-наследование), Б17 полная интеграция — после A1/A2 (единый
enforcement-слой, через который MCP-tools и должны проходить). Всё
остальное — независимые самодостаточные задачи, можно раздавать в любом
порядке.
