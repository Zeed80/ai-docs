"""Система координат вида: миллиметры детали ↔ пиксели листа."""

from __future__ import annotations

from dataclasses import dataclass

from app.ai.cad_recognize.verifiers.contract import BBox


@dataclass(frozen=True)
class ViewFrame:
    """Где вид стоит на листе и в каком он масштабе.

    ``origin_px`` — пиксель начала координат вида (у вала — левый торец на
    оси, у пластины — её левый нижний угол). ``mm_per_px`` — масштаб ИМЕННО
    этого вида: у выносного элемента 5:1 он свой. На листе ось y идёт вниз, а
    у вида v — вверх, отсюда минус в переводе.

    ``mm_per_px`` — масштаб по u; ``mm_per_px_v`` — по v, если он другой.
    Выпрямленное фото листа не изотропно: размер выхода оценивается по
    сторонам четырёхугольника, и перспектива сжимает одну ось сильнее
    (plate-3 корпуса v7: 0,222 мм/px по x и 0,215 по y). Один масштаб на обе
    оси сдвигал верх плана на 16 px.
    """

    bbox_px: BBox
    mm_per_px: float
    origin_px: tuple[float, float]
    mm_per_px_v: float | None = None

    @property
    def scale_v(self) -> float:
        return self.mm_per_px_v or self.mm_per_px

    @property
    def scale_mean(self) -> float:
        """Средний масштаб — для длин без направления (радиус окружности)."""
        return (self.mm_per_px * self.scale_v) ** 0.5

    def to_px(self, u_mm: float, v_mm: float) -> tuple[float, float]:
        ox, oy = self.origin_px
        return ox + u_mm / self.mm_per_px, oy - v_mm / self.scale_v

    def to_mm(self, x_px: float, y_px: float) -> tuple[float, float]:
        ox, oy = self.origin_px
        return (x_px - ox) * self.mm_per_px, (oy - y_px) * self.scale_v

    def roi_px(self, u_mm: float, v_mm: float, radius_mm: float) -> BBox:
        """Квадратная область вокруг точки вида, обрезанная рамкой вида."""
        x, y = self.to_px(u_mm, v_mm)
        r = radius_mm / self.scale_mean
        x0, y0, x1, y1 = self.bbox_px
        return (max(x0, x - r), max(y0, y - r), min(x1, x + r), min(y1, y + r))
