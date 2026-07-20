"""Tiled direct CAD graph candidates and source-coordinate refinement."""

from app.ai.cad_recognize.neural import NeuralRecognizer


class EdgeGraphRecognizer(NeuralRecognizer):
    name = "edge-graph"

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
            endpoint="/detect-edge-graph",
        )


def snap_edge_graph_entities(
    entities,
    ink,
    *,
    snap_radius: float = 12.0,
    min_support: float = 0.85,
):
    """Snap learned topology to source skeleton vertices and reject gaps."""

    import cv2
    import numpy as np

    from app.ai.cad_ir.schema import Point, Segment
    from app.ai.drawing_vectorize import _skeletonize, _trace_paths

    skeleton = _skeletonize(ink > 0)
    paths, junctions = _trace_paths(skeleton)
    nodes = [tuple(point) for point in junctions.tolist()]
    for path in paths:
        if len(path):
            nodes.extend((tuple(path[0]), tuple(path[-1])))
    if not nodes:
        return []
    node_array = np.asarray(nodes, dtype=np.float32)
    supported = cv2.dilate(
        (ink > 0).astype(np.uint8),
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)),
    )

    def snap(point):
        distances = np.linalg.norm(node_array - (point.x, point.y), axis=1)
        index = int(distances.argmin())
        if float(distances[index]) > snap_radius:
            return None
        return Point(x=float(node_array[index, 0]), y=float(node_array[index, 1]))

    kept = []
    seen = set()
    for entity in entities:
        if not isinstance(entity, Segment):
            continue
        p1, p2 = snap(entity.p1), snap(entity.p2)
        if p1 is None or p2 is None or (p1.x == p2.x and p1.y == p2.y):
            continue
        length = max(int(np.hypot(p2.x - p1.x, p2.y - p1.y)) + 1, 2)
        xs = np.linspace(p1.x, p2.x, length).round().astype(int)
        ys = np.linspace(p1.y, p2.y, length).round().astype(int)
        xs = np.clip(xs, 0, ink.shape[1] - 1)
        ys = np.clip(ys, 0, ink.shape[0] - 1)
        support = float(supported[ys, xs].mean())
        if support < min_support:
            continue
        key = tuple(sorted(((round(p1.x), round(p1.y)), (round(p2.x), round(p2.y)))))
        if key in seen:
            continue
        seen.add(key)
        kept.append(
            entity.model_copy(
                update={
                    "p1": p1,
                    "p2": p2,
                    "evidence": [
                        *entity.evidence,
                        "source-skeleton-snapped",
                        f"line-support:{support:.4f}",
                    ],
                }
            )
        )
    return kept


class SourceSnappedEdgeGraphRecognizer(EdgeGraphRecognizer):
    name = "edge-graph-snapped"

    def recognize(self, ink, exclusion_boxes=None):
        from app.ai.cad_recognize.base import RecognizeOutput

        result = super().recognize(ink, exclusion_boxes)
        if result is None:
            return None
        entities = snap_edge_graph_entities(result.entities, ink)
        if not entities:
            return None
        return RecognizeOutput(
            entities=entities,
            keep_raster=None,
            thin_px=result.thin_px,
            thick_px=result.thick_px,
            notes={
                **result.notes,
                "source_coordinates_preserved": True,
                "source_snapped": True,
                "pre_snap_entities": len(result.entities),
                "post_snap_entities": len(entities),
            },
        )
