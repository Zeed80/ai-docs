"""Client for the unordered primitive-set detector candidate."""

from __future__ import annotations

from app.ai.cad_recognize.neural import NeuralRecognizer


class PrimitiveSetRecognizer(NeuralRecognizer):
    """Local multi-type primitive proposals with tiled sheet stitching.

    This recognizer deliberately reuses NeuralRecognizer's transport,
    fail-closed parsing and overlap ownership, but addresses a separate
    endpoint/checkpoint.  It is opt-in until the entity-level promotion gate
    passes.
    """

    name = "primitive-set"

    def __init__(
        self,
        base_url: str | None = None,
        *,
        tile_size: int = 640,
        tile_overlap: int = 160,
    ):
        super().__init__(
            base_url,
            tile_size=tile_size,
            tile_overlap=tile_overlap,
            endpoint="/detect-primitives",
        )
