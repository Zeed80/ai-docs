"""Лестница деградации: тот же лист хуже — и эталон, пересчитанный вместе с ним.

Эксперимент E2 плана ищет порог измеримости каждого проверяльщика: при каком
разрешении и какой грязи он ещё отвечает верно. Для этого один и тот же лист
нужен во всех ступенях, а эталон — в координатах КАЖДОЙ ступени. Эталон,
не пересчитанный вместе с растром, молча мерил бы не то место.

Ступени — разрешение (300 → 75 dpi), размытие, шум, JPEG и фото на столе
(`lora_degrade.simulate_photo`). Для фото эталон переносится гомографией по
точным углам листа, которые возвращает сама имитация.
"""

from __future__ import annotations

import copy
import io
from typing import Any

import numpy as np

# Каждая ступень — от чистого листа в 300 dpi. ``scale`` — доля разрешения.
LADDER: tuple[dict[str, Any], ...] = (
    {"name": "clean-300", "scale": 1.0},
    {"name": "dpi-200", "scale": 200 / 300},
    {"name": "dpi-150", "scale": 150 / 300},
    {"name": "dpi-100", "scale": 100 / 300},
    {"name": "dpi-75", "scale": 75 / 300},
    {"name": "blur-150", "scale": 150 / 300, "blur_sigma": 1.2},
    {"name": "noise-150", "scale": 150 / 300, "noise_sigma": 18.0},
    {"name": "jpeg-150", "scale": 150 / 300, "jpeg_quality": 30},
    {"name": "photo", "photo": True},
)


def step_by_name(name: str) -> dict[str, Any]:
    return next(step for step in LADDER if step["name"] == name)


def degrade(png: bytes, truth: dict, step: dict[str, Any], *, seed: int) -> tuple[bytes, dict]:
    """Ступень лестницы: новый растр и эталон в его координатах."""
    import cv2
    from PIL import Image

    image = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
    rng = np.random.default_rng(seed)
    out_truth = copy.deepcopy(truth)
    out_truth["degradation"] = dict(step)

    if step.get("photo"):
        from app.ai.lora_degrade import simulate_photo

        height, width = image.shape[:2]
        photo, quad = simulate_photo(image, rng)
        source = np.float32([[0, 0], [width, 0], [width, height], [0, height]])
        homography = cv2.getPerspectiveTransform(source, np.float32(quad))
        _map_points(out_truth, lambda points: _apply_homography(homography, points))
        out_truth["image_size_px"] = [int(photo.shape[1]), int(photo.shape[0])]
        # Перспектива делает масштаб неравномерным — одного числа больше нет.
        out_truth["px_per_part_mm"] = None
        out_truth["homography_from_clean"] = homography.tolist()
        return _encode(photo), out_truth

    scale = float(step.get("scale") or 1.0)
    if scale != 1.0:
        height, width = image.shape[:2]
        image = cv2.resize(
            image,
            (max(1, round(width * scale)), max(1, round(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
        _map_points(out_truth, lambda points: points * scale)
        if out_truth.get("px_per_part_mm"):
            out_truth["px_per_part_mm"] = float(out_truth["px_per_part_mm"]) * scale
    if step.get("blur_sigma"):
        image = cv2.GaussianBlur(image, (0, 0), float(step["blur_sigma"]))
    if step.get("noise_sigma"):
        noise = rng.normal(0.0, float(step["noise_sigma"]), image.shape)
        image = np.clip(image.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    out_truth["image_size_px"] = [int(image.shape[1]), int(image.shape[0])]
    if step.get("jpeg_quality"):
        ok, buffer = cv2.imencode(
            ".jpg",
            cv2.cvtColor(image, cv2.COLOR_RGB2BGR),
            [cv2.IMWRITE_JPEG_QUALITY, int(step["jpeg_quality"])],
        )
        if ok:
            image = cv2.cvtColor(cv2.imdecode(buffer, cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)
    return _encode(image), out_truth


def _encode(image: np.ndarray) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.fromarray(image).save(buffer, format="PNG")
    return buffer.getvalue()


def _apply_homography(homography: np.ndarray, points: np.ndarray) -> np.ndarray:
    ones = np.ones((points.shape[0], 1))
    mapped = np.hstack([points, ones]) @ homography.T
    return mapped[:, :2] / mapped[:, 2:3]


def _map_points(truth: dict, transform) -> None:
    """Перенести все координаты эталона.

    Рамка (`bbox_px`) переносится по четырём углам и снова становится
    описанным прямоугольником; при перспективе дополнительно сохраняется сам
    четырёхугольник (`quad_px`) — описанный прямоугольник наклонённой подписи
    шире её самой.
    """
    for label in truth.get("labels") or []:
        _map_bbox(label, transform)
        if isinstance(label.get("label"), dict):
            _map_bbox(label["label"], transform)
        anchors = label.get("anchors_px")
        if anchors:
            label["anchors_px"] = transform(np.asarray(anchors, dtype=float)).round(2).tolist()


def _map_bbox(record: dict, transform) -> None:
    bbox = record.get("bbox_px")
    if not bbox:
        return
    x0, y0, x1, y1 = (float(value) for value in bbox)
    corners = transform(np.asarray([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=float))
    record["bbox_px"] = [
        round(float(corners[:, 0].min()), 2),
        round(float(corners[:, 1].min()), 2),
        round(float(corners[:, 0].max()), 2),
        round(float(corners[:, 1].max()), 2),
    ]
    record["quad_px"] = corners.round(2).tolist()
