# E21.3a — Перепланирования на общем бюджете

Дата: 5 октября 2026. Исходный commit: `ec18e204`. Исполнитель и приёмка:
Claude Code (root).

## Контракт

До среза перепланирование ограничивалось только на уровне WorkOrder
(`plan_revision <= _max_replans_for(order)`); измерение `replans` в ledger было
объявлено, но никто его не резервировал и не списывал. Каждый заказ линии
получал собственный полный запас.

Теперь все четыре перехода «сбой → replanning» идут через `_replan_or_block`:

- действуют оба лимита: прежний per-order и `replans` общего ledger линии;
- `charge_replan_in_transaction` списывает 1 единицу в ТОЙ ЖЕ транзакции,
  что и переход: при откате перехода откатывается и списание;
- идемпотентно по `replan:{order}:from-r{revision}`;
- для guarded-вызовов допустимость перехода проверяется до списания;
- исчерпание общего запаса → заказ `blocked` с
  `{"code": "replan_budget_exhausted", "cause": <прежний blocker>}`;
  ledger-wide blocker НЕ ставится, соседние заказы линии не останавливаются;
- заказ без ledger сохраняет прежнее per-order правило (durable execution
  такую работу и так не исполняет).

Exploratory-корень без явного `max_replans` получает в ledger 30, как и его
per-order значение по умолчанию (`_DEFAULT_MAX_REPLANS_EXPLORATORY`); обычный —
прежние 3. Иначе общий лимит молча урезал бы exploratory-работу вдесятеро.

## Тесты

`backend/tests/test_work_budget_replans.py`: потомок не выходит за общий запас
при собственном `max_replans=5`; одна ревизия — одно списание; откат вместе с
недопустимым переходом; per-order лимит 0 ничего не списывает; exploratory = 30.

Регрессия: 39 файлов, затрагивающих replanning/work orders — набор падений
совпадает с базовой линией побайтово (8 зависящих от порядка в общей БД:
outbox, gap detection, telegram rebind; новых нет). Отдельными процессами:
planner 23, verifier 17, ledger/lease/corpus 57, replans + work orders +
durable chat 68 — все passed.

## Находка: декомпозиция не исполнима

Шаг `decompose` создаёт дочерние WorkOrder без привязки к ledger и с шагом
`agent_turn`, а headless `agent_turn` выведен в E21.2b5
(`headless_agent_turn_requires_durable_intake`). Дочерняя работа поэтому не
может выполниться вообще. `initialize_budget_ledger` дочь к существующей линии
не привяжет: у родителя уже есть история, и он требует явной baseline-миграции.
В production за всё время 0 дочерних заказов — путь латентный. Чинится
отдельным срезом: привязка потомка к ledger родителя при создании и durable
форма исполнения дочернего шага.
