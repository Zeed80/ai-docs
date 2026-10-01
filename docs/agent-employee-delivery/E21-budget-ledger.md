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
  E21.2/E21.3. В production новый reserve API пока никто не вызывает.
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

- Ledger API инертен до E21.2. Старые JSON budgets продолжают действовать как раньше.
- Active time без approval wait, provider/tool attempt hooks, headless email и
  parent-wide historic baseline ещё не реализованы.
- Rollback E21.1: downgrade удаляет nullable link, reservations и ledgers; поскольку
  runtime wiring отсутствует, production usage этой схемы пока не создаётся.
- Экономия модели не измерена: usage-счётчики делегирования недоступны.

Review gate и следующая карточка:

- Независимый review E21.1 и регрессии durable chat/checkpoint выполнены; это
  не проверка будущего provider/runtime wiring E21.2.
- E21.2: baseline reconciliation и reserve/settle во всех фактических provider/tool
  attempts, включая fallback/error/crash и headless email.
- E21.3: active-time intervals без human approval wait и атомарный replan budget.
- Только после E21.2/E21.3, production build/health и review карточка E21 может
  перейти из `IN_PROGRESS` в `REVIEWED`.
