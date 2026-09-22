# E09 — реестр независимых проверок артефактов

Дата: 22 сентября 2026. Статус: **SCOPED COMPLETE / REVIEWED / DEPLOYED**.

## Граница

E09 вводит детерминированный реестр, а не model-selected callback. В нём ровно
две проверенные пары операции и типа артефакта:

| Операция | Тип артефакта | Авторитетный snapshot |
| --- | --- | --- |
| `agent_control.task_propose` | `agent_task` | `agent_tasks` |
| `warehouse.update_item` | `inventory_item` | `inventory_items` |

При записи receipt для этих операций в той же транзакции создаётся versioned
`WorkArtifact` descriptor. Миграция `20260922_0002` backfill-ит такие descriptors
только для этих двух уже проверенных операций; новый schema object не создаётся.

До чтения receipt, descriptor или содержимого получателя проверяется owner
`WorkOrder`. Verdict содержит версию verifier, ID и version/hash артефакта,
ожидаемые version/hash receipt, scope, время наблюдения и источник evidence.
Его целостность связывается HMAC от конфигурационного секрета приложения.

## Отрицательные результаты и границы полномочий

Изменившийся артефакт возвращает `changed`: старый verdict не доказывает новую
версию. Подделанный verdict, отсутствующая версия descriptor и недоступный
получатель fail-closed либо дают `inconclusive`; отсутствующий local record не
объявляется доказанным. Неподдержанный recipient также `inconclusive`.

Внешняя reference остаётся opaque text evidence. E09 не делает HTTP-запрос,
не dereference-ит URL и не создаёт адаптер по просьбе модели или человека.
Отсутствие либо недоступность внешнего результата не может стать успехом без
отдельного безопасного адаптера и доказанного полного авторитетного журнала.

Verifier read-only: matched один артефакт не записывает evidence/verdict для
критериев и не завершает `WorkOrder` с другими required criteria.
`can_replay` и `can_resume` остаются `false`.

## Gate A2

E09 не использует verdict для resume, replay или completion. Отдельный контракт
продолжения остаётся задачей E10; до его review не открывать `can_resume` и не
ослаблять completion gates.

## Проверка

Покрыты exact registry, Alice/Bob owner isolation, stale artifact, forged
verdict, unavailable recipient, missing descriptor version, unsupported external
recipient и opaque URL, а также matched artifact при незакрытом другом criterion.
Миграция проверена isolated-schema upgrade/downgrade roundtrip.

Исполнитель: 47 focused / 175 expanded passed. Независимо: 147 passed. Один
Alembic head; Ruff/format/diff clean. Следующая карточка — E10.

Production-стек пересобран и перезапущен через `make prod-build`.
Alembic применил `20260922_0002 (head)`; backfill корректно не создал
descriptors при отсутствии production receipts двух supported operations.
`https://localhost/health` вернул `{"status":"ok"}`; SHA-256 трёх runtime-модулей
совпали на host, backend и обычном Celery worker.
