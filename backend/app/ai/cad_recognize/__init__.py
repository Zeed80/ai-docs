"""Pluggable recognition backends that fill the CAD IR.

Backends (CV skeleton fit today; neural seq2seq and VLM crops later) only
*propose* entities with per-entity confidence. The independent verifier
(``verify.py``) rasterizes proposals and scores them against the source ink —
generation never grades itself.
"""

from app.ai.cad_recognize.base import RecognizeOutput
from app.ai.cad_recognize.cv import CvRecognizer
from app.ai.cad_recognize.directional_fields import DirectionalFieldRecognizer
from app.ai.cad_recognize.edge_graph import EdgeGraphRecognizer, SourceSnappedEdgeGraphRecognizer
from app.ai.cad_recognize.evidence_heatmap import EvidenceHeatmapRecognizer
from app.ai.cad_recognize.hybrid_engineering import HybridEngineeringRecognizer
from app.ai.cad_recognize.hierarchical_sheet import HierarchicalSheetRecognizer
from app.ai.cad_recognize.multi_type import MultiTypeProposalRecognizer
from app.ai.cad_recognize.primitive_set import PrimitiveSetRecognizer

__all__ = [
    "CvRecognizer",
    "DirectionalFieldRecognizer",
    "EdgeGraphRecognizer",
    "SourceSnappedEdgeGraphRecognizer",
    "EvidenceHeatmapRecognizer",
    "HybridEngineeringRecognizer",
    "HierarchicalSheetRecognizer",
    "PrimitiveSetRecognizer",
    "MultiTypeProposalRecognizer",
    "RecognizeOutput",
]
