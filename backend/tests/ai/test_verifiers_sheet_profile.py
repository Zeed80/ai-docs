"""Профиль вала по листу: уступы вида + надписи, которые выписал ридер."""

from __future__ import annotations

import numpy as np

from app.ai.cad_recognize.verifiers.reconcile import apply_profile, profile_decision
from app.ai.cad_recognize.verifiers.shaft_frame import ShaftProfile
from app.ai.cad_recognize.verifiers.sheet_profile import propose_profile, sheet_labels

PX = 10.0  # px/мм


def _profile(segments, *, line_px: float = 6.0) -> ShaftProfile:
    """``segments``: ``(начало мм, конец мм, Ø мм)`` подряд от левого торца."""
    x0 = 100
    end_px = int(round(segments[-1][1] * PX))
    half = np.full(end_px + 1, np.nan)
    for start, end, diameter in segments:
        half[int(round(start * PX)) : int(round(end * PX)) + 1] = diameter / 2.0 * PX
    return ShaftProfile(
        x0=x0, x1=x0 + end_px, axis_y=500.0, half_px=half, faces_px=(), line_px=line_px
    )


def _dims(*texts: str) -> dict:
    return {"dimensions": [{"value": text} for text in texts]}


# z4-r4: лист нарисован не точно в масштабе (уступы до 1,5 мм от надписей,
# Ø на 1,5 % шире — анизотропия выпрямленного фото), размеры — от торцов,
# M18 нарисована Ø16, между Ø25 и Ø35 — канавка.
Z4_DRAWN = [
    (0.0, 15.2, 16.0 * 1.015),
    (15.2, 31.9, 25.0 * 1.015),
    (31.9, 34.5, 23.4 * 1.015),  # канавка
    (34.5, 66.0, 35.0 * 1.015),
    (66.0, 93.8, 30.0 * 1.015),
    (93.8, 115.1, 25.0 * 1.015),
    (115.1, 131.5, 24.0 * 1.015),
    (131.5, 185.0, 22.0 * 1.015),
]
Z4_LABELS = _dims(
    "185", "22", "2", "1,5", "25", "10", "M18×15-6g", "Ø25", "15", "34", "Ø35", "Ø30",
    "Ø25", "M24×1,5-6g", "Ø22", "52", "70", "90", "120", "3,5", "Ø24,5", "Ø29,5", "4",
    "Ø21,7", "Ø15,7", "8",
)  # fmt: skip
Z4_TRUTH = [(18.0, 15.0), (25.0, 19.0), (35.0, 31.0), (30.0, 30.0), (25.0, 20.0), (24.0, 18.0), (22.0, 52.0)]  # fmt: skip


def _steps(proposal):
    return [(step["diameter_mm"], step["length_mm"]) for step in proposal.steps]


def test_labels_are_split_by_meaning_and_a_lost_pitch_comma_is_dropped():
    labels = sheet_labels(
        {
            "dimensions": [
                {"value": "185", "bbox": [10, 0, 30, 5]},
                {"value": "15"},
                {"value": "15"},
                {"value": "Ø80js6"},
                {"value": "M18×15-6g"},
                {"value": "M24×1,5-6g"},
                {"value": "Длина ступени: 15"},
            ]
        }
    )

    assert labels.axial == ((15.0, None), (15.0, None), (185.0, 20.0))
    assert labels.diameters == (80.0,)
    assert labels.threads == ((18.0, None), (24.0, 1.5))


def test_an_off_scale_sheet_gives_the_labelled_profile_with_its_threads():
    proposal, why = propose_profile(
        _profile(Z4_DRAWN), sheet_labels(Z4_LABELS), 192.0, reserved=(22.0, 4.0)
    )

    assert proposal is not None, why
    assert _steps(proposal) == Z4_TRUTH
    threads = [(step.get("thread") or {}).get("designation") for step in proposal.steps]
    assert threads == ["M18", None, None, None, None, "M24x1.5", None]
    assert proposal.total_mm == 185.0
    assert 1.0 < proposal.station_error_mm < 2.0


def test_a_label_the_keyway_took_is_not_free_to_explain_a_shoulder():
    # Без «22» паза: 93 = 115 − 22 почти так же близко к уступу 93,8, как
    # 95 = 185 − 90 — неоднозначно, и это отказ, а не догадка.
    proposal, why = propose_profile(_profile(Z4_DRAWN), sheet_labels(Z4_LABELS), 192.0)

    assert proposal is None
    assert "почти одинаково" in why


def test_a_chain_with_an_open_link_is_assembled_exactly():
    truth = [(25.0, 12.0), (28.0, 30.0), (25.0, 35.0), (40.0, 80.0), (30.0, 15.0), (35.0, 80.0), (22.0, 15.0)]  # fmt: skip
    segments, station = [], 0.0
    for diameter, length in truth:
        segments.append((station, station + length, diameter))
        station += length
    labels = _dims(
        "12", "30", "35", "80", "15", "15", "267", "Ø40", "Ø35", "Ø25", "Ø28", "Ø22",
        "Ø30", "19", "4.3", "53", "Ø4",
    )  # fmt: skip

    proposal, why = propose_profile(_profile(segments), sheet_labels(labels), 275.0)

    assert proposal is not None, why
    assert _steps(proposal) == truth


def test_a_coarse_sheet_gives_no_profile():
    proposal, why = propose_profile(
        _profile(Z4_DRAWN, line_px=3.0), sheet_labels(Z4_LABELS), 192.0, reserved=(22.0, 4.0)
    )

    assert proposal is None
    assert "грубый" in why


def test_a_shoulder_without_a_label_gives_no_profile():
    labels = _dims("185", "15", "34", "120", "90", "Ø25", "Ø35", "Ø30", "Ø22", "Ø24", "Ø18")

    proposal, why = propose_profile(_profile(Z4_DRAWN), sheet_labels(labels), 192.0)

    assert proposal is None
    assert "не объясня" in why


def _report(statuses, proposal_steps):
    return {
        "items": [
            {"kind": "shaft_step", "path": f"main_view.outer[{i}]", "status": status}
            for i, status in enumerate(statuses)
        ],
        "profile_proposal": {
            "steps": proposal_steps,
            "total_mm": 185.0,
            "station_error_mm": 1.4,
        },
    }


READ_SPEC = {
    "main_view": {
        "outer": [
            {"id": "0:outer:0", "diameter_mm": 15.7, "length_mm": 15.0},
            {"id": "0:outer:1", "diameter_mm": 25.0, "length_mm": 19.0},
            {"id": "0:outer:2", "diameter_mm": 35.0, "length_mm": 40.0},
        ],
        "keyways": [{"axial_start_mm": 50.0, "length_mm": 22.0, "on_section_id": "0:outer:2"}],
    },
    "value_provenance": {"main_view/outer/0/diameter_mm": {"votes": 5}, "part": {"votes": 5}},
    "unresolved": [
        "малые элементы: резьбы указаны, но не привязаны к участкам: M18x1,5: несущий участок не локализован",
        "малые элементы: поперечное отверстие Ø25 указано, но не локализовано",
        "малые элементы: поперечное отверстие Ø0.8 указано, но не локализовано",
    ],
}
PROPOSED = [
    {
        "diameter_mm": 18.0,
        "length_mm": 15.0,
        "thread": {"designation": "M18", "nominal_diameter_mm": 18.0, "internal": False},
    },
    {"diameter_mm": 25.0, "length_mm": 19.0},
    {"diameter_mm": 35.0, "length_mm": 31.0},
    {"diameter_mm": 30.0, "length_mm": 30.0},
]


def test_a_sheet_profile_replaces_a_reading_the_sheet_did_not_confirm():
    decision = profile_decision(READ_SPEC, _report(["unmeasurable"] * 3, PROPOSED))

    assert decision is not None and decision["action"] == "adopt"
    assert "M18×15 · Ø25×19 · Ø35×31 · Ø30×30" in decision["reason"]
    spec = apply_profile(READ_SPEC, decision)
    outer = spec["main_view"]["outer"]
    assert [(s["diameter_mm"], s["length_mm"]) for s in outer] == [
        (18.0, 15.0),
        (25.0, 19.0),
        (35.0, 31.0),
        (30.0, 30.0),
    ]
    assert outer[0]["thread"]["designation"] == "M18" and outer[0]["thread"]["length_mm"] == 15.0
    # Паз с 50 мм — на ступени Ø35 нового профиля (34…65).
    assert spec["main_view"]["keyways"][0]["on_section_id"] == "0:outer:2"
    assert spec["provenance"]["main_view.outer[3].length_mm"]["origin"] == "sheet_measurement"
    assert "main_view/outer/0/diameter_mm" not in spec["value_provenance"]
    assert "part" in spec["value_provenance"]
    # Замечания о прежнем профиле сняты; про Ø0,8 — не о профиле, осталось.
    assert spec["unresolved"] == [
        "малые элементы: поперечное отверстие Ø0.8 указано, но не локализовано"
    ]
    # Исходный спек не тронут.
    assert READ_SPEC["main_view"]["outer"][0]["diameter_mm"] == 15.7


def test_a_confirmed_reading_is_not_replaced_and_no_proposal_means_no_decision():
    assert profile_decision(READ_SPEC, _report(["confirmed"] * 3, PROPOSED)) is None
    assert profile_decision(READ_SPEC, _report(["refuted"] * 3, None)) is None
