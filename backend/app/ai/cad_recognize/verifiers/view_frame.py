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
    """

    bbox_px: BBox
    mm_per_px: float
    origin_px: tuple[float, float]

    def to_px(self, u_mm: float, v_mm: float) -> tuple[float, float]:
        ox, oy = self.origin_px
        return ox + u_mm / self.mm_per_px, oy - v_mm / self.mm_per_px

    def to_mm(self, x_px: float, y_px: float) -> tuple[float, float]:
        ox, oy = self.origin_px
        return (x_px - ox) * self.mm_per_px, (oy - y_px) * self.mm_per_px

    def roi_px(self, u_mm: float, v_mm: float, radius_mm: float) -> BBox:
        """Квадратная область вокруг точки вида, обрезанная рамкой вида."""
        x, y = self.to_px(u_mm, v_mm)
        r = radius_mm / self.mm_per_px
        x0, y0, x1, y1 = self.bbox_px
        return (max(x0, x - r), max(y0, y - r), min(x1, x + r), min(y1, y + r))
