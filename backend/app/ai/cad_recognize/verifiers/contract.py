"""Контракт проверяльщика: гипотеза на входе, вердикт на выходе."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

VerdictStatus = Literal["confirmed", "refuted", "unmeasurable"]

BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class Hypothesis:
    """То, что модель утверждает о листе, и где.

    ``kind`` выбирает проверяльщика; ``path`` — место значения в спеке (через
    него вердикт находит своё утверждение в графе); ``expected`` — прочитанные
    значения в мм; ``region_px`` — рамка на листе, которую дала модель.
    """

    kind: str
    path: str
    expected: dict[str, float] = field(default_factory=dict)
    region_px: BBox | None = None


@dataclass(frozen=True)
class Verdict:
    """Ответ проверяльщика. «Не измеримо» — тоже вердикт, с причиной."""

    status: VerdictStatus
    measured: dict[str, float] = field(default_factory=dict)
    evidence_bbox_px: BBox | None = None
    anchors_px: tuple[tuple[float, float], ...] = ()
    reason: str = ""
    cost_ms: float = 0.0

    def as_payload(self) -> dict[str, Any]:
        """Полезная нагрузка свидетельства `trace_run` в графе."""
        payload = asdict(self)
        payload["anchors_px"] = [list(point) for point in self.anchors_px]
        payload["evidence_bbox_px"] = (
            list(self.evidence_bbox_px) if self.evidence_bbox_px is not None else None
        )
        return payload
