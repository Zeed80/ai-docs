---
name: procurement
description: "Закупки: заявки, запрос КП (RFQ), сравнение коммерческих предложений, контракты, проверка поставщика перед запросом"
---
# Закупки

## Документы
- **Заявка на закупку** — внутренний документ: `procurement` `action=list_requests`/`create_request`.
- **Запрос КП (RFQ)** — письмо поставщикам: `procurement` `action=send_rfq` [GATE].
- **Коммерческое предложение (КП)** — ответ поставщика, сравнивается через `analytics` `compare_*`.
- **Контракт** — `procurement` `action=list_contracts`/`get_contract`.

## Перед запросом КП
1. `suppliers` `action=trust_score` — надёжность поставщика.
2. `suppliers` `action=check_requisites` — история реквизитов.
3. `suppliers` `action=price_history` — история цен.
4. Покажи черновик запроса с позициями и условиями.
5. `procurement` `action=send_rfq` [GATE] — уйдёт после решения человека.

## Сравнение КП
1. `analytics` `action=compare_create` — сессия сравнения.
2. `analytics` `action=compare_align` — выравнивание позиций.
3. Таблица сравнения — на Рабочий стол.
4. Рекомендация: лучшее соотношение цены, сроков и надёжности.
5. `analytics` `action=compare_decide` [GATE] — решение руководителя.

## Сигналы
- Ценовой скачок выше порога — карточка `anomalies` `action=create_card`.
- Новый поставщик — аномалия new_supplier и проверка реквизитов.
