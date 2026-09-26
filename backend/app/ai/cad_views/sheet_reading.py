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
    "Если рамка — лишь часть изображения, которое продолжается в другой рамке, "
    "дай ей ту же роль и в of — номер рамки с основной частью этого изображения.\n"
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


def _frame_lines(ink: Any) -> Any:
    """Линии рамок листа — и вложенных (лист в странице методички, два листа
    на слайде): длинные линии, замкнутые в большой прямоугольник.

    Порог «почти через весь лист» ловил только внешнюю рамку; лист внутри
    страницы оставался одной областью во весь чертёж (методички: 4 листа из
    41). Одиночная длинная линия (контур вала, линия таблицы) рамкой не
    считается — у рамки есть стороны по всем четырём краям.
    """
    import cv2
    import numpy as np

    height, width = ink.shape
    long_h = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (int(width * 0.2), 1))
    )
    long_v = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, int(height * 0.2)))
    )
    lines = (long_h | long_v).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        cv2.dilate(lines, np.ones((7, 7), np.uint8)), 8
    )
    frames = np.zeros_like(lines)
    for index in range(1, count):
        x, y, w, h, _area = stats[index]
        if w * h < 0.08 * width * height:
            continue
        part = (labels[y : y + h, x : x + w] == index) & (lines[y : y + h, x : x + w] > 0)
        band = max(8, int(0.02 * min(w, h)))
        sides = (
            part[:band].any(axis=0).mean(),
            part[-band:].any(axis=0).mean(),
            part[:, :band].any(axis=1).mean(),
            part[:, -band:].any(axis=1).mean(),
        )
        if min(sides) >= 0.7:
            # Только полоса по периметру: длинные кромки вала, касающиеся рамки,
            # иначе снимались вместе с ней, и вид вала пропадал.
            rim = np.zeros_like(part)
            rim[:band], rim[-band:], rim[:, :band], rim[:, -band:] = True, True, True, True
            frames[y : y + h, x : x + w] |= (part & rim).astype(np.uint8)
    # И линии почти через весь лист (рамка без одной стороны, линейка страницы).
    very_h = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (int(width * 0.45), 1))
    )
    very_v = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, int(height * 0.45)))
    )
    return frames | very_h | very_v


def propose_regions(gray: Any) -> list[tuple[int, int, int, int]]:
    """Кандидатные области: изображения — по основным линиям, прочее — отдельно.

    Рамка листа и линии штампа, идущие почти через весь лист, убираются —
    иначе они склеивают главный вид с собой в одну «область» (вал и втулка из
    методички: главного вида среди рамок не было вовсе). Изображения
    собираются по ОСНОВНЫМ линиям: тонкие размерные и выносные тянутся между
    видами и сшивали бы их. Остаток (таблицы, текст, штамп) — своими рамками.
    Дробление одного изображения на части допустимо: модель связывает части
    ссылкой ``of``.
    """
    import cv2
    import numpy as np

    g = np.asarray(gray)
    height, width = g.shape
    threshold, _ = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    ink = (g < min(threshold, 200)).astype(np.uint8)
    work = ink & (1 - cv2.dilate(_frame_lines(ink), np.ones((5, 5), np.uint8)))
    runs: list[int] = []
    for y in range(0, height, max(1, height // 200)):
        edges = np.diff(np.concatenate([[0], work[y], [0]]))
        starts, ends = np.where(edges == 1)[0], np.where(edges == -1)[0]
        runs.extend(int(r) for r in ends - starts if 1 <= r <= 40)
    thin = float(np.percentile(runs, 30)) if runs else 2.0
    thick = float(np.percentile(runs, 85)) if runs else 4.0
    kernel = max(2, int(round((thin + thick) / 2.0)))
    main = cv2.morphologyEx(work, cv2.MORPH_OPEN, np.ones((kernel, kernel), np.uint8))
    gap = max(8, int(0.012 * float(np.hypot(height, width))))

    def clusters(mask: Any, min_share: float) -> list[tuple[int, int, int, int]]:
        grouped = cv2.dilate(mask, np.ones((gap, gap), np.uint8))
        count, _labels, stats, _ = cv2.connectedComponentsWithStats(grouped, 8)
        found = []
        for index in range(1, count):
            x, y, w, h, _area = stats[index]
            if w * h >= min_share * width * height:
                found.append(
                    (
                        int(x + gap // 2),
                        int(y + gap // 2),
                        int(x + w - gap // 2),
                        int(y + h - gap // 2),
                    )
                )
        return found

    pictures = clusters(main, 0.002)
    rest = work.copy()
    for x0, y0, x1, y1 in pictures:
        rest[max(0, y0 - gap) : y1 + gap, max(0, x0 - gap) : x1 + gap] = 0
    boxes = pictures + clusters(rest, 0.004)
    # Сверху вниз, слева направо — номера читаются как текст.
    band = max(1, height // 12)
    boxes.sort(key=lambda b: (b[1] // band, b[0]))
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
    pictures_all = {r.n: r for r in regions if r.role in ("view", "section")}
    if main is not None and main not in pictures_all:
        # Номер не изображения (подпись, штамп): живой лист вала — «main» указал
        # на подпись, а «главный вид» модель назвала у другой рамки.
        main = None
    named = [r.n for r in pictures_all.values() if r.name and "главн" in r.name.lower()]
    if main is None and named:
        main = named[0]
    if main is None and kind == "detail":
        # Чертёж одной детали без названного главного вида (вал и корпус из
        # методичек): по ГОСТ 2.305 главное изображение даёт наибольшее
        # представление о детали — берётся наибольший вид или разрез.
        pictures = [r for r in regions if r.role in ("view", "section")]
        if pictures:
            main = max(pictures, key=lambda r: (r.box[2] - r.box[0]) * (r.box[3] - r.box[1])).n
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
