# E15 — Telegram durable intake

Дата: 29 сентября 2026. Статус: **REVIEWED / TESTED / DEPLOYED**.

Текст и распознанный голос Telegram больше не запускают in-memory
`AgentSession`: проверенные allowlist, private chat, активный пользователь и
активная привязка передаются общему E12 intake. Telegram `update_id` служит
устойчивым ключом повтора; старый update нельзя перенести новому владельцу
после перепривязки. Для ранее невиданного старого update дополнительно
проверяется Telegram message date относительно создания текущей привязки.

Исходная привязка записывается в `DurableChatRun.source_binding_id`. Worker
создаёт owner-bound `chat.reply_ready` outbox только на эту привязку, а не на
произвольную активную привязку владельца. Intake и `chat.intake.accepted`
outbox коммитятся вместе; сбой producer откатывает оба. Отзыв привязки теперь
soft-revoke, новая привязка получает новый ID; исторические записи не меняют
адресата. Частичный unique index оставляет активной не более одной привязки
для Telegram ID, а advisory lock сериализует конкурентный bind.

Голос, фото и документы не скачиваются до проверки private chat и активной
привязки. Старый callback больше не вызывает legacy approval; полноценное
одноразовое подтверждение — E16. Бот не удаляет backlog при restart.

Независимая приёмка root: 178 backend-тестов прошли вместе с Telegram API,
outbox, durable chat, checkpoints и receipts. Проверены repeat/restart,
другой allowlist ID, группа, чужой файл до скачивания, rebind, невиданный
старый update, атомарный rollback, несколько bindings, отзыв исходной
привязки и конкурентный bind. Ruff, format, diff check и единственная Alembic
head `20260929_0003` пройдены. Исполнители: `gpt-5.6-sol` (основа; прерван
лимитом), `gpt-5.6-terra` (исправление binding); root проверил и добавил
отсечение невиданного старого update.

Ограничения: реальный Telegram delivery adapter остаётся выключен, сообщения
людям не отправлялись. Перед его включением нужны отдельно проверенные
политика `unknown` и owner-bound чтение содержимого. Старый путь ingest
Telegram-файлов использует внутренний HTTP-заголовок и не стал durable intake;
он лишь закрыт проверкой до скачивания. Это отдельная миграция, не E15.
Telegram `message.date` имеет секундную точность: сообщение в первую секунду
после новой привязки может быть консервативно отклонено; повторная отправка
после этой секунды безопасна. Тесты Telegram API сохраняют два старых warning
о не-await `_BotManager.start`; pytest также предупреждает об
`asyncio_loop_scope`.

Production пересобран через `make prod-build`: backend, frontend и обычный
Celery worker healthy, beat запущен, `/health` вернул `{"status":"ok"}`,
Alembic в backend — `20260929_0003 (head)`.
