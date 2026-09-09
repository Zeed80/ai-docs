"""Режимы просмотра исходника: что доступно без трассировки, а что нет.

Ссылка «Открыть bbox на полном листе» запрашивала `mode=overlay` и получала
404 «Trace proposal для overlay не найден». Подпись и режим разошлись: полный
лист — это `sheet`, а `overlay` сравнивает ПЕРЕЧЕРЧЕННЫЙ кандидат с
исходником и требует предложения гибридной трассировки.

Таких предложений у оцифровки «по описанию» не бывает вовсе: их пишет только
проход `engineering_model_reader`, через который этот путь не идёт. На стенде
таблица `trace_proposals` пуста целиком — ноль записей во всей базе, — то есть
режим наложения не отрабатывал ни разу ни у кого, а сообщение об этом
читалось как потерянный файл.
"""

from __future__ import annotations

import inspect

from app.api import image_generation


def _endpoint_source() -> str:
    return inspect.getsource(image_generation.get_generation_assertion_source_overlay)


def test_the_two_modes_that_need_no_proposal_skip_the_lookup():
    """`source` и `sheet` обязаны работать всегда — сравнивать им не с чем."""
    source = _endpoint_source()

    assert 'if mode not in {"source", "sheet"}:' in source


def test_the_full_sheet_mode_widens_the_crop_to_the_whole_image():
    crop = inspect.getsource(image_generation._assertion_source_crop)

    assert "if full_sheet:" in crop
    assert "bbox = (0, 0, image.width, image.height)" in crop


def test_the_refusal_names_the_real_reason_not_a_missing_record():
    """«Не найден» отправляло искать запись, которой не бывает по устройству."""
    source = _endpoint_source()

    assert "Наложение недоступно" in source
    assert "гибридной трассировки" in source
    assert "source и sheet" in source
