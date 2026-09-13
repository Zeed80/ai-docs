"""Улучшение грубого листа перед оцифровкой: SeedVR2 через локальный ComfyUI (E17).

Опыт на корпусе с эталоном (план, E17/E17b): лист 75–100 dpi, увеличенный
SeedVR2 до 300 dpi, проверяется почти как настоящий скан 300 dpi (пазы,
поперечные отверстия, фаски, окружность болтов — «не измеримо» → как на
300 dpi), ридер читает не хуже (найдено 61 → 62 из 65 чисел), а подмены цифр
апскейлом не найдено ни на одной подписи. Выдумки ридера на увеличенном листе
остаются — но теперь их опровергает проверка с замером.

Включается только на грубом листе — основная линия тоньше, чем нужно
проверяльщикам (`shaft_profile._MIN_LINE_PX`), та же мера толщины, что у
системы координат вала. Результат принимается только если, уменьшенный
обратно, совпадает с исходником плитка за плиткой: генеративная модель,
перерисовавшая цифру, обрушивает совпадение именно в своей плитке. Любой отказ
(ComfyUI недоступен, нет модели, ошибка, расхождение) — работа с исходником,
как без апскейла. Модели локальные, наружу ничего не уходит.
"""

from __future__ import annotations

import io
import json
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()

# Выход не больше этого по длинной стороне: SeedVR2 7B идёт плитками, но
# время и память растут с площадью (лист A4 при 300 dpi — 3508 px).
MAX_SIDE_PX = 7200
# Свободная видеопамять, при которой SeedVR2 7B int8 помещается без выгрузки
# моделей Ollama (замер: ~9 ГБ на лист A4).
_VRAM_NEEDED = 11 * 1024**3
# Совпадение увеличенного листа с исходником (см. `agreement`). Калибровка
# (`scripts/calibrate_sheet_upscale.py`, корпус v9-sr, 48 честных пар и 46 с
# подменённой подписью размера): 0,75 — отвергнута 1 честная пара, пропущена
# 1 подделка; 0,70 — 0 и 4. Ложный отказ лишь возвращает исходник, пропуск —
# выдуманная цифра, поэтому порог выше.
MIN_TILE_AGREEMENT = 0.75
_TILE_PX = 16
_TILE_MIN_STD = 12.0


@dataclass
class UpscaleResult:
    content: bytes
    applied: bool
    reason: str
    line_px: float = 0.0
    factor: int = 0
    seconds: float = 0.0
    agreement: dict[str, float] = field(default_factory=dict)

    def as_event(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "reason": self.reason,
            "line_px": round(self.line_px, 2),
            "factor": self.factor,
            "seconds": round(self.seconds, 1),
            "agreement": {k: round(v, 3) for k, v in self.agreement.items()},
        }


def main_line_px(gray: Any) -> float:
    """Толщина основной линии листа, px — мера предохранителей проверяльщиков.

    90-й процентиль нижнего квартиля поперечной массы ДЛИННЫХ горизонталей —
    как эталон толщины в `shaft_frame.locate_shaft_views`.
    """
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _ink, _stroke
    from app.ai.cad_recognize.verifiers.shaft_frame import _segments

    gray = np.asarray(gray)
    ink = _ink(gray)
    min_length = max(6, int(round(0.006 * min(gray.shape))))
    lines = _segments(ink, min_length)
    long_lines = [line for line in lines if line.end - line.start >= 4 * min_length]
    if not long_lines:
        return 0.0
    weights = [
        _stroke(gray, ink, line, (line.start, line.end), axis=0, quantile=0.25)
        for line in long_lines
    ]
    return float(np.percentile(weights, 90))


def upscale_factor(line_px: float, shape: tuple[int, int], min_line_px: float) -> int:
    """Во сколько раз увеличить: 0 — не нужно или некуда.

    Как в опыте: 75 dpi (линия ~1,5 px) — ×4, 100 dpi (~2 px) — ×3; выход не
    больше `MAX_SIDE_PX`.
    """
    if line_px <= 0.0 or line_px >= min_line_px:
        return 0
    factor = 4 if line_px < 0.6 * min_line_px else 3
    while factor >= 2 and max(shape) * factor > MAX_SIDE_PX:
        factor -= 1
    return factor if factor >= 2 else 0


def agreement(original: Any, upscaled: Any) -> dict[str, float]:
    """Совпадение увеличенного листа с исходником после уменьшения обратно.

    Корреляция градаций серого по плиткам 16×16 с шагом 8 (плитки без
    рисунка пропускаются). Двоичные маски чернил не годятся: на 75–100 dpi
    цифры — пятна в 8–12 px, и маски разных цифр с допуском в пиксель
    совпадают (калибровка: подмена подписи проходила в 41 случае из 46), а
    крапины, убранные апскейлом, давали нулевые плитки на честных листах.
    Серое различает и размытые цифры. ``worst_tile`` — наихудшая плитка:
    одна перерисованная подпись обрушивает именно её.
    """
    import cv2
    import numpy as np

    original = np.asarray(original, dtype=float)
    height, width = original.shape
    back = cv2.resize(
        np.asarray(upscaled, dtype=np.uint8), (width, height), interpolation=cv2.INTER_AREA
    ).astype(float)
    step = _TILE_PX // 2
    values = []
    for y in range(0, height - _TILE_PX + 1, step):
        for x in range(0, width - _TILE_PX + 1, step):
            a = original[y : y + _TILE_PX, x : x + _TILE_PX]
            b = back[y : y + _TILE_PX, x : x + _TILE_PX]
            if a.std() < _TILE_MIN_STD and b.std() < _TILE_MIN_STD:
                continue
            a = a - a.mean()
            b = b - b.mean()
            denominator = float(np.sqrt((a * a).sum() * (b * b).sum()))
            values.append(float((a * b).sum()) / denominator if denominator > 0 else 0.0)
    if not values:
        return {"worst_tile": 1.0, "tile_p1": 1.0, "tiles": 0.0}
    return {
        "worst_tile": min(values),
        "tile_p1": float(np.percentile(values, 1)),
        "tiles": float(len(values)),
    }


def workflow(image_name: str, scale: int, seed: int = 959948902156062) -> dict:
    """Рабочий процесс оператора: Lanczos ×N → SeedVR2 7B int8, один шаг."""
    tiled = {"tile_size": 512, "overlap": 128, "temporal_size": 4096, "temporal_overlap": 8}
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "cad_upscale/sheet", "images": ["66:59", 0]},
        },
        "66:48": {
            "class_type": "VAEEncodeTiled",
            "inputs": {**tiled, "pixels": ["66:58", 0], "vae": ["66:51", 0]},
        },
        "66:50": {
            "class_type": "JoinImageWithAlpha",
            "inputs": {"image": ["1", 0], "alpha": ["1", 1]},
        },
        "66:51": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": "seedvr2_ema_vae_fp16.safetensors"},
        },
        "66:52": {
            "class_type": "UNETLoader",
            "inputs": {
                "unet_name": "seedvr2_7b_int8_convrot.safetensors",
                "weight_dtype": "default",
            },
        },
        "66:54": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": 1,
                "cfg": 1,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1,
                "model": ["66:52", 0],
                "positive": ["66:61", 0],
                "negative": ["66:61", 1],
                "latent_image": ["66:48", 0],
            },
        },
        "66:55": {
            "class_type": "VAEDecodeTiled",
            "inputs": {**tiled, "samples": ["66:54", 0], "vae": ["66:51", 0]},
        },
        "66:57": {
            "class_type": "ResizeImageMaskNode",
            "inputs": {
                "resize_type": "scale by multiplier",
                "resize_type.multiplier": scale,
                "scale_method": "lanczos",
                "input": ["66:50", 0],
            },
        },
        "66:58": {"class_type": "SeedVR2Preprocess", "inputs": {"resized_images": ["66:57", 0]}},
        "66:59": {
            "class_type": "SeedVR2PostProcessing",
            "inputs": {
                "color_correction_method": "none",
                "images": ["66:55", 0],
                "original_resized_images": ["66:57", 0],
            },
        },
        "66:61": {
            "class_type": "SeedVR2Conditioning",
            "inputs": {"model": ["66:52", 0], "vae_conditioning": ["66:48", 0]},
        },
    }


def _request(
    url: str, *, data: bytes | None = None, headers: dict | None = None, timeout: float = 60
):
    request = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _upload(comfy: str, png: bytes) -> str:
    boundary = uuid.uuid4().hex
    name = f"cad_upscale_{uuid.uuid4().hex[:12]}.png"
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
            f'filename="{name}"\r\nContent-Type: image/png\r\n\r\n'
        ).encode()
        + png
        + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\ntrue'
        f"\r\n--{boundary}--\r\n".encode()
    )
    reply = _request(
        f"{comfy}/upload/image",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    return json.loads(reply)["name"]


def run_comfy_upscale(comfy: str, png: bytes, scale: int, *, timeout_s: float = 600) -> bytes:
    """Увеличить PNG рабочим процессом SeedVR2 в ComfyUI; вернуть PNG результата."""
    started = time.monotonic()
    name = _upload(comfy, png)
    payload = json.dumps({"prompt": workflow(name, scale), "client_id": uuid.uuid4().hex})
    reply = _request(
        f"{comfy}/prompt", data=payload.encode(), headers={"Content-Type": "application/json"}
    )
    prompt_id = json.loads(reply)["prompt_id"]
    while time.monotonic() - started < timeout_s:
        history = json.loads(_request(f"{comfy}/history/{prompt_id}", timeout=30)).get(prompt_id)
        if history and history.get("outputs"):
            image = history["outputs"]["9"]["images"][0]
            query = urllib.parse.urlencode(
                {"filename": image["filename"], "subfolder": image["subfolder"], "type": "output"}
            )
            return _request(f"{comfy}/view?{query}", timeout=120)
        status = (history or {}).get("status") or {}
        if status.get("status_str") == "error":
            raise RuntimeError(json.dumps(status, ensure_ascii=False)[:600])
        time.sleep(2.0)
    raise TimeoutError(f"ComfyUI не ответил за {timeout_s:.0f} с")


def _vram_free(comfy: str) -> int | None:
    try:
        stats = json.loads(_request(f"{comfy}/system_stats", timeout=10))
        devices = stats.get("devices") or []
        return int(devices[0]["vram_free"]) if devices else None
    except Exception:  # noqa: BLE001 — нет ответа — решит сам запуск
        return None


def upscale_sheet(
    content: bytes,
    *,
    comfy_url: str,
    min_line_px: float | None = None,
    timeout_s: float = 600,
) -> UpscaleResult:
    """Увеличить грубый лист; при любом сомнении вернуть исходник с причиной."""
    import numpy as np
    from PIL import Image

    from app.ai.cad_recognize.verifiers.shaft_profile import _MIN_LINE_PX

    threshold = float(min_line_px or _MIN_LINE_PX)
    gray = np.asarray(Image.open(io.BytesIO(content)).convert("L"))
    line_px = main_line_px(gray)
    factor = upscale_factor(line_px, gray.shape, threshold)
    if factor == 0:
        reason = (
            "лист достаточно чёткий"
            if line_px >= threshold
            else "линии листа не найдены или лист слишком велик для увеличения"
        )
        return UpscaleResult(content, False, reason, line_px=line_px)
    comfy = comfy_url.rstrip("/")
    started = time.monotonic()
    from app.ai import gpu_lock

    try:
        free = _vram_free(comfy)
        if free is not None and free < _VRAM_NEEDED:
            # Как перед каждым запуском диффузии в Студии: карта одна, модель
            # ридера занимает её почти целиком (OOM подтверждён 2026-07-05).
            gpu_lock.unload_ollama()
        buffer = io.BytesIO()
        Image.fromarray(gray).save(buffer, format="PNG")
        raw = run_comfy_upscale(comfy, buffer.getvalue(), factor, timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001 — апскейл не должен ронять оцифровку
        logger.warning("cad_upscale_failed", error=str(exc)[:200])
        return UpscaleResult(
            content,
            False,
            f"апскейл недоступен: {str(exc)[:160]}",
            line_px=line_px,
            factor=factor,
            seconds=time.monotonic() - started,
        )
    finally:
        # Ридер идёт следом на той же карте.
        gpu_lock.unload_comfyui()
    upscaled = np.asarray(Image.open(io.BytesIO(raw)).convert("L"))
    seconds = time.monotonic() - started
    scores = agreement(gray, upscaled)
    if scores["worst_tile"] < MIN_TILE_AGREEMENT:
        return UpscaleResult(
            content,
            False,
            (
                "увеличенный лист расходится с исходником "
                f"(худшая плитка {scores['worst_tile']:.2f} < {MIN_TILE_AGREEMENT:g})"
            ),
            line_px=line_px,
            factor=factor,
            seconds=seconds,
            agreement=scores,
        )
    buffer = io.BytesIO()
    Image.fromarray(upscaled).save(buffer, format="PNG")
    return UpscaleResult(
        buffer.getvalue(),
        True,
        f"лист увеличен ×{factor}: линия {line_px:.1f} px",
        line_px=line_px,
        factor=factor,
        seconds=seconds,
        agreement=scores,
    )
