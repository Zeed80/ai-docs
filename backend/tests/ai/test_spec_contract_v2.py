"""What the sheet says, that the contract could not carry until now.

The hand-checked reference spec for the spindle lists exactly what the old
contract had to throw away: the 7:24 taper of the nose, M75x1,5 and M54,5x2,
both keyways, the cross-drillings and "6 chamfers 1x45°". A part described
without them is not the part on the sheet — it is a smooth stand-in with the
same overall size, and nothing said so.

These features never change the stepped silhouette, which is why they live
beside outer[] rather than in it: a chamfer annotates an edge, a groove cuts
into a surface, a thread labels one. The validators here answer one question —
does the feature fit inside the material it is cut from?
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.ai.cad_recognize.spec_vectorize import (
    EngineeringDrawingSpec,
    SpecBody,
    SpecChamfer,
    SpecCrossHole,
    SpecGroove,
    SpecKeyway,
    SpecTaper,
    SpecThread,
)

_SHAFT = {
    "type": "тело вращения (вал)",
    "outer": [
        {"diameter_mm": 80.0, "length_mm": 150.0},
        {"diameter_mm": 102.0, "length_mm": 200.0},
        {"diameter_mm": 60.0, "length_mm": 120.0},
    ],
}


def test_a_spindle_nose_states_its_taper_one_way_only():
    assert SpecTaper(kind="ratio", ratio="7:24").ratio == "7:24"
    assert SpecTaper(kind="end_diameter", end_diameter_mm=54.5).end_diameter_mm == 54.5

    with pytest.raises(ValidationError):  # two statements can disagree
        SpecTaper(kind="ratio", ratio="7:24", end_diameter_mm=54.5)
    with pytest.raises(ValidationError):  # and none says nothing
        SpecTaper(kind="ratio")


def test_a_thread_annotates_the_step_it_sits_on():
    thread = SpecThread(designation="M75x1,5", nominal_diameter_mm=75.0, pitch_mm=1.5)
    assert thread.system == "metric" and thread.hand == "right"
    assert thread.internal is False


def test_cross_hole_counterbore_requires_a_complete_larger_step():
    hole = SpecCrossHole(
        diameter_mm=10,
        axial_position_mm=40,
        through=False,
        depth_mm=8.5,
        counterbore_diameter_mm=24,
        counterbore_depth_mm=3,
    )
    assert hole.counterbore_diameter_mm == 24

    with pytest.raises(ValidationError):
        SpecCrossHole(
            diameter_mm=10,
            axial_position_mm=40,
            counterbore_diameter_mm=24,
        )


def test_a_groove_states_its_depth_or_its_root_never_both():
    assert SpecGroove(axial_position_mm=40.0, width_mm=3.0, depth_mm=2.0)
    assert SpecGroove(axial_position_mm=40.0, width_mm=3.0, root_diameter_mm=76.0)

    with pytest.raises(ValidationError):
        SpecGroove(axial_position_mm=40.0, width_mm=3.0, depth_mm=2.0, root_diameter_mm=76.0)
    with pytest.raises(ValidationError):
        SpecGroove(axial_position_mm=40.0, width_mm=3.0)


def test_features_must_fit_inside_the_part():
    """A misread that puts a keyway past the end of the shaft is caught here.

    The kernel would either fail obscurely or — worse — succeed, and hand back
    a part that is not the one on the sheet.
    """
    body = SpecBody.model_validate(
        {
            **_SHAFT,
            "keyways": [
                {"axial_start_mm": 20.0, "length_mm": 85.0, "width_mm": 12.0, "depth_mm": 6.0}
            ],
        }
    )
    assert body.keyways[0].length_mm == 85.0

    with pytest.raises(ValidationError):  # runs past the 470 mm part
        SpecBody.model_validate(
            {
                **_SHAFT,
                "keyways": [
                    {"axial_start_mm": 450.0, "length_mm": 85.0, "width_mm": 12.0, "depth_mm": 6.0}
                ],
            }
        )
    with pytest.raises(ValidationError):  # deeper than the shaft's radius
        SpecBody.model_validate(
            {
                **_SHAFT,
                "keyways": [
                    {"axial_start_mm": 20.0, "length_mm": 85.0, "width_mm": 12.0, "depth_mm": 60.0}
                ],
            }
        )
    with pytest.raises(ValidationError):  # a groove off the end of the part
        SpecBody.model_validate(
            {**_SHAFT, "grooves": [{"axial_position_mm": 900.0, "width_mm": 3.0, "depth_mm": 2.0}]}
        )
    with pytest.raises(ValidationError):  # a cross hole nowhere near the part
        SpecBody.model_validate(
            {**_SHAFT, "cross_holes": [{"diameter_mm": 14.0, "axial_position_mm": -5.0}]}
        )


def test_a_keyway_is_a_slot_not_a_point():
    with pytest.raises(ValidationError):
        SpecKeyway(axial_start_mm=10.0, length_mm=5.0, width_mm=12.0, depth_mm=6.0)


def test_a_bore_can_be_blind_and_start_away_from_the_face():
    """Every bore used to be a through hole from the left face — the only
    thing the contract could say, and a different part when it was wrong."""
    body = SpecBody.model_validate(
        {
            **_SHAFT,
            "bore": [{"diameter_mm": 40.0, "length_mm": 100.0}],
            "bore_start_mm": 30.0,
            "bore_from_end": "right",
            "bore_blind": True,
        }
    )
    assert body.bore_start_mm == 30.0
    assert body.bore_from_end == "right"
    assert body.bore_blind is True


def test_a_chamfer_names_a_place_not_an_edge_id():
    """The reader cannot know an edge key — that exists only after the solid
    is built. It states WHERE, and the kernel resolves the edge."""
    chamfer = SpecChamfer(size_mm=1.0, location="shoulder", at_diameter_mm=80.0)
    assert chamfer.angle_deg == 45.0
    assert chamfer.at_diameter_mm == 80.0


def test_a_spec_written_before_any_of_this_still_validates():
    """Every stored spec predates these fields; none of them may break."""
    spec = EngineeringDrawingSpec.model_validate(
        {
            "schema_version": 1,
            "part": "Вал",
            "main_view": _SHAFT,
        }
    )
    assert spec.main_view.chamfers == []
    assert spec.main_view.bore_start_mm == 0.0
    assert spec.unresolved == []


def test_the_spindle_can_finally_be_described_in_full():
    """The features the reference spec had to list in prose, as data."""
    spec = EngineeringDrawingSpec.model_validate(
        {
            "part": "Шпиндель",
            "main_view": {
                **_SHAFT,
                "outer": [
                    {
                        "diameter_mm": 102.0,
                        "length_mm": 150.0,
                        "taper": {"kind": "ratio", "ratio": "7:24"},
                    },
                    {"diameter_mm": 80.0, "length_mm": 200.0, "tolerance": "js6"},
                    {
                        "diameter_mm": 75.0,
                        "length_mm": 120.0,
                        "thread": {
                            "designation": "M75x1,5",
                            "nominal_diameter_mm": 75.0,
                            "pitch_mm": 1.5,
                        },
                    },
                ],
                "keyways": [
                    {
                        "axial_start_mm": 40.0,
                        "length_mm": 85.0,
                        "width_mm": 12.0,
                        "depth_mm": 5.0,
                        "standard_ref": "ГОСТ 23360",
                    },
                ],
                "grooves": [
                    {
                        "kind": "thread_runout",
                        "axial_position_mm": 400.0,
                        "width_mm": 3.0,
                        "depth_mm": 1.5,
                    },
                ],
                "cross_holes": [
                    {"diameter_mm": 14.0, "axial_position_mm": 200.0, "through": True},
                ],
                "chamfers": [
                    {"size_mm": 1.0, "angle_deg": 45.0, "location": "left_end"},
                    {"size_mm": 1.0, "angle_deg": 45.0, "location": "right_end"},
                ],
            },
        }
    )
    body = spec.main_view
    assert body.outer[0].taper.ratio == "7:24"
    assert body.outer[2].thread.designation == "M75x1,5"
    assert len(body.keyways) == 1 and len(body.chamfers) == 2
    assert body.cross_holes[0].through is True
    # None of it is unresolved: these are read facts, not missing ones.
    assert spec.unresolved == []
