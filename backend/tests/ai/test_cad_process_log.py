import uuid

import pytest

from app.ai.cad_process_log import (
    install_cad_process_recorder,
    record_cad_process_event,
    reset_cad_process_recorder,
)


@pytest.mark.asyncio
async def test_process_events_are_noop_without_production_recorder():
    await record_cad_process_event("reader", "started", "read", {"pass": 1})


@pytest.mark.asyncio
async def test_process_events_reach_installed_recorder_and_reset():
    events = []

    async def recorder(stage, status, message, details):
        events.append((stage, status, message, details))

    token = install_cad_process_recorder(recorder)
    try:
        await record_cad_process_event("kernel.compile", "started", "compile", {"sha256": "abc"})
    finally:
        reset_cad_process_recorder(token)
    await record_cad_process_event("kernel.compile", "completed", "ignored")

    assert events == [("kernel.compile", "started", "compile", {"sha256": "abc"})]


@pytest.mark.asyncio
async def test_persisted_process_log_tracks_sequence_and_only_terminal_failure(
    monkeypatch,
):
    from app.tasks.cad_trace import _append_cad_process_event

    class Gen:
        params = {}

    gen = Gen()

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _model, _key):
            return gen

        async def commit(self):
            return None

    monkeypatch.setattr("app.db.session._get_session_factory", lambda: lambda: Session())
    gen_id = uuid.uuid4()

    await _append_cad_process_event(
        gen_id,
        "reader.fragment.question",
        "completed",
        "one prompt completed",
        {
            "_progress_pct": 25,
            "_partial_spec": {"main_view": {"outer": [{"diameter_mm": 10}]}},
            "_model_output": {
                "model": "test-model",
                "prompt": "read",
                "answer": '{"ok":true}',
                "thinking": "",
                "parsed": True,
            },
        },
    )
    await _append_cad_process_event(gen_id, "pipeline", "failed", "terminal", {"terminal": True})

    process = gen.params["cad_process"]
    assert [event["sequence"] for event in process["events"]] == [1, 2]
    assert process["events"][0]["status"] == "completed"
    assert process["events"][0]["details"]["model_output_id"] == "model-output-1"
    assert "_model_output" not in process["events"][0]["details"]
    assert process["progress_pct"] == 25
    assert gen.params["cad_partial_spec"]["main_view"]["outer"][0]["diameter_mm"] == 10
    assert gen.params["cad_model_outputs"][0]["answer"] == '{"ok":true}'
    assert process["status"] == "failed"
    assert process["current_stage"] == "pipeline"


@pytest.mark.asyncio
async def test_load_cad_partial_spec_assigns_stable_feature_ids(monkeypatch):
    # A timeout-recovered spec never went through read_spec_best_effort's own
    # return path (the source of assign_stable_feature_ids in normal reads —
    # see spec_fragments.read_spec_best_effort) — a live run on
    # detal_126.png hit exactly this branch and produced a spec whose
    # chamfers/cross_holes had no id, starving the native EMG builder of
    # anything to emit a Feature node for. _load_cad_partial_spec must apply
    # the same pass itself.
    from app.tasks.cad_trace import _load_cad_partial_spec

    class Gen:
        params = {
            "cad_partial_spec": {
                "main_view": {
                    "chamfers": [
                        {"size_mm": 1, "angle_deg": 45, "location": "left_end"},
                    ]
                },
            },
        }

    gen = Gen()

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _model, _key):
            return gen

    monkeypatch.setattr("app.db.session._get_session_factory", lambda: lambda: Session())

    spec = await _load_cad_partial_spec(uuid.uuid4())
    assert spec["main_view"]["chamfers"][0]["id"] == "0:chamfers:0"


@pytest.mark.asyncio
async def test_load_cad_partial_spec_tolerates_missing_generation(monkeypatch):
    from app.tasks.cad_trace import _load_cad_partial_spec

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _model, _key):
            return None

    monkeypatch.setattr("app.db.session._get_session_factory", lambda: lambda: Session())

    assert await _load_cad_partial_spec(uuid.uuid4()) == {}


# ── Схлопывание повторов в журнале прогона ───────────────────────────────────


def _append(events: list[dict], stage: str, status: str, message: str, details: dict) -> None:
    """Та же логика склейки, что и в `_append_cad_process_event`."""
    sequence = int(events[-1]["sequence"]) + 1 if events else 1
    previous = events[-1] if events else None
    if (
        previous is not None
        and previous.get("stage") == stage
        and previous.get("status") == status
        and previous.get("message") == message
    ):
        previous["repeat"] = int(previous.get("repeat") or 1) + 1
        previous["details"] = details
        return
    events.append(
        {
            "sequence": sequence,
            "stage": stage,
            "status": status,
            "message": message,
            "details": details,
        }
    )


def test_identical_consecutive_events_collapse_into_one():
    """Пять одинаковых отказов подряд — один факт, повторённый на каждом проходе.

    В живом журнале из 141 события такие серии занимали место, в котором надо
    было искать настоящую причину отказа.
    """
    events: list[dict] = []
    for index in range(5):
        _append(events, "reader.diameter_dimensions", "failed", "Диаметры не связаны", {"i": index})

    assert len(events) == 1
    assert events[0]["repeat"] == 5


def test_the_latest_details_win_over_the_first():
    """У повтора детали могут отличаться — показывать надо свежие."""
    events: list[dict] = []
    _append(events, "reader.pass", "failed", "проход", {"pass": 1})
    _append(events, "reader.pass", "failed", "проход", {"pass": 2})

    assert events[0]["details"] == {"pass": 2}


def test_a_different_message_starts_a_new_event():
    events: list[dict] = []
    _append(events, "reader.pass", "failed", "проход 1", {})
    _append(events, "reader.pass", "failed", "проход 2", {})
    _append(events, "reader.pass", "completed", "проход 2", {})

    assert [e["message"] for e in events] == ["проход 1", "проход 2", "проход 2"]
    assert all("repeat" not in e for e in events)


def test_a_single_event_carries_no_repeat_marker():
    events: list[dict] = []
    _append(events, "reader", "started", "чтение", {})
    assert "repeat" not in events[0]
