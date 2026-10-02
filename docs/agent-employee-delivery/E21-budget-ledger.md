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

## E21.2b1 — physical nested tool/transport attempts durable AgentSession

Карточка / статус / дата / base commit / commit результата:

- E21 и E21.2 остаются `IN_PROGRESS`; ограниченный этап E21.2b1 имеет статус
  `TESTED` и передан на независимую приёмку главному агенту.
- Дата: 2 октября 2026 года.
- Base: `ff9cf7df` (`Enforce durable streaming provider call budgets`).
- Commit/deploy выполняет только главный агент; исполнитель их не делал.
- Исполнитель: `gpt-5.6-sol`, high; один цикл замечаний сеньора. Учитывался
  приоритет nonterminal checkpoint/journal перед отложенным settlement blocker.
  Экономия модели не измерена.

Изменено (пути и контракт):

- `backend/app/ai/work_budget_context.py`: тот же immutable
  `WorkBudgetContext(work_order_id, step_id, attempt_id, session_factory)` теперь
  independently резервирует `tool_attempts=1` непосредственно перед каждым
  физическим HTTP-вызовом и settlement-ит единицу после ответа, ошибки или
  `BaseException`. Новая reservation никогда не открывается из существующей;
  live lease повторно проверяется после reserve, а E21.2a execution marker
  остаётся единым fence всего attempt.
- `backend/app/ai/agent_loop.py`: durable AgentSession передаёт context в обычный
  nested dispatch и существующий fast-intent route. Policy, approval, разбор
  аргументов, headers и создание клиента происходят до reserve. Unsupported
  method, неизвестный skill, approval pause/reject и другой local preflight
  расходуют ноль. Каждый разрешённый reviewed read transport retry получает
  отдельный reserve/charge; write/unknown не получил нового retry.
- Ошибка settlement после dispatch становится sticky
  `BudgetExecutionStopped`, но уже известный ToolResult сначала проходит
  существующие history/checkpoint/action-journal границы. Для обычного known
  result worker затем сохраняет явный budget blocker и не продолжает tool/LLM
  tail. `partial`/`outcome_unknown`/`waiting_approval` после durable journal
  сохраняют более сильное существующее nonterminal состояние; обычный resume
  остаётся запрещённым, а reservation остаётся consumed evidence.
- `backend/app/tasks/durable_chat.py`: `chat.tool_call` остаётся audit event, но
  удалён как физический счётчик по старому JSON `max_tool_calls`. Авторитетный
  default 200 или явный root limit теперь берётся только из shared ledger.
- `backend/tests/test_work_budget_tools.py`,
  `backend/tests/test_chat_checkpoints.py`, `backend/tests/test_durable_chat.py`:
  PostgreSQL/fake-transport regressions для max=0, physical retry, failure/crash,
  parent/child last slot, approval, lease/cancel, duplicate replay, settlement,
  journal/checkpoint и API resume.

Не изменено и почему:

- `tool_transport.py` policy и существующие списки reviewed read/write effects
  не расширялись; E21.2b1 считает фактические попытки, но не разрешает новые.
- AIRouter planner/verifier, generic/headless executors, scenario runner,
  email/background LLM paths, token/cost bounds, active time, replans и legacy
  parent/child reconciliation остаются следующими E21.2b+/E21.3 этапами.
- Вызовы `execute_skill` вне durable AgentSession без server-owned context не
  объявлены покрытыми. Durable context с отсутствующим ledger fail-closed с
  `legacy_budget_baseline_required`; исторический baseline не придумывается.
- `AGENT_EMPLOYEE_EXECUTION_PLAYBOOK.md` — root-owned параллельный WIP и этим
  исполнителем не редактировался. Несвязанные CAD-модули не затрагивались.

Проверки (точные команды, passed/failed/skipped):

- `python3 -m pytest backend/tests/test_work_budget_provider.py backend/tests/test_tool_transport.py -q`
  — 222 passed, 0 failed, 0 skipped.
- Первый новый прогон `python3 -m pytest backend/tests/test_work_budget_tools.py -q`
  — 8 passed, 1 failed, 0 skipped: test-only monkeypatch `asyncio.sleep` вызывал
  сам себя. Production-код не менялся; исправленный финальный повтор — 16 passed,
  0 failed, 0 skipped.
- Первый совместный checkpoint/durable повтор после правки — 98 passed, 1 failed,
  0 skipped: новый тест сравнивал полный dict результата с одной строкой `Answer`.
  Assertion приведён к фактическому контракту `result["text"]`; расширенный
  повтор после добавления review-regressions — 104 passed, 0 failed, 0 skipped.
- Frozen-профиль
  `python3 -m pytest backend/tests/test_work_budget_ledger.py backend/tests/test_work_budget_provider.py backend/tests/test_work_budget_tools.py backend/tests/test_durable_chat.py backend/tests/test_chat_checkpoints.py backend/tests/test_work_order_checkpoint.py backend/tests/test_tool_transport.py -q`
  — 379 passed, 0 failed, 0 skipped за 28.36s.
- Дополнительная граница
  `python3 -m pytest backend/tests/test_work_orders.py backend/tests/test_agent_execution_boundary.py backend/tests/test_agent_delegations.py backend/tests/test_action_receipts.py backend/tests/test_chat_action_journal.py -q`
  — 113 passed, 0 failed, 0 skipped за 21.29s.
- `ruff check` и `ruff format --check` по трём production- и трём test-файлам —
  passed; `git diff --check` — passed. Во всех pytest-прогонах осталось известное
  предупреждение `Unknown config option: asyncio_loop_scope`.

Конкурентность/безопасность:

- Два разных fresh attempts root/child с общим ledger и последним tool slot
  стартовали параллельно. Ровно один создал tool reservation и пересёк HTTP
  boundary; второй получил `tool_attempt_budget_exceeded`. Тест не маскируется
  same-attempt execution marker.
- Reviewed read с transport failure и успешным повтором создал две charged
  reservations. Write transport ambiguity создал одну charged reservation и
  `outcome_unknown` без повтора. `BaseException` после dispatch также charged;
  новый context того же attempt остановлен E21.2a marker до HTTP.
- Cancel после independently committed tool reserve, но до HTTP, оставил reserve
  consumed и остановил effect по live lease. Max=0, broken reserve и legacy
  unbound останавливаются до HTTP. Approval pause расходует ноль; авторизованный
  вызов — ровно одну попытку.
- Settlement DB failure не открывает read retry. Known result записан как
  `tool_recorded`, затем WorkOrder заблокирован typed budget error. Unknown result
  записан как `outcome_unknown`, `can_replay=false`, checkpoint `can_resume=false`,
  обычный resume API возвращает 409; independent recipient verification остаётся
  отдельным существующим gate.

Production / известные ограничения / rollback / следующий gate:

- Исполнитель по ограничению задания не выполнял `make prod-build`, restart,
  `/health`, commit или push. Реальные LLM, SMTP, Telegram и деловые эффекты не
  вызывались; HTTP использовал только fakes.
- При полной недоступности budget DB может быть невозможно сохранить новый
  checkpoint/blocker. Независимо committed reservation остаётся consumed/reserved
  evidence, а execution marker запрещает слепой replay; отчёт не выдаёт это за
  подтверждённый recipient outcome.
- E21.2b1 не доказывает полный parent/legacy integration и не закрывает E21.2.
  Следующий gate — независимый review diff главным агентом, production
  build/health и отдельные узкие E21.2b+ этапы для token/cost, AIRouter/headless
  путей и legacy reconciliation.

### Независимая приёмка E21.2b1 главным агентом — 2 октября 2026

- Исполнитель: `gpt-5.6-sol`; один пишущий агент, без дочерних агентов.
  Review исправил приоритет nonterminal результата перед sticky budget stop;
  после freeze главный агент независимо проверил diff и негативный resume gate.
- Независимый профиль ledger/provider/tools/durable/checkpoints/work_orders/
  channel parity/execution boundary/delegations/transport: **423 passed**, без
  failed/skipped. Единственный warning — существующий `asyncio_loop_scope`.
  Ruff check, format-check шести Python-файлов и `git diff --check` прошли.
- `make prod-build` завершился с кодом 0: backend и workers пересозданы;
  неизменённый frontend остался healthy. `/health`: `{"status":"ok"}`.
  Backend и основной worker healthy, beat запущен; Alembic `20261001_0001 (head)`.
  SHA256 трёх production-файлов совпадает между checkout/backend/worker.
- E21.2b1 принят и развёрнут. E21 остаётся IN_PROGRESS; следующий этап должен
  отдельно охватить AIRouter/headless, затем доказуемый token/cost accounting,
  legacy reconciliation и E21.3. Push не выполнялся: запрет публикации
  накопленной истории не снят. Экономия лимитов количественно не измерялась.

## E21.2b2 — physical capability WorkStep HTTP attempts

Карточка / статус / дата / base:

- E21 и E21.2 остаются `IN_PROGRESS`; ограниченный этап E21.2b2 имеет статус
  `TESTED` и передан на независимую приёмку главному агенту.
- Дата: 2 октября 2026 года. Base принятой линии: `6e6ddd29`.
- Исполнитель: `gpt-5.6-sol`, resumed после usage limit предыдущего исполнителя;
  один цикл реализации с ранними замечаниями главного агента. Commit/deploy/push
  выполняет только главный агент. Экономия модели не измерена.

Изменено и контракт:

- `backend/app/tasks/work_orders.py`: только generic `kind=capability` получает
  authoritative `WorkBudgetContext(order/step/attempt)`. После local preflight и
  непосредственно перед каждым physical POST атомарно резервируется
  `tool_attempts=1`; любой HTTP response, transport error или `BaseException`
  settlement-ит одну попытку. Existing `WorkToolCall` и execution marker остаются
  fence одного attempt; reservation не разрешает replay.
- Max=0, legacy unbound, reserve DB failure и cancellation после reserve
  останавливают HTTP fail-closed. Root/child конкурируют за общий последний slot.
  Новых retries, permissions или approval semantics не добавлено.
- Settlement failure после ответа сначала сохраняет исходный HTTP body/text,
  status и checkpoint, затем ставит typed budget blocker. HTTP response означает
  только полученный ответ, а не подтверждение business effect. Transport failure
  сохраняется как unconfirmed evidence и не выдаётся за известный result.
- Validated v1 `partial`/`outcome_unknown` сохраняют lifecycle и блокируют tail,
  dependents, verifier и replan. `waiting_approval` сохраняет прежний generic
  approval gate; после решения новый attempt обязан сделать новый reserve из того
  же ledger. Arbitrary legacy 423 не становится ToolResult и не доверяет recipient
  checkpoint. Legacy partial при failed settlement сохраняет checkpoint/output,
  но получает typed budget blocker без retry.
- `backend/tests/test_work_budget_work_orders.py`: isolated PostgreSQL и fake HTTP
  покрывают zero/legacy/preflight/reserve failure, 4xx/5xx/transport/crash,
  malformed JSON/client-exit, known/unconfirmed settlement failure, v1 lifecycle,
  raw 423, approved fresh attempt, duplicate fence, shared root/child slot и cancel.

Проверки исполнителя:

- Финальный frozen профиль
  `python3 -m pytest backend/tests/test_work_budget_work_orders.py backend/tests/test_work_order_checkpoint.py backend/tests/test_work_orders.py backend/tests/test_work_order_lease.py backend/tests/test_work_order_replanning.py backend/tests/test_work_order_verifier.py -q`
  — 92 passed, 0 failed, 0 skipped за 14.84s.
- Предыдущий budget regression
  `python3 -m pytest backend/tests/test_work_budget_ledger.py backend/tests/test_work_budget_provider.py backend/tests/test_work_budget_tools.py backend/tests/test_chat_checkpoints.py backend/tests/test_durable_chat.py -q`
  — 133 passed, 0 failed, 0 skipped за 28.44s.
- Единственное предупреждение pytest — существующий unknown config
  `asyncio_loop_scope`. Ruff и `git diff --check` проверяются на frozen diff.

Не изменено / ограничения:

- Planner/verifier direct Ollama, headless AgentSession, token/cost bounds,
  legacy reconciliation, active time и replans не входят в E21.2b2.
- При полной недоступности budget DB durable persistence blocker также может быть
  недоступна; independently committed reserve/marker остаются evidence, но не
  подтверждают outcome получателя.
- Исполнитель не выполнял production build/restart, `/health`, commit или push;
  реальные capability/LLM/SMTP/Telegram effects не вызывались.

### Независимая приёмка E21.2b2 главным агентом

- Исполнитель `gpt-5.6-sol` завершил сохранённый WIP после восстановления лимита.
  Два тематических цикла review: сохранение/классификация ответа при malformed
  JSON и client-exit; raw 423 не получает доверенный ToolResult/checkpoint,
  исходный v1 failed body сохраняется без замены нормализованным ответом.
  Последующие уточнения и соответствующие негативные тесты включены в эти циклы.
- Независимые чистые testcontainer-процессы: прежний профиль budget/durable/
  checkpoint/orders/channel/boundary/delegations/transport — **423 passed**;
  lease/replanning/verifier — **27 passed**; новый профиль — **21 passed**.
  Всего 471 проверка без failed/skipped в раздельных прогонах. Ruff check,
  format-check и `git diff --check` прошли; известный warning `asyncio_loop_scope`.
- Первый объединённый прогон: 468 passed, 3 failed. Причина — session-level БД
  и старые глобальные запросы: ledger тест увидел reservations нового профиля,
  lease тесты получили чужие ready steps. Assertions не ослаблены; отдельные
  чистые процессы воспроизвели все три теста успешно. Полный объединённый suite
  не объявляется зелёным; исправление изоляции fixtures остаётся отдельной задачей.
- `make prod-build`: exit 0; backend/workers пересозданы, неизменённый frontend
  healthy. `/health`: `{"status":"ok"}`; backend/основной worker healthy, beat up.
  Alembic `20261001_0001 (head)`. SHA256 `tasks/work_orders.py` одинаковый в
  checkout/backend/worker. Реальные деловые эффекты и LLM не запускались.
- E21.2b2 REVIEWED / DEPLOYED, полная E21 остаётся IN_PROGRESS. Следующий узкий
  этап — authoritative lifecycle бюджетирования direct Ollama planner/verifier
  либо безопасная миграция headless; не прикреплять выдуманный running attempt
  и не включать недоказанные token/cost bounds. Экономия лимитов не измерена.
  Создаётся scoped локальный commit; новый push в этом этапе не выполняется.
