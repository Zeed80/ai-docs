"""User input sanitizer — detect and redact prompt injection attempts."""

from __future__ import annotations

import re

_INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(r"ignore\s+(previous|all|above)\s+instructions?", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(?:a|an|the)?\s*\w+\s+without\s+restrictions?", re.IGNORECASE),
    re.compile(r"<\s*system\s*>", re.IGNORECASE),
    re.compile(r"\[INST\]|\[/?SYS\]|<<SYS>>", re.IGNORECASE),
    re.compile(r"###\s*System\s*:", re.IGNORECASE),
    re.compile(r"ASSISTANT\s*:\s*(?:OK|Sure|Of course)", re.IGNORECASE),
    re.compile(r"disregard\s+(?:all\s+)?(?:previous|prior)\s+(?:instructions?|prompts?|context)", re.IGNORECASE),
    re.compile(r"new\s+instructions?\s*:", re.IGNORECASE),
]

_MAX_INPUT_LEN = 32_768


def sanitize_user_input(text: str) -> tuple[str, list[str]]:
    """
    Sanitize user text before passing to AI.
    Returns (sanitized_text, warnings).
    Does NOT block requests — strips matched patterns and logs warnings.
    """
    warnings: list[str] = []

    if len(text) > _MAX_INPUT_LEN:
        text = text[:_MAX_INPUT_LEN]
        warnings.append("input_truncated")

    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            warnings.append("injection_detected")
            text = pattern.sub("[redacted]", text)

    return text, warnings
