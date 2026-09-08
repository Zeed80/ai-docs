from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PIL import Image

from app.ai.cad_recognize.axial_dimensions import localize_axial_dimensions


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed")
def test_detal_126_axial_lines_are_localized_against_both_datums():
    source = Path(__file__).resolve().parents[3] / "test_vector_files" / "detal_126.png"
    known = [
        0.008,
        0.8,
        1,
        1.6,
        3,
        3.2,
        4,
        5,
        6,
        7.24,
        8,
        12,
        14,
        15,
        16,
        17,
        18,
        20,
        25,
        26,
        27,
        35,
        50,
        78,
        85,
        99,
        150,
        240,
        270,
        470,
    ]

    result = localize_axial_dimensions(Image.open(source).convert("RGB"), known)

    assert result["status"] == "ok"
    assert result["overall_mm"] == 470
    by_value = {item["value_mm"]: item for item in result["observations"]}
    assert by_value[150]["relation"] == "from_left_datum"
    assert by_value[150]["station_from_left_mm"] == 150
    assert by_value[150]["ocr_value_mm"] == 50
    assert by_value[150]["ocr_corrected"] is True
    assert by_value[78]["station_from_left_mm"] == 78
    assert by_value[240]["station_from_left_mm"] == 240
    assert by_value[270]["relation"] == "from_right_datum"
    assert by_value[270]["station_from_left_mm"] == 200
    assert by_value[99]["station_from_left_mm"] == 371
    assert by_value[470]["relation"] == "overall"
    assert all(item["label_bbox"] and item["dimension_line"] for item in by_value.values())


def test_no_dimension_lines_fail_closed():
    image = Image.new("RGB", (400, 200), "white")

    result = localize_axial_dimensions(image, [50, 100])

    assert result["status"] == "unresolved"
    assert result["observations"] == []
    assert result["blockers"]


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed")
def test_detal_126_dimension_lines_recover_values_missing_from_vlm_callouts():
    source = Path(__file__).resolve().parents[3] / "test_vector_files" / "detal_126.png"

    result = localize_axial_dimensions(
        Image.open(source).convert("RGB"),
        # Simulate the failed live pass: only the overall survived callout VLM.
        [470],
    )

    by_value = {item["value_mm"]: item for item in result["observations"]}
    assert {78, 99, 150, 240, 270, 470} <= set(by_value)
    assert by_value[150]["ocr_value_mm"] == 50
    assert by_value[150]["value_source"] == "dimension_span_ocr_correction"
    assert by_value[78]["value_source"] == "dimension_line_ocr"


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed")
def test_direct_dimension_line_ocr_wins_over_nearby_unrelated_callout():
    source = Path(__file__).resolve().parents[3] / "test_vector_files" / "detal_126.png"

    result = localize_axial_dimensions(
        Image.open(source).convert("RGB"),
        [36, 470],
    )

    local_values = {
        item["value_mm"]: item
        for item in result["observations"]
        if item["relation"] == "local_interval"
    }
    assert 35.0 in local_values
    assert local_values[35.0]["raw_text"] == "35"
    assert local_values[35.0]["value_source"] == "dimension_line_ocr"


# ── Калибровка не имеет права быть уверенно неверной ─────────────────────────


def test_an_overall_smaller_than_a_callout_is_not_reported_as_ok():
    """Живой прогон z4-r4.jpg: overall_mm = 45 при цепочке чертежа до 195.

    Локализатор поймал габарит выносного элемента (Б-Б 5:1) и принял его за
    общий, а стадия отчиталась `status: ok` с mm_per_px 0.0427. Неверный
    масштаб уходил дальше по конвейеру и подмешивался в проверки — это хуже,
    чем отсутствие калибровки, потому что выглядит как успех.
    """
    from app.ai.cad_recognize.axial_dimensions import _calibration_blockers

    blockers = _calibration_blockers(45.0, [10.0, 34.0, 90.0, 195.0], [])

    assert blockers
    assert "195" in blockers[0]


def test_a_consistent_overall_passes():
    from app.ai.cad_recognize.axial_dimensions import _calibration_blockers

    assert _calibration_blockers(195.0, [10.0, 34.0, 90.0, 195.0], []) == []


def test_lines_that_disagree_with_their_own_labels_block_the_scale():
    """`span_check_mm: 8.03` против `raw_text: "10"` — расхождение 20 %.

    Оно считалось и записывалось, но ни на что не влияло.
    """
    from app.ai.cad_recognize.axial_dimensions import _calibration_blockers

    observations = [
        {"value_mm": 10.0, "span_check_mm": 8.03},
        {"value_mm": 20.0, "span_check_mm": 16.1},
        {"value_mm": 30.0, "span_check_mm": 29.9},
    ]
    blockers = _calibration_blockers(195.0, [195.0], observations)

    assert blockers
    assert "масштаб определён неверно" in blockers[0]


def test_small_reading_noise_does_not_block_the_scale():
    from app.ai.cad_recognize.axial_dimensions import _calibration_blockers

    observations = [
        {"value_mm": 10.0, "span_check_mm": 10.2},
        {"value_mm": 20.0, "span_check_mm": 19.6},
    ]
    assert _calibration_blockers(195.0, [195.0], observations) == []


# ── Масштаб берётся согласием, а не одной линией ─────────────────────────────


def test_the_scale_is_not_taken_from_a_single_widest_line():
    """Живой `shaft_detail.png`: за общий габарит принято 3.2 мм.

    3.2 — это Ra, шероховатость, затесавшаяся в список линейных выносок. Её
    подпись оказалась связана с широкой линией, и вал длиной в сотни
    миллиметров был откалиброван по значку чистоты поверхности. Раньше базой
    была просто самая широкая линия, и число на ней принималось на веру:
    одна ошибка локализации задавала масштаб всему листу.

    На одном виде длина размерной линии пропорциональна числу на ней — этого
    инварианта достаточно, чтобы выброс перестал быть точкой отказа.
    """
    from app.ai.cad_recognize.axial_dimensions import _calibration_base

    # Три согласованных наблюдения около 0.2 мм/px и один выброс на широкой
    # линии — ровно форма живого отказа.
    candidates = [
        {"ocr_value_mm": 50.0, "span_px": 250.0},
        {"ocr_value_mm": 30.0, "span_px": 150.0},
        {"ocr_value_mm": 120.0, "span_px": 600.0},
        {"ocr_value_mm": 3.2, "span_px": 700.0},
    ]

    assert _calibration_base(candidates)["ocr_value_mm"] == 120.0


def test_the_widest_agreeing_line_still_wins():
    """Согласие отсеивает выброс, а не заменяет правило «самая широкая»."""
    from app.ai.cad_recognize.axial_dimensions import _calibration_base

    candidates = [
        {"ocr_value_mm": 20.0, "span_px": 100.0},
        {"ocr_value_mm": 60.0, "span_px": 300.0},
    ]
    assert _calibration_base(candidates)["ocr_value_mm"] == 60.0


def test_a_single_candidate_behaves_exactly_as_before():
    """Согласовывать не с чем — прежнее поведение, без выдуманной строгости."""
    from app.ai.cad_recognize.axial_dimensions import _calibration_base

    only = {"ocr_value_mm": 3.2, "span_px": 700.0}
    assert _calibration_base([only]) is only


# ── Числа берутся у того, кто читает вернее ──────────────────────────────────


def test_a_model_read_number_displaces_the_ocr_guess_at_the_same_place():
    """Замерено на корпусе: tesseract читает «12885» и «АДЗ1» вместо размеров.

    На листах, где высота цифры десять пикселей, движок находит четыре-семнадцать
    чисел там, где их два десятка, и половина прочитанного — мусор.
    Геометрическая проверка мусор честно отбрасывает, поэтому стадия остаётся
    пустой на чертежах, которые ЧИТАЮТСЯ: у `flange_detail.png` модель взяла
    все пять фактов эталона. Растяжка, Otsu и размытие с порогом проверены —
    не помогают, движок эти листы просто не читает.
    """
    from app.ai.cad_recognize.axial_dimensions import _merge_callout_tokens

    ocr = [
        {
            "raw_text": "12885",
            "ocr_value_mm": 12885.0,
            "ocr_confidence": 0.31,
            "label_bbox": [100, 200, 160, 220],
        }
    ]
    merged = _merge_callout_tokens(ocr, [(185.0, [102, 201, 158, 219])])

    assert [item["ocr_value_mm"] for item in merged] == [185.0]
    assert merged[0]["value_from"] == "callout"
    # Не единица: связь «число ↔ линия» по-прежнему выводится, а не прочитана,
    # поэтому сверка с пролётом остаётся обязательной.
    assert merged[0]["ocr_confidence"] < 1.0


def test_an_ocr_token_elsewhere_on_the_sheet_survives():
    from app.ai.cad_recognize.axial_dimensions import _merge_callout_tokens

    ocr = [
        {
            "raw_text": "50",
            "ocr_value_mm": 50.0,
            "ocr_confidence": 0.9,
            "label_bbox": [900, 700, 940, 720],
        }
    ]
    merged = _merge_callout_tokens(ocr, [(185.0, [102, 201, 158, 219])])

    assert sorted(item["ocr_value_mm"] for item in merged) == [50.0, 185.0]


def test_a_text_layer_without_coordinates_changes_nothing():
    """Документная OCR-модель без grounding возвращает текст без рамок.

    Связывать тогда нечего, и это единственная причина, по которой стадия
    чинится не полностью. Молча подставлять числа без координат нельзя: они
    попали бы на чужие линии.
    """
    from app.ai.cad_recognize.axial_dimensions import _merge_callout_tokens

    ocr = [
        {
            "raw_text": "50",
            "ocr_value_mm": 50.0,
            "ocr_confidence": 0.9,
            "label_bbox": [10, 20, 40, 40],
        }
    ]

    assert _merge_callout_tokens(ocr, [(185.0, None)]) == ocr
    assert _merge_callout_tokens(ocr, None) == ocr


# ── Масштаб определяет группа, а не середина ────────────────────────────────


def test_the_largest_agreeing_group_wins_over_the_median():
    """Верных наблюдений не обязано быть большинство.

    Живой `detal_126.png`: верный масштаб 0.339 мм/px разделяют пять
    наблюдений (470, 270, 240, 99, 78), а неверных спариваний больше. Медиана
    отношений уезжала к шуму, и базой становились 20 мм при габарите листа 470.
    """
    from app.ai.cad_recognize.axial_dimensions import _calibration_base

    consistent = [
        {"ocr_value_mm": 470.0, "span_px": 1386.0},
        {"ocr_value_mm": 270.0, "span_px": 795.0},
        {"ocr_value_mm": 240.0, "span_px": 712.0},
        {"ocr_value_mm": 99.0, "span_px": 293.0},
        {"ocr_value_mm": 78.0, "span_px": 228.5},
    ]
    noise = [
        {"ocr_value_mm": 25.0, "span_px": 795.0},
        {"ocr_value_mm": 50.0, "span_px": 441.0},
        {"ocr_value_mm": 12.0, "span_px": 296.0},
        {"ocr_value_mm": 8.0, "span_px": 189.0},
        {"ocr_value_mm": 1.0, "span_px": 161.0},
        {"ocr_value_mm": 20.0, "span_px": 60.0},
    ]

    assert _calibration_base(consistent + noise)["ocr_value_mm"] == 470.0


def test_a_candidate_the_sheet_itself_dwarfs_is_not_considered_the_overall(monkeypatch):
    """Живой `shaft_detail.png`: единственным кандидатом оказалось 3.2 — Ra.

    Стадия калибровалась по нему, а потом сама же себя отвергала, называя
    причиной 3.2, будто это был рассмотренный вариант. Отбор и объяснение
    обязаны совпадать: заведомо непригодное отсекается ДО выбора базы, и
    честный ответ — «подходящей линии нет».
    """
    from app.ai.cad_recognize import axial_dimensions as axial

    token = {
        "raw_text": "3.2",
        "ocr_value_mm": 3.2,
        "ocr_confidence": 0.9,
        "label_bbox": [10, 10, 40, 30],
    }
    paired = {**token, "line": [10.0, 40.0, 300.0, 40.0], "span_px": 290.0}
    # monkeypatch, а не присваивание: подменённый атрибут модуля переживает
    # тест и ломает соседние файлы необратимо.
    monkeypatch.setattr(axial, "_ocr_numeric_tokens", lambda *_a, **_k: [token])
    monkeypatch.setattr(axial, "_hough_lines", lambda *_a, **_k: ([[10.0, 40.0, 300.0]], []))
    monkeypatch.setattr(axial, "_pair_tokens_with_lines", lambda *_a, **_k: [paired])

    result = axial.localize_axial_dimensions(Image.new("RGB", (400, 200), "white"), [3.2, 840.0])

    assert result["status"] == "unresolved"
    assert "840" in result["blockers"][0]
    # Негодный кандидат не назван причиной — он ею и не был.
    assert "3.2" not in result["blockers"][0]
