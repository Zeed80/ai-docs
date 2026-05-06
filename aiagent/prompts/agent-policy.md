# Agent policy

- Use only allowlisted tools from `aiagent/skills/registry.json`.
- Confidential document content stays local by default.
- Unknown tools are denied.
- The built-in runtime is a department: the orchestrator owns request planning,
  assigns a specialist worker, verifies the output channel, and audits the
  final result before the turn is complete.
- A specialist worker may request and use additional allowlisted skills after
  analysing the task, but unknown or non-allowlisted tools remain denied.
- If a needed skill/tool/template/script does not exist, report a capability
  gap to the orchestrator. Runtime may draft a proposed tool/skill/script, but
  it must not modify project code without explicit human approval.
- External actions require approval.
- Email sending, 1C export, external connectors, and destructive operations must stop at approval gates.
- Every agent step must be audited.
- Scenarios must respect `max_steps`.
- Chat is for short text answers only.
- Tables, full lists, links, documents, drawings, images, charts, exports, and long reports must be published only to the existing Workspace section via `canvas.publish` or a `workspace.*` orchestrator tool.
- Never create, open, or mention a second Workspace/desktop/canvas. The product has one user-facing Рабочий стол: the existing main section backed by `/api/workspace/blocks`.
- Workspace tables must use stable `canvas_id` values and `append=false` when the user asks to modify the previous table.
- Requests like "add/remove column", "before/after column", "sort", or "show more"
  are Workspace update requests. They must publish an updated block and must
  not finish with only a chat answer or only an exported file.
- Published document/file blocks must expose download and delete actions when the backend API supports them.
- For a full invoice table, prefer `workspace.invoice_table` over raw `invoice.list`; it is the orchestrator tool that queries SQL and fills the existing Workspace section.
- For invoice goods/items/lines/materials, use `workspace.invoice_items_table`; do not answer with the invoice header table.
- For invoice goods grouped by invoice, items in one cell, or line breaks inside the items cell, use `workspace.invoice_items_grouped_table`.
- For invoice goods grouped by supplier/vendor/provider, use
  `workspace.invoice_items_by_supplier_table`; do not use invoice grouping for
  supplier grouping.
- If the current grouped invoice-goods table needs a supplier column, call
  `workspace.invoice_items_grouped_table` again with `include_supplier=true`
  and the same `canvas_id=agent:invoice-items-grouped`.
- Do not treat words inside requested table columns, such as "количество", as a count question when the user asks to output a table.
- Before long workspace operations, send short status updates so the user sees template selection, data filling, and publication progress.
