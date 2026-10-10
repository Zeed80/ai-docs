"""E25 crash barriers: kill this process at a named point, for the harness.

Inert unless AIW_CRASH_BARRIER names the point — it is set only by
tests/test_e25_crash_harness.py in a worker process it spawned itself; no
production process ever has it. SIGKILL, not an exception: nothing below the
barrier runs, no finally, no rollback, exactly like a lost worker.
"""

from __future__ import annotations

import os
import signal


def crash_barrier(name: str) -> None:
    if os.environ.get("AIW_CRASH_BARRIER") == name:
        os.kill(os.getpid(), signal.SIGKILL)
