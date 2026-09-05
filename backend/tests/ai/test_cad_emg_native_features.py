"""Native Feature/Sheet/View EMG nodes for a read mechanical spec (Фаза 1.2).

Additive to the legacy-spec dotted-path passthrough — see
cad_emg_compat.native_feature_graph_additions. Fail-closed contract under
test: a represented_by edge is emitted ONLY from a view's own
features_shown; an element with no stable id produces no Feature node at
all (assign_stable_feature_ids never ran, or a leaf this builder doesn't
cover yet).
"""

from __future__ import annotations

from app.ai.cad_emg_compat import feature_spec_path, native_feature_graph_additions


def _node_ids(nodes, node_type=None):
    return {n.id for n in nodes if node_type is None or n.type == node_type}


def _edges_of_type(edges, edge_type):
    return [e for e in edges if e.type == edge_type]


def test_sheet_and_view_nodes_always_emitted_for_declared_views():
    spec = {
        "main_view": {"outer": []},
        "views": [
            {"kind": "front", "view_id": "front", "body_index": 0},
            {"kind": "side", "view_id": "side", "body_index": 0},
        ],
    }
    nodes, edges, assertions = native_feature_graph_additions(spec)

    assert "sheet:0" in _node_ids(nodes, "Sheet")
    assert _node_ids(nodes, "View") == {"view:front", "view:side"}
    contains = _edges_of_type(edges, "contains")
    assert {"document-set:root", "sheet:0"} <= {e.source_id for e in contains} | {
        e.target_id for e in contains
    }
    assert assertions == []  # no id-tagged features anywhere on the sheet


def test_no_views_declared_means_no_view_nodes():
    """Empty views[] = front view only, per the reader's own contract — this
    builder must not invent a view the spec never declared."""
    nodes, edges, assertions = native_feature_graph_additions({"main_view": {}})

    assert _node_ids(nodes, "View") == set()
    assert _node_ids(nodes, "Sheet") == {"sheet:0"}


def test_element_without_a_stable_id_produces_no_feature_node():
    spec = {"main_view": {"chamfers": [{"size_mm": 1, "angle_deg": 45, "location": "left_end"}]}}

    nodes, edges, assertions = native_feature_graph_additions(spec)

    assert _node_ids(nodes, "Feature") == set()
    assert assertions == []


def test_id_tagged_feature_gets_a_node_with_kind_location_and_params():
    spec = {
        "main_view": {
            "chamfers": [
                {
                    "id": "0:chamfers:0",
                    "size_mm": 1,
                    "angle_deg": 45,
                    "location": "left_end",
                }
            ]
        }
    }

    nodes, edges, assertions = native_feature_graph_additions(spec)

    assert "feature:0:chamfers:0" in _node_ids(nodes, "Feature")
    by_predicate = {a.predicate: a for a in assertions}
    assert by_predicate["feature.kind"].value.value == "chamfer"
    assert by_predicate["feature.location"].value.value == "left_end"
    assert by_predicate["feature.param.size_mm"].value.value == 1
    assert by_predicate["feature.param.angle_deg"].value.value == 45
    assert by_predicate["feature.param.angle_deg"].unit == "deg"
    assert by_predicate["feature.param.size_mm"].unit == "mm"


def test_confidence_reflects_whether_the_feature_was_localized():
    spec = {
        "main_view": {
            "cross_holes": [
                {
                    "id": "0:cross_holes:0",
                    "diameter_mm": 9,
                    "axial_position_mm": 10,
                    "evidence": [{"image_index": 0, "bbox": [1, 2, 3, 4]}],
                },
                {"id": "0:cross_holes:1", "diameter_mm": 5, "axial_position_mm": 40},
            ]
        }
    }

    _, _, assertions = native_feature_graph_additions(spec)
    by_subject = {}
    for a in assertions:
        if a.predicate == "feature.kind":
            by_subject[a.subject_id] = a.confidence

    assert by_subject["feature:0:cross_holes:0"] > by_subject["feature:0:cross_holes:1"]


def test_represented_by_only_from_explicit_features_shown():
    spec = {
        "main_view": {
            "cross_holes": [
                {"id": "0:cross_holes:0", "diameter_mm": 9, "axial_position_mm": 10},
            ]
        },
        "views": [
            {
                "kind": "front",
                "view_id": "front",
                "body_index": 0,
                "features_shown": ["0:cross_holes:0"],
            },
            {"kind": "side", "view_id": "side", "body_index": 0, "features_shown": []},
        ],
    }

    _, edges, _ = native_feature_graph_additions(spec)
    shown = _edges_of_type(edges, "represented_by")

    assert len(shown) == 1
    assert shown[0].source_id == "feature:0:cross_holes:0"
    assert shown[0].target_id == "view:front"


def test_evidence_ids_carry_the_whole_sheet_reference_when_source_uri_given():
    spec = {
        "main_view": {
            "chamfers": [
                {"id": "0:chamfers:0", "size_mm": 1, "angle_deg": 45, "location": "left_end"},
            ]
        }
    }

    with_source = native_feature_graph_additions(spec, source_uri="s3://bucket/sheet.png")[2]
    without_source = native_feature_graph_additions(spec)[2]

    assert all(a.evidence_ids == ["evidence:whole-sheet"] for a in with_source)
    assert all(a.evidence_ids == [] for a in without_source)


def test_sections_and_profile_holes_are_also_covered():
    spec = {
        "parts": [
            {
                "outer": [{"id": "1:outer:0", "diameter_mm": 20, "length_mm": 30}],
                "profile": {
                    "shape": "rectangle",
                    "width_mm": 50,
                    "height_mm": 30,
                    "thickness_mm": 10,
                    "holes": [
                        {
                            "id": "1:profile.holes:0",
                            "center_x_mm": 5,
                            "center_y_mm": 5,
                            "diameter_mm": 6,
                        }
                    ],
                },
            }
        ],
    }

    nodes, _, assertions = native_feature_graph_additions(spec)

    feature_ids = _node_ids(nodes, "Feature")
    assert "feature:1:outer:0" in feature_ids
    assert "feature:1:profile.holes:0" in feature_ids
    kinds = {a.subject_id: a.value.value for a in assertions if a.predicate == "feature.kind"}
    assert kinds["feature:1:outer:0"] == "section_outer"
    assert kinds["feature:1:profile.holes:0"] == "hole"


def test_duplicate_view_id_is_only_emitted_once():
    spec = {
        "main_view": {},
        "views": [
            {"kind": "front", "view_id": "front", "body_index": 0},
            {"kind": "front", "view_id": "front", "body_index": 0},
        ],
    }

    nodes, edges, _ = native_feature_graph_additions(spec)

    assert list(_node_ids(nodes, "View")) == ["view:front"]
    assert len(_edges_of_type(edges, "contains")) == 2  # root→sheet, sheet→front only


def test_feature_spec_path_decodes_main_view_list_item():
    assert feature_spec_path("0:chamfers:0") == "main_view.chamfers[0]"


def test_feature_spec_path_decodes_part_index_offset_by_one():
    # body_index 1 = parts[0], 2 = parts[1] — main_view is always body_index 0.
    assert feature_spec_path("1:outer:0") == "parts[0].outer[0]"
    assert feature_spec_path("2:bore:3") == "parts[1].bore[3]"


def test_feature_spec_path_decodes_nested_profile_list():
    assert feature_spec_path("1:profile.holes:0") == "parts[0].profile.holes[0]"


def test_feature_spec_path_returns_none_for_ids_it_did_not_assign():
    assert feature_spec_path("chamfer-left") is None  # no assign_stable_feature_ids shape
    assert feature_spec_path("0:chamfers") is None  # missing index segment
    assert feature_spec_path("x:chamfers:0") is None  # non-numeric body_index
    assert feature_spec_path("0:chamfers:y") is None  # non-numeric index
