"""From a fully read sheet to the operations that build the part.

The contract can now carry a taper, a groove, a keyway, a cross-drilling and a
chamfer. This is the other half: each of them has to arrive at the kernel as an
operation, with the sheet's own numbers, or the reading was for nothing.

Nothing here talks to the kernel — see scripts/cad_kernel_smoke.py for that.
These tests check the translation.
"""

from __future__ import annotations

from app.ai.cad_recognize.spec_vectorize import taper_end_diameter
from app.ai.cad_solid import feature_tree_from_spec

_SHAFT = {
    "part": "Шпиндель",
    "main_view": {
        "type": "тело вращения (вал)",
        "outer": [
            {"diameter_mm": 80.0, "length_mm": 150.0},
            {"diameter_mm": 102.0, "length_mm": 200.0},
            {"diameter_mm": 60.0, "length_mm": 120.0},
        ],
    },
}


def _tree(**body_extras):
    spec = {
        "part": "Шпиндель",
        "main_view": {**_SHAFT["main_view"], **body_extras},
    }
    candidate = feature_tree_from_spec(spec)
    assert candidate is not None
    return candidate


def _of_kind(candidate, kind):
    return [feature for feature in candidate.features if feature.kind == kind]


def test_a_taper_is_resolved_the_same_way_however_the_sheet_states_it():
    """7:24, an included angle and a far diameter all describe one generatrix."""
    ratio = {"d": 100.0, "l": 48.0, "taper": {"kind": "ratio", "ratio": "7:24"}}
    assert taper_end_diameter(ratio) == 86.0  # 7/24 * 48 = 14 mm of diameter

    end = {"d": 100.0, "l": 48.0, "taper": {"kind": "end_diameter", "end_diameter_mm": 86.0}}
    assert taper_end_diameter(end) == 86.0

    plain = {"d": 100.0, "l": 48.0}
    assert taper_end_diameter(plain) is None

    # A cone whose length is unknown has no computable end — and is not guessed.
    unknown = {"d": 100.0, "l": None, "taper": {"kind": "ratio", "ratio": "7:24"}}
    assert taper_end_diameter(unknown) is None


def test_a_conical_step_reaches_the_solid_as_a_cone():
    """Built as a cylinder, a 7:24 spindle nose is a part that fits nothing."""
    candidate = feature_tree_from_spec({
        "main_view": {
            "type": "тело вращения (вал)",
            "outer": [
                {"diameter_mm": 100.0, "length_mm": 48.0,
                 "taper": {"kind": "ratio", "ratio": "7:24"}},
                {"diameter_mm": 80.0, "length_mm": 200.0},
            ],
        },
    })
    assert candidate is not None
    points = candidate.features[0].params["profile_points"]
    # Enter at r=50, leave at r=43 — the cone, not a step.
    assert points[0] == {"r": 50.0, "z": 0.0}
    assert points[1] == {"r": 43.0, "z": 48.0}


def test_a_groove_becomes_a_groove_operation():
    candidate = _tree(grooves=[
        {"kind": "relief", "axial_position_mm": 250.0, "width_mm": 6.0, "depth_mm": 3.0}
    ])
    grooves = _of_kind(candidate, "groove")
    assert len(grooves) == 1
    assert grooves[0].params == {
        "axial_position_mm": 250.0, "width_mm": 6.0, "internal": False, "depth_mm": 3.0
    }


def test_a_groove_may_state_its_root_instead_of_its_depth():
    candidate = _tree(grooves=[
        {"axial_position_mm": 250.0, "width_mm": 6.0, "root_diameter_mm": 96.0}
    ])
    assert _of_kind(candidate, "groove")[0].params["root_diameter_mm"] == 96.0


def test_a_keyway_carries_the_sheets_own_numbers():
    candidate = _tree(keyways=[{
        "axial_start_mm": 40.0, "length_mm": 85.0, "width_mm": 12.0,
        "depth_mm": 5.0, "angle_deg": 90.0, "end_type": "closed",
    }])
    keyway = _of_kind(candidate, "keyway")[0]
    assert keyway.params["length_mm"] == 85.0
    assert keyway.params["angle_deg"] == 90.0
    assert keyway.param_provenance["depth_mm"].origin == "stated"


def test_a_ring_of_cross_holes_is_expanded_around_the_axis():
    """Four oil ways at 90° apart are four operations, not one with a count."""
    candidate = _tree(cross_holes=[
        {"diameter_mm": 9.0, "axial_position_mm": 200.0, "count": 4, "through": True}
    ])
    holes = _of_kind(candidate, "hole")
    assert len(holes) == 4
    assert [hole.params["angle_deg"] for hole in holes] == [0.0, 90.0, 180.0, 270.0]
    assert all(hole.params["axis"] == "radial" for hole in holes)


def test_radial_counterbore_compiles_as_a_second_shallow_cut():
    candidate = _tree(cross_holes=[{
        "diameter_mm": 10.0,
        "axial_position_mm": 200.0,
        "angle_deg": 0.0,
        "through": False,
        "depth_mm": 8.5,
        "counterbore_diameter_mm": 24.0,
        "counterbore_depth_mm": 3.0,
    }])

    holes = _of_kind(candidate, "hole")
    assert [(item.params["diameter_mm"], item.params["depth_mm"]) for item in holes] == [
        (10.0, 8.5),
        (24.0, 3.0),
    ]


def test_a_chamfer_points_at_the_end_face_it_names():
    candidate = _tree(chamfers=[{"size_mm": 1.0, "angle_deg": 45.0, "location": "left_end"}])
    chamfer = _of_kind(candidate, "chamfer")[0]
    assert chamfer.params["edge_selector"] == {
        "curve": "Circle", "at_z_mm": 0.0, "diameter_mm": 80.0
    }
    assert chamfer.params["size_mm"] == 1.0


def test_internal_bore_thread_reaches_the_feature_tree_with_axial_location():
    candidate = _tree(bore=[
        {"diameter_mm": 50, "length_mm": 445},
        {
            "diameter_mm": 55,
            "length_mm": 25,
            "thread": {
                "designation": "M54,5x2",
                "nominal_diameter_mm": 54.5,
                "pitch_mm": 2,
                "internal": True,
            },
        },
    ])

    thread = _of_kind(candidate, "thread")[0]
    assert thread.params == {
        "spec": "M54,5x2",
        "diameter_mm": 54.5,
        "axial_start_mm": 445.0,
        "length_mm": 25.0,
        "internal": True,
        "pitch_mm": 2.0,
    }


def test_a_chamfer_on_a_shoulder_is_placed_by_the_diameter_it_names():
    """"1x45° at the Ø80 shoulder" is a place; the kernel needs an edge."""
    candidate = _tree(chamfers=[
        {"size_mm": 2.0, "location": "shoulder", "at_diameter_mm": 80.0}
    ])
    selector = _of_kind(candidate, "chamfer")[0].params["edge_selector"]
    # The Ø80 step ends at z=150 — that is where its shoulder is.
    assert selector == {"curve": "Circle", "at_z_mm": 150.0, "diameter_mm": 80.0}


def test_an_unplaceable_chamfer_is_declared_not_invented():
    """A chamfer on the wrong shoulder is a part that looks right and is not."""
    candidate = _tree(chamfers=[{"size_mm": 1.0, "location": "shoulder"}])
    assert not _of_kind(candidate, "chamfer")
    assert any("не удалось определить ребро" in item for item in candidate.missing_data)


def test_a_feature_read_without_its_size_is_declared_too():
    candidate = _tree(grooves=[{"axial_position_mm": 250.0, "width_mm": 6.0}])
    assert not _of_kind(candidate, "groove")
    assert any("глубины" in item for item in candidate.missing_data)


def test_a_plain_shaft_still_builds_exactly_as_before():
    """None of this may change a part that has none of these features."""
    candidate = feature_tree_from_spec(_SHAFT)
    assert candidate is not None
    assert [feature.kind for feature in candidate.features] == ["revolve"]


# Ф2.6c: source_feature_ids — which native EMG Feature node(s) (Ф1.2, ids
# from assign_stable_feature_ids) each compiled operation was built from,
# so spec_feature_tree_as_graph can draw a realizes edge back to it.

def test_a_plain_shaft_has_no_source_feature_ids_when_nothing_is_id_tagged():
    """The existing fixtures never carry ids — must stay empty, not guessed."""
    candidate = feature_tree_from_spec(_SHAFT)
    assert candidate.features[0].source_feature_ids == []


def test_base_revolve_source_feature_ids_cover_outer_and_bore_sections():
    candidate = feature_tree_from_spec({
        "main_view": {
            "type": "тело вращения (вал)",
            "outer": [
                {"id": "0:outer:0", "diameter_mm": 80.0, "length_mm": 150.0},
                {"id": "0:outer:1", "diameter_mm": 60.0, "length_mm": 120.0},
            ],
            "bore": [{"id": "0:bore:0", "diameter_mm": 30.0, "length_mm": 270.0}],
        },
    })
    revolve = _of_kind(candidate, "revolve")[0]
    assert revolve.source_feature_ids == ["0:outer:0", "0:outer:1", "0:bore:0"]


def test_groove_keyway_and_chamfer_each_carry_their_own_source_id():
    candidate = _tree(
        grooves=[{
            "id": "0:grooves:0", "axial_position_mm": 250.0,
            "width_mm": 6.0, "depth_mm": 3.0,
        }],
        keyways=[{
            "id": "0:keyways:0", "axial_start_mm": 40.0, "length_mm": 85.0,
            "width_mm": 12.0, "depth_mm": 5.0,
        }],
        chamfers=[{
            "id": "0:chamfers:0", "size_mm": 1.0, "location": "left_end",
        }],
    )
    assert _of_kind(candidate, "groove")[0].source_feature_ids == ["0:grooves:0"]
    assert _of_kind(candidate, "keyway")[0].source_feature_ids == ["0:keyways:0"]
    assert _of_kind(candidate, "chamfer")[0].source_feature_ids == ["0:chamfers:0"]


def test_cross_hole_ring_and_its_counterbore_share_one_source_id():
    """Four expanded holes plus a counterbore are all ONE read cross_holes[]
    item — every compiled operation must point back to that same id, not a
    synthetic per-instance one that does not exist as a Feature node."""
    candidate = _tree(cross_holes=[{
        "id": "0:cross_holes:0",
        "diameter_mm": 10.0, "axial_position_mm": 200.0, "count": 4,
        "through": False, "depth_mm": 8.5,
        "counterbore_diameter_mm": 24.0, "counterbore_depth_mm": 3.0,
    }])
    holes = _of_kind(candidate, "hole")
    assert len(holes) == 8  # 4 main + 4 counterbore
    assert all(hole.source_feature_ids == ["0:cross_holes:0"] for hole in holes)
