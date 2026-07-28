from app.tasks.cad_trace import _unplaced_callouts


def test_unplaced_callouts_counts_only_geometric_dimensions():
    result = _unplaced_callouts(
        {"_dimensions": [{"value_mm": 80.0}]},
        {"dimensions": [
            {"value": "Ø80"},
            {"value": "470"},
            {"value": "R4"},
            {"value": "1x45°"},
        ]},
    )
    assert result["read"] == 2
    assert result["placed"] == 1
    assert result["unplaced"] == ["470"]
