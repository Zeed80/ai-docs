"""Visible degradation for "best effort" agent features.

Many agent subsystems (semantic audit, feedback memory, canvas map, metrics)
must never crash a user turn, so their failures are suppressed. A bare
``except Exception: pass`` makes "feature broken" indistinguishable from
"feature off" in production. Route those suppressions through
:func:`log_degraded` instead: the failure is logged once per call site with
context and counted in the ``aiworkspace_agent_degraded_total`` metric.

Usage::

    try:
        ...optional work...
    except Exception as exc:
        log_degraded("orchestrator.semantic_audit", exc)
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()


def log_degraded(component: str, exc: BaseException, **context: object) -> None:
    """Record a suppressed failure of an optional agent feature.

    ``component`` is a stable dotted identifier of the call site
    (e.g. ``"orchestrator.feedback_record"``) — it becomes the metric label,
    so keep the cardinality low (no IDs or user input).
    """
    try:
        logger.warning(
            "agent_component_degraded",
            component=component,
            error=f"{type(exc).__name__}: {exc}",
            **context,
        )
        from app.core.metrics import agent_degraded_total

        agent_degraded_total.labels(component=component).inc()
    except Exception:
        # Degradation reporting must never become a new failure source.
        pass
