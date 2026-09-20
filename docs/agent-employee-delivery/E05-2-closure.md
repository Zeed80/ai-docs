# E05.2 — закрытие простых DB write-адаптеров ToolResult

Дата: 20 сентября 2026. Статус: **SCOPED COMPLETE / REVIEWED**.

## Граница закрытия

E03 обнаружила 124 операции класса `one-db-commit`. В E05.2 на агентской
HTTP-границе мигрированы ровно 28 простых, доказанных операций; остальные 96
полностью классифицированы и намеренно не входят в этот этап. Пропущенных
простых DB-only кандидатов нет. Это не означает, что 96 операций безопасны для
универсальной миграции: у каждой ниже сохранён fail-closed прежний контракт до
отдельной карточки.

Мигрированный allowlist (28): `analytics.calendar_create_reminder`,
`analytics.collection_add_item`, `analytics.collection_close`,
`analytics.collection_create`, `analytics.compare_align`,
`analytics.compare_create`, `analytics.table_create_view`,
`analytics.table_inline_edit`, `documents.link`, `email.draft`,
`email.templates.create`, `email.templates.update`, `invoices.update`,
`invoices.validate`, `memory.source_propose`, `normalization.create_norm_card`,
`normalization.update_canonical_item`, `normalization.update_norm_card`,
`payments.create_schedule`, `procurement.create_request`, `suppliers.update`,
`tech.correction_record`, `tech.operation_template_create`,
`tool_catalog.create_supplier`, `warehouse.adjust_stock`,
`warehouse.create_item`, `warehouse.create_receipt`, `warehouse.update_item`.

Контракт E05.2.1 сохранён: raw 2xx payload находится в `data`; явная
domain-ошибка и 4xx дают `failed`; ошибка после возможного dispatch —
`outcome_unknown`; автоматического retry нет. Публичные business API не меняются.

## Реестр остатка E03 (96)

Категории взаимоисключающие: `3 + 4 + 27 + 6 + 16 + 2 + 3 + 35 = 96`.

### Только администратор (3)

`agent_control.ai_config_set`, `agent_control.task_propose`,
`analytics.auto_approval_create`.

### Только человек или явно запрещено агенту (4)

`agent_control.task_decide`, `memory.promote`, `memory.promotion_decide`,
`memory.source_decide`.

### Approval/lifecycle/status/decision/delete (27)

`analytics.compare_decide`, `analytics.table_apply_diff`,
`analytics.table_export_1c`, `documents.bulk_delete`, `documents.delete`,
`email.delete_template`, `email.templates.delete`, `invoices.approve`,
`invoices.bulk_approve`, `invoices.bulk_delete`, `invoices.bulk_reject`,
`invoices.delete`, `invoices.export_1c`, `invoices.receive`, `invoices.reject`,
`memory.prune`, `normalization.activate_rule`, `payments.mark_paid`,
`procurement.create_contract`, `procurement.update_contract`,
`procurement.update_request`, `sheets.delete`, `tool_catalog.approve`,
`warehouse.delete_item`, `warehouse.issue_stock`, `warehouse.update_status`,
`workspace.spec_table_cell_edit`.

### Условная, partial или несколько границ (6)

`analytics.auto_approval_check`, `analytics.calendar_extract_dates`,
`memory.embeddings_rebuild`, `memory.reindex`, `normalization.apply_rules`,
`normalization.suggest_rule`.

### AI, network, chat_bus или иной внешний эффект (16)

`documents.summarize`, `email.compose`, `email.reply`,
`email.templates.from_message`, `memory.embeddings_index_active`,
`memory.source_discover`, `sheets.create`, `sheets.patch_cells`,
`sheets.add_row`, `sheets.add_column`, `sheets.set_formula`,
`sheets.rename_column`, `sheets.merge_cells`, `sheets.unmerge_cells`,
`sheets.from_spec`, `sheets.from_template`.

### Несоответствие identity path (2)

`analytics.calendar_generate_followup` (catalog `{entity_id}`, recipient
`{reminder_id}`), `memory.promotion_evaluate` (catalog `{entity_id}`, recipient
`{fact_id}`). Оба расхождения отложены: адаптер не угадывает identity.

### Несоответствие каталога фактическому эффекту (3)

`email.render_template`, `email.templates.render`, `suppliers.trust_score`.

### Отдельный CAD/engineering-контур (35)

`engineering.analysis_case_create`, `engineering.assembly_component_add`,
`engineering.assembly_create`, `engineering.assembly_mate_add`,
`engineering.material_assign`, `engineering.material_create`,
`engineering.project_create`, `engineering.projection_create`,
`engineering.revision_approve`, `engineering.revision_create`,
`engineering.revision_validate`, `image_studio.accept`,
`image_studio.accept_techdraw`, `image_studio.accept_vectorize`,
`image_studio.techdraw`, `tech.analyze_surfaces`, `tech.blank_spec_set`,
`tech.bom_approve`, `tech.bom_create`, `tech.bom_purchase_request`,
`tech.bom_update`, `tech.calculate_cutting_params`,
`tech.learning_rule_activate`, `tech.learning_rule_create`,
`tech.learning_rule_reject`, `tech.norm_estimate_approve`,
`tech.norm_estimate_create`, `tech.norm_estimate_suggest`,
`tech.normcontrol_resolve`, `tech.operation_add`, `tech.process_plan_approve`,
`tech.process_plan_create`, `tech.process_plan_draft_from_document`,
`tech.process_plan_validate`, `tech.resource_create`.

## Поправки безопасности при закрытии

- `email.render_template` и `email.templates.render` исключены из read retry:
  обработчик пишет `use_count` и `last_used_at`, хотя catalog помечал операции
  чтением.
- `analytics.compare_decide` внесён в capability manifest и классификатор риска
  как approval-gated.
- Для `email.templates.delete` явно закреплены alias-gate и риск; canonical
  `email.delete_template` остаётся под тем же запретом.
- `agent_control.task_propose.admin_only` приведён в соответствие с реальным
  admin-only recipient.

Эти изменения не разрешают автоматический retry и не меняют права endpoint-ов.

## Проверка и следующий шаг

Независимый сфокусированный набор текущего среза: 190 passed. Дополнительный
набор capability-router/route-contract/policy: 31 passed. Ruff check и format
check изменённых Python-файлов, а также `git diff --check` прошли.

Старший агент выполнил `make prod-build`: backend и Celery-контейнеры
пересозданы, `https://localhost/health` вернул `{"status":"ok"}`, backend и
workers healthy. SHA-256 четырёх изменённых runtime-файлов (`policy_engine.py`,
`tool_catalog.py`, `tool_transport.py`, `capabilities.yml`) в checkout и
production backend-контейнере совпали.

E05 в целом остаётся **IN PROGRESS**. Следующая последовательная карточка —
**E05.3 async jobs**; E05.4 external/MCP handlers и E06 не закрыты.
