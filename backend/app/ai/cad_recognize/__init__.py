"""Pluggable recognition backends that fill the CAD IR.

Backends (CV skeleton fit today; neural seq2seq and VLM crops later) only
*propose* entities with per-entity confidence. The independent verifier
(``verify.py``) rasterizes proposals and scores them against the source ink —
generation never grades itself.
"""

from app.ai.cad_recognize.base import RecognizeOutput
from app.ai.cad_recognize.cv import CvRecognizer

__all__ = ["CvRecognizer", "RecognizeOutput"]
