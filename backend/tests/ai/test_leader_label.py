"""Полки выносок с номерами позиций (Ф2 `leader_label`)."""

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app.ai.cad_recognize.verifiers.leader_label import position_shelves, position_verdicts


def _sheet() -> np.ndarray:
    """Полка с номером и наклонной выноской к точке на детали — и размерная
    линия со стрелками и числом над ней, которую за полку принимать нельзя."""
    image = Image.new("L", (2400, 1600), 255)
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("DejaVuSans.ttf", 34)
    # Основной контур детали (толстые линии) — эталон толщины основной линии.
    draw.rectangle([300, 700, 1500, 1200], outline=0, width=8)
    # Позиция 7: точка на детали, выноска вверх-вправо, полка, номер над ней.
    draw.ellipse([1180, 880, 1192, 892], outline=0, width=2)
    draw.line([(1186, 886), (1700, 600)], fill=0, width=3)
    draw.line([(1700, 600), (1800, 600)], fill=0, width=3)
    draw.text((1750, 596), "7", fill=0, font=font, anchor="mb")
    # Размерная «40»: выносные вниз, линия со стрелками, число над ней.
    draw.line([(400, 400), (400, 690)], fill=0, width=3)
    draw.line([(500, 400), (500, 690)], fill=0, width=3)
    draw.line([(400, 450), (500, 450)], fill=0, width=3)
    draw.polygon([(400, 450), (430, 440), (430, 460)], fill=0)
    draw.polygon([(500, 450), (470, 440), (470, 460)], fill=0)
    draw.text((450, 446), "40", fill=0, font=font, anchor="mb")
    return np.asarray(image)


def test_a_shelf_with_a_leader_is_found_and_a_dimension_line_is_not():
    shelves = position_shelves(_sheet())

    assert len(shelves) == 1
    x0, x1, y = shelves[0]["shelf"]
    assert abs(x0 - 1700) <= 10 and abs(x1 - 1800) <= 4 and abs(y - 600) <= 3
    assert shelves[0]["leader"]["x"] == shelves[0]["shelf"][0]


def test_a_number_from_a_shelf_adds_a_position_only_when_the_specification_has_it():
    """Номер с полки подтверждает выписанную моделью позицию; не найденная
    полка — «не измеримо»; новая позиция — только со строкой спецификации."""
    on_sheet = [{"position": 3, "bbox_px": [1, 2, 3, 4]}, {"position": 4, "bbox_px": [5, 6, 7, 8]}]

    verdicts = position_verdicts([3, 13], on_sheet, rows_without_position=[])

    assert [(v["path"], v["status"]) for v in verdicts] == [
        ("positions[2]", "confirmed"),
        ("positions[12]", "unmeasurable"),
    ]
    verdicts = position_verdicts([3], on_sheet, rows_without_position=[4])
    assert verdicts[-1]["kind"] == "assembly_position_found"
    assert verdicts[-1]["measured"] == {"position": 4}
