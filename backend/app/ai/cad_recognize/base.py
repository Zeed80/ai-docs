"""Recognition backend contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.ai.cad_ir.schema import Entity


@dataclass
class RecognizeOutput:
    """Entities proposed by one backend for a binarized sheet.

    ``keep_raster`` is a bool HxW mask of regions intentionally left as raster
    (solid fills, excluded text/title block) — renders copy them through and
    the verifier excludes them from precision scoring.
    """

    entities: list[Entity]
    keep_raster: Any | None = None  # numpy bool array
    thin_px: int = 1
    thick_px: int = 2
    notes: dict[str, Any] = field(default_factory=dict)


class Recognizer(Protocol):
    """A backend takes a binarized ink mask (uint8, 255 = ink) plus optional
    exclusion boxes and proposes IR entities, or None to decline."""

    name: str

    def recognize(
        self,
        ink: Any,
        exclusion_boxes: list[tuple[int, int, int, int]] | None = None,
    ) -> RecognizeOutput | None: ...
