"""Cross-encoder reranker scoring helpers (Qwen3-Reranker via Ollama generate).

These cover the pure scoring logic without a live Ollama: log-probability
softmax over yes/no, and the text/logprob fallback chain.
"""

from __future__ import annotations

import math

from app.ai.providers.ollama import (
    _rerank_score_from_response,
    _score_from_logprobs,
)


def _lp(*pairs: tuple[str, float]) -> list[dict]:
    """Build a one-token logprobs payload; the chosen token is the first pair."""
    return [{"token": pairs[0][0], "logprob": pairs[0][1], "top_logprobs": [
        {"token": tok, "logprob": lp} for tok, lp in pairs
    ]}]


def test_logprob_softmax_prefers_yes():
    # yes is much more likely than no → score near 1.
    score = _score_from_logprobs(_lp(("yes", math.log(0.9)), ("no", math.log(0.1))))
    assert score is not None and score > 0.85


def test_logprob_softmax_prefers_no():
    score = _score_from_logprobs(_lp(("no", math.log(0.95)), ("yes", math.log(0.05))))
    assert score is not None and score < 0.15


def test_logprob_ranking_orders_documents():
    # A relevant doc (high P(yes)) must outrank a weak one.
    strong = _score_from_logprobs(_lp(("yes", math.log(0.8)), ("no", math.log(0.2))))
    weak = _score_from_logprobs(_lp(("yes", math.log(0.3)), ("no", math.log(0.7))))
    assert strong is not None and weak is not None
    assert strong > weak


def test_logprob_only_yes_candidate():
    score = _score_from_logprobs([{"token": "yes", "logprob": math.log(0.7)}])
    assert score is not None and abs(score - 0.7) < 1e-6


def test_logprob_camelcase_and_leading_space():
    # Tolerate alternate key casing and leading-space token text.
    payload = [{
        "token": " Yes",
        "logProb": math.log(0.6),
        "topLogprobs": [
            {"token": " Yes", "logProb": math.log(0.6)},
            {"token": " No", "logProb": math.log(0.4)},
        ],
    }]
    score = _score_from_logprobs(payload)
    assert score is not None and score > 0.5


def test_logprobs_missing_returns_none():
    assert _score_from_logprobs(None) is None
    assert _score_from_logprobs([]) is None
    assert _score_from_logprobs([{"token": "maybe", "logprob": -0.1}]) is None


def test_response_falls_back_to_text_when_no_logprobs():
    assert _rerank_score_from_response({"response": "yes"}) == 1.0
    assert _rerank_score_from_response({"response": "No, irrelevant"}) == 0.0
    assert _rerank_score_from_response({"response": "unsure"}) == 0.5


def test_response_prefers_logprobs_over_text():
    body = {
        "response": "no",  # text says no...
        "logprobs": _lp(("yes", math.log(0.92)), ("no", math.log(0.08))),  # ...but logprobs say yes
    }
    assert _rerank_score_from_response(body) > 0.85
