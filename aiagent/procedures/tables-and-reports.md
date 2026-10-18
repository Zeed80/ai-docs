---
name: tables-and-reports
description: "Таблицы, отчёты и аналитика по данным: spec_table, sql_table, листы с формулами, экспорт в Excel и 1С"
---
# Таблицы, отчёты, аналитика

## Какой инструмент выбрать

1. **Таблица из БД** → `workspace` `action=spec_table`. Задаёшь источник,
   колонки, фильтры, сортировку — данные приходят из БД целиком (истинный
   total). Источники: invoices, invoice_items, suppliers, warehouse,
   documents, payments, anomalies, emails, drawings, а также:
   - `vector_search` — похожие документы по смыслу (фильтр query="…текст…");
   - `graph_query` — связи сущности в графе памяти (фильтр start_node="…").
   «Сгруппируй по X», «по каждому поставщику» — `group_by: [поле]`: одна
   строка на группу, числа суммируются, текст собирается перечнем.
   Перечень позиций с количеством — колонка `item_with_qty` (invoice_items).
   Правка готовой таблицы — `action=spec_table_patch` (command или ops).
2. **Нестандартный срез, которого нет в источниках** → `workspace`
   `action=sql_table` (task="…"): валидированный SELECT, только чтение.
   Предпочитай spec_table; sql_table — запасной путь.
3. **Расчёт с формулами (Excel-лист)** → `sheets`. Лист не пишет в боевую БД,
   это рабочая область:
   - `action=from_template` (estimate «смета», price_comparison, budget,
     payment_schedule) — готовые формулы;
   - `action=create` — свой лист {columns:[{key,header,type,formula?}], rows};
   - `action=from_spec` — скопировать таблицу из п. 1 для доработки;
   - `patch_cells`, `add_row`, `add_column`, `set_formula`, `rename_column`;
   - формулы: =A1*B1, =SUM(A1:A10), =ROUND(quantity*price,2), IF/AND/MIN/MAX.
4. **Правка боевых данных из таблицы** → `workspace`
   `action=spec_table_cell_edit` (уходит на подтверждение человеку).
5. **Поиск по содержимому** → `search` `action=hybrid`.

## Как строить аналитику

- Таблицы — на Рабочий стол, в чат — короткий итог.
- Период указывай явно (с — по); для рядов добавляй разницу с прошлым периодом.
- Если `total` больше показанного — offset/limit или прямо назови охват.
- Тренд — минимум по 3 точкам; данных меньше — так и скажи, не экстраполируй.

## Экспорт

- Excel → `analytics` `action=table_export_excel`.
- 1С → `analytics` `action=table_export_1c` (нужны invoice_ids).
- Аномальные счета → `anomalies` `action=list` (фильтр severity/type).
