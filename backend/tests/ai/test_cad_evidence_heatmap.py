from __future__ import annotations

import numpy as np

from app.ai.cad_recognize.evidence_heatmap import EvidenceHeatmapRecognizer


def test_evidence_recognizer_marks_fitted_geometry_as_inferred(monkeypatch) -> None:
    import cv2
    import httpx

    mask = np.zeros((128, 128), dtype=np.uint8)
    cv2.line(mask, (10, 64), (118, 64), 255, 2)
    ok, png = cv2.imencode(".png", mask)
    assert ok

    class Response:
        content = png.tobytes()
        headers = {"x-cad-evidence-step": "1200"}

        @staticmethod
        def raise_for_status() -> None:
            return None

    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: Response())
    result = EvidenceHeatmapRecognizer("http://candidate").recognize(mask)

    assert result is not None
    assert result.entities
    assert all(entity.origin == "neural" for entity in result.entities)
    assert all(entity.assurance == "inferred" for entity in result.entities)
    assert result.notes["deterministic_fitter"] == "cv"


def test_evidence_recognizer_tiles_large_sheet_without_scale_collapse(monkeypatch) -> None:
    import cv2
    import httpx

    requested_shapes = []

    class Response:
        headers = {"x-cad-evidence-step": "1200"}

        def __init__(self, content: bytes):
            self.content = content

        @staticmethod
        def raise_for_status() -> None:
            return None

    def post(*args, **kwargs):
        image = cv2.imdecode(
            np.frombuffer(kwargs["files"]["file"][1], dtype=np.uint8),
            cv2.IMREAD_GRAYSCALE,
        )
        requested_shapes.append(image.shape)
        mask = np.zeros_like(image)
        cv2.line(mask, (4, image.shape[0] // 2), (image.shape[1] - 4, image.shape[0] // 2), 255, 2)
        ok, png = cv2.imencode(".png", mask)
        assert ok
        return Response(png.tobytes())

    monkeypatch.setattr(httpx, "post", post)
    ink = np.zeros((900, 1100), dtype=np.uint8)
    cv2.line(ink, (0, 320), (1099, 320), 255, 2)
    cv2.line(ink, (0, 580), (1099, 580), 255, 2)
    result = EvidenceHeatmapRecognizer(
        "http://candidate", tile_size=640, tile_overlap=160
    ).recognize(ink)

    assert result is not None
    assert result.notes["tiled"] is True
    assert result.notes["tiles"] == 4
    assert result.notes["source_coordinates_preserved"] is True
    assert set(requested_shapes) == {(640, 640)}
