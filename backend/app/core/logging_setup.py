"""E47: one structured-log setup for the API and the workers.

Logs carry ids, sizes and hashes — not the text people and models wrote.
A value under a content-like key (prompt, messages, content, text, body …)
or a secret-like key (password, token, api_key …) is replaced by its length
and a short hash, enough to tell two records apart or match one to the
database without copying it. ``LOG_INCLUDE_CONTENT=true`` turns content
back on for a debugging session; secrets stay masked regardless.

Every line of an HTTP request carries its ``request_id``
(``SecurityHeadersMiddleware``), so one request is followed through the log
without dumping its context.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

import structlog

CONTENT_KEYS = frozenset(
    {
        "prompt",
        "system",
        "system_prompt",
        "messages",
        "content",
        "text",
        "body",
        "body_text",
        "body_html",
        "user_text",
        "assistant_text",
        "question",
        "answer",
        "instruction",
        "raw",
        "response_text",
    }
)
SECRET_KEYS = frozenset(
    {
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "authorization",
        "cookie",
        "value_encrypted",
    }
)


def _fingerprint(value: Any) -> str:
    text = value if isinstance(value, str) else repr(value)
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:10]
    return f"<{len(text)} chars sha256:{digest}>"


def redact_event(_logger, _method, event_dict: dict) -> dict:
    include_content = os.environ.get("LOG_INCLUDE_CONTENT", "").lower() == "true"
    for key in list(event_dict):
        lowered = key.lower()
        if lowered in SECRET_KEYS or any(s in lowered for s in ("password", "secret", "api_key")):
            event_dict[key] = "<masked>"
        elif lowered in CONTENT_KEYS and not include_content and event_dict[key] is not None:
            event_dict[key] = _fingerprint(event_dict[key])
    return event_dict


def configure_logging(*, debug: bool, level: str) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            redact_event,
            structlog.dev.ConsoleRenderer() if debug else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
    )
