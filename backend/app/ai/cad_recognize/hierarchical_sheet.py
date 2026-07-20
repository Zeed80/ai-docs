"""Global-view → local-primitive candidate recognizer."""

from __future__ import annotations

from app.ai.cad_recognize.neural import NeuralRecognizer


class HierarchicalSheetRecognizer(NeuralRecognizer):
    """Detect view regions before proposing any local geometry.

    No full-sheet fallback is hidden here.  If the global model cannot prove a
    view region, the candidate declines and production CV remains available.
    """

    name = "hierarchical-sheet"

    def __init__(self, base_url: str | None = None):
        super().__init__(base_url, endpoint="/detect-hierarchical")
