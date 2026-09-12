"""Реестр проверяльщиков и порог измеримости каждого."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.ai.cad_recognize.verifiers.contract import Hypothesis, Verdict
from app.ai.cad_recognize.verifiers.view_frame import ViewFrame

VerifierFn = Callable[[Hypothesis, ViewFrame | None, Any], Verdict]


@dataclass(frozen=True)
class _Registration:
    kind: str
    fn: VerifierFn
    # Ниже этого размера элемента на листе проверяльщик не отвечает, а честно
    # говорит «не измеримо» — порог приходит из эксперимента E2, а не из догадки.
    min_feature_px: float


_REGISTRY: dict[str, _Registration] = {}


def register(kind: str, *, min_feature_px: float = 0.0) -> Callable[[VerifierFn], VerifierFn]:
    def decorate(fn: VerifierFn) -> VerifierFn:
        _REGISTRY[kind] = _Registration(kind=kind, fn=fn, min_feature_px=min_feature_px)
        return fn

    return decorate


def registered_kinds() -> list[str]:
    return sorted(_REGISTRY)


def verify(hypothesis: Hypothesis, frame: ViewFrame | None, sheet: Any) -> Verdict:
    """Вердикт по гипотезе; ни одна гипотеза не уходит без ответа."""
    registration = _REGISTRY.get(hypothesis.kind)
    if registration is None:
        return Verdict(status="unmeasurable", reason=f"нет проверяльщика для «{hypothesis.kind}»")
    size = hypothesis.expected.get("feature_px")
    if size is not None and size < registration.min_feature_px:
        return Verdict(
            status="unmeasurable",
            reason=(
                f"элемент {size:.1f} px меньше порога измеримости "
                f"{registration.min_feature_px:.1f} px"
            ),
        )
    started = time.monotonic()
    verdict = registration.fn(hypothesis, frame, sheet)
    cost = round((time.monotonic() - started) * 1000.0, 2)
    return Verdict(
        status=verdict.status,
        measured=verdict.measured,
        evidence_bbox_px=verdict.evidence_bbox_px,
        anchors_px=verdict.anchors_px,
        reason=verdict.reason,
        cost_ms=cost,
    )
