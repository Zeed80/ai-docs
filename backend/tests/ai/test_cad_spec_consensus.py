"""Consensus reading: keep what the passes agree on, flag what they don't."""

from __future__ import annotations

from app.ai.cad_recognize.spec_consensus import consensus_spec


def _read(outer, **extra) -> dict:
    spec = {
        "part": "Вал",
        "main_view": {"type": "тело вращения (вал)", "outer": outer},
        "title_block": {"material": "Сталь 45"},
    }
    spec["main_view"].update(extra.pop("main_view", {}))
    spec.update(extra)
    return spec


_PROFILE = [
    {"diameter_mm": 30, "length_mm": 40},
    {"diameter_mm": 50, "length_mm": 60},
]


def test_agreeing_passes_keep_the_profile():
    spec = consensus_spec([_read(_PROFILE), _read(_PROFILE), _read(_PROFILE)])
    assert len(spec["main_view"]["outer"]) == 2
    assert spec["unresolved"] == []
    assert spec["consensus"]["usable"] == 3
    diameter = spec["value_provenance"]["main_view/outer/0/diameter_mm"]
    assert diameter["votes"] == 3
    assert diameter["confidence"] == 1.0


def test_agreeing_partial_profile_is_kept_for_audit_but_stays_unresolved():
    partial = [
        {"diameter_mm": 65, "length_mm": None},
        {"diameter_mm": 56.55, "length_mm": None},
    ]
    reads = [
        _read(
            partial,
            unresolved=[
                "body:0:outer:0:length-missing",
                "body:0:outer:1:length-missing",
            ],
        )
        for _ in range(3)
    ]

    spec = consensus_spec(reads)

    assert spec["main_view"]["outer"] == partial
    assert "body:0:outer:0:length-missing" in spec["unresolved"]
    assert not any("не сошлись на профиле" in item for item in spec["unresolved"])


def test_small_reading_noise_still_counts_as_agreement():
    """559.9 and 560.0 off the same sheet are the same dimension."""
    noisy = [
        {"diameter_mm": 30.1, "length_mm": 40},
        {"diameter_mm": 50, "length_mm": 60},
    ]
    spec = consensus_spec([_read(_PROFILE), _read(noisy), _read(_PROFILE)])
    assert spec["main_view"]["outer"]
    assert spec["unresolved"] == []
    provenance = spec["value_provenance"]["main_view/outer/0/diameter_mm"]
    assert provenance["votes"] == 3
    assert [item["value"] for item in provenance["observations"]] == [30, 30.1, 30]


def test_provenance_maps_tile_evidence_to_full_sheet_coordinates():
    profile = [
        {
            "diameter_mm": 30,
            "length_mm": 40,
            "evidence": [{"image_index": 1, "bbox": [10, 20, 110, 70], "raw_text": "Ø30"}],
        }
    ]
    read = _read(
        profile,
        source_images=[
            {
                "image_index": 1,
                "image_width": 1400,
                "image_height": 1400,
                "source_bbox": [1200, 800, 2600, 2200],
            }
        ],
    )
    spec = consensus_spec([read, read, read])
    evidence = spec["value_provenance"]["main_view/outer/0/diameter_mm"]["evidence"][0]
    assert evidence["source_bbox"] == [1210.0, 820.0, 1310.0, 870.0]


def test_a_profile_that_changes_between_passes_is_kept_for_review():
    """Разошедшийся профиль сохраняется помеченным, а не исчезает.

    Раньше здесь стояло ``"outer" not in spec["main_view"]``. Живой прогон
    показал, чего это стоит: два прохода полного чтения листа вернули валидный
    ступенчатый вал и разошлись на ПЕРВОЙ ступени (Ø15 против Ø18) — весь
    профиль был отброшен, а оператор получил «выбран тип „тело вращения“, но
    чтение не подтвердило осевой ступенчатый профиль», то есть обвинение в
    неверно выбранном типе детали вместо расхождения в одном размере.

    Теперь выживает лучше всего подтверждённое чтение, спорная ступень несёт
    ``review_required`` со списком значений, а причина по-прежнему в
    ``unresolved`` — построить из этого геометрию молча нельзя.
    """
    other = [
        {"diameter_mm": 30, "length_mm": 40},
        {"diameter_mm": 80, "length_mm": 60},
    ]
    third = [{"diameter_mm": 30, "length_mm": 40}]
    spec = consensus_spec([_read(_PROFILE), _read(other), _read(third)])

    outer = spec["main_view"]["outer"]
    assert outer, "профиль не должен исчезать целиком из-за одной ступени"
    assert any("не сошлись на профиле" in item for item in spec["unresolved"])
    # Первая ступень совпала у всех проходов — её помечать не за что.
    assert outer[0]["diameter_mm"] == 30
    assert outer[0].get("review_required") is not True
    # Вторая — разошлась, и это видно в самой ступени.
    assert outer[1]["review_required"] is True
    assert sorted(outer[1]["disputed_values"]) == [50.0, 80.0]


def test_more_passes_never_produce_less_than_fewer_passes():
    """Немонотонность, из-за которой «5 проходов» были хуже одного.

    Один пригодный проход проходит целиком и без проверки, а два слегка
    разошедшихся раньше давали ноль — то есть больше свидетельств давали
    строго худший ответ, и увеличение числа проходов снижало шанс успеха при
    впятеро большем времени.
    """
    other = [
        {"diameter_mm": 30, "length_mm": 40},
        {"diameter_mm": 80, "length_mm": 60},
    ]
    one_pass = consensus_spec([_read(_PROFILE)])
    two_passes = consensus_spec([_read(_PROFILE), _read(other)])

    assert len(one_pass["main_view"]["outer"]) == len(_PROFILE)
    assert len(two_passes["main_view"]["outer"]) == len(_PROFILE)


def test_agreement_threshold_scales_with_usable_passes():
    """Порог — строгое большинство пришедших проходов, а не константа 2."""
    from app.ai.cad_recognize.spec_consensus import required_agreement

    assert required_agreement(1) == 1
    assert required_agreement(2) == 2
    assert required_agreement(3) == 2
    assert required_agreement(4) == 3
    # Ровно тот случай, где константа 2 была не большинством, а парой голосов
    # против трёх других.
    assert required_agreement(5) == 3


def test_consensus_never_invents_a_profile_no_pass_described():
    """Никаких химер: результат — список ОДНОГО прохода, взятый целиком.

    Гарантия та же, что и раньше; изменился только способ её проверить. Смешать
    диаметры одного чтения с длинами другого нельзя — поэтому при расхождении
    в длине второй ступени возвращается ровно один из двух прочитанных списков,
    а не третий, которого никто не описывал.
    """
    a = [{"diameter_mm": 30, "length_mm": 40}, {"diameter_mm": 50, "length_mm": 60}]
    b = [{"diameter_mm": 30, "length_mm": 99}, {"diameter_mm": 50, "length_mm": 60}]
    spec = consensus_spec([_read(a), _read(b)])

    outer = spec["main_view"]["outer"]
    lengths = [section["length_mm"] for section in outer]
    assert lengths in ([40, 60], [99, 60]), "собран профиль, которого не читал ни один проход"
    assert outer[0]["review_required"] is True
    assert any("не сошлись на профиле" in item for item in spec["unresolved"])


def test_a_value_only_one_pass_saw_is_not_confirmed():
    reads = [
        _read(_PROFILE, dimensions=[{"value": "Ø50js6"}, {"value": "Ø999"}]),
        _read(_PROFILE, dimensions=[{"value": "Ø50js6"}]),
        _read(_PROFILE, dimensions=[{"value": "Ø50js6"}]),
    ]
    spec = consensus_spec(reads)
    values = [d["value"] for d in spec["dimensions"]]
    assert values == ["Ø50js6"]


def test_multiple_labeled_sections_are_not_collapsed_by_consensus():
    views = [
        {"kind": "section", "view_id": "a", "label": "А-А"},
        {"kind": "section", "view_id": "b", "label": "Б-Б"},
    ]
    spec = consensus_spec(
        [
            _read(_PROFILE, views=views),
            _read(_PROFILE, views=views),
            _read(_PROFILE, views=views),
        ]
    )
    assert [view["view_id"] for view in spec["views"]] == ["a", "b"]


def test_a_disputed_stamp_field_is_optional_not_blocking():
    reads = [
        _read(_PROFILE, title_block={"material": "Сталь 45"}),
        _read(_PROFILE, title_block={"material": "Чугун СЧ20"}),
        _read(_PROFILE, title_block={"material": "Сталь 20"}),
    ]
    spec = consensus_spec(reads)
    assert "material" not in spec["title_block"]
    assert any("material" in item for item in spec["optional_unresolved"])
    assert spec["unresolved"] == []


def test_one_pass_admitting_it_could_not_prove_a_value_is_enough_to_block():
    reads = [
        _read(_PROFILE),
        _read(_PROFILE, unresolved=["габарит не найден"]),
        _read(_PROFILE),
    ]
    spec = consensus_spec(reads)
    assert "габарит не найден" in spec["unresolved"]


def test_a_bore_only_some_passes_saw_becomes_a_review_item():
    reads = [
        _read(_PROFILE, main_view={"bore": [{"diameter_mm": 16, "length_mm": 90}]}),
        _read(_PROFILE),
        _read(_PROFILE),
    ]
    spec = consensus_spec(reads)
    assert "bore" not in spec["main_view"]
    assert any("расточка" in item for item in spec["unresolved"])


def test_agreeing_cut_features_survive_consensus():
    keyway = {
        "kind": "parallel",
        "axial_start_mm": 277,
        "length_mm": 85,
        "width_mm": 12,
        "depth_mm": 5,
    }
    reads = [
        _read(_PROFILE, main_view={"keyways": [keyway]}),
        _read(_PROFILE, main_view={"keyways": [dict(keyway)]}),
        _read(_PROFILE),
    ]

    spec = consensus_spec(reads)

    assert spec["main_view"]["keyways"] == [keyway]


def test_agreeing_cross_hole_boolean_survives_consensus():
    hole = {
        "diameter_mm": 14,
        "axial_position_mm": 410,
        "angle_deg": 0,
        "through": True,
        "count": 1,
    }
    reads = [_read(_PROFILE, main_view={"cross_holes": [dict(hole)]}) for _ in range(3)]

    spec = consensus_spec(reads)

    assert spec["main_view"]["cross_holes"] == [hole]


def test_profile_thread_needs_majority_even_when_section_geometry_agrees():
    threaded = {
        "diameter_mm": 55,
        "length_mm": 25,
        "thread": {
            "designation": "M54,5x2",
            "nominal_diameter_mm": 54.5,
            "pitch_mm": 2,
            "internal": True,
        },
    }
    plain = {"diameter_mm": 55, "length_mm": 25}
    reads = [
        _read(_PROFILE, main_view={"bore": [dict(threaded)]}),
        _read(_PROFILE, main_view={"bore": [dict(threaded)]}),
        _read(_PROFILE, main_view={"bore": [dict(plain)]}),
    ]

    spec = consensus_spec(reads)

    assert spec["main_view"]["bore"][0]["thread"]["designation"] == "M54,5x2"
    assert spec["unresolved"] == []


def test_disputed_cut_feature_is_not_silently_dropped():
    reads = [
        _read(
            _PROFILE,
            main_view={
                "cross_holes": [
                    {
                        "diameter_mm": 9,
                        "axial_position_mm": 455,
                        "count": 1,
                    }
                ]
            },
        ),
        _read(_PROFILE),
        _read(_PROFILE),
    ]

    spec = consensus_spec(reads)

    assert "cross_holes" not in spec["main_view"]
    assert any("cross_holes" in item for item in spec["unresolved"])


def test_a_flange_profile_needs_more_than_one_pass_to_survive():
    """Live: one pass returned a complete circle profile, another returned none."""
    flange = {"shape": "circle", "diameter_mm": 560, "thickness_mm": 20, "holes": []}
    seen = _read([], main_view={"profile": flange})
    unseen = _read([])
    spec = consensus_spec([seen, unseen, unseen])
    assert "profile" not in spec["main_view"]
    assert any("контур" in item for item in spec["unresolved"])

    twice = consensus_spec([seen, seen, unseen])
    assert twice["main_view"]["profile"]["diameter_mm"] == 560


def test_a_single_usable_pass_is_passed_through_unchanged():
    spec = consensus_spec([_read(_PROFILE)])
    assert spec["main_view"]["outer"] == _PROFILE
    assert spec["consensus"]["agreement"] == "single_pass"


def test_no_usable_reads_yield_nothing():
    assert consensus_spec([]) == {}
    assert consensus_spec([{}, {}]) == {}


# ── Долги D5/D6: консенсус не терял и не пропускал непроверенное ───────────


def _plate(holes, **profile) -> dict:
    return {
        "part": "Плита",
        "main_view": {
            "type": "плита",
            "profile": {
                "shape": profile.pop("shape", "rectangle"),
                "width_mm": 120,
                "height_mm": 80,
                "thickness_mm": 10,
                "holes": holes,
                **profile,
            },
        },
    }


_HOLE_A = {"center_x_mm": -40, "center_y_mm": 20, "diameter_mm": 9}
_HOLE_B = {"center_x_mm": 40, "center_y_mm": 20, "diameter_mm": 9}
_GHOST = {"center_x_mm": 0, "center_y_mm": -30, "diameter_mm": 14}


def test_a_hole_only_one_pass_imagined_is_not_built_but_is_reported():
    """Отверстия копировались из «самого богатого» прохода без проверки."""
    merged = consensus_spec(
        [
            _plate([_HOLE_A, _HOLE_B, _GHOST]),
            _plate([_HOLE_A, _HOLE_B]),
            _plate([{**_HOLE_B, "id": "hole-7"}, _HOLE_A]),
        ]
    )

    holes = merged["main_view"]["profile"]["holes"]
    assert len(holes) == 2
    assert all(hole["diameter_mm"] == 9 for hole in holes)
    assert any("holes" in note and "меньшинством" in note for note in merged["optional_unresolved"])
    assert not merged["unresolved"]


def test_holes_the_passes_never_agree_on_block_the_profile():
    merged = consensus_spec([_plate([_HOLE_A]), _plate([_HOLE_B]), _plate([_GHOST])])

    assert "profile" not in merged["main_view"]
    assert any("holes" in item for item in merged["unresolved"])


def test_the_corner_radius_and_the_sketch_survive_consensus():
    square = [
        {"kind": "line", "to": [50.0, 0.0]},
        {"kind": "line", "to": [50.0, 30.0]},
        {"kind": "line", "to": [0.0, 30.0]},
        {"kind": "line", "to": [0.0, 0.0]},
    ]
    rounded = consensus_spec([_plate([], corner_radius_mm=6), _plate([], corner_radius_mm=6)])
    sketched = consensus_spec(
        [_plate([], shape="sketch", sketch=square), _plate([], shape="sketch", sketch=square)]
    )

    assert rounded["main_view"]["profile"]["corner_radius_mm"] == 6
    assert sketched["main_view"]["profile"]["sketch"] == square


def test_a_blind_offset_bore_is_not_turned_into_a_through_hole():
    bore = [{"diameter_mm": 12, "length_mm": 30}]
    read = _read(
        _PROFILE,
        main_view={"bore": bore, "bore_start_mm": 5, "bore_from_end": "right", "bore_blind": True},
    )

    merged = consensus_spec([read, read])

    body = merged["main_view"]
    assert (body["bore_start_mm"], body["bore_from_end"], body["bore_blind"]) == (5, "right", True)


def test_additional_bodies_survive_consensus():
    """`parts` обнулялся при любых двух и более проходах."""
    second = {"name": "Втулка", "type": "втулка", "outer": [{"diameter_mm": 40, "length_mm": 20}]}
    read = _read(_PROFILE, parts=[second])

    merged = consensus_spec([read, read, _read(_PROFILE, parts=[])])

    assert len(merged["parts"]) == 1
    assert merged["parts"][0]["name"] == "Втулка"
    assert merged["parts"][0]["outer"] == second["outer"]
