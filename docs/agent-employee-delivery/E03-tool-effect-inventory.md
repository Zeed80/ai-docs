# E03 — инвентаризация эффектов и транзакционных границ

Дата: 15 сентября 2026. Исходный commit: `385257dd`.
Статус: REVIEWED.

## Делегирование

- Сеньор: главный агент сессии; ограничение scope, независимый review и приёмка.
- Исполнитель: `gpt-5.6-terra`, reasoning high.
- Изменения исполнителя: `docs/agent-employee-delivery/tool-effect-inventory.md`
  и `backend/tests/test_capability_catalog_consistency.py`.
- Циклов замечаний: 0. Исполнитель не менял runtime, RBAC, endpoint, receipt,
  production stack, commit или push; сеньор повторил проверочный тест и прочитал
  фактический diff.
- Учёт расхода по моделям недоступен; экономия лимитов не измерена.

## Результат

- Матрица покрывает ровно 323 активные операции `TOOLS`: route и target handler,
  gateway/catalog/endpoint RBAC, фактический эффект, прямую commit boundary,
  внешний dispatch, внутренний retry и recipient receipt.
- Классы: `read-only`, `one-db-commit`, `db-async-enqueue`,
  `external-dispatch`, `browser-script-mcp`, `unknown`. У 47 неоднозначных,
  indirect или multi-boundary операций указан `unknown` и automatic retry
  запрещён. Это осознанная неполнота доказательств, не разрешение retry.
- Новый fail-closed тест требует точного равенства операций инвентаря и активного
  `TOOLS`, запрещает дубликаты и проверяет retry запрет у каждого `unknown`.
- Выявлено расхождение: `agent_control.task_propose` имеет catalog
  `admin_only=False`, тогда как handler требует `admin`. E03 не ослабляет
  endpoint и не изменяет права. Квитанция поддерживается только при валидном
  `Idempotency-Key`; legacy путь без ключа её не создаёт.
- Кандидат E08 — `agent_control.task_propose`: у ключевого пути единый финальный
  DB commit и receipt, однако migration не начата до E07 и отдельного решения
  catalog/admin divergence.

## Проверки

Независимый прогон сеньора:

```bash
python3 -m pytest backend/tests/test_capability_catalog_consistency.py -q
ruff check backend/tests/test_capability_catalog_consistency.py
git diff --check
```

Результат: 9 тестов прошли; `ruff` и diff check прошли. Тест подтвердил 323
inventory rows без дубликатов и 47 `unknown` с `auto-retry prohibited`.
Сохранилось известное предупреждение pytest о `asyncio_loop_scope`.

## Выкладка и ограничения

Изменены только документация и тест, production runtime не менялся, поэтому
пересборка стека не требуется. Это не закрывает расхождение RBAC и не добавляет
exactly-once/receipt wrapper к legacy operations. Следующая карточка — E04,
контракт ToolResult и правила классификации.
