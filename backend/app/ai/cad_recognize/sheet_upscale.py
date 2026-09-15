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
import math
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
_MAX_FACTOR = 8
# Толщина линии после SeedVR2 — доля простого увеличения (part_02: 4,44 / (7 × 0,77)).
_SR_THINNING = 0.82
# Свободная видеопамять, при которой SeedVR2 7B int8 помещается без выгрузки
# моделей Ollama (замер: ~9 ГБ на лист A4).
_VRAM_NEEDED = 11 * 1024**3
# Совпадение увеличенного листа с исходником (см. `agreement`). Калибровка
# (`scripts/calibrate_sheet_upscale.py`, корпус v9-sr, 48 честных пар и 46 с
# подменённой подписью размера): 0,72 — честных не отвергнуто, пропущено 2
# подделки; честный минимум 0,79; настоящее фото z4-r4 — 0,77. Ложный отказ
# лишь возвращает исходник, пропуск — выдуманная цифра.
MIN_TILE_AGREEMENT = 0.72
# Допуск сдвига плитки при сравнении, px.
_SHIFT_PX = 1
# Больше этой доли несошедшихся плиток — расхождение системное, лист целиком
# не берётся; меньше — несошедшиеся заменяются простым увеличением.
_MAX_PATCHED_SHARE = 0.01
# С этого коэффициента SeedVR2 рисует толстую линию полой (part_02, ×8).
_HOLLOW_FILL_FACTOR = 5
_TILE_PX = 16
_TILE_MIN_STD = 12.0
# Плитка сравнивается, только если у исходника в ней есть чернила: на фото
# SeedVR2 кладёт лёгкую текстуру на ровное поле за листом (после выпрямления),
# и корреляция там ноль при честном листе (живой z4-r4: худшая плитка 0,00).
_TILE_MIN_INK = 8
# Полоса у краёв изображения не сравнивается: SeedVR2 обрабатывает край кадра
# иначе, чем середину (z4-r4: рамка в последних 16 px — 0,68 при честном листе).
_EDGE_PX = 16


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

    Столько, чтобы основная линия дошла до порога, но не меньше ×3 (опыт:
    100 dpi — ×3) и не больше ×8; выход не больше `MAX_SIDE_PX`. SeedVR2
    утончает линию до ~0,82 простого увеличения (живой part_02: лист 600 px,
    линия 0,77 px; ×7 дало 4,44 px при пороге 4,5). Прежнее «не больше ×4»
    давало там 2,7 px — все проверки «не измеримо».
    """
    if line_px <= 0.0 or line_px >= min_line_px:
        return 0
    factor = min(_MAX_FACTOR, max(3, math.ceil(min_line_px / (_SR_THINNING * line_px))))
    while factor >= 2 and max(shape) * factor > MAX_SIDE_PX:
        factor -= 1
    return factor if factor >= 2 else 0


def tile_scores(
    original: Any, upscaled: Any, *, shift_px: int = _SHIFT_PX
) -> list[tuple[float, int, int]]:
    """Совпадение по плиткам: ``(корреляция, x, y)`` в координатах исходника.

    Корреляция градаций серого по плиткам 16×16 с шагом 8 — только там, где у
    исходника есть чернила и есть рисунок, и не у самого края кадра. Двоичные
    маски чернил не годятся: на 75–100 dpi цифры — пятна в 8–12 px, маски
    разных цифр с допуском в пиксель совпадают (подмена подписи проходила в 41
    случае из 46). Сдвиг на пиксель — не подмена: SeedVR2 рисует тонкий штрих
    чётче и на пиксель в сторону (part_01: «(√)» знака шероховатости — 0,44
    без допуска, 0,85 с ним); берётся лучшая корреляция по сдвигам ±``shift_px``.
    Калибровка (корпус v9-sr): честных 0 из 48 отвергнуто, подделок подписи
    пропущено 2 из 46 — как без допуска.
    """
    import cv2
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _ink

    source = np.asarray(original, dtype=np.uint8)
    height, width = source.shape
    back = cv2.resize(
        np.asarray(upscaled, dtype=np.uint8), (width, height), interpolation=cv2.INTER_AREA
    ).astype(float)
    grey = source.astype(float)
    ink = _ink(source)
    step = _TILE_PX // 2
    scores: list[tuple[float, int, int]] = []
    for y in range(_EDGE_PX, height - _TILE_PX - _EDGE_PX + 1, step):
        for x in range(_EDGE_PX, width - _TILE_PX - _EDGE_PX + 1, step):
            if int(ink[y : y + _TILE_PX, x : x + _TILE_PX].sum()) < _TILE_MIN_INK:
                continue
            a = grey[y : y + _TILE_PX, x : x + _TILE_PX]
            if (
                a.std() < _TILE_MIN_STD
                and back[y : y + _TILE_PX, x : x + _TILE_PX].std() < _TILE_MIN_STD
            ):
                continue
            a = a - a.mean()
            best = -1.0
            for dy in range(-shift_px, shift_px + 1):
                for dx in range(-shift_px, shift_px + 1):
                    b = back[y + dy : y + dy + _TILE_PX, x + dx : x + dx + _TILE_PX]
                    b = b - b.mean()
                    denominator = float(np.sqrt((a * a).sum() * (b * b).sum()))
                    best = max(best, float((a * b).sum()) / denominator if denominator > 0 else 0.0)
            scores.append((best, x, y))
    return scores


def agreement(original: Any, upscaled: Any, *, shift_px: int = _SHIFT_PX) -> dict[str, float]:
    """Сводка `tile_scores`: ``worst_tile`` — наихудшая плитка (одна
    перерисованная подпись обрушивает именно её), ``tile_p1`` — 1-й процентиль."""
    import numpy as np

    values = [score for score, _x, _y in tile_scores(original, upscaled, shift_px=shift_px)]
    if not values:
        return {"worst_tile": 1.0, "tile_p1": 1.0, "tiles": 0.0}
    return {
        "worst_tile": min(values),
        "tile_p1": float(np.percentile(values, 1)),
        "tiles": float(len(values)),
    }


def patch_disagreeing(
    original: Any, upscaled: Any, factor: int, *, threshold: float = MIN_TILE_AGREEMENT
) -> tuple[Any, int]:
    """Несошедшиеся плитки — простым увеличением исходника (Lanczos).

    Живой part_01: из 16 802 плиток не сошлись 2 — дата в штампе, которую
    SeedVR2 перерисовал («30.07.2020» → «3000.1000»); весь лист из-за них
    отвергался. Там, где увеличение расходится с исходником, берётся простое
    увеличение: оно размыто, но ничего не выдумывает. Возвращает лист и
    число заменённых плиток.
    """
    import cv2
    import numpy as np

    source = np.asarray(original, dtype=np.uint8)
    result = np.array(upscaled, dtype=np.uint8, copy=True)
    plain = cv2.resize(source, (result.shape[1], result.shape[0]), interpolation=cv2.INTER_LANCZOS4)
    failing = [(x, y) for score, x, y in tile_scores(source, result) if score < threshold]
    margin = _TILE_PX // 2
    for x, y in failing:
        x0, y0 = max(0, (x - margin) * factor), max(0, (y - margin) * factor)
        x1 = min(result.shape[1], (x + _TILE_PX + margin) * factor)
        y1 = min(result.shape[0], (y + _TILE_PX + margin) * factor)
        result[y0:y1, x0:x1] = plain[y0:y1, x0:x1]
    return result, len(failing)


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


def fill_hollow_strokes(upscaled: Any, factor: int) -> Any:
    """Закрыть светлый зазор внутри штриха, нарисованного SeedVR2 «полым».

    Живой part_02 (лист 600 px, ×8): основная линия вышла двумя полосками по
    2–3 px с белым зазором 2 px — разбор вида видел две тонкие линии, у
    ступени не было пары основных кромок, и вал не находился. Серое
    морфологическое открытие 3×3 (минимум, затем максимум) закрывает
    зазоры до 2 px; просветы букв при таком увеличении много шире. Только
    для больших коэффициентов (от ×5): при ×3 полых штрихов не было
    (z4-r4), и лист не трогается.
    """
    import cv2
    import numpy as np

    if factor < _HOLLOW_FILL_FACTOR:
        return upscaled
    return cv2.morphologyEx(
        np.asarray(upscaled, dtype=np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8)
    )


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
    upscaled = fill_hollow_strokes(np.asarray(Image.open(io.BytesIO(raw)).convert("L")), factor)
    seconds = time.monotonic() - started
    scores = agreement(gray, upscaled)
    patched = 0
    if scores["worst_tile"] < MIN_TILE_AGREEMENT:
        candidate, patched = patch_disagreeing(gray, upscaled, factor)
        if patched <= _MAX_PATCHED_SHARE * max(1.0, scores["tiles"]):
            upscaled = candidate
            scores = {**agreement(gray, upscaled), "patched_tiles": float(patched)}
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
        f"лист увеличен ×{factor}: линия {line_px:.1f} px"
        + (f"; {patched} плиток расходились — там простое увеличение" if patched else ""),
        line_px=line_px,
        factor=factor,
        seconds=seconds,
        agreement=scores,
    )
