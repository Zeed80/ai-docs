"""Recognition backends that fill the CAD IR.

Two production backends remain, and both are auxiliary to the spec drafter:
``CvRecognizer`` (classical trace) and ``TechnicalVectorizerRecognizer``
(pretrained line model). They only *propose* entities with per-entity
confidence; the independent verifier (``verify.py``) rasterizes proposals and
scores them against the source ink — generation never grades itself.

Seven from-scratch neural proposers (primitive-set, hybrid-engineering,
hierarchical-sheet, evidence-heatmap, directional-fields, edge-graph,
multi-type) were removed on 2026-07-25: every one of them was rejected by the
fail-closed promotion gate (best entity F1 0.066 against a 0.995 bar) and none
was ever reachable from the production path. The lesson they encode is in
``DXF_CAD_DEVELOPMENT_PLAN.md``: line-level pixel proposers cannot recover the
semantics (views, dimensions, tolerances) an ЕСКД redraw needs.
"""

from app.ai.cad_recognize.base import RecognizeOutput
from app.ai.cad_recognize.cv import CvRecognizer
from app.ai.cad_recognize.technical_vectorizer import TechnicalVectorizerRecognizer

__all__ = [
    "CvRecognizer",
    "TechnicalVectorizerRecognizer",
    "RecognizeOutput",
]
