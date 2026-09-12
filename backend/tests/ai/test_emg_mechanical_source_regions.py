"""Локализованное значение ридера не делает механический граф недопустимым."""

from __future__ import annotations

from app.ai.cad_emg_compat import legacy_spec_as_low_assurance
from app.services.engineering_model_graph import _domain_rule_issues


def test_a_source_region_edge_is_admissible_in_the_mechanical_domain():
    """С 2026-08-09 значение с рамкой на листе даёт ребро SourceRegion →
    DocumentSet (`located_in`); механический адаптер его не знал, и каждое
    чтение «по описанию» с рамками уходило в черновик с блокером
    `domain_unsupported_edge_type`."""
    spec = {
        "main_view": {"profile": {"shape": "rectangle", "width_mm": 80, "height_mm": 50}},
        "value_provenance": {
            "main_view/profile/width_mm": {
                "evidence": [{"source_bbox": [10, 10, 60, 30], "raw_text": "80"}]
            }
        },
    }
    graph = legacy_spec_as_low_assurance(spec, graph_id="g", source_uri="minio://sheet.png")

    assert graph.profile == "mechanical"
    assert any(edge.type == "located_in" for edge in graph.edges)
    codes = [issue["code"] for issue in _domain_rule_issues(graph)]
    assert "domain_unsupported_edge_type" not in codes
