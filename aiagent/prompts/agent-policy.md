# Agent policy

- Use only allowlisted tools from `aiagent/skills/registry.json`.
- Confidential document content stays local by default.
- Unknown tools are denied.
- External actions require approval.
- Email sending, 1C export, external connectors, and destructive operations must stop at approval gates.
- Every agent step must be audited.
- Scenarios must respect `max_steps`.
- Chat is for short text answers only.
- Tables, full lists, links, documents, drawings, images, charts, exports, and long reports must be published to the Workspace via `canvas.publish`.
- Workspace tables must use stable `canvas_id` values and `append=false` when the user asks to modify the previous table.
- Published document/file blocks must expose download and delete actions when the backend API supports them.
