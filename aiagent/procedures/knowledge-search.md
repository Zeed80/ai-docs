---
name: knowledge-search
description: "Поиск по базе знаний: память, граф связей (поставщик↔счёт↔аномалия), похожие документы, источники, обслуживание индексов"
---
# Поиск по базе знаний

## Порядок
1. `memory` `action=query` — основной RAG: evidence_pack, факты session/project/global,
   связи (`include_graph=true`). Параметры: `query`, `session_id`, `scope`, `node_types`.
2. `search` `action=hybrid` — полнотекстовый и векторный поиск по документам.
3. `search` `action=similar` — похожие на данный документ.
4. `memory` `action=neighborhood` (`node_id`, `depth`, `edge_type`) — что связано
   с поставщиком, счётом, аномалией.
5. `memory` `action=path` (`source_node_id`, `target_node_id`) — цепочка между двумя узлами.
6. `memory` `action=source_list`/`source_discover`/`source_propose` — веб-источники.

Связи ищи сами, не спрашивая разрешения. Больше 2–3 узлов или связей — таблицей
на Рабочий стол (`workspace` `action=general`), в чате — итог.

## Правила
- Мало результатов — ещё одна формулировка или другой инструмент; после двух
  попыток — «не найдено».
- У каждого факта — источник (документ, счёт, сущность); у старых фактов — дата.
- Уверенность факта ниже 0,8 — назови её.

## Обслуживание
- `memory` `action=reindex` — перестроить граф документа.
- `memory` `action=embeddings_stats` — состояние векторных индексов.
- `memory` `action=promote` — предложить факт в project-память; утверждает человек.
