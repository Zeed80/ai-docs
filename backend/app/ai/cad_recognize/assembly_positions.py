"""Сборочный чертёж: позиции ↔ строки спецификации (план, Ф6/X5).

Сборку по сборочному чертежу в 3D не восстановить (лубрикатор — 21 деталь в
четырёх видах и разрезах), но то, ради чего сборочный чертёж читают, —
состав: какие позиции на нём показаны и что каждая значит по спецификации
(ГОСТ 2.106, ГОСТ 2.109). Номера позиций стоят на полках линий-выносок.

Модель выписывает номера позиций с чертежа и строки спецификации (с того же
листа или с отдельного); связь — по номеру. Несвязанное не прячется: позиция
без строки и строка без позиции — отдельными списками, это и есть то, что
оператору проверять.
"""

from __future__ import annotations

import io
from collections.abc import Awaitable, Callable
from typing import Any

Asker = Callable[[str, Any, dict], Awaitable[dict]]

_POSITIONS_PROMPT = (
    "Это сборочный чертёж. Номера позиций деталей стоят на полках линий-выносок "
    "(короткая горизонтальная черта под числом, от неё тонкая линия к детали). "
    "Перечисли ВСЕ номера позиций, которые видишь на чертеже, — только номера "
    "на полках, не размеры и не надписи разрезов. ОДНОЙ строкой JSON:\n"
    '{"positions":[1,2,3]}\nТолько JSON.'
)
_POSITIONS_SCHEMA = {
    "type": "object",
    "properties": {"positions": {"type": "array", "maxItems": 200, "items": {"type": "integer"}}},
    "required": ["positions"],
}

_ROWS_PROMPT = (
    "Это спецификация сборки (ГОСТ 2.106): таблица с колонками «Поз.», "
    "«Обозначение», «Наименование», «Кол.». Выпиши КАЖДУЮ строку с номером "
    "позиции — разделы «Документация» без позиции пропусти. ОДНОЙ строкой JSON:\n"
    '{"rows":[{"position":1,"designation":"","name":"","quantity":1}]}\n'
    "Кириллицу пиши буквами. Только JSON."
)
_ROWS_SCHEMA = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "maxItems": 200,
            "items": {
                "type": "object",
                "properties": {
                    "position": {"type": ["integer", "null"]},
                    "designation": {"type": ["string", "null"]},
                    "name": {"type": ["string", "null"]},
                    "quantity": {"type": ["number", "null"]},
                },
            },
        }
    },
    "required": ["rows"],
}


def link_positions(positions: list[int], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Связь позиций чертежа со строками спецификации по номеру.

    Повтор номера на чертеже — не ошибка (одна позиция показана на двух
    видах), повтор номера в спецификации — ошибка чтения или листа, и он
    выносится отдельно: связь с ним неоднозначна.
    """
    shown = sorted({int(number) for number in positions if int(number) > 0})
    by_number: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        number = row.get("position")
        if isinstance(number, int) and not isinstance(number, bool) and number > 0:
            by_number.setdefault(number, []).append(row)
    duplicated = sorted(number for number, found in by_number.items() if len(found) > 1)
    linked = [
        {"position": number, **by_number[number][0]}
        for number in shown
        if number in by_number and number not in duplicated
    ]
    return {
        "positions": shown,
        "rows": len(rows),
        "linked": linked,
        "positions_without_row": [number for number in shown if number not in by_number],
        "rows_without_position": sorted(number for number in by_number if number not in shown),
        "duplicated_rows": duplicated,
        "link_rate": round(len(linked) / len(shown), 3) if shown else 0.0,
    }


async def read_assembly(
    drawing: bytes,
    specification: bytes | None = None,
    *,
    ask: Asker | None = None,
) -> dict[str, Any]:
    """Позиции со сборочного чертежа и строки спецификации — со связью.

    ``specification`` — отдельный лист спецификации; без него строки ищутся
    на самом сборочном чертеже (ГОСТ 2.106 допускает спецификацию на листе).
    """
    from PIL import Image

    if ask is None:
        ask = _default_ask
    sheet = Image.open(io.BytesIO(drawing)).convert("RGB")
    table = Image.open(io.BytesIO(specification)).convert("RGB") if specification else sheet
    positions_answer = await ask(_POSITIONS_PROMPT, sheet, _POSITIONS_SCHEMA)
    rows_answer = await ask(_ROWS_PROMPT, table, _ROWS_SCHEMA)
    positions = [
        int(value)
        for value in (positions_answer or {}).get("positions") or []
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    rows = [row for row in (rows_answer or {}).get("rows") or [] if isinstance(row, dict)]
    return {
        **link_positions(positions, rows),
        "specification_source": "отдельный лист" if specification else "сборочный чертёж",
        "rows_read": rows,
    }


async def _default_ask(prompt: str, image: Any, schema: dict) -> dict:
    from app.ai.cad_recognize.spec_fragments import _ask, _overview
    from app.ai.router import ai_router

    return await _ask(
        prompt,
        _overview(image, side=1800),
        router=ai_router,
        confidential=True,
        num_predict=2400,
        schema=schema,
        timeout_seconds=180.0,
    )
