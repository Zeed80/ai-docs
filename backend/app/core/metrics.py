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
    agent_degraded_total = Counter(
        "aiworkspace_agent_degraded_total",
        "Suppressed agent-component failures (feature degraded, not off)",
        ["component"],
    )
    orchestrator_plan_fallback_total = Counter(
        "aiworkspace_orchestrator_plan_fallback_total",
        "Orchestrator LLM plan fallbacks to the heuristic planner",
        ["reason"],
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

    # Infrastructure gauges
    celery_queue_depth = Gauge(
        "aiworkspace_celery_queue_depth",
        "Number of pending Celery tasks",
        ["queue"],
    )
    ollama_vram_used_bytes = Gauge(
        "aiworkspace_ollama_vram_used_bytes",
        "VRAM used by Ollama models in bytes",
        ["model"],
    )
    ollama_model_loaded = Gauge(
        "aiworkspace_ollama_model_loaded",
        "Whether an Ollama model is currently loaded (1=yes, 0=no)",
        ["model"],
    )

    # GPU telemetry (sidecar gpu-temp-helper / nvidia-smi fallback)
    gpu_utilization_percent = Gauge(
        "aiworkspace_gpu_utilization_percent",
        "GPU utilization percent",
    )
    gpu_temperature_celsius = Gauge(
        "aiworkspace_gpu_temperature_celsius",
        "GPU temperature in Celsius",
        ["sensor"],  # gpu | mem_junction
    )
    gpu_power_watts = Gauge(
        "aiworkspace_gpu_power_watts",
        "GPU power in watts",
        ["kind"],  # draw | limit
    )
    gpu_vram_bytes = Gauge(
        "aiworkspace_gpu_vram_bytes",
        "GPU VRAM in bytes",
        ["kind"],  # used | total
    )
    gpu_fan_percent = Gauge(
        "aiworkspace_gpu_fan_percent",
        "GPU fan speed percent",
    )

    # CPU telemetry (sidecar gpu-temp-helper)
    cpu_utilization_percent = Gauge(
        "aiworkspace_cpu_utilization_percent",
        "CPU utilization percent",
    )
    cpu_temperature_celsius = Gauge(
        "aiworkspace_cpu_temperature_celsius",
        "CPU temperature in Celsius (Tctl/package)",
    )
    cpu_power_watts = Gauge(
        "aiworkspace_cpu_power_watts",
        "CPU package power draw in watts (RAPL)",
    )
    cpu_frequency_mhz = Gauge(
        "aiworkspace_cpu_frequency_mhz",
        "CPU frequency in MHz",
        ["kind"],  # current | limit
    )

    # Agent step counter
    agent_steps_total = Counter(
        "aiworkspace_agent_steps_total",
        "Total agent reasoning steps executed",
        ["scenario"],
    )

    # LLM / model inference
    llm_tokens_total = Counter(
        "aiworkspace_llm_tokens_total",
        "Total LLM tokens generated",
        ["model", "task"],
    )
    llm_request_duration_seconds = Histogram(
        "aiworkspace_llm_request_duration_seconds",
        "LLM inference request duration in seconds",
        ["model", "task"],
        buckets=[0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0],
    )

    # Технологические процессы (ТП)
    tp_created_total = Counter(
        "aiworkspace_tp_created_total",
        "Total manufacturing process plans created",
        ["tp_type"],
    )
    normcontrol_passed_total = Counter(
        "aiworkspace_normcontrol_passed_total",
        "Normcontrol checks passed",
    )
    normcontrol_failed_total = Counter(
        "aiworkspace_normcontrol_failed_total",
        "Normcontrol checks failed",
    )
    gost_forms_exported_total = Counter(
        "aiworkspace_gost_forms_exported_total",
        "GOST forms exported",
        ["form_type", "format"],
    )
    tp_generation_duration_seconds = Histogram(
        "aiworkspace_tp_generation_duration_seconds",
        "TP generation pipeline duration in seconds",
        buckets=[5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
    )

    # Scenario traces
    scenario_runs_total = Counter(
        "aiworkspace_scenario_runs_total",
        "Total scenario executions",
        ["scenario"],
    )
    scenario_errors_total = Counter(
        "aiworkspace_scenario_errors_total",
        "Scenario execution errors",
        ["scenario", "reason"],
    )
    scenario_duration_seconds = Histogram(
        "aiworkspace_scenario_duration_seconds",
        "Scenario execution duration in seconds",
        ["scenario"],
        buckets=[1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0],
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
    agent_degraded_total = _noop
    orchestrator_plan_fallback_total = _noop
    extraction_duration_seconds = _noop
    extraction_errors_total = _noop
    celery_task_duration_seconds = _noop
    invoices_approved_total = _noop
    invoices_rejected_total = _noop
    anomalies_detected_total = _noop
    approval_wait_seconds = _noop
    ws_connections_active = _noop
    celery_queue_depth = _noop
    ollama_vram_used_bytes = _noop
    ollama_model_loaded = _noop
    gpu_utilization_percent = _noop
    gpu_temperature_celsius = _noop
    gpu_power_watts = _noop
    gpu_vram_bytes = _noop
    gpu_fan_percent = _noop
    cpu_utilization_percent = _noop
    cpu_temperature_celsius = _noop
    cpu_power_watts = _noop
    cpu_frequency_mhz = _noop
    agent_steps_total = _noop
    llm_tokens_total = _noop
    llm_request_duration_seconds = _noop
    tp_created_total = _noop
    normcontrol_passed_total = _noop
    normcontrol_failed_total = _noop
    gost_forms_exported_total = _noop
    tp_generation_duration_seconds = _noop
    scenario_runs_total = _noop
    scenario_errors_total = _noop
    scenario_duration_seconds = _noop
