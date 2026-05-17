"""Prometheus metrics for the AI Workspace backend."""

from __future__ import annotations

try:
    from prometheus_client import Counter, Histogram, Gauge, REGISTRY
    _PROMETHEUS_AVAILABLE = True
except ImportError:
    _PROMETHEUS_AVAILABLE = False

if _PROMETHEUS_AVAILABLE:
    # HTTP request metrics
    http_requests_total = Counter(
        "aiworkspace_http_requests_total",
        "Total HTTP requests",
        ["method", "path", "status"],
    )
    http_request_duration_seconds = Histogram(
        "aiworkspace_http_request_duration_seconds",
        "HTTP request duration in seconds",
        ["method", "path"],
        buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    )

    # Agent metrics
    agent_turns_total = Counter(
        "aiworkspace_agent_turns_total",
        "Total agent conversation turns",
        ["outcome"],
    )
    agent_turn_duration_seconds = Histogram(
        "aiworkspace_agent_turn_duration_seconds",
        "Agent turn duration in seconds",
        buckets=[1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 270.0],
    )
    agent_tool_calls_total = Counter(
        "aiworkspace_agent_tool_calls_total",
        "Total agent tool calls",
        ["tool"],
    )

    # Extraction / processing
    extraction_duration_seconds = Histogram(
        "aiworkspace_extraction_duration_seconds",
        "Document extraction duration in seconds",
        buckets=[0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
    )
    extraction_errors_total = Counter(
        "aiworkspace_extraction_errors_total",
        "Document extraction errors",
        ["reason"],
    )

    # Queue / Celery
    celery_task_duration_seconds = Histogram(
        "aiworkspace_celery_task_duration_seconds",
        "Celery task execution duration",
        ["task_name"],
    )

    # Business metrics
    invoices_approved_total = Counter(
        "aiworkspace_invoices_approved_total",
        "Invoices approved",
    )
    invoices_rejected_total = Counter(
        "aiworkspace_invoices_rejected_total",
        "Invoices rejected",
    )
    anomalies_detected_total = Counter(
        "aiworkspace_anomalies_detected_total",
        "Anomalies detected",
        ["anomaly_type"],
    )
    approval_wait_seconds = Histogram(
        "aiworkspace_approval_wait_seconds",
        "Time waiting for approval decision",
        buckets=[60, 300, 900, 3600, 14400, 86400],
    )

    # Active connections
    ws_connections_active = Gauge(
        "aiworkspace_ws_connections_active",
        "Active WebSocket connections",
    )
else:
    # Stubs when prometheus_client not available
    class _Noop:
        def labels(self, **_kw):
            return self
        def inc(self, *a, **kw): pass
        def observe(self, *a, **kw): pass
        def set(self, *a, **kw): pass
        def time(self):
            import contextlib
            return contextlib.nullcontext()

    _noop = _Noop()
    http_requests_total = _noop
    http_request_duration_seconds = _noop
    agent_turns_total = _noop
    agent_turn_duration_seconds = _noop
    agent_tool_calls_total = _noop
    extraction_duration_seconds = _noop
    extraction_errors_total = _noop
    celery_task_duration_seconds = _noop
    invoices_approved_total = _noop
    invoices_rejected_total = _noop
    anomalies_detected_total = _noop
    approval_wait_seconds = _noop
    ws_connections_active = _noop
