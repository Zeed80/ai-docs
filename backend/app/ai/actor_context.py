"""Acting-user context for agent turns.

The agent calls backend endpoints with the internal service key
(app.ai.agent_loop._internal_headers), which authenticates as ``agent-service``
— a full-admin service account. That is fine for company-wide data, but it
means the agent has no notion of *on whose behalf* a turn runs, so any
per-user scoping downstream (personal mailboxes, private documents) cannot be
applied.

This module carries the sub of the human who started the turn through the
async call chain as a ContextVar: the WebSocket handler sets it once per
connection, every nested task inherits it, and ``_internal_headers()`` turns it
into an ``X-Acting-User`` header. Endpoints resolve it via
``app.auth.acting.get_effective_user``.

Fail-closed by design: headless turns (AgentCron, Telegram) leave it unset, and
endpoints then treat the caller as the bare service account — which owns no
personal data and therefore sees none.

The header is only honoured for callers already authenticated with the internal
service key, so it de-escalates an admin identity rather than escalating a
user one.
"""

from __future__ import annotations

from contextvars import ContextVar

_acting_user: ContextVar[str | None] = ContextVar("agent_acting_user", default=None)

# Service accounts are not people — never propagate them as an acting user.
_NON_HUMAN_SUBS = {"agent-service", "anonymous"}


def set_acting_user(sub: str | None) -> None:
    """Bind the human this agent turn acts for (None clears the binding)."""
    _acting_user.set(sub if sub and sub not in _NON_HUMAN_SUBS else None)


def get_acting_user() -> str | None:
    return _acting_user.get()
