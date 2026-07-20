"""Direct segment proposals decoded from learned directional geometry fields."""

from __future__ import annotations

from app.ai.cad_recognize.neural import NeuralRecognizer


class DirectionalFieldRecognizer(NeuralRecognizer):
    """Opt-in endpoint/junction/direction candidate.

    Unlike evidence-heatmap, this path does not invoke the legacy CV fitter.
    The service itself emits only direction- and support-consistent inferred
    segments.  Overlap ownership maps tile-local proposals into sheet space.
    """

    name = "directional-fields"

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
            endpoint="/detect-directional",
        )
