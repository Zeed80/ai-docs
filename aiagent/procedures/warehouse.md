---
name: warehouse
description: "Склад: приём ТМЦ по счёту, выдача в производство, остатки и движения, расхождения с документами"
---
# Склад

Остатки и движения — только из `warehouse` (`action=list_inventory`,
`get_item`, `list_movements`); количества называй с единицей измерения.

## Документы
- **Счёт-фактура / накладная** — основание прихода; количество и номенклатуру
  сверяй с документом до подтверждения.
- **Акт о расхождении** — поставка не совпала со счётом: `anomalies` `action=create_card`.
- **Требование-накладная** — выдача в производство.

## Приём
1. `invoices` `action=get` — счёт.
2. Проверь аномалии new_supplier и requisite_change.
3. Покажи количество и номенклатуру.
4. `warehouse` `action=confirm_receipt` [GATE].

## Выдача
1. `warehouse` `action=get_item` — текущий остаток.
2. Остаток меньше запрошенного — скажи прямо.
3. `warehouse` `action=issue_stock` [GATE].

## Низкий запас
`warehouse` `action=low_stock`; предложи заявку на закупку —
`tech` `action=bom_purchase_request`.
