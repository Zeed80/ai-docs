# ADR 004: Approval gate pattern for external actions

**Status**: Accepted  
**Date**: 2026-05

## Context

The agent ("Света") can perform actions with real-world consequences: approving invoices, sending emails, exporting to 1С, applying bulk changes to tables. Automating these without human oversight is risky.

Requirements:
- Agent proposes an action, human explicitly approves or rejects
- Rejected actions are never retried without new user intent
- Audit trail for every approval decision
- Agent can continue other work while waiting for approval

## Decision

**9 approval gates** defined in `aiagent/skills/capabilities.yml` under `gate_actions`:

```yaml
gate_actions: [approve, reject, export_1c, bulk_approve, bulk_reject]
```

Flow:
1. Agent calls capability action that is in `gate_actions`
2. Backend creates `Approval` record (status=pending, entity_id=...)
3. Backend returns HTTP 200 with `requires_approval: true` + approval URL
4. Agent streams `[Ожидание подтверждения]` to user
5. User approves/rejects via UI or chat command
6. Backend webhook/endpoint processes decision → marks Approval (approved/rejected)
7. If approved: action executes; if rejected: agent notified, stops retrying

SLA: approvals expire after `APPROVAL_EXPIRY_HOURS` (default 48h); expired approvals escalate to admin via Celery beat task `approval.escalate_expired`.

## Consequences

**Positive:**
- No autonomous financial actions without human sign-off
- Full audit trail (Approval table + AuditLog)
- Agent can work on other tasks while awaiting approval
- Delegation: approval can be transferred to another user

**Negative:**
- Adds latency to any gate-protected action
- Requires UI/notification infrastructure to inform approvers
- Complex state machine (pending → approved/rejected/delegated/escalated/expired)

## Alternatives considered

- **Auto-approve with undo**: simpler UX but recovery from mistakes is hard
- **Confirmation dialog only (no record)**: no audit trail, no async approval
- **Per-user trust levels**: deferred to auto-approval rules with `min_trust_score`
