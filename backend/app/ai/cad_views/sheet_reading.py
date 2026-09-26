"""Этап A: что где на листе — как это видит инженер.

Зрение предлагает области (скопления линий: виды, штамп, таблицы, текст),
модель получает лист с пронумерованными рамками и называет роль каждой рамки:
вид, разрез, сечение, выносной элемент, аксонометрия, основная надпись,
спецификация, технические требования, подпись к другой рамке. Рамки,
относящиеся к одной детали, модель группирует. Геометрию модель не выдаёт —
только смысл; геометрия будет мериться по самим рамкам (этап B).

Никаких правил под класс детали: одинаково для вала, корпуса, сборки, схемы.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Роли рамок. «label» — надпись, относящаяся к другой рамке (обозначение
# вида «А-А», «Б (4:1)», подпись рисунка): её рамка склеивается с той.
ROLES = (
    "view",  # вид (главный, сверху, слева, по стрелке)
    "section",  # разрез
    "cross_section",  # сечение (вынесенное/наложенное)
    "detail_view",  # выносной элемент
    "isometric",  # аксонометрия / 3D-изображение
    "title_block",  # основная надпись (штамп)
    "specification",  # спецификация / перечень / таблица составных частей
    "table",  # прочая таблица (параметры зубчатого колеса и т.п.)
    "notes",  # технические требования, текст
    "label",  # надпись, принадлежащая другой рамке
    "other",
)
SHEET_KINDS = ("detail", "detail_set", "assembly", "scheme", "specification", "text", "other")

PROMPT = (
    "Ты — инженер-конструктор, читаешь технический чертёж. На листе рамками с "
    "номерами обведены области. Для КАЖДОЙ рамки скажи, что в ней:\n"
    "view — вид детали (главный, сверху, слева, по стрелке);\n"
    "section — разрез; cross_section — сечение; detail_view — выносной элемент;\n"
    "isometric — аксонометрия или 3D-изображение;\n"
    "title_block — основная надпись (штамп); specification — спецификация или "
    "таблица составных частей; table — другая таблица (например, параметры "
    "зубчатого венца); notes — технические требования или текст;\n"
    "label — надпись, которая относится к другой рамке (обозначение «А-А», "
    "«Б (4:1)», «Вид В», подпись рисунка) — укажи номер той рамки в of;\n"
    "other — прочее (размерная надпись, оторванная от вида, рамка листа).\n"
    "Для видов, разрезов и сечений дай name — как этот вид назвал бы инженер "
    "(«главный вид», «вид слева», «А-А», «Б (4:1)») — и part — название детали, "
    "к которой он относится (если на листе несколько деталей — разные названия).\n"
    "Также: sheet_kind — detail (чертёж одной детали), detail_set (несколько "
    "деталей), assembly (сборочный чертёж), scheme (схема), specification "
    "(только спецификация), text; и main — номер рамки главного вида "
    "(для сборки — главного изображения).\n"
    "Не придумывай рамок, которых нет. Ответ — ОДНОЙ строкой JSON."
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sheet_kind": {"type": "string", "enum": list(SHEET_KINDS)},
        "main": {"type": ["integer", "null"]},
        "regions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "role": {"type": "string", "enum": list(ROLES)},
                    "name": {"type": ["string", "null"]},
                    "part": {"type": ["string", "null"]},
                    "of": {"type": ["integer", "null"]},
                },
                "required": ["n", "role"],
            },
        },
    },
    "required": ["sheet_kind", "regions"],
}


@dataclass
class Region:
    """Область листа: рамка в пикселях исходного листа и её смысл."""

    n: int
    box: tuple[int, int, int, int]
    role: str = "other"
    name: str | None = None
    part: str | None = None
    of: int | None = None


@dataclass
class SheetReading:
    sheet_kind: str
    main: int | None
    regions: list[Region] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def views(self) -> list[Region]:
        return [
            r for r in self.regions if r.role in ("view", "section", "cross_section", "detail_view")
        ]


def propose_regions(gray: Any) -> list[tuple[int, int, int, int]]:
    """Кандидатные области: скопления линий, штамп, колонка текста.

    Берутся из общей разметки листа (`sheet_layout`); дробление на подписи
    допустимо — модель склеит их ролью ``label``.
    """
    import numpy as np
    from PIL import Image

    from app.ai.cad_recognize.sheet_layout import detect_sheet_layout

    layout = detect_sheet_layout(Image.fromarray(np.asarray(gray)))
    boxes = []
    for box in (layout.title_block, layout.notes_column, *layout.views):
        if box is not None:
            boxes.append((int(box.x0), int(box.y0), int(box.x1), int(box.y1)))
    # Сверху вниз, слева направо — номера читаются как текст.
    boxes.sort(key=lambda b: (b[1] // 200, b[0]))
    return boxes


def marked_overview(gray: Any, boxes: list[tuple[int, int, int, int]], side: int = 1600):
    """Лист с пронумерованными рамками, уменьшенный до ``side`` по большей стороне."""
    import cv2
    import numpy as np
    from PIL import Image

    image = cv2.cvtColor(np.asarray(gray), cv2.COLOR_GRAY2BGR)
    height, width = image.shape[:2]
    scale = min(1.0, side / max(height, width))
    image = cv2.resize(
        image, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA
    )
    thickness = max(2, int(round(side / 500)))
    for number, (x0, y0, x1, y1) in enumerate(boxes, start=1):
        p0 = (int(x0 * scale), int(y0 * scale))
        p1 = (int(x1 * scale), int(y1 * scale))
        cv2.rectangle(image, p0, p1, (0, 0, 255), thickness)
        label = str(number)
        size = 0.6 + side / 2600
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, size, 2)
        cv2.rectangle(image, p0, (p0[0] + tw + 6, p0[1] + th + 8), (0, 0, 255), -1)
        cv2.putText(
            image,
            label,
            (p0[0] + 3, p0[1] + th + 4),
            cv2.FONT_HERSHEY_SIMPLEX,
            size,
            (255, 255, 255),
            2,
        )
    return Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))


def parse_reading(answer: dict[str, Any], boxes: list[tuple[int, int, int, int]]) -> SheetReading:
    """Ответ модели → рамки со смыслом; номера вне списка отбрасываются."""
    kind = answer.get("sheet_kind") if answer.get("sheet_kind") in SHEET_KINDS else "other"
    by_number = {}
    for item in answer.get("regions") or []:
        if not isinstance(item, dict) or not isinstance(item.get("n"), int):
            continue
        if not 1 <= item["n"] <= len(boxes):
            continue
        by_number[item["n"]] = item
    regions = []
    for number, box in enumerate(boxes, start=1):
        item = by_number.get(number) or {}
        role = item.get("role") if item.get("role") in ROLES else "other"
        of = (
            item.get("of")
            if isinstance(item.get("of"), int) and 1 <= item["of"] <= len(boxes)
            else None
        )
        regions.append(
            Region(
                n=number,
                box=box,
                role=role,
                name=item.get("name") or None,
                part=item.get("part") or None,
                of=of,
            )
        )
    main = answer.get("main") if isinstance(answer.get("main"), int) else None
    if main is not None and not 1 <= main <= len(boxes):
        main = None
    return SheetReading(sheet_kind=kind, main=main, regions=regions, raw=answer)


async def read_sheet(gray: Any, *, router: Any = None, confidential: bool = True) -> SheetReading:
    """Роли областей листа одним вопросом модели по пронумерованным рамкам."""
    from app.ai.cad_recognize.spec_fragments import _ask

    if router is None:
        from app.ai.router import ai_router

        router = ai_router
    boxes = propose_regions(gray)
    overview = marked_overview(gray, boxes)
    answer = await _ask(
        PROMPT,
        overview,
        router=router,
        confidential=confidential,
        num_predict=3000,
        schema=SCHEMA,
        timeout_seconds=150.0,
    )
    return parse_reading(answer or {}, boxes)
