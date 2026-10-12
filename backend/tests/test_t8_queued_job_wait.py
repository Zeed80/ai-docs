"""T8g: a turn waits, briefly, for the background job it queued (E52 catalog)."""

import pytest

from app.ai import agent_loop

QUEUED = {
    "version": 1,
    "status": "partial",
    "data": {"status": "queued", "task_id": "t-1", "supplier_id": "s"},
    "error_code": "job_queued",
    "retryable": False,
    "evidence": {"adapter_contract": "http_async_job_response_v1"},
    "checkpoint": {"task_id": "t-1", "supplier_id": "s", "status": "queued"},
}


class _Job:
    def __init__(self, states, result):
        self._states = list(states)
        self.result = result

    @property
    def state(self):
        return self._states.pop(0) if len(self._states) > 1 else self._states[0]


def _session(monkeypatch, job):
    import celery.result

    monkeypatch.setattr(celery.result, "AsyncResult", lambda task_id, app=None: job)
    events = []

    async def send(event):
        events.append(event)

    session = agent_loop.AgentSession(send)
    session.QUEUED_JOB_POLL_S = 0.01
    return session


@pytest.mark.asyncio
async def test_a_job_finished_within_the_wait_answers_the_call(monkeypatch):
    job = _Job(["PENDING", "STARTED", "SUCCESS"], {"pages_read": 3, "entries_created": 5})
    result = await _session(monkeypatch, job)._await_queued_job(QUEUED)
    assert result["status"] == "succeeded"
    assert result["data"]["job_result"]["entries_created"] == 5
    assert result["evidence"]["job_state"] == "SUCCESS"


@pytest.mark.asyncio
async def test_a_failed_job_is_a_failure_not_a_success(monkeypatch):
    job = _Job(["FAILURE"], RuntimeError("site down"))
    result = await _session(monkeypatch, job)._await_queued_job(QUEUED)
    assert result["status"] == "failed" and result["error_code"] == "job_failed"
    assert "site down" in result["evidence"]["job_error"]


@pytest.mark.asyncio
async def test_a_job_still_running_after_the_wait_stays_queued(monkeypatch):
    session = _session(monkeypatch, _Job(["PENDING"], None))
    session.QUEUED_JOB_WAIT_S = 0.05
    assert await session._await_queued_job(QUEUED) == QUEUED


@pytest.mark.asyncio
async def test_other_results_are_not_touched(monkeypatch):
    session = _session(monkeypatch, _Job(["SUCCESS"], {}))
    done = {"version": 1, "status": "succeeded", "data": {"id": "x"}}
    assert await session._await_queued_job(done) == done
    assert await session._await_queued_job({"ok": True}) == {"ok": True}
