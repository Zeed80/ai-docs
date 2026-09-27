"""Тело вращения по полупрофилю вида/разреза — операция ядра `revolve`.

Масштабы раздельные: вдоль оси (линейные надписи) и по радиусу (надписи Ø)
— выпрямленное фото анизотропно (`revolve_profile.fit_axial_scale`).
Расточка — вторым профилем `bore_points`: ядро вращает его и вычитает.
"""

from __future__ import annotations

from typing import Any

from app.ai.cad_views.revolve_profile import HalfProfile


def revolve_points(
    profile: HalfProfile, axial_mm_per_px: float, radial_mm_per_px: float
) -> tuple[list[dict[str, float]], list[dict[str, float]]]:
    """(profile_points, bore_points) в мм: z от левого торца, r от оси."""

    def convert(points: list[tuple[float, float]]) -> list[dict[str, float]]:
        out: list[dict[str, float]] = []
        for x, r in points:
            z = round((x - profile.x0) * axial_mm_per_px, 4)
            radius = round(max(0.0, r) * radial_mm_per_px, 4)
            if out and abs(out[-1]["z"] - z) < 1e-6:
                out[-1]["r"] = radius  # вертикальная грань — две точки на одном z
                out.append({"r": radius, "z": z})
                continue
            out.append({"r": radius, "z": z})
        return out

    outer = convert(profile.outer)
    bore = convert(profile.inner) if profile.inner else []
    if bore:
        # Расточка насквозь: торцы расточки чуть за торцами тела, иначе
        # вычитание оставляет плёнку нулевой толщины.
        length = outer[-1]["z"]
        bore[0]["z"] = min(bore[0]["z"], 0.0) - 0.05
        bore[-1]["z"] = max(bore[-1]["z"], length) + 0.05
        # Стенка не тоньше 0,05 мм: у тонкой стенки замер радиусов шумит.
        for point in bore:
            limit = max((p["r"] for p in outer if abs(p["z"] - point["z"]) <= 0.5), default=None)
            if limit is not None and point["r"] > limit - 0.05:
                point["r"] = max(0.0, limit - 0.05)
    return outer, bore


def revolve_candidate(
    outer: list[dict[str, float]], bore: list[dict[str, float]], label: str
) -> dict[str, Any]:
    params: dict[str, Any] = {"profile_points": outer}
    if bore and any(p["r"] > 0 for p in bore):
        params["bore_points"] = bore
    return {
        "candidate": {
            "features": [{"kind": "revolve", "params": params, "confidence": 0.7}],
            "score": 0.7,
            "label": label,
        },
        "confirm_assumptions": True,
        "metadata": {"source": "cad_views"},
    }
