# Agent policy

- Use only allowlisted tools from `openclaw/skills/registry.json`.
- Confidential document content stays local by default.
- Unknown tools are denied.
- External actions require approval.
- Email sending, 1C export, external connectors, and destructive operations must stop at approval gates.
- Every agent step must be audited.
- Scenarios must respect `max_steps`.
