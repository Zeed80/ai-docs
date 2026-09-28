"""Смысл надписи чертежа по ЕСКД / ISO — одно место для всех правил.

Раньше эти правила были разбросаны по ридеру и согласованию и открывались по
одному на живых листах: «Ø» — диаметр; размер лыски «поперёк» пишут без Ø;
«гл.» — глубина; обозначение документа («ПТС 170.10.03.008») — не размер;
квалитет с заглавной буквой (H10) — у отверстия, со строчной (h9) — у вала.
Последнее отвечает на вопрос «наружная это поверхность или внутренняя»,
который модель на «Опоре пружин» решила неверно.

Разбор чисто текстовый: к какой геометрии относится надпись, решает этап C
по положению надписи на листе.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_NUM = r"(\d+(?:[.,]\d+)?)"
_DIAMETER_MARK = r"[ØøΦφФ⌀]"
# Поле допуска: буква(ы) основного отклонения и квалитет (H7, h9, js6, C11).
_FIT = r"(?P<fit>(?:[A-Z]{1,2}|[a-z]{1,2})\d{1,2})"
_DESIGNATION = re.compile(r"\b\d+(?:[.,]\d+){2,}\b")


@dataclass
class Label:
    """Разобранная надпись. ``kind`` — что это за величина."""

    text: str
    kind: str  # diameter|radius|thread|chamfer|angle|linear|sphere|square|thickness|pcd|count|roughness|designation|text
    value: float | None = None
    fit: str | None = None
    surface: str | None = None  # "hole" (H, внутренняя) | "shaft" (h, наружная) | None
    depth: float | None = None  # «гл. N»
    count: int | None = None  # «4 отв.», «2 фаски», «n×»
    pitch: float | None = None  # резьба «M10x0,5»
    angle: float | None = None  # фаска «1×45°»
    tolerance: tuple[float, float] | None = None  # (+0,058; 0)
    extras: dict[str, str] = field(default_factory=dict)


def _num(text: str) -> float:
    return float(text.replace(",", "."))


def _surface(fit: str | None) -> str | None:
    if not fit:
        return None
    letters = re.match(r"[A-Za-z]+", fit).group()
    if letters.lower() == "js" or letters == "JS":
        return "hole" if letters.isupper() else "shaft"
    return "hole" if letters[0].isupper() else "shaft"


def _tolerance(text: str) -> tuple[float, float] | None:
    """«(+0,058)», «+0,12 / +0,06», «-0,1» — предельные отклонения."""
    found = [
        _num(m)
        for m in re.findall(r"([+\-−]\s*\d+(?:[.,]\d+)?)", text.replace("−", "-").replace(" ", ""))
    ]
    if not found:
        return None
    if len(found) == 1:
        return (found[0], 0.0) if found[0] > 0 else (0.0, found[0])
    return (max(found[:2]), min(found[:2]))


def parse_label(text: str) -> Label:
    """Надпись → смысл. Неразобранное — ``kind='text'``, число не выдумывается."""
    raw = str(text or "").strip()
    clean = raw.replace("×", "x").replace("х", "x").replace("Х", "x")
    if not clean:
        return Label(raw, "text")
    if _DESIGNATION.search(clean) and not re.search(_DIAMETER_MARK, clean):
        return Label(raw, "designation")
    # Шероховатость: Ra 3,2 / Rz 20.
    match = re.search(r"\bR([az])\s*" + _NUM, clean)
    if match:
        return Label(
            raw, "roughness", value=_num(match.group(2)), extras={"param": "R" + match.group(1)}
        )
    count = None
    match = re.match(r"^\s*(\d+)\s*(?:отв\.?|отверст\w*|holes?|x|фаск\w*|паз\w*)\s*", clean, re.I)
    if match and re.search(
        _DIAMETER_MARK + r"|M\d|фаск|паз|R\d", clean[match.end() - 1 :] + clean, re.I
    ):
        count = int(match.group(1))
        clean = clean[match.end() :]
    depth = None
    match = re.search(r"гл\.?\s*" + _NUM, clean)
    if match:
        depth = _num(match.group(1))
        clean = clean[: match.start()] + clean[match.end() :]
    # Резьба: M10x0,5-6g, M24x1,5, G1/2.
    match = re.search(r"(?<![A-Za-zА-Яа-я])[MМ]\s*" + _NUM + r"(?:\s*x\s*" + _NUM + r")?", clean)
    if match:
        return Label(
            raw,
            "thread",
            value=_num(match.group(1)),
            pitch=_num(match.group(2)) if match.group(2) else None,
            depth=depth,
            count=count,
        )
    # Фаска: 1x45°, 0,5x45°.
    match = re.search(_NUM + r"\s*x\s*" + _NUM + r"\s*°", clean)
    if match:
        return Label(
            raw, "chamfer", value=_num(match.group(1)), angle=_num(match.group(2)), count=count
        )
    # Сфера: SØ20, Сфера R10.
    match = re.search(r"(?:S|Сфера\s*)" + r"(?:" + _DIAMETER_MARK + r"|R)\s*" + _NUM, clean)
    if match:
        return Label(raw, "sphere", value=_num(match.group(1)))
    # Диаметр: Ø8,5H10(+0,058), φ25, Ø30 k6.
    match = re.search(_DIAMETER_MARK + r"\s*" + _NUM + r"\s*" + r"(?:" + _FIT + r")?", clean)
    if match:
        fit = match.group("fit")
        surface = _surface(fit)
        # Выноска отверстия: «Ø6 120°» (угол радиального отверстия на
        # сечении) или «Ø4 гл.11.9» — отверстие, не ступень вала (искажённое
        # «Ø61 20°» объясняло след секущей как бурт Ø61, многоосевой вал 5).
        angle_match = re.search(_NUM + r"\s*°", clean[match.end() :])
        if surface is None and (angle_match or depth is not None):
            surface = "hole"
        return Label(
            raw,
            "diameter",
            value=_num(match.group(1)),
            fit=fit,
            surface=surface,
            depth=depth,
            count=count,
            angle=_num(angle_match.group(1)) if angle_match else None,
            tolerance=_tolerance(clean[match.end() :]),
        )
    # Окружность центров отверстий: «PCD 200», «Ø200 окр. центров».
    match = re.fullmatch(r"\s*(?:PCD|Pcd)\s*" + _NUM + r"\s*", clean)
    if match:
        return Label(raw, "pcd", value=_num(match.group(1)))
    # Толщина плоской детали (ЕСКД): «s3», «s 2,5*» — вместо второго вида.
    match = re.fullmatch(r"\s*[sS]\s*" + _NUM + r"\s*\*?\s*", clean)
    if match:
        return Label(raw, "thickness", value=_num(match.group(1)))
    # Квадрат: □20.
    match = re.search(r"[□◻]\s*" + _NUM, clean)
    if match:
        return Label(raw, "square", value=_num(match.group(1)))
    # Радиус: R0,3*, R16.
    match = re.search(r"(?<![A-Za-z])R\s*" + _NUM, clean)
    if match:
        return Label(raw, "radius", value=_num(match.group(1)), count=count)
    # Угол: 18°, 45°.
    match = re.fullmatch(r"\s*" + _NUM + r"\s*°\s*(?:[±+\-].*)?", clean)
    if match:
        return Label(raw, "angle", value=_num(match.group(1)))
    # Линейный: 29, 21,2-0,1, 6,7+0,1, 50h7 (вал без Ø), 3,4+0,1.
    match = re.fullmatch(r"\s*\*?" + _NUM + r"\s*\*?\s*(?:" + _FIT + r")?\s*(?P<rest>.*)", clean)
    if match and not re.search(r"[A-Za-zА-Яа-я]{3,}", match.group("rest") or ""):
        fit = match.group("fit")
        surface = _surface(fit)
        # Квалитет «50h7» без Ø — это диаметр вала; «470 h14» — грубый линейный.
        if fit and re.search(r"\d", fit) and int(re.search(r"\d+", fit).group()) <= 9:
            return Label(raw, "diameter", value=_num(match.group(1)), fit=fit, surface=surface)
        return Label(
            raw,
            "linear",
            value=_num(match.group(1)),
            fit=fit,
            surface=surface,
            tolerance=_tolerance(match.group("rest") or ""),
            depth=depth,
            count=count,
        )
    return Label(raw, "text", depth=depth, count=count)
