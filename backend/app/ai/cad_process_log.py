"""Per-generation observability hook for the CAD reading/drafting pipeline.

The CAD readers live several modules below the Celery task.  Passing a database
session through every prompt helper would couple inference code to persistence,
so the task installs one async recorder in this context variable.  Reader,
normalizer and kernel helpers emit small structured events through it; outside a
production CAD task the calls are harmless no-ops (unit tests and benchmarks do
not need a database).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from typing import Any

CadProcessRecorder = Callable[[str, str, str, dict[str, Any] | None], Awaitable[None]]

_recorder: ContextVar[CadProcessRecorder | None] = ContextVar("cad_process_recorder", default=None)


def install_cad_process_recorder(recorder: CadProcessRecorder) -> Token:
    """Install a recorder for the current async task and return its token."""

    return _recorder.set(recorder)


def reset_cad_process_recorder(token: Token) -> None:
    """Restore the previous recorder after the pipeline has finished."""

    _recorder.reset(token)


async def record_cad_process_event(
    stage: str,
    status: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Emit one structured event when running inside an observed CAD task."""

    recorder = _recorder.get()
    if recorder is not None:
        await recorder(stage, status, message, details)
