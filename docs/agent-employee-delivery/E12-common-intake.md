# E12 — общий intake

Дата: 29 сентября 2026. Статус: **REVIEWED / TESTED / DEPLOYED**.

Создание durable chat run, сообщения и work order перенесено из HTTP handler в
`domain/agent_intake.py`. Сервис принимает identity проверенного адаптера,
сверяет канал с ней и не запускает модель. HTTP сохраняет human-only gate и
прежний формат ответа. Для существующих HTTP-запросов сохранён исходный digest
всего валидированного тела, включая метаданные вложения; внутренний канал не
может подставить произвольный digest. Новый ключ идемпотентности —
`(intake_channel, owner_key, external_message_id)`; старая UUID остаётся в
ответе. Миграция заполняет старым записям `http` и `request_id::text`.

До записи проверяются владение вложениями, принадлежность сессии, старый
неперенесённый чат и активный ход. Сообщение, вложения, work order, run и план
коммитятся одной транзакцией; ошибка записи откатывает всё. Конкурентные дубли
сериализуются advisory lock, а уникальный индекс сохраняет DB-инвариант.

Независимая приёмка root: 135 backend-тестов пройдены в связке durable chat,
checkpoints и receipts. Проверены повтор HTTP, конфликт изменённого ввода,
параллельный дубль, раздельные каналы, чужое вложение, ошибка записи и
upgrade/downgrade/upgrade миграции с legacy row. Ruff, format и diff check
пройдены. Исполнитель: `gpt-5.6-terra`; root проверил и ограничил совместимый
digest только HTTP. E12 не включает доставку/outbox — это E13/E14.

Production пересобран через `make prod-build`: backend, frontend и обычный
Celery worker healthy, `/health` вернул `{"status":"ok"}`, Alembic в backend —
`20260929_0001 (head)`.
