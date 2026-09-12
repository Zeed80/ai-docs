"""Вердикт проверяльщика → патч графа EMG (план, Ф1, P1.3).

Проверяльщик не заменяет прочитанное — он меняет его заверенность:

* «подтверждено» — утверждение замещается тем же значением с заверенностью
  `corroborated` и ссылкой на свидетельство проверки;
* «опровергнуто» — замещается с `contradicted`, а измеренное значение
  входит в граф отдельным утверждением (`origin=traced`); оба варианта — в
  набор гипотез БЕЗ выбора: выбирает согласование, а не проверяльщик;
* «не измеримо» — заверенность прежняя, но утверждение ссылается на
  свидетельство с причиной: видно, что проверка была и почему не ответила.

Утверждение, которое уже подтвердил человек или ограничение
(`human_approved`, `constraint_validated`), не замещается вовсе — к графу
добавляется только свидетельство: проверяльщик не понижает чужое решение.
Патч пишется от имени `tracer` — ему разрешены `corroborated`/`contradicted`.
"""

from __future__ import annotations

from app.ai.cad_recognize.verifiers.contract import Hypothesis, Verdict
from app.domain.engineering_model_graph import (
    Assertion,
    EngineeringModelGraph,
    Evidence,
    ExactValue,
    GraphPatch,
    HypothesisOption,
    HypothesisSet,
)

_UNTOUCHABLE = {"human_approved", "constraint_validated"}
_STATUS_ASSURANCE = {"confirmed": "corroborated", "refuted": "contradicted"}


def verdict_patch(
    graph: EngineeringModelGraph,
    hypothesis: Hypothesis,
    verdict: Verdict,
    *,
    assertion_id: str,
    pass_id: str,
    measured_key: str = "value_mm",
    source_id: str | None = None,
) -> GraphPatch:
    """Патч, записывающий вердикт по утверждению ``assertion_id``."""
    current = next(item for item in graph.assertions if item.id == assertion_id)
    suffix = f"{pass_id}:{assertion_id}"
    evidence = Evidence(
        id=f"evidence:trace:{suffix}",
        kind="trace_run",
        source_id=source_id,
        payload={"verifier": hypothesis.kind, "path": hypothesis.path, **verdict.as_payload()},
    )
    base = {
        "patch_id": f"patch:trace:{suffix}",
        "base_revision": graph.revision,
        "base_sha256": graph.canonical_sha256,
        "producer": "tracer",
        "pass_id": pass_id,
        "idempotency_key": f"trace:{suffix}",
        "add_evidence": [evidence],
    }
    if current.assurance in _UNTOUCHABLE:
        return GraphPatch(**base)

    replacement = current.model_copy(
        update={
            "id": f"{assertion_id}@trace:{pass_id}",
            "assurance": _STATUS_ASSURANCE.get(verdict.status, current.assurance),
            "evidence_ids": [*current.evidence_ids, evidence.id],
            "supersedes_assertion_id": assertion_id,
            "state": "active",
        }
    )
    assertions = [replacement]
    options: list[HypothesisOption] = []
    sets: list[HypothesisSet] = []
    measured = verdict.measured.get(measured_key)
    if verdict.status == "refuted" and measured is not None:
        observed = Assertion(
            id=f"{assertion_id}@measured:{pass_id}",
            subject_id=current.subject_id,
            predicate=current.predicate,
            value=ExactValue(kind="exact", value=measured),
            unit=current.unit,
            coordinate_system=current.coordinate_system,
            origin="traced",
            assurance="observed",
            evidence_ids=[evidence.id],
            confidence=0.0,
            impacts=list(current.impacts),
        )
        assertions.append(observed)
        read_option = HypothesisOption(id=f"option:read:{suffix}", assertion_ids=[replacement.id])
        measured_option = HypothesisOption(
            id=f"option:measured:{suffix}", assertion_ids=[observed.id]
        )
        options = [read_option, measured_option]
        sets = [
            HypothesisSet(
                id=f"hypotheses:trace:{suffix}",
                option_ids=[read_option.id, measured_option.id],
            )
        ]
    return GraphPatch(
        **base,
        add_assertions=assertions,
        supersede_assertion_ids=[assertion_id],
        add_hypothesis_options=options,
        add_hypothesis_sets=sets,
    )
