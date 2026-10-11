# E47 — расходы и эксплуатационные сигналы

Дата: 10 октября 2026.

## Сигналы «что застряло»

`GET /api/work-orders/metrics` отдаёт блок `ops` — запросы к durable-журналу,
не оценка модели (не-админ видит своё):

| Сигнал | Откуда |
|---|---|
| `queue_oldest_ready_age_seconds` | старейший готовый шаг активного поручения |
| `unknown_effect_calls` | вызовы `outcome_unknown`/`partial` до сверки (E23) |
| `awaiting_human_steps` | шаги `waiting_approval` |
| `expired_leases` | шаги `running` с истёкшей арендой |
| `failed_checks` | критерии приёмки, проваленные за окно |
| `budget_blocked_ledgers` | книги бюджета с блокером |
| `outbox_pending`, `outbox_oldest_age_seconds`, `outbox_dead_or_unknown` | исходящие уведомления |

Шапка `/work-orders` показывает их, ненулевые тревожные — выделены.

## Расход по линии заказа

`GET /api/work-orders/{id}/usage` — вызовы модели линии (с дочерними
заказами): по провайдеру, состоянию резервации и исходу квитанции; токены
суммируются только известные, остальные вызовы считаются отдельно как
неизвестные; стоимость `known` только если цена известна у всех вызовов, иначе
`unknown` — без тихого нуля.

## Логи

Одна настройка structlog для API и воркеров (`app/core/logging_setup.py`; воркеры
раньше писали без неё). Значения под ключами содержимого (`prompt`, `content`,
`text`, `body`, `messages`…) заменяются длиной и коротким хэшем, секретные
(`password`, `token`, `api_key`…) — `<masked>`; `LOG_INCLUDE_CONTENT=true`
возвращает содержимое для отладки, секреты всё равно скрыты. Так ушли строки
оркестратора с началом сообщения пользователя. `X-Request-ID` уже был; теперь
чужой идентификатор принимается только если похож на идентификатор.

## Не сделано (в плане, T4)

Цена по версионированному тарифу — сделано 2026-10-12 (T4): `llm_tariffs`,
цена копируется в квитанцию на момент вызова, `cost-report.receipts`, карточка
«Тарифы и расход» (`backend/tests/test_t4_tariffs.py`,
`frontend/tests/e2e/tariffs-card.spec.ts`).

## Проверки

`python3 -m pytest backend/tests/test_work_order_view.py backend/tests/test_log_redaction.py -q`
