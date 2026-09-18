"""Сборочный чертёж: позиции ↔ строки спецификации (Ф6/X5)."""

from __future__ import annotations

import asyncio
import io

from PIL import Image

from app.ai.cad_recognize.assembly_positions import link_positions, read_assembly


def test_positions_link_to_rows_and_the_rest_is_listed_not_hidden():
    rows = [{"position": n, "name": f"деталь {n}"} for n in range(1, 20)]
    result = link_positions([*range(1, 22), 4, 4], rows)

    # Лубрикатор: на чертеже 1…21, на первом листе спецификации 1…19.
    assert len(result["linked"]) == 19
    assert result["positions_without_row"] == [20, 21]
    assert result["rows_without_position"] == []
    assert result["link_rate"] == round(19 / 21, 3)


def test_a_position_repeated_in_the_specification_is_not_linked_silently():
    rows = [{"position": 3, "name": "Червяк"}, {"position": 3, "name": "Вал"}]
    result = link_positions([3], rows)

    assert result["linked"] == []
    assert result["duplicated_rows"] == [3]


def test_the_specification_can_come_from_a_separate_sheet():
    def png() -> bytes:
        buffer = io.BytesIO()
        Image.new("L", (200, 300), 255).save(buffer, format="PNG")
        return buffer.getvalue()

    seen = []

    async def ask(prompt, image, schema):
        seen.append("positions" if "positions" in schema["properties"] else "rows")
        if "positions" in schema["properties"]:
            return {"positions": [1, 2, 2, 5]}
        return {"rows": [{"position": 1}, {"position": 2}, {"position": 3}]}

    result = asyncio.run(read_assembly(png(), png(), ask=ask))

    assert seen == ["positions", "rows"]
    assert result["specification_source"] == "отдельный лист"
    assert [item["position"] for item in result["linked"]] == [1, 2]
    assert result["positions_without_row"] == [5]
    assert result["rows_without_position"] == [3]
