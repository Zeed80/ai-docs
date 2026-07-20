from app.ai.cad_recognize.directional_fields import DirectionalFieldRecognizer


def test_directional_recognizer_uses_direct_tiled_endpoint() -> None:
    recognizer = DirectionalFieldRecognizer(
        "http://candidate",
        tile_size=512,
        tile_overlap=128,
    )
    assert recognizer.name == "directional-fields"
    assert recognizer._endpoint == "/detect-directional"
    assert recognizer._tile_size == 512
    assert recognizer._tile_overlap == 128
