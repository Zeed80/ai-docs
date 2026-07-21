"""Opt-in client for full CadIR proposal heads; never a truth authority."""

from app.ai.cad_recognize.neural import NeuralRecognizer


class MultiTypeProposalRecognizer(NeuralRecognizer):
    name = "multi-type-proposal"

    def __init__(self, base_url: str | None = None):
        super().__init__(
            base_url,
            endpoint="/detect-multi-type",
            tile_size=640,
            tile_overlap=160,
        )
