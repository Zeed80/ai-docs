# E08 — атомарная квитанция `warehouse.update_item`

Дата: 22 сентября 2026. Статус: **SCOPED COMPLETE / REVIEWED / DEPLOYED**.

## Граница операции

E08 закрывает ровно одну DB-операцию без внешнего эффекта:
`PATCH /api/warehouse/inventory/{item_id}` (`warehouse.update_item`). Её доменная
таблица — `InventoryItem` / `inventory_items`. До E08 маршрут изменял объект и
выполнял прямой `commit` в handler-е.

Изменение вынесено в helper без внутреннего commit. При наличии
`Idempotency-Key` маршрут под текущей авторизацией и common order lock/fence
сначала проверяет exact journaled logical action, owner, request digest,
operation и attempt. Затем изменение `InventoryItem` и `ActionReceipt` пишутся
и коммитятся одной транзакцией. Ключ не является bearer-разрешением.

Перед созданием квитанции объект refresh-ится после flush: сериализованный
`updated_at` стабилен. Ответ, возвращённый реальным ASGI route, совпадает с
сохранённым ответом receipt и может быть только прочитан при повторной доставке
source/current attempt; другой attempt не получает право на новый effect.

Путь без ключа сохранён для авторизованных legacy-клиентов. Durable gateway
передаёт journal key исключительно exact recipients
`agent_control.task_propose` и `warehouse.update_item`, не прочим операциям.

## Проверка

Покрыты настоящий ASGI route и exact stored response, стабильность `updated_at`,
два независимых соединения с lost response, rollback сбоя записи receipt,
отмена под common lock, foreign owner, mismatched item/payload/action/random
attempt, отсутствие bearer-authority у ключа, legacy path и source/current
attempt receipt-only replay.

Исполнитель: 41 focused / 317 expanded passed. Независимо: 317 passed.
Ruff/format/diff clean.

Production-стек пересобран и перезапущен через `make prod-build`.
`https://localhost/health` вернул `{"status":"ok"}`; backend и Celery workers
запущены. SHA-256 трёх runtime-модулей совпали на host, backend и
обычном Celery worker.

E08 доказывает только эту операцию. Каждая следующая требует отдельной E08.N;
DB+queue-эффекты требуют outbox E13. SMTP и browser не объявляются exactly-once.
Следующая карточка — E09.
