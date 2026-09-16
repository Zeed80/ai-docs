"""Вердикт проверяльщика → граф EMG (план, Ф1, P1.3)."""

from __future__ import annotations

from app.ai.cad_recognize.verifiers import Hypothesis, Verdict
from app.ai.cad_recognize.verifiers.graph import verdict_patch
from app.domain.engineering_model_graph import (
    Assertion,
    EngineeringModelGraph,
    ExactValue,
    GraphEdge,
    GraphNode,
    apply_graph_patch,
)


def _graph(assurance: str = "proposed") -> EngineeringModelGraph:
    return EngineeringModelGraph(
        graph_id="emg:verify",
        profile="mechanical",
        nodes=[
            GraphNode(id="docs", type="DocumentSet"),
            GraphNode(id="product", type="Product"),
        ],
        edges=[GraphEdge(id="e1", type="contains", source_id="docs", target_id="product")],
        assertions=[
            Assertion(
                id="a-length",
                subject_id="product",
                predicate="main_view.outer.0.length_mm",
                value=ExactValue(kind="exact", value=50.0),
                unit="mm",
                origin="observed",
                assurance=assurance,
                confidence=0.6,
            )
        ],
    ).sealed()


_HYPOTHESIS = Hypothesis("dimension_line", "main_view/outer/0/length_mm", {"value_mm": 50.0})


def _apply(graph, verdict):
    patch = verdict_patch(graph, _HYPOTHESIS, verdict, assertion_id="a-length", pass_id="p1")
    return apply_graph_patch(graph, patch)


def _active(graph):
    return [item for item in graph.assertions if item.state == "active"]


def test_a_confirmed_value_is_corroborated_not_replaced():
    graph = _apply(_graph(), Verdict(status="confirmed", measured={"value_mm": 50.1}))

    (active,) = _active(graph)
    assert active.assurance == "corroborated"
    assert active.value.value == 50.0  # значение — прочитанное, не измеренное
    assert any(
        item.kind == "trace_run" and item.id in active.evidence_ids for item in graph.evidence
    )


def test_a_refuted_value_becomes_a_hypothesis_set_without_a_choice():
    graph = _apply(
        _graph(), Verdict(status="refuted", measured={"value_mm": 65.0}, reason="линия 65 мм")
    )

    active = {item.id: item for item in _active(graph)}
    read = active["a-length@trace:p1"]
    measured = active["a-length@measured:p1"]
    assert read.assurance == "contradicted" and read.value.value == 50.0
    assert measured.origin == "observed" and measured.value.value == 65.0
    # Замер по листу — не трасса: уровень 8 графа не требует для него visual_verification.
    from app.services.engineering_model_graph import verify_graph

    _state, issues = verify_graph(graph)
    assert not [i for i in issues if i["code"] == "trace_verification_incomplete"]
    (hypotheses,) = graph.hypothesis_sets
    assert len(hypotheses.option_ids) == 2 and hypotheses.selected_option_id is None


def test_an_unmeasurable_check_leaves_a_trace_but_not_a_verdict():
    graph = _apply(_graph(), Verdict(status="unmeasurable", reason="элемент меньше порога"))

    (active,) = _active(graph)
    assert active.assurance == "proposed"
    evidence = next(item for item in graph.evidence if item.id in active.evidence_ids)
    assert evidence.payload["reason"] == "элемент меньше порога"


def test_a_human_decision_is_never_downgraded_by_a_verifier():
    graph = _apply(_graph("human_approved"), Verdict(status="refuted", measured={"value_mm": 65.0}))

    (active,) = _active(graph)
    assert active.id == "a-length" and active.assurance == "human_approved"
    assert any(item.kind == "trace_run" for item in graph.evidence)  # но проверка записана
