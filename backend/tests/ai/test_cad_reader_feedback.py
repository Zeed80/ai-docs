"""Capturing reader corrections — the corpus item 6 needs before any training."""

from __future__ import annotations

from app.ai.cad_reader_feedback import (
    build_correction_record,
    corpus_summary,
    diff_spec,
    merge_correction,
    reconcile_corrected_feature_blockers,
)


def _read() -> dict:
    return {
        "part": "Фланец",
        "main_view": {
            "type": "фланец",
            "profile": {"shape": "circle", "diameter_mm": 560, "thickness_mm": 20,
                        "holes": [{"center_x_mm": 0, "center_y_mm": 0, "diameter_mm": 18}]},
        },
        "title_block": {"material": "Чугун СЧ20 ГОСТ 1412", "scale": "1:1"},
        "dimensions": [{"value": "Ø560"}],
    }


def test_a_corrected_bore_shows_up_as_a_field_level_change():
    """The exact live mistake: Ø80H7 read as Ø18."""
    corrected = _read()
    corrected["main_view"]["profile"]["holes"][0]["diameter_mm"] = 80
    diff = diff_spec(_read(), corrected)
    assert "profile" in diff["changed"]
    assert diff["changed_count"] == 1


def test_confirmations_are_recorded_as_well_as_mistakes():
    """A corpus of only mistakes teaches that everything is a mistake."""
    diff = diff_spec(_read(), _read())
    assert diff["changed"] == {}
    assert "material" in diff["unchanged"]
    assert "part" in diff["unchanged"]
    assert diff["confirmed_count"] >= 4


def test_empty_fields_are_not_counted_as_confirmed():
    read = {"part": "", "title_block": {}, "main_view": {}}
    diff = diff_spec(read, dict(read))
    assert diff["unchanged"] == []


def test_a_correction_touches_only_the_supplied_fields():
    merged = merge_correction(_read(), {"material": "Сталь 45"})
    assert merged["title_block"]["material"] == "Сталь 45"
    # Everything else survives.
    assert merged["title_block"]["scale"] == "1:1"
    assert merged["main_view"]["profile"]["diameter_mm"] == 560
    assert merged["dimensions"] == [{"value": "Ø560"}]


def test_a_correction_can_create_a_missing_branch():
    merged = merge_correction({"part": "Вал"}, {"scale": "1:2"})
    assert merged["title_block"]["scale"] == "1:2"
    assert merged["part"] == "Вал"


def test_a_record_carries_its_provenance():
    corrected = _read()
    corrected["title_block"]["material"] = "Сталь 45"
    record = build_correction_record(
        generation_id="gen-1",
        source_path="image-gen/x/gen-1_normalized.png",
        read_spec=_read(),
        corrected_spec=corrected,
        corrected_by="user-7",
        reader_models=["qwen3_vl_30b_a3b_ollama"],
    )
    assert record["corrected_by"] == "user-7"
    assert record["reader_models"] == ["qwen3_vl_30b_a3b_ollama"]
    # The sheet is referenced, not embedded, so the corpus stays rebuildable.
    assert record["source_path"].endswith(".png")
    assert record["diff"]["changed_count"] == 1
    assert record["recorded_at"]


def test_the_summary_says_which_fields_actually_need_teaching():
    corrected = _read()
    corrected["main_view"]["profile"]["holes"][0]["diameter_mm"] = 80
    records = [
        build_correction_record(
            generation_id=f"gen-{index}", source_path=None,
            read_spec=_read(), corrected_spec=corrected, corrected_by="u",
        )
        for index in range(3)
    ]
    records.append(build_correction_record(
        generation_id="gen-ok", source_path=None,
        read_spec=_read(), corrected_spec=_read(), corrected_by="u",
    ))
    summary = corpus_summary(records)
    assert summary["records"] == 4
    assert summary["sheets_with_any_correction"] == 3
    assert summary["corrections_per_field"]["profile"] == 3
    assert summary["confirmations_per_field"]["material"] == 4


def test_a_correction_does_not_rewrite_the_spec_it_corrects():
    """The read half of the pair is the training signal; it must survive."""
    read = _read()
    merged = merge_correction(read, {"profile": {"shape": "circle", "diameter_mm": 999}})
    assert merged["main_view"]["profile"]["diameter_mm"] == 999
    # The original is untouched, so the diff still has something to compare.
    assert read["main_view"]["profile"]["diameter_mm"] == 560
    assert diff_spec(read, merged)["changed_count"] == 1


def test_later_correction_can_build_on_the_previous_human_state():
    read = {
        "main_view": {
            "outer": [{"diameter_mm": 90, "length_mm": 100}],
            "axial_holes": [],
        }
    }
    first = merge_correction(
        read, {"outer": [{"diameter_mm": 102, "length_mm": 100}]}
    )
    second = merge_correction(
        first,
        {
            "axial_holes": [
                {
                    "count": 2,
                    "bolt_circle_diameter_mm": 65,
                    "thread": {"designation": "M8"},
                }
            ]
        },
    )

    assert second["main_view"]["outer"][0]["diameter_mm"] == 102
    assert len(second["main_view"]["axial_holes"]) == 1
    assert read["main_view"]["outer"][0]["diameter_mm"] == 90


def test_completed_axial_pattern_removes_only_its_own_blocker():
    spec = {
        "main_view": {
            "axial_holes": [{
                "count": 2,
                "bolt_circle_diameter_mm": 65,
                "from_face": "zmax",
                "through": False,
                "depth_mm": 12,
                "pilot_diameter_mm": 6.8,
                "thread": {"designation": "M8", "nominal_diameter_mm": 8},
            }],
        },
        "unresolved": [
            "малые элементы: осевые отверстия M8: не определены торец",
            "расточка: не прочитана",
        ],
    }

    result = reconcile_corrected_feature_blockers(spec, {"axial_holes"})

    assert result["unresolved"] == ["расточка: не прочитана"]


def test_partial_axial_correction_keeps_precise_missing_fields():
    spec = {
        "main_view": {"axial_holes": [{
            "count": 2,
            "bolt_circle_diameter_mm": 65,
            "from_face": "zmax",
            "through": False,
            "depth_mm": None,
            "pilot_diameter_mm": None,
            "thread": {"designation": "M8", "nominal_diameter_mm": 8},
        }]},
        "unresolved": ["малые элементы: осевые отверстия M8: старый текст"],
    }

    result = reconcile_corrected_feature_blockers(spec, {"axial_holes"})

    assert len(result["unresolved"]) == 1
    assert "глубина" in result["unresolved"][0]
    assert "подготовительного" in result["unresolved"][0]


def test_six_corrected_chamfers_remove_count_blocker():
    spec = {
        "main_view": {
            "chamfers": [
                {
                    "size_mm": 1,
                    "angle_deg": 45,
                    "location": "shoulder",
                    "at_z_mm": index * 10,
                }
                for index in range(6)
            ]
        },
        "dimensions": [{"value": "6 фасок 1×45°"}],
        "unresolved": ["малые элементы: указано 6 фасок, локализовано 1"],
    }

    result = reconcile_corrected_feature_blockers(spec, {"chamfers"})

    assert result["unresolved"] == []


def test_chamfer_count_survives_when_only_the_blocker_had_the_count():
    spec = {
        "main_view": {
            "chamfers": [
                {"size_mm": 1, "angle_deg": 45, "location": "left_end"}
            ]
        },
        "dimensions": [],
        "unresolved": ["малые элементы: указано 6 фасок, локализовано 1"],
    }

    result = reconcile_corrected_feature_blockers(spec, {"chamfers"})

    assert result["unresolved"] == [
        "малые элементы: указано 6 фасок, локализовано 1"
    ]


def test_unlocated_shoulder_chamfer_gets_an_actionable_blocker():
    spec = {
        "main_view": {
            "chamfers": [
                {"size_mm": 1, "angle_deg": 45, "location": "shoulder"}
            ]
        },
        "unresolved": [],
    }

    result = reconcile_corrected_feature_blockers(spec, {"chamfers"})

    assert result["unresolved"] == [
        "малые элементы: фаска 1: не задано положение по Z или Ø"
    ]
