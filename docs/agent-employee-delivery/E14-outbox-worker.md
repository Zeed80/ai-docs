# E14 — worker outbox и восстановление доставки

Дата: 29 сентября 2026. Статус: **REVIEWED / TESTED / DEPLOYED**.

Добавлены ограниченные claims `SKIP LOCKED`, lease token и fenced
compare-and-set для завершения. Beat вызывает worker раз в 30 секунд через
очередь `scheduler`; **реальных адаптеров доставки пока нет**, поэтому
production task лишь сверяет истёкшие неопределённые отправки и не отправляет
сообщения людям. WorkOrder исполняется независимо от доступности канала.

Idempotent recipient получает постоянный ключ outbox ID и может безопасно
повторяться с ограниченным backoff и максимум пятью попытками. Для внешнего
канала без подтверждённой идемпотентности состояние `sending` коммитится
**до** вызова транспорта. Timeout, неоднозначная ошибка или смерть worker
после отправки переводят запись в `unknown`, без автоматического повтора.
Истёкшее `sending` переводится в `unknown` даже при пустом registry адаптеров.
`OutboxRecipientUnavailable` разрешён только для доказанного отказа до
отправки. Отозванный binding и отсутствующий owner-bound ресурс блокируют
отправку.

Независимая приёмка root: 150 backend-тестов прошли вместе с durable chat,
checkpoints и receipts. Проверены два worker, stale fencing, внутренний
duplicate после crash, внешний crash после send до ack, неоднозначная ошибка,
недоступный или отозванный получатель, пустой registry и ограничения payload.
Ruff/format/diff check пройдены. Существующее предупреждение pytest о
`asyncio_loop_scope` остаётся. Исполнитель: `gpt-5.6-terra`; root провёл два
цикла review и проверил тесты. Никакой гарантии exactly-once у будущих
внешних адаптеров не заявляется; каждый требует отдельного review.

Production пересобран через `make prod-build`: backend, frontend и обычный
Celery worker healthy, Celery beat запущен, `/health` вернул `{"status":"ok"}`,
Alembic в backend — `20260929_0002 (head)`.
