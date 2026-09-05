"""Control points for the engineering reference tables (ISO 286 / ГОСТ 25347-82).

These numbers are checked against the standard textbook example set for the
H7/k6, H7/m6, H7/n6, H7/p6 fits at 18-30mm (a commonly cited reference range)
so a reviewer can cross-check them against any ЕСДП handbook without needing
the full table.
"""

from __future__ import annotations

from app.ai import techdraw_reference as ref


def test_h7_tolerance_band_18_30():
    band = ref.tolerance_band("h7", 25)
    assert band == ref.ToleranceBand(es_um=0.0, ei_um=-21.0)


def test_H7_tolerance_band_18_30():
    band = ref.tolerance_band("H7", 25)
    assert band == ref.ToleranceBand(es_um=21.0, ei_um=0.0)


def test_k6_tolerance_band_18_30():
    band = ref.tolerance_band("k6", 25)
    assert band == ref.ToleranceBand(es_um=15.0, ei_um=2.0)


def test_m6_tolerance_band_18_30():
    band = ref.tolerance_band("m6", 25)
    assert band == ref.ToleranceBand(es_um=21.0, ei_um=8.0)


def test_n6_tolerance_band_18_30():
    band = ref.tolerance_band("n6", 25)
    assert band == ref.ToleranceBand(es_um=28.0, ei_um=15.0)


def test_p6_tolerance_band_18_30():
    band = ref.tolerance_band("p6", 25)
    assert band == ref.ToleranceBand(es_um=35.0, ei_um=22.0)


def test_js6_symmetric():
    band = ref.tolerance_band("js6", 40)  # range 30-50, IT6=16 -> +/-8
    assert band == ref.ToleranceBand(es_um=8.0, ei_um=-8.0)


def test_f_and_g_clearance_fields_match_iso_286_tables_20_and_21():
    assert ref.tolerance_band("f7", 25) == ref.ToleranceBand(es_um=-20.0, ei_um=-41.0)
    assert ref.tolerance_band("g6", 80) == ref.ToleranceBand(es_um=-10.0, ei_um=-29.0)
    assert ref.tolerance_band("f7", 400) == ref.ToleranceBand(es_um=-62.0, ei_um=-119.0)
    assert ref.tolerance_band("g9", 180) == ref.ToleranceBand(es_um=-14.0, ei_um=-114.0)


def test_F_and_G_hole_fields_are_reflected_clearance_deviations():
    assert ref.tolerance_band("F7", 25) == ref.ToleranceBand(es_um=41.0, ei_um=20.0)
    assert ref.tolerance_band("G6", 80) == ref.ToleranceBand(es_um=29.0, ei_um=10.0)


def test_zero_line_and_symmetric_fields_cover_additional_it_grades():
    assert ref.tolerance_band("h5", 25) == ref.ToleranceBand(es_um=0.0, ei_um=-9.0)
    assert ref.tolerance_band("H12", 80) == ref.ToleranceBand(es_um=300.0, ei_um=0.0)
    assert ref.tolerance_band("js10", 40) == ref.ToleranceBand(es_um=50.0, ei_um=-50.0)
    assert ref.tolerance_band("JS16", 400) == ref.ToleranceBand(es_um=1800.0, ei_um=-1800.0)


def test_tolerance_band_unknown_symbol():
    assert ref.tolerance_band("zz6", 25) is None


def test_tolerance_band_out_of_range():
    assert ref.tolerance_band("h6", 5000) is None


def test_is_valid_tolerance_symbol():
    assert ref.is_valid_tolerance_symbol("h7")
    assert ref.is_valid_tolerance_symbol("H11")
    assert ref.is_valid_tolerance_symbol("F8")
    assert ref.is_valid_tolerance_symbol("JS14")
    assert not ref.is_valid_tolerance_symbol("zzz")
    assert not ref.is_valid_tolerance_symbol("h99")


def test_nearest_ra():
    assert ref.nearest_ra(0.9) == 0.8
    assert ref.nearest_ra(1.6) == 1.6


def test_parse_thread_coarse():
    t = ref.parse_thread("M20")
    assert t is not None
    assert t.major_d_mm == 20 and t.coarse_pitch_mm == 2.5


def test_parse_thread_fine():
    t = ref.parse_thread("M20x1.5")
    assert t is not None
    assert t.coarse_pitch_mm == 1.5


def test_parse_thread_unknown_diameter():
    assert ref.parse_thread("M13") is None


def test_parse_thread_invalid_pitch():
    assert ref.parse_thread("M20x9.9") is None


def test_minor_diameter():
    t = ref.METRIC_THREAD_TABLE[20]
    # ISO 68-1: d1 = D - 1.0825*P = 20 - 1.0825*2.5 = 17.29375
    assert abs(ref.minor_diameter_mm(t) - 17.29375) < 1e-6


def test_choose_sheet_format_small_fits_a4():
    assert ref.choose_sheet_format(50).name == "A4"


def test_choose_sheet_format_escalates():
    assert ref.choose_sheet_format(500).name == "A3"
    assert ref.choose_sheet_format(800).name == "A2"


def test_choose_sheet_format_prefer_overrides():
    assert ref.choose_sheet_format(50, prefer="A2").name == "A2"


def test_hatch_angle_alternates():
    assert ref.hatch_angle_for_index(0) == 45.0
    assert ref.hatch_angle_for_index(1) == -45.0


def test_classify_material_exact():
    spec = ref.classify_material("Сталь 45 ГОСТ 1050-2013")
    assert spec is not None and spec.group == "steel_carbon"


def test_classify_material_unknown_returns_none_without_tp_generator_group():
    # A string tp_generator would classify as "steel_carbon" but with no
    # matching catalog entry text should still resolve via group fallback.
    spec = ref.classify_material("сталь ст3")
    assert spec is not None and spec.group == "steel_carbon"
