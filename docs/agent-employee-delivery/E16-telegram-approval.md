# E16 — Telegram approval/handoff

Дата: 29 сентября 2026. Статус: **REVIEWED / TESTED / DEPLOYED**.

Для явного назначенного manager/admin при выдаче work-order approval в той же
транзакции создаются короткий случайный callback token и
`approval.requested` outbox row. Требуются активная личная Telegram-привязка,
активный пользователь и конечный будущий срок. Payload outbox содержит только
ссылку на WorkOrder; `approval_id` хранится отдельным FK, не в callback data.
Реальный адаптер отправки по-прежнему отключён, кнопки людям не отправлялись.

Нажатие принимается только из private chat пользователя той же привязки.
Перед решением заново проверяются active binding, роль и assignee, срок,
pending status и digest точного действия/аргументов. Одноразовый token
потребляется в одной транзакции с обычным `decide_approval`, действующим от
реального human identity. Старые `appr:` callback не исполняются. Доставка
outbox повторно проверяет то же approval и binding до вызова адаптера;
просроченный, изменённый или отозванный запрос уходит в dead-letter без
отправки. Групповой notifier и старые proactive-напоминания теперь текстовые,
без кнопок и raw approval ID.

Независимая приёмка root: 210 backend-тестов прошли в общем наборе Telegram,
outbox, approvals, durable chat, checkpoints и receipts. Дополнительный
upgrade/downgrade/upgrade тест миграции `20260929_0004` прошёл отдельно.
Проверены forwarded/group click, double click, stale/expired решение,
отозванный binding, изменённые args и делегированная доставка fake adapter.
Ruff, format и diff check пройдены. Остаются старые предупреждения
`asyncio_loop_scope` и `_BotManager.start` в тестах. Исполнитель:
`gpt-5.6-terra`; root провёл два review-цикла и исправил изоляцию тестов.

Внешняя отправка Telegram остаётся отключённой до отдельной проверки
адаптера и политики неоднозначных результатов; эта карточка не заявляет
exactly-once доставку.

Production пересобран через `make prod-build`: backend, frontend и обычный
Celery worker healthy, beat запущен, `/health` вернул `{"status":"ok"}`,
Alembic в backend — `20260929_0004 (head)`.
