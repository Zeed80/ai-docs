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
