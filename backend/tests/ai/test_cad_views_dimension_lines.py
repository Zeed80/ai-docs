"""Метод `views`: размерные линии вдоль оси и станции по ним."""

from __future__ import annotations


def _sheet():
    """Ступенчатый вал не в масштабе с двумя размерами над видом."""
    import cv2
    import numpy as np

    g = np.full((300, 700), 255, np.uint8)
    axis = 200
    # Контур: Ø «малый» 100…300 px, Ø «большой» 300…600 px.
    cv2.rectangle(g, (100, axis - 30), (300, axis + 30), 0, 4)
    cv2.rectangle(g, (300, axis - 60), (600, axis + 60), 0, 4)
    # Выносные от контура вверх и размерные линии со стрелками.
    for x, top in ((100, axis - 30), (300, axis - 60), (600, axis - 60)):
        cv2.line(g, (x, top), (x, 40), 0, 1)
    for x0, x1, y in ((100, 300, 100), (100, 600, 60)):
        cv2.line(g, (x0, y), (x1, y), 0, 1)
        for x, d in ((x0, 1), (x1, -1)):
            pts = np.array([[x, y], [x + d * 18, y - 4], [x + d * 18, y + 4]], np.int32)
            cv2.fillPoly(g, [pts], 0)
    return g, axis


def test_axial_spans_are_found_between_witness_lines():
    from app.ai.cad_views.dimension_lines import axial_spans

    g, axis = _sheet()

    def radius_at(x: float) -> float:
        if 100 <= x <= 300:
            return 30.0
        if 300 < x <= 600:
            return 60.0
        return 0.0

    spans = axial_spans(g, 100, 600, axis, radius_at, 4.0, reach=180)
    pairs = sorted((round(s.a), round(s.b)) for s in spans)
    # Выносная уступа 300 пересекает габаритный размер без стрелки — он целый.
    assert [(abs(a - 100) <= 2, abs(b - e) <= 2) for (a, b), e in zip(pairs, (300, 600))] == [
        (True, True),
        (True, True),
    ]
    assert len(pairs) == 2


def test_labels_go_to_spans_by_order_of_length_not_by_scale():
    from app.ai.cad_views.dimension_lines import match_spans

    # Шпиндель: «10» нарисовано длиннее «14» относительно общего масштаба,
    # но порядок длин тот же.
    spans = [(158, 292), (260, 292), (1208, 1359), (159, 386), (550, 1359), (159, 1359)]
    matched = match_spans(spans, [10, 2, 22, 85, 127, 14])
    assert {(a, b): v for a, b, v in matched} == {
        (260, 292): 2,
        (158, 292): 10,
        (1208, 1359): 14,
        (159, 386): 22,
        (550, 1359): 85,
        (159, 1359): 127,
    }


def test_stations_follow_their_dimension_lines():
    from app.ai.cad_views.dimension_lines import stations_from_spans

    # Две близкие точки одного уступа (300 и 301) — одна станция; размер
    # паза (1208, не станция профиля) пропускается.
    stations = [159, 260, 292, 300, 301, 550, 1359]
    matched = [
        (260, 292, 2),
        (158, 292, 10),
        (1208, 1359, 14),
        (550, 1359, 85),
        (159, 1359, 127),
    ]
    solved = stations_from_spans(stations, matched, tolerance=3.0)
    assert solved[159] == 0
    assert solved[292] == 10
    assert solved[260] == 8
    assert solved[550] == 42
    assert solved[1359] == 127
    assert solved[300] == solved[301]


def test_english_diameter_and_square_thread():
    from app.ai.cad_views.labels import parse_label

    thread = parse_label("SQ THD, DIA 38×7")
    assert (thread.kind, thread.value, thread.pitch) == ("thread", 38.0, 7.0)
    assert parse_label("DIA 38").kind == "diameter"
    assert parse_label("MEDIA 5").kind == "text"


def test_axial_scale_from_the_dimension_chain():
    from app.ai.cad_views.dimension_lines import scale_from_spans

    # Вал-шестерня part_01: цепочка 16 · 56 · 54 · 14,5 · 18 при 11,45 px/мм
    # и плотный ряд Ø, по которым масштаб уезжал на 12 %.
    px = 1 / 0.08735
    spans, x = [], 383.0
    for value in (16, 56, 54, 14.5, 18):
        spans.append((x, x + value * px))
        x += value * px
    scale, hits = scale_from_spans(spans, [16, 56, 54, 14.5, 18, 23.5, 182, 4.5])
    assert hits == 5
    assert abs(scale - 0.08735) < 1e-4


def test_profile_is_clipped_by_the_overall_dimension_only():
    from app.ai.cad_views.pipeline import _clip_to_overall
    from app.ai.cad_views.revolve_profile import HalfProfile

    profile = HalfProfile(
        axis_y=100.0,
        line_px=4.0,
        x0=0,
        x1=2300,
        outer=[(0, 50.0), (2080, 50.0), (2080, 30.0), (2300, 30.0)],
        inner=[],
    )
    # Габарит 182 — от левого торца до 2071: за ним выносной элемент.
    clipped = _clip_to_overall(profile, [(1.0, 2071.0, 182.0)], 4.0, 182.0)
    assert clipped is not None and clipped.x1 == 2071
    # Наибольший найденный размер, но не габарит листа, — не обрезает.
    assert _clip_to_overall(profile, [(1.0, 2071.0, 120.0)], 4.0, 185.0) is None
