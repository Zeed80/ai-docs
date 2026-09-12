"""Система координат плана пластины — по самому листу, без эталона.

Проверяльщику отверстий нужен `ViewFrame`: где на листе левый нижний угол
плана и сколько миллиметров в пикселе. Харнесс брал его из эталона, и на фото
это давало 10 % верных вердиктов: эталон после выпрямления лежит неточно.

Ширину и высоту пластины ридер читает надёжно (базовая линия v2: 100 %), а
контур плана — прямоугольник, пусть и со скруглёнными углами. Поэтому здесь
ищутся две горизонтальные и две вертикальные линии, которые образуют
прямоугольник с отношением сторон ширина/высота. Отношение и отсекает всё
остальное: вид сбоку стоит на тех же строках, но у́же; размерная линия
ширины даёт с кромкой пару другой высоты; выносные, продолжающие кромки,
только удлиняют их и не мешают.

Линии берутся по середине полосы чернил — как у размерных линий эталона, где
точка привязки лежит на кромке. Пороги — в долях размера листа и вида, а не в
пикселях одного разрешения.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Any

from app.ai.cad_recognize.verifiers.view_frame import ViewFrame

# Допуск отношения сторон — фильтр, а не выбор (выбирает толщина линии).
# Широкий из-за фото: после выпрямления оси сжаты по-разному — на корпусе
# v7 до 13 % (plate-6@photo: план 343×244 px при 80×50 мм), и с узким
# допуском настоящий контур отсеивался, а проходил чужой.
_ASPECT_TOLERANCE = 0.2
# Доля стороны, покрытая линией: скругления углов укорачивают кромку на 2R.
_MIN_SIDE_COVERAGE = 0.5
# Доля большей стороны листа, меньше которой план не бывает.
_MIN_EXTENT = 0.12


@dataclass(frozen=True)
class _Line:
    position: float  # y горизонтальной / x вертикальной линии
    start: float
    end: float

    def overlap(self, low: float, high: float) -> float:
        return max(0.0, min(self.end, high) - max(self.start, low))


def locate_plate_frame(sheet: Any, width_mm: float, height_mm: float) -> ViewFrame | None:
    """Прямоугольник плана ``width_mm × height_mm`` на листе; ``None`` — не найден.

    ``sheet`` — серое изображение (numpy, 0 — чернила).
    """
    import numpy as np

    if not width_mm or not height_mm or width_mm <= 0 or height_mm <= 0:
        return None
    gray = np.asarray(sheet)
    ink = _ink(gray)
    min_length = max(10, int(round(0.02 * min(gray.shape))))
    # План — главный вид, а не штрих: большая сторона не меньше этой доли
    # листа (plate-19 при 75 dpi: прямоугольник 45 px из штрихов подписей).
    min_extent = _MIN_EXTENT * max(gray.shape)
    horizontal = _lines(ink, min_length, axis=0)
    vertical = _lines(ink, min_length, axis=1)

    # Пары вертикалей, отсортированные по расстоянию, — для поиска по ширине.
    pairs = sorted(
        (
            (b.position - a.position, a, b)
            for i, a in enumerate(vertical)
            for b in vertical[i + 1 :]
            if b.position - a.position >= 2 * min_length
        ),
        key=lambda item: item[0],
    )
    spans = [item[0] for item in pairs]

    scored = []
    for candidate in _candidates(
        horizontal, pairs, spans, width_mm, height_mm, min_length, min_extent
    ):
        mismatch, cover, top, bottom, left, right = candidate
        side_x = (left.position, right.position)
        side_y = (top.position, bottom.position)
        # Отношение сторон — только фильтр. Размерные и выносные линии тоже
        # складываются в прямоугольник нужной пропорции (plate-8: размер 200
        # над планом, кромка и две выносные — ровно 2:1), и «точнее по
        # пропорции» выбирало его. Контур детали отличает толщина: основная
        # линия вдвое толще тонкой (ГОСТ 2.303) — берётся самая тонкая сторона.
        weight = min(
            _stroke(gray, ink, top, side_x, axis=0),
            _stroke(gray, ink, bottom, side_x, axis=0),
            _stroke(gray, ink, left, side_y, axis=1),
            _stroke(gray, ink, right, side_y, axis=1),
        )
        scored.append((weight, mismatch, cover, top, bottom, left, right))
    if not scored:
        return None
    # Толщина — в долях самой толстой найденной: у контура все четыре стороны
    # основные (≈ 1), у прямоугольника с размерной или выносной — ≈ 0,5.
    heaviest = max(item[0] for item in scored) or 1.0
    best = min(
        scored,
        key=lambda item: (-round(item[0] / heaviest, 1), round(item[1], 3), -item[2]),
    )
    top, bottom, left, right = best[3:]
    dx = right.position - left.position
    dy = bottom.position - top.position
    margin = max(3.0, 0.01 * dy)
    return ViewFrame(
        bbox_px=(
            left.position - margin,
            top.position - margin,
            right.position + margin,
            bottom.position + margin,
        ),
        mm_per_px=width_mm / dx,
        origin_px=(left.position, bottom.position),
        mm_per_px_v=height_mm / dy,
    )


def _candidates(horizontal, pairs, spans, width_mm, height_mm, min_length, min_extent):
    """Прямоугольники из двух горизонталей и двух вертикалей с пропорцией плана."""
    for i, top in enumerate(horizontal):
        for bottom in horizontal[i + 1 :]:
            dy = bottom.position - top.position
            if dy < 2 * min_length:
                continue
            expected = width_mm * dy / height_mm
            low = bisect.bisect_left(spans, expected * (1 - _ASPECT_TOLERANCE))
            high = bisect.bisect_right(spans, expected * (1 + _ASPECT_TOLERANCE))
            for dx, left, right in pairs[low:high]:
                if max(dx, dy) < min_extent:
                    continue
                side_x = (left.position, right.position)
                side_y = (top.position, bottom.position)
                cover = min(
                    top.overlap(*side_x) / dx,
                    bottom.overlap(*side_x) / dx,
                    left.overlap(*side_y) / dy,
                    right.overlap(*side_y) / dy,
                )
                if cover >= _MIN_SIDE_COVERAGE:
                    yield abs(dx / expected - 1.0), cover, top, bottom, left, right


_STROKE_SAMPLES = 60
_STROKE_REACH = 25


def _stroke(gray: Any, ink: Any, line: _Line, span: tuple[float, float], *, axis: int) -> float:
    """Толщина линии — медиана поперечной массы затемнения на стороне.

    Масса, а не длина прогона чернил: при 75 dpi (сглаженное ужатие с 300)
    и основная, и тонкая линия занимают по 1–2 пикселя, но основная — два
    пикселя по 64, тонкая — 128 и 191; масса (≈ 1,5 против ≈ 0,75 px)
    толщину сохраняет, длина прогона — нет. Медиана, а не среднее: сторону
    пересекают выносные, к ней примыкают стрелки — это несколько отсчётов
    из многих.
    """
    import numpy as np

    low, high = max(span[0], line.start), min(span[1], line.end)
    if high <= low:
        return 0.0
    centre = int(round(line.position))
    limit = ink.shape[0] if axis == 0 else ink.shape[1]
    runs = []
    for along in np.linspace(low, high, _STROKE_SAMPLES):
        a = int(round(along))
        lo, hi = max(0, centre - _STROKE_REACH), min(limit, centre + _STROKE_REACH + 1)
        cut = ink[lo:hi, a] if axis == 0 else ink[a, lo:hi]
        middle = centre - lo
        # Остаточный наклон фото: центр полосы может мазать на пиксель.
        hit = next(
            (
                middle + d
                for d in (0, -1, 1, -2, 2)
                if 0 <= middle + d < cut.size and cut[middle + d]
            ),
            None,
        )
        if hit is None:
            runs.append(0)
            continue
        up = hit
        while up > 0 and cut[up - 1]:
            up -= 1
        down = hit
        while down < cut.size - 1 and cut[down + 1]:
            down += 1
        # Масса полосы с полями по пикселю (сглаженные края), от местного фона.
        profile = (gray[lo:hi, a] if axis == 0 else gray[a, lo:hi]).astype(float)
        background = float(np.percentile(profile, 90))
        window = profile[max(0, up - 1) : down + 2]
        runs.append(float(np.clip(background - window, 0.0, None).sum()) / max(background, 1.0))
    return float(np.median(runs))


def _ink(gray: Any) -> Any:
    """Чернила по местному фону — тот же порог, что у размерных линий (фото)."""
    import numpy as np
    from PIL import Image

    from app.ai.cad_recognize.axial_dimensions import _ink_rows

    return np.asarray(_ink_rows(Image.fromarray(np.ascontiguousarray(gray, dtype=np.uint8))))


def _lines(ink: Any, min_length: int, *, axis: int) -> list[_Line]:
    """Прямые вдоль оси: ``axis=0`` — горизонтальные, ``axis=1`` — вертикальные.

    Полоса слегка утолщается поперёк (остаточный наклон фото рвёт линию в
    1 px на куски), затем размыкание длинным ядром оставляет только то, что
    тянется не меньше ``min_length``: стрелки, цифры и окружности уходят.
    Положение линии — центр тяжести оставшихся пикселей полосы.
    """
    import cv2
    import numpy as np

    mask = ink.astype(np.uint8)
    if axis == 0:
        thick = cv2.dilate(mask, np.ones((3, 1), np.uint8))
        opened = cv2.morphologyEx(thick, cv2.MORPH_OPEN, np.ones((1, min_length), np.uint8))
    else:
        thick = cv2.dilate(mask, np.ones((1, 3), np.uint8))
        opened = cv2.morphologyEx(thick, cv2.MORPH_OPEN, np.ones((min_length, 1), np.uint8))
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(opened, connectivity=8)
    lines = []
    for index in range(1, count):
        x, y, w, h, _area = stats[index]
        if axis == 0:
            lines.append(_Line(float(centroids[index][1]), float(x), float(x + w - 1)))
        else:
            lines.append(_Line(float(centroids[index][0]), float(y), float(y + h - 1)))
    return sorted(lines, key=lambda line: line.position)
