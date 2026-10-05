# E21.2b8 — Budget handoff для SQL-table HTTP recipient

Дата: 5 октября 2026. Исходный commit: `4456e16a`.
Исполнитель: `gpt-5.6-sol`, high (прерван usage limit на частичной реализации).
Root довёл код и тесты; отдельный `gpt-5.6-sol`, high выполняет read-only review.
Статус: TESTED / REVIEWED / DEPLOYED.

## Контракт ограниченного среза

Пилот охватывает direct catalog `workspace.sql_table` и фиксированный POST
`/api/workspace/agent/generated/sql-table`. Server-owned подписанный handoff
связывает точные method/path/body, actor, WorkOrder, step/attempt, ledger,
plan/revision и резерв родительской HTTP-попытки. Получатель отдельно проверяет
service key, execution actor, активного владельца и актуальную execution lease.

Model calls получателя используют общий ledger с родителем и отдельные
operation keys. Одноразовая recipient fence запрещает повторное исполнение.
Резерв родительской tool attempt должен оставаться `reserved` перед каждой
physical model attempt, после закрытия provider client и перед публикацией.
Timeout/settlement родителя не разрешает detached получателю продолжать работу.

Повторная попытка может подтвердить только отсутствие dispatch этим запросом;
она не доказывает отсутствие эффекта предыдущей попытки. Наблюдаемый ответ
получателя сохраняется вызывающим агентом до sticky budget stop, без нового
model tail или recovery. Синхронная запись результата требует актуальной fence.

Generic capability proxy в этом пилоте не переносит полномочия handoff.
Неподдерживаемые employee SQL-table вызовы блокируются до LLM. Existing human
flow, SQL allowlist, approval gates и запрет production-data migration сохраняются.
Token/cost bounds и прочие HTTP recipients остаются остатком E21.

## Проверки

Baseline root до изменений: `test_agent_execution_boundary.py` и
`test_agent_sql_perimeter.py` — 36 passed в изолированной testcontainers БД.
Это не является приёмкой окончательного diff.

Root проверил handoff, physical calls, действительную SQL/title цепочку,
конкурентный once-fence, stale binding/actor/plan, timeout родителя после закрытия
model client и checkpoint перед sticky stop. Publication использует order →
ledger → User locks; step/attempt не блокируются в обратном порядке относительно
heartbeat. Проверка свежей lease проводится в той же transaction прямо перед
синхронной записью, без промежуточного закрытия другой DB session.

Первичные диагностические тесты: collection import error исправлен на package
`tests.*`; 14 passed / 9 failed обнаружили async SQL mock и inactive shared actor
fixture; затем 22 passed / 1 failed обнаружили чтение packed checkpoint без unpacker.
Расширенный диагностический запуск 26 passed / 2 failed обнаружил пропущенный
test import и аргумент timeout. Fixtures исправлены, защитные assertions сохранены.

Проверки root на изолированной testcontainers PostgreSQL:

- Окончательный HTTP/direct/AIRouter/provider/tools/durable/checkpoints/boundary/
  SQL/generic WorkOrders профиль — **237 passed**, включая 30 новых HTTP tests.
- Промежуточный HTTP/tools/checkpoints после auth-failure stop — 93 passed;
  не добавляется к итоговому числу.
- Detached planner — 23 passed, verifier — 17 passed, каждый отдельным процессом.
- Ruff check/format и `git diff --check` прошли.

Всего окончательная приёмка — **277 passed**, 0 failed, 0 skipped.
Все три профиля сообщают существующее warning об `asyncio_loop_scope`.
Команды (каждый запуск с `POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=1`):

```bash
python3 -m pytest backend/tests/test_work_budget_http_recipient.py backend/tests/test_work_budget_direct_text.py backend/tests/test_work_budget_airouter.py backend/tests/test_work_budget_provider.py backend/tests/test_work_budget_tools.py backend/tests/test_durable_chat.py backend/tests/test_chat_checkpoints.py backend/tests/test_agent_execution_boundary.py backend/tests/test_agent_sql_perimeter.py backend/tests/test_work_budget_work_orders.py -q --tb=short
python3 -m pytest backend/tests/test_work_budget_planner.py -q --tb=short
python3 -m pytest backend/tests/test_work_budget_verifier.py -q --tb=short
```

Отдельный read-only reviewer `gpt-5.6-sol`, high проверил frozen diff и новые
tests: blocking/high/medium/low findings отсутствуют в заявленном scope.
Первый запуск reviewer был недоступен из-за model capacity; повтор завершён.
Review root до заморозки исправил nested lock deadlock, отсутствующий recipient
preflight, heartbeat lock inversion, HTTP 4xx tail и неверное not_published
evidence для повтора после parent settlement. Защитные assertions не ослаблены.

Все model/provider и recipient effects подставные; живая LLM не использовалась.
Generic proxy пока блокируется до HTTP. Новых миграций нет. Notification failure
после synchronous store write сохраняет `outcome_unknown`; once marker не выдаётся
за recipient-issued durable success receipt после crash. Полный E24 и прочие
recipients не закрыты. Экономия модели не измерена.

## Production

`make prod-build` завершился успешно, backend/workers/beat пересозданы.
Backend/frontend/worker/GPU-worker/LoRA-worker healthy, beat running.
`curl -ksS https://localhost/health` → `{"status":"ok"}`.
SHA256 пяти runtime-файлов совпадает на host, backend и celery-worker:

| Файл | SHA256 |
|---|---|
| `ai/agent_loop.py` | `25f8ffe0e0b617c28b3c3306281be33e878f700ceda9ded45f339850b1eab716` |
| `ai/work_budget_context.py` | `8bbced8c0270fb21591c00faaf50e7dfe3fabacd3401323e90d806f7b93b0684` |
| `auth/work_budget_handoff.py` | `eae5d65475534cb1cd448dcd30b7afbfbb24234b96bf6554133b427344da07ec` |
| `api/workspace.py` | `464b623b2c21d3b3d7876485072e189579b638d740c8e8cf44daa4fca480ad34` |
| `tasks/work_orders.py` | `3d8c0615d9ee1d5b4f702da39ab54003c6e3b3de8a08e48577bc3cfb41e9e91c` |

Health/hash подтверждают выпуск кода; live model quality и новые платные маршруты
этим не проверялись. Production data migrations и push не выполнялись.
