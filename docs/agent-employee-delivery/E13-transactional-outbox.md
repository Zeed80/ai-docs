# E13 — транзакционный outbox

Дата: 29 сентября 2026. Статус: **REVIEWED / TESTED / DEPLOYED**.

Добавлены `AgentOutbox`, миграция `20260929_0002` и producer
`produce_agent_outbox`. Producer в транзакции вызывающего кода создаёт
`WorkEvent` и outbox row; сам не коммитит и не делает сетевых вызовов.
Обязательны совпадение владельца WorkOrder и channel binding, версия payload,
ключ дедупликации, состояние/попытки/lease для будущей доставки. Уникальные
ограничения БД защищают пару owner + destination + dedup и единственную строку
на событие; advisory lock сериализует конкурентных producer.

Повтор с тем же ключом возвращает исходную строку только при том же digest
канонического запроса; изменение event type, ссылки или версии даёт конфликт
без второго domain event. Payload принимает только ссылку на тот же WorkOrder
и опциональную положительную resource version: текст, секреты и контекст модели
в outbox не копируются. E14 должен повторно проверить права и получить
содержимое по owner-bound ссылке перед доставкой. Внешняя доставка в E13
**не включена**; exactly-once внешнего отправителя не заявляется.

Независимая приёмка root: 141 backend-тест прошёл в связке E13, durable chat,
checkpoints и receipts; отдельно проверены одновременные одинаковый и
изменённый дубль, откат обеих записей, чужой binding, закрытый формат payload,
upgrade/downgrade/upgrade миграции. Ruff и diff check пройдены. Единственное
предупреждение pytest — существующая настройка `asyncio_loop_scope`.
Исполнитель: `gpt-5.6-terra`; root провёл review dedup/payload и повторные тесты.

Production пересобран через `make prod-build`; backend, frontend и обычный
Celery worker healthy, `/health` вернул `{"status":"ok"}`, Alembic в backend —
`20260929_0002 (head)`.
