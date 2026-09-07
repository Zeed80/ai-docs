"""Один контракт текстового слоя — независимо от того, чем он прочитан.

До этого в проекте жили два несвязанных текстовых слоя. Метод «по описанию»
разбирал ответ модели ПОСТРОЧНО регулярками: в список размеров попадали строки
markdown целиком, вместе с ``**`` и дефисами, а координат не было вовсе.
Растровая трассировка тем временем уже просила строгий JSON с bbox и переводила
координаты в пиксели листа — но пользовалась этим только она одна.

Живой пример из журнала `z4-r4.jpg`: ответ начинался с «Я прочитаю все надписи
и размеры с этого чертежа», содержал заголовки «## Основная надпись (штамп):» и
строку «**185** — общая длина вала (вероятно)». Последняя становилась размером
наравне с прочитанным — при том что модель сама пометила её догадкой.
"""

from __future__ import annotations

from app.ai.cad_recognize.text_layer import (
    layer_from_engine,
    layer_from_prose,
    layer_from_vlm_json,
)

# ── Все три источника дают одну структуру ────────────────────────────────────


def test_engine_tokens_keep_their_coordinates_and_confidence():
    """tesseract отдаёт bbox и уверенность сам — переименование, не разбор."""
    layer = layer_from_engine(
        [
            {
                "raw_text": "Ø25",
                "label_bbox": [10, 20, 60, 40],
                "ocr_confidence": 0.87,
                "orientation": "vertical",
            }
        ],
        model="tesseract",
    )

    assert layer.source == "engine"
    assert layer.grounded is True
    token = layer.tokens[0]
    assert (token.text, token.bbox, token.orientation) == (
        "Ø25",
        [10.0, 20.0, 60.0, 40.0],
        "vertical",
    )
    assert token.confidence == 0.87


def test_vlm_json_coordinates_are_mapped_back_to_sheet_pixels():
    """qwen3-vl отдаёт боксы в сетке 0..1000, а не в пикселях изображения."""
    layer = layer_from_vlm_json(
        [{"text": "Ø25", "bbox": [100, 200, 300, 400]}],
        model="qwen3-vl",
        image_size=(1000, 500),
        normalized_scale=1000.0,
    )

    assert layer.tokens[0].bbox == [100.0, 100.0, 300.0, 200.0]
    assert layer.grounded is True


def test_qwen_writes_its_own_field_name():
    """Просить её отвечать «bbox» бесполезно — она пишет `bbox_2d`."""
    layer = layer_from_vlm_json(
        [{"text": "Ra 6,3", "bbox_2d": [0, 0, 1000, 1000]}],
        model="qwen3-vl",
        image_size=(200, 100),
        normalized_scale=1000.0,
    )
    assert layer.tokens[0].bbox == [0.0, 0.0, 200.0, 100.0]


def test_a_model_without_grounding_says_so_instead_of_losing_the_layer():
    """«Не смог вернуть координаты» — факт о модели, а не повод молчать."""
    layer = layer_from_vlm_json([{"text": "Ø25"}, {"text": "M18×1,5"}], model="glm-ocr")

    assert layer.source == "vlm_ungrounded"
    assert layer.grounded is False
    assert [t.text for t in layer.tokens] == ["Ø25", "M18×1,5"]
    assert all(t.bbox is None for t in layer.tokens)


def test_prose_is_still_read_just_without_coordinates():
    layer = layer_from_prose("Ø25\nM18×1,5-6g\nСталь 45", model="deepseek")

    assert layer.source == "vlm_ungrounded"
    assert [t.text for t in layer.tokens] == ["Ø25", "M18×1,5-6g", "Сталь 45"]


# ── Разметка и оговорки не становятся данными ────────────────────────────────


def test_markdown_decoration_is_not_part_of_the_reading():
    layer = layer_from_prose(
        "## Основная надпись (штамп):\n- **Ø25**\n- **Сталь 45 ГОСТ 1050-2014**",
        model="m",
    )
    assert [t.text for t in layer.tokens] == ["Ø25", "Сталь 45 ГОСТ 1050-2014"]


def test_a_value_the_model_itself_called_a_guess_is_dropped():
    """`**185** — общая длина вала (вероятно)` — не размер с листа.

    Число там есть, поэтому построчный разбор отправлял такую строку прямо в
    список размеров, откуда её брали как подтверждённую.
    """
    layer = layer_from_prose("**185** — общая длина вала (вероятно)\nØ25", model="m")

    assert [t.text for t in layer.tokens] == ["Ø25"]


def test_sheet_metadata_does_not_become_a_dimension():
    layer = layer_from_prose("Масштаб 2:1\nЛист 1\nØ25", model="m")
    assert [t.text for t in layer.tokens] == ["Ø25"]


def test_a_repeated_line_is_read_once_but_coordinates_win():
    """Модель зацикливается; из двух одинаковых токенов лучше тот, что с bbox."""
    layer = layer_from_vlm_json(
        [
            {"text": "Ø25"},
            {"text": "Ø25", "bbox": [10, 10, 20, 20]},
        ],
        model="m",
    )

    assert len(layer.tokens) == 1
    assert layer.tokens[0].bbox is not None


# ── Производные размеры и надписи ────────────────────────────────────────────


def test_callouts_are_derived_deterministically_and_keep_the_bbox():
    layer = layer_from_engine(
        [
            {"raw_text": "Ø25", "label_bbox": [1, 2, 3, 4]},
            {"raw_text": "Ra 6,3", "label_bbox": [5, 6, 7, 8]},
            {"raw_text": "Сталь 45", "label_bbox": [9, 10, 11, 12]},
            {"raw_text": "M18×1,5", "label_bbox": [13, 14, 15, 16]},
        ],
        model="tesseract",
    )

    callouts = layer.as_callouts()

    assert [d["value"] for d in callouts["dimensions"]] == ["Ø25"]
    assert callouts["dimensions"][0]["bbox"] == [1.0, 2.0, 3.0, 4.0]
    kinds = {a["kind"] for a in callouts["annotations"]}
    assert kinds == {"roughness", "material", "thread"}
    # Координаты доезжают и до надписей — иначе привязать значение к месту
    # на листе по-прежнему нечем.
    assert all("bbox" in a for a in callouts["annotations"])


def test_text_without_a_number_is_neither_a_dimension_nor_an_annotation():
    layer = layer_from_prose("Неразборчивая надпись", model="m")
    callouts = layer.as_callouts()
    assert callouts == {"dimensions": [], "annotations": []}


def test_an_empty_answer_yields_an_empty_layer_not_an_error():
    layer = layer_from_prose("", model="m")
    assert layer.tokens == []
    assert layer.grounded is False


# ── Вопрос подбирается под модель, ответ приводится к одному виду ────────────


def test_a_document_ocr_model_gets_a_plain_ask():
    """Просить координаты у модели, которая их не умеет, — портить ответ.

    Живой прогон: glm-ocr (документная модель на 1.1B) на просьбу вернуть
    JSON-массив с bbox выдала разрозненные объекты без координат, а потом
    зациклилась, повторив формулировку запроса четырнадцать раз.
    """
    from app.ai.cad_recognize.spec_fragments import _ocr_prompt_for

    prompt, grounded = _ocr_prompt_for("glm_ocr_ollama")
    assert grounded is False
    assert "JSON" not in prompt


def test_a_model_that_holds_a_schema_is_asked_for_coordinates():
    from app.ai.cad_recognize.spec_fragments import _ocr_prompt_for

    prompt, grounded = _ocr_prompt_for("claude_sonnet_anthropic")
    assert grounded is True
    assert "bbox" in prompt


def test_an_unknown_model_is_asked_the_safe_way():
    from app.ai.cad_recognize.spec_fragments import _ocr_prompt_for

    _prompt, grounded = _ocr_prompt_for("модели-такой-нет")
    assert grounded is False


# ── Один разбор на все формы ответа ──────────────────────────────────────────


def test_scattered_json_objects_are_read_too():
    """Форма, которой отвечает glm-ocr: несколько объектов подряд, не массив.

    Разбирать только массив значило терять такой ответ целиком — фигурные
    скобки и кавычки уходили в «токены», и от богатой транскрипции штампа
    оставался мусор.
    """
    from app.ai.cad_recognize.text_layer import layer_from_answer

    answer = (
        '```json\n{"text": "ПЗ-137.04.10.02.008.09\\nВал\\nСталь 45 ГОСТ 1050-2014"}\n```\n'
        '```json\n{"text": "Ra 6,3"}\n```'
    )
    layer = layer_from_answer(answer, model="glm-ocr")

    assert [t.text for t in layer.tokens] == [
        "ПЗ-137.04.10.02.008.09",
        "Вал",
        "Сталь 45 ГОСТ 1050-2014",
        "Ra 6,3",
    ]
    assert layer.grounded is False


def test_a_looping_model_does_not_fill_the_layer_with_its_own_prompt():
    """Зациклившаяся модель повторяет вопрос — это не содержимое листа."""
    from app.ai.cad_recognize.text_layer import layer_from_answer

    prompt = "Прочитай все надписи и размеры с этого чертежа."
    answer = "Ø25\n" + "Все надписи и размеры с этого чертежа\n" * 14
    layer = layer_from_answer(answer, model="m", prompt=prompt)

    assert [t.text for t in layer.tokens] == ["Ø25"]


def test_a_grounded_array_still_wins_when_the_model_can_do_it():
    from app.ai.cad_recognize.text_layer import layer_from_answer

    answer = '[{"text": "Ø25", "bbox": [100, 200, 300, 400]}]'
    layer = layer_from_answer(answer, model="vlm", image_size=(1000, 500), normalized_scale=1000.0)

    assert layer.grounded is True
    assert layer.tokens[0].bbox == [100.0, 100.0, 300.0, 200.0]


# ── Ссылка на стандарт — не размер ───────────────────────────────────────────


def test_a_standard_citation_never_becomes_a_dimension():
    """Живой прогон: «Смаль 45 ГОСТ 1050-2014» уехало в список размеров.

    «Сталь», прочитанное с опечаткой, не попало в материалы по словарю — и
    строка со ссылкой на стандарт стала размером, отдав в пул чисел и номер
    стандарта, и год издания. Из этого пула выбираются диаметры и осевые
    станции; проверка калибровки потом честно отвергла масштаб, но причиной
    назвала «лист несёт больший размер 2014 мм».
    """
    from app.ai.cad_recognize.text_layer import layer_from_prose

    layer = layer_from_prose("Смаль 45 ГОСТ 1050-2014\nØ25", model="m")
    callouts = layer.as_callouts()

    assert [d["value"] for d in callouts["dimensions"]] == ["Ø25"]
    assert any("ГОСТ" in a["text"] for a in callouts["annotations"])


def test_a_correctly_read_material_line_is_still_a_material():
    from app.ai.cad_recognize.text_layer import layer_from_prose

    layer = layer_from_prose("Материал: Сталь 45 ГОСТ 1050-2014", model="m")
    kinds = {a["kind"] for a in layer.as_callouts()["annotations"]}

    assert kinds == {"material"}


def test_a_non_breaking_hyphen_does_not_leak_the_year():
    """`ГОСТ 1050‑2014` через U+2011 вычищался только до «ГОСТ 1050».

    Хвост «‑2014» доезжал до списка размеров как число: класс дефисов в
    регулярном выражении покрывал обычный, en- и em-dash, но не неразрывный,
    который вставляют и модели, и типографика.
    """
    from app.ai.cad_recognize.spec_fragments import _STANDARD_REFERENCE

    for dash in ("-", "‐", "‑", "‒", "–", "—", "−"):
        cleaned = _STANDARD_REFERENCE.sub(" ", f"Сталь 45 ГОСТ 1050{dash}2014")
        assert "2014" not in cleaned, f"год пережил дефис {dash!r}"
        assert "1050" not in cleaned
    # Настоящий размер рядом со ссылкой не страдает.
    assert "470" in _STANDARD_REFERENCE.sub(" ", "470 h14 ГОСТ 1050-2014")
