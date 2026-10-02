# E21 — единый budget ledger

Карточка / статус / дата / base commit / commit результата:

- E21 остаётся `IN_PROGRESS`; подкарточка E21.1 независимо проверена и выпущена (`REVIEWED / DEPLOYED`).
- Дата: 1 октября 2026 года.
- Base: `6724fd2e` (`Retire legacy agent WebSocket lifecycle`).
- Commit результата: фиксируется главным агентом в Git вместе с отчётом.
- Исполнитель: `gpt-5.6-sol`, два цикла замечаний сеньора.

Изменено (пути и контракт):

- `backend/app/db/work_budget_models.py`: отдельные модели общего ledger и
  неизменяемых reservations; деньги и единицы хранятся как `Numeric(30, 8)`.
- `backend/app/db/models.py`: регистрация моделей и nullable FK
  `WorkOrder.budget_ledger_id`. FK цикла именован и вынесен через `use_alter`.
- `backend/migrations/versions/20261001_0001_work_budget_ledger.py`: только
  additive/reversible schema. Существующие WorkOrder не обновляются и не получают
  ложный нулевой baseline.
- `backend/app/domain/work_budget_ledger.py`: server-side root resolution по
  `parent_id`, root-first locks, owner/cycle/binding checks, общий ledger для
  descendants, independently committed reserve/settle, immutable operation и
  request digests, точный Decimal fingerprint без context rounding.
- `backend/tests/test_work_budget_ledger.py`: defaults/explicit zero, invalid
  Decimal, parent/child concurrency, последний общий слот, legacy rejection,
  idempotency/conflicts, unknown/crash, actual overrun, migration cycle.

Не изменено и почему:

- `tasks/work_orders.py`, provider adapters, `AgentSession`, durable chat/email,
  active-time/replan accounting и старый `enforce_budgets` не подключались: это
  E21.2/E21.3. На момент выпуска E21.1 API был инертен; текущий статус подключения
  описан ниже в разделе E21.2a.
- Старые lineage с attempts, WorkToolCall, `chat.tool_call`, execution timestamps/
  statuses или plan revisions не связываются с пустым ledger: сервис возвращает
  `LegacyBudgetBaselineRequired`. Исторический baseline требует отдельной
  миграции E21.2; неизвестные token/cost нельзя заменять нулём.
- Tighter descendant limits требуют явной reconciliation; они не теряются при
  переходе на parent ledger.

Проверки (точные команды, passed/failed/skipped, где запущены):

- `python3 -m pytest backend/tests/test_work_budget_ledger.py -q` — 21 passed,
  0 failed, 0 skipped; реальный изолированный PostgreSQL testcontainer.
- `python3 -m pytest backend/tests/test_work_order_lease.py backend/tests/test_work_order_decompose.py backend/tests/test_work_orders.py -q`
  — 42 passed, 0 failed, 0 skipped.
- `ruff check backend/app/db/work_budget_models.py backend/app/db/models.py backend/app/domain/work_budget_ledger.py backend/tests/test_work_budget_ledger.py backend/migrations/versions/20261001_0001_work_budget_ledger.py`
  — passed.
- `git diff --check` — passed.
- Один промежуточный повтор не дошёл до DB assertions: fixture попыталась открыть
  Docker socket для testcontainers и получила sandbox `PermissionError`
  (8 pure tests passed, 11 setup errors). Повтор с разрешённым testcontainer дал
  19/19; исходный инфраструктурный результат не скрыт.
- Новый boundary regression сначала дал 20 passed / 1 failed: unary Decimal
  negation выполнился до входа в `localcontext` и оставил blocker
  `budget_actual_overrun` вместо точного `budget_charge_exceeded`. Арифметика
  subtraction перенесена внутрь precision-64 context; точечный повтор 1/1 и
  полный финальный повтор 21/21 прошли.
- Общее предупреждение pytest `Unknown config option: asyncio_loop_scope` осталось.

Конкурентность/безопасность:

- `SELECT ... FOR UPDATE` общего ledger сериализует root и child: из двух
  конкурентных попыток занять последний tool slot проходит ровно одна.
- Exact duplicate reserve/settle идемпотентен; смена order/dimension/units или
  SHA-256 request fingerprint конфликтует. Decimal `1` и `1.0` совпадают, а два
  разных 30-significant-digit значения не схлопываются.
- `unknown` сохраняет весь reserve. Неизвестная стоимость хранится как NULL плюс
  explicit marker/blocker и запрещает следующий cost reserve. Actual выше reserve
  сохраняется полностью и выставляет blocker.
- Повтор `reserve_budget` для уже `charged`/`unknown` возвращает старую запись
  только как свидетельство. Это не разрешение повторно dispatch effect; E21.2
  обязан проверить state перед внешним вызовом.

Production:

- Исполнитель не выполнял deploy. Главный агент независимо прочитал diff и
  проверил `test_work_budget_ledger.py` после freeze: 21 passed; Ruff passed.
- Независимый общий прогон ledger/durable chat/chat checkpoints/work order
  checkpoints: 132 passed до последнего arithmetic regression; окончательный
  frozen ledger отдельно повторно проверен (21 passed). Числа не суммируются.
- `make prod-build` — exit 0: backend/workers пересобраны и перезапущены;
  неизменённый frontend использовал уже здоровый deployment E20.
- `/health` — `{"status":"ok"}`; backend/frontend/celery-worker healthy,
  celery-beat up. `alembic current` — `20261001_0001 (head)`.
- Schema применена, но runtime wiring отсутствует: фактическое исполнение
  продолжает пользоваться прежними budget checks до E21.2/E21.3.
- Реальные LLM/SMTP/Telegram/деловые эффекты не запускались.

Известные ограничения / выключенные флаги / rollback:

- Самостоятельный E21.1 был инертен; E21.2a подключает только physical streaming
  calls долговечного чата. Остальные JSON budgets пока действуют как раньше.
- Active time без approval wait, provider/tool attempt hooks, headless email и
  parent-wide historic baseline ещё не реализованы.
- Downgrade E21.1 удаляет nullable link, reservations и ledgers. После включения
  E21.2a он может уничтожить реальный учёт бюджета и не является безопасным
  автоматическим rollback; нужен отдельный план с сохранением ledger evidence.
- Экономия модели не измерена: usage-счётчики делегирования недоступны.

Review gate и следующая карточка:

- Независимый review E21.1 и регрессии durable chat/checkpoint выполнены; это
  не проверка будущего provider/runtime wiring E21.2.
- E21.2: baseline reconciliation и reserve/settle во всех фактических provider/tool
  attempts, включая fallback/error/crash и headless email.
- E21.3: active-time intervals без human approval wait и атомарный replan budget.
- Только после E21.2/E21.3, production build/health и review карточка E21 может
  перейти из `IN_PROGRESS` в `REVIEWED`.

## E21.2a — intake ledger и physical streaming provider calls

Карточка / статус / дата / base commit / commit результата:

- E21 и E21.2 остаются `IN_PROGRESS`; ограниченный этап E21.2a имеет статус
  `REVIEWED / DEPLOYED` после независимой приёмки главным агентом.
- Дата: 2 октября 2026 года.
- Base реализации ledger: `8bfb7175` (`Add atomic shared work budget ledger foundation`).
  Фактический HEAD при freeze: `9faaca64`; более поздние CAD-коммиты не относятся
  к E21.2a и не изменялись исполнителем.
- Commit результата создаёт только главный агент; исполнитель commit/deploy/push не делал.
- Исполнитель: `gpt-5.6-sol`, high; два цикла review. Экономия не измерена.

Изменено (пути и контракт):

- `backend/app/domain/agent_intake.py`: новый WorkOrder получает ledger внутри той
  же транзакции, что user message, DurableChatRun и plan. Ошибка последующей
  записи откатывает ledger вместе со всем intake.
- `backend/app/domain/work_budget_ledger.py`: выделен внутритранзакционный
  `initialize_budget_ledger`; сохранён прежний independently committed API.
  Для effect-dispatch добавлен ответ `created`, потому что существующая reservation
  является только свидетельством прежней границы, а не разрешением повторить вызов.
- `backend/app/ai/work_budget_context.py`: immutable identity WorkOrder/step/attempt
  и injected `expire_on_commit=False` session factory; sticky typed stop;
  нулевой `execution:{attempt_id}` marker; проверка live lease; атомарный reserve
  одного `llm_calls` до каждого physical provider attempt и settlement после
  success/error/BaseException. Marker имеет units=0 и не считается LLM-вызовом.
- `backend/app/ai/agent_loop.py`: реальный streaming dispatcher учитывает primary,
  Ollama retry и fallback как отдельные physical calls. Ошибка budget DB,
  исчерпание limit и неподтверждённый finite token/cost bound не попадают в
  provider retry/fallback. Sticky stop проверяется перед tool boundary и перед
  сохранением финального ответа.
- `backend/app/tasks/durable_chat.py`, `backend/app/tasks/work_orders.py`: context
  создаётся из server-owned execution IDs; typed stop сохраняет явный blocker,
  переводит step в failed, очищает lease/next attempt и не запускает replan.
  Duplicate delivery сериализуется блокировкой WorkStepAttempt и существующим
  WorkToolCall marker; проигравший worker не меняет состояние общего attempt.
- `backend/tests/test_work_budget_provider.py`,
  `backend/tests/test_durable_chat.py`: реальные provider fakes и PostgreSQL
  assertions для success/error/retry/fallback, DB failures, crash, duplicate
  workers, max=0, finite token/cost, legacy unbound, intake idempotency/rollback.

Не изменено и почему:

- Nested tool attempts, tool transport accounting, AIRouter planner/verifier,
  generic/headless executors, email и прочие фоновые LLM-пути относятся к E21.2b+.
- Token/cost accounting и provider-specific доказанные upper bounds не добавлены.
  Поэтому любой finite token или cost cap на этом пути останавливает вызов до
  provider; `max_output_tokens` не выдаётся за bound total tokens.
- Legacy baseline не придуман: unbound historic work получает
  `legacy_budget_baseline_required`, а не пустой ledger с нулевым usage.
- Active-time intervals и общее replan accounting остаются E21.3.
- `AGENT_EMPLOYEE_EXECUTION_PLAYBOOK.md` уже был изменён в исходном WIP; исполнитель
  этот root-owned diff не редактировал.

Проверки (точные команды, passed/failed/skipped):

- `python3 -m pytest backend/tests/test_work_budget_provider.py -q` — 8 passed,
  0 failed, 0 skipped.
- `python3 -m pytest backend/tests/test_durable_chat.py -q` — 40 passed,
  0 failed, 0 skipped.
- `python3 -m pytest backend/tests/test_work_budget_ledger.py -q` — 21 passed,
  0 failed, 0 skipped.
- `python3 -m pytest backend/tests/test_chat_checkpoints.py backend/tests/test_work_order_checkpoint.py backend/tests/test_work_orders.py -q`
  — финальный повтор 90 passed, 0 failed, 0 skipped.
- `python3 -m pytest backend/tests/test_agent_channel_parity.py -q` — 5 passed,
  0 failed, 0 skipped.
- Единый финальный профиль всех семи файлов выше — 164 passed, 0 failed,
  0 skipped за 25.36s на изолированном PostgreSQL testcontainer.
- `ruff check` по шести production-файлам и трём целевым test-файлам — passed;
  `ruff format --check` — passed; `git diff --check` — passed.
- Первый checkpoint/work-order regression прогон дал 78 passed / 12 failed:
  старые unit fixtures создавали AgentSession без `__init__`, а новый preflight
  напрямую читал отсутствующий optional attribute. Доступ сделан совместимым
  через `getattr`; полный повтор выше прошёл. Предупреждение pytest о неизвестном
  `asyncio_loop_scope` осталось; skipped не было.
- Первый запуск трёх lock-order regressions не дошёл до setup: локальная тестовая
  БД была недоступна, а sandbox запретил Docker socket testcontainers. Повтор с
  разрешённым изолированным testcontainer — 3 passed; это не test failure.

Конкурентность/безопасность:

- Два настоящих `execute_claimed_step` одного attempt дали ровно один provider
  dispatch: победитель завершил step, проигравший вернул false и не снял его lease.
- Конкурентный cancel, уже удерживающий WorkOrder lock, и prepare завершились без
  deadlock: prepare дождался root lock, перечитал canceled step, не создал
  WorkToolCall и не вызвал provider. Initial prepare использует порядок
  WorkOrder -> WorkStepAttempt без дополнительного `FOR UPDATE` на WorkStep.
- Два прямых streaming dispatcher context с одинаковым attempt, но разными
  provider/request payload, создали один execution marker и один physical call;
  второй получил `llm_execution_already_started` до provider.
- Provider crash после reserve оставил physical call charged; новый context того
  же attempt не смог повторить provider. Ошибка settlement оставила reservation
  consumed/reserved и не открыла fallback.
- DB failure во время reserve/settlement превращается в sticky
  `BudgetExecutionStopped`; broad `Exception` recovery его не подавляет.
- Intake retry возвращает тот же WorkOrder и один ledger; forced plan failure
  не оставляет ни message/order/run, ни orphan ledger.

Production:

- По прямому ограничению задания исполнитель не выполнял deploy, `make prod-build`,
  restart или `/health`. Главный агент выполнил их после review 2 октября 2026:
  `make prod-build` exit 0; `/health` → `{"status":"ok"}`;
  backend/frontend/celery-worker healthy, celery-beat up;
  `alembic current` → `20261001_0001 (head)`, новых миграций в E21.2a нет.
- SHA-256 шести production-файлов E21.2a совпали в рабочем дереве, backend
  и celery-worker; health не выдан за проверку живой LLM.
- Окончательный независимый прогон ledger/provider/durable chat/checkpoints/
  work orders/channel parity/execution boundary/delegations/tool transport:
  **405 passed**, 0 failed, 0 skipped. Ruff check и format-check прошли.
- История checkout содержит параллельные CAD-коммиты; они не переписывались
  и не включаются в scoped diff E21.2a. Push накопленной истории не выполняется.
- Реальные LLM, SMTP, Telegram и деловые внешние вызовы не выполнялись.

Известные ограничения / rollback / следующий gate:

- Это только single physical AgentSession streaming context долговечного чата;
  отчёт не утверждает, что все LLM/tool paths системы уже учтены.
- Execution marker остаётся после crash намеренно. Автоматического replay того же
  attempt нет; безопасное продолжение требует новой явно авторизованной попытки и
  последующего контракта E21.2b+.
- Следующий этап — E21.2b+: verified token/cost bounds/accounting, nested tools,
  AIRouter/headless/legacy reconciliation. E21 нельзя закрывать до E21.2/E21.3,
  production validation и независимого review.
