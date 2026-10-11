"""T4: usage receipts name the provider and model; Strata counts are read.

Strata (the main model) had no receipts at all and an unobserved Strata call
was receipted as "ollama". Receipts now carry provider and model (v2); an
Ollama receipt without a model keeps the v1 shape exactly.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.domain.work_budget_usage import (
    LLM_USAGE_SCHEMA,
    LLM_USAGE_SCHEMA_V2,
    canonical_usage_payload,
    capture_ollama_usage,
    openai_usage_from_body,
    record_openai_compatible_body,
    unknown_ollama_usage,
    valid_usage_receipt_payload,
)


def _reservation():
    return SimpleNamespace(
        id=uuid.uuid4(),
        ledger_id=uuid.uuid4(),
        work_order_id=uuid.uuid4(),
        operation_key="llm:x",
        request_digest="r" * 64,
        binding_digest="b" * 64,
    )


def test_openai_body_counts_are_read_raw_and_never_invented():
    known = openai_usage_from_body(
        {
            "usage": {"prompt_tokens": 57, "completion_tokens": 8},
            "choices": [{"finish_reason": "stop"}],
        },
        provider="strata",
        model="qwen3.8-flash-next",
    )
    assert (known.input_tokens.units, known.output_tokens.units, known.terminal_observed) == (
        57,
        8,
        True,
    )
    missing = openai_usage_from_body({"choices": []}, provider="strata", model="m")
    assert missing.input_tokens.units is None and missing.input_tokens.unknown_reason == "missing"
    coerced = openai_usage_from_body(
        {"usage": {"prompt_tokens": "57"}}, provider="strata", model="m"
    )
    assert coerced.input_tokens.units is None


def test_v2_receipt_round_trips_and_a_forged_model_fails():
    reservation = _reservation()
    evidence = openai_usage_from_body(
        {
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "choices": [{"finish_reason": "stop"}],
        },
        provider="strata",
        model="qwen3.8-flash-next",
    )
    payload = canonical_usage_payload(reservation, evidence, owner_key="alice")
    assert payload["schema"] == LLM_USAGE_SCHEMA_V2
    assert (payload["provider"], payload["model"]) == ("strata", "qwen3.8-flash-next")
    assert payload["tokens"]["total"] == {"status": "known", "units": 15}
    assert valid_usage_receipt_payload(payload, reservation, owner_key="alice")
    assert not valid_usage_receipt_payload(
        {**payload, "tokens": {**payload["tokens"], "total": {"status": "known", "units": 99}}},
        reservation,
        owner_key="alice",
    )
    assert not valid_usage_receipt_payload(
        {**payload, "provider": "anthropic-made-up"}, reservation, owner_key="alice"
    )


def test_an_unobserved_strata_call_is_receipted_as_strata_not_ollama():
    with capture_ollama_usage(provider="strata", model="qwen3.8-flash-next") as usage:
        pass
    payload = canonical_usage_payload(_reservation(), usage.evidence, owner_key="alice")
    assert payload["provider"] == "strata" and payload["tokens"]["total"]["status"] == "unknown"


def test_capture_records_the_openai_body_for_its_provider():
    with capture_ollama_usage(provider="strata", model="m") as usage:
        record_openai_compatible_body(
            {
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
                "choices": [{"finish_reason": "stop"}],
            }
        )
    assert (usage.evidence.provider, usage.evidence.input_tokens.units) == ("strata", 3)


def test_ollama_without_model_keeps_the_v1_shape():
    reservation = _reservation()
    payload = canonical_usage_payload(
        reservation, unknown_ollama_usage("response_not_observed"), owner_key="alice"
    )
    assert payload["schema"] == LLM_USAGE_SCHEMA and "model" not in payload
    assert valid_usage_receipt_payload(payload, reservation, owner_key="alice")


class _Response:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http")

    def json(self):
        return self._body


def test_the_verifier_strata_answer_is_counted_as_strata():
    # Live 2026-10-11: generate_json read Strata's OpenAI-shaped body as
    # Ollama's, so the verifier receipt said "ollama" with unknown counters.
    from app.ai.ollama_client import _capture_budgeted_ollama_response

    body = {
        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
    }
    _, evidence, error = _capture_budgeted_ollama_response(
        _Response(body), provider="strata", model="qwen3.8-flash-next"
    )
    payload = canonical_usage_payload(_reservation(), evidence, owner_key="alice")
    assert error is None
    assert (payload["provider"], payload["model"]) == ("strata", "qwen3.8-flash-next")
    assert payload["tokens"]["total"] == {"status": "known", "units": 150}
    _, failed, _ = _capture_budgeted_ollama_response(
        _Response({}, 502), provider="strata", model="m"
    )
    assert failed.provider == "strata" and failed.input_tokens.units is None


def test_a_metered_cloud_call_is_receipted_under_its_own_name():
    payload = canonical_usage_payload(
        _reservation(),
        unknown_ollama_usage("missing", provider="anthropic", model="claude-x"),
        owner_key="alice",
    )
    assert payload["provider"] == "anthropic" and payload["tokens"]["total"]["status"] == "unknown"
