"""Точечная перерисовка спорного места листа FLUX.2 dev (план, E30b).

Проверка по листу отвечает «не измеримо», когда фигура найдена, но её
линии слиплись (хорда мелкой лыски с дугой и выносной) или закрыты линиями
размеров (канал отверстия на сечении). Генеративная перерисовка всего листа
для замера не годится — кромки дрейфуют нежёстко до ±3,5 px (E30b), — но
вырез вокруг спорного места FLUX.2 перерисовывает тонкими линиями и
слипшееся в половине случаев разводит верно.

Поэтому перерисовка — гипотеза, как ответ модели, и принимается только
проверкой:

1. два сида перерисовывают один вырез;
2. каждая перерисовка не выдумывает линий: её чернила лежат в чернилах
   исходника (расплывшаяся линия исходника накрывает тонкую линию
   перерисовки, выдуманная — нет);
3. оба варианта, вклеенные в лист, дают спорному элементу один вердикт и
   один замер (в допуске элемента);
4. ни один вердикт, уже измеренный по исходнику в этом вырезе, не меняется.

Принятый вырез вклеивается в копию листа для проверок; лист для ридера и
показа остаётся исходным.
"""

from __future__ import annotations

import io
import json
import time
import urllib.parse
import urllib.request
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Сиды — подобраны на корпусе спорных вырезов (см. журнал плана, E30c).
DEFAULT_SEEDS = (1, 2)
# Вырезов на лист: ~60 с на вырез и сид (FLUX.2 dev 20 шагов, 0,15 Мп).
DEFAULT_BUDGET = 3
# Перерисовка не выдумывает: доля её осей в чернилах исходника.
_MIN_PRECISION = 0.97
# Сторона выреза, px: FLUX.2 работает кратно 16; меньше — мало контекста.
# Промпт — «только геометрия»: просьба сохранить размеры на малом вырезе
# заставляла FLUX дорисовывать десятки выдуманных размеров (E30c); числа
# берутся с исходника, перерисовке они не нужны.
_MIN_SIDE = 256

_MODEL = "flux2_dev_fp8mixed.safetensors"
_CLIP = "mistral_3_small_flux2_bf16.safetensors"
_VAE = "full_encoder_small_decoder.safetensors"
_STEPS = 20
_GUIDANCE = 4.0

PROMPT = "\n\n".join(
    [
        "Clean up this fragment of a scanned technical drawing. This is a restoration task only: the fragment is already complete.",
        "Keep ONLY the geometry of the part exactly as it is in the source: visible outlines, section outlines, hatching, circles, arcs and holes, each line exactly in its original position. Where two parallel lines have merged into one thick line, draw them as two separate thin lines in their original positions.",
        "Remove everything that is not the part: dimension lines, extension lines, arrows, leader lines, numbers, text and symbols. Leave their place pure white.",
        "Do NOT add anything: no new lines, no dimensions, no text, no details, no frames. Do not complete, extend or continue the drawing beyond what is visible. Do not move, resize or redesign anything.",
        "Output: pure white background, sharp thin pure black lines, the same framing as the input.",
    ]
)


def flux2_graph(image_name: str, megapixels: float, seed: int, prompt: str | None = None) -> dict:
    """API-граф ComfyUI: FLUX.2 dev, опорный латент из выреза (как воркфлоу
    Студии «Очистка (Flux.2 dev — качество, 20 шагов)»)."""
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "2": {
            "class_type": "ImageScaleToTotalPixels",
            "inputs": {
                "image": ["1", 0],
                "upscale_method": "lanczos",
                "megapixels": round(megapixels, 3),
                "resolution_steps": 1,
            },
        },
        "3": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": _MODEL, "weight_dtype": "default"},
        },
        "4": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": _CLIP, "type": "flux2", "device": "default"},
        },
        "5": {"class_type": "VAELoader", "inputs": {"vae_name": _VAE}},
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt or PROMPT, "clip": ["4", 0]},
        },
        "7": {
            "class_type": "FluxGuidance",
            "inputs": {"guidance": _GUIDANCE, "conditioning": ["6", 0]},
        },
        "8": {"class_type": "VAEEncode", "inputs": {"pixels": ["2", 0], "vae": ["5", 0]}},
        "9": {
            "class_type": "ReferenceLatent",
            "inputs": {"conditioning": ["7", 0], "latent": ["8", 0]},
        },
        "10": {"class_type": "GetImageSize", "inputs": {"image": ["2", 0]}},
        "11": {
            "class_type": "EmptyFlux2LatentImage",
            "inputs": {"width": ["10", 0], "height": ["10", 1], "batch_size": 1},
        },
        "12": {
            "class_type": "Flux2Scheduler",
            "inputs": {"steps": _STEPS, "width": ["10", 0], "height": ["10", 1]},
        },
        "13": {
            "class_type": "BasicGuider",
            "inputs": {"model": ["3", 0], "conditioning": ["9", 0]},
        },
        "14": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "15": {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed)}},
        "16": {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["15", 0],
                "guider": ["13", 0],
                "sampler": ["14", 0],
                "sigmas": ["12", 0],
                "latent_image": ["11", 0],
            },
        },
        "17": {"class_type": "VAEDecode", "inputs": {"samples": ["16", 0], "vae": ["5", 0]}},
        "18": {
            "class_type": "SaveImage",
            "inputs": {"images": ["17", 0], "filename_prefix": "cad_redraw"},
        },
    }


def run_redraw(comfy: str, crop: Any, seed: int, *, timeout_s: float = 600) -> Any:
    """Перерисовать вырез (серый массив) — серый массив того же размера."""
    import numpy as np
    from PIL import Image

    from app.ai.cad_recognize.sheet_upscale import _request, _upload

    height, width = crop.shape
    buffer = io.BytesIO()
    Image.fromarray(np.asarray(crop, dtype=np.uint8)).save(buffer, format="PNG")
    name = _upload(comfy, buffer.getvalue())
    graph = flux2_graph(name, width * height / 1e6, seed)
    reply = _request(
        comfy + "/prompt",
        data=json.dumps({"prompt": graph}).encode(),
        headers={"Content-Type": "application/json"},
    )
    prompt_id = json.loads(reply)["prompt_id"]
    started = time.monotonic()
    while True:
        history = json.loads(_request(comfy + f"/history/{prompt_id}")).get(prompt_id) or {}
        if history.get("outputs") or (history.get("status") or {}).get("status_str") == "error":
            break
        if time.monotonic() - started > timeout_s:
            raise TimeoutError(f"перерисовка не закончилась за {timeout_s:g} с")
        time.sleep(1.0)
    images = [
        image for out in (history.get("outputs") or {}).values() for image in out.get("images", [])
    ]
    if not images:
        raise RuntimeError(f"перерисовка без результата: {json.dumps(history.get('status'))[:300]}")
    query = urllib.parse.urlencode(
        {key: images[0][key] for key in ("filename", "subfolder", "type")}
    )
    with urllib.request.urlopen(f"{comfy}/view?{query}", timeout=60) as response:
        data = response.read()
    result = Image.open(io.BytesIO(data)).convert("L").resize((width, height), Image.LANCZOS)
    return np.asarray(result)


def precision(source: Any, redrawn: Any, line_px: float) -> float:
    """Доля осей перерисовки, лежащих в чернилах исходника (с допуском в полтолщины)."""
    import cv2
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _ink

    source_ink = _ink(np.asarray(source)).astype(np.uint8)
    drawn = _ink(np.asarray(redrawn)).astype(np.uint8)
    if not drawn.any():
        return 0.0
    skeleton = cv2.ximgproc.thinning(drawn * 255) > 0
    distance = cv2.distanceTransform((1 - source_ink).astype(np.uint8), cv2.DIST_L2, 5)
    tolerance = max(2.0, 0.5 * line_px + 1.0)
    return float((distance[skeleton] <= tolerance).mean())


def _box(item: dict[str, Any], shape: tuple[int, int]) -> tuple[int, int, int, int] | None:
    box = item.get("redraw_box")
    if not box or len(box) != 4:
        return None
    x0, y0, x1, y1 = (float(v) for v in box)
    side = max(_MIN_SIDE, x1 - x0, y1 - y0)
    side = int(-(-side // 16) * 16)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    left = int(max(0, min(shape[1] - side, cx - side / 2.0)))
    top = int(max(0, min(shape[0] - side, cy - side / 2.0)))
    if side > min(shape):
        return None
    return left, top, left + side, top + side


def _inside(item: dict[str, Any], box: tuple[int, int, int, int]) -> bool:
    evidence = item.get("evidence_bbox_px")
    if not evidence or len(evidence) != 4:
        return False
    cx, cy = (evidence[0] + evidence[2]) / 2.0, (evidence[1] + evidence[3]) / 2.0
    return box[0] <= cx <= box[2] and box[1] <= cy <= box[3]


def _png(gray: Any) -> bytes:
    import numpy as np
    from PIL import Image

    buffer = io.BytesIO()
    Image.fromarray(np.asarray(gray, dtype=np.uint8)).save(buffer, format="PNG")
    return buffer.getvalue()


def _same_measure(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Два варианта мерят одно: каждое общее число — в допуске элемента."""
    tolerance = max([float(v) for v in (a.get("tolerance_mm") or {}).values()] or [0.5])
    for key in set(a.get("measured") or {}) & set(b.get("measured") or {}):
        va, vb = a["measured"][key], b["measured"][key]
        if not isinstance(va, (int, float)) or not isinstance(vb, (int, float)):
            continue
        limit = 6.0 if key.endswith("_deg") else tolerance
        if abs(float(va) - float(vb)) > limit:
            return False
    return True


def patch_disputed(
    content: bytes,
    spec: dict[str, Any],
    verification: dict[str, Any],
    *,
    comfy_url: str,
    seeds: tuple[int, int] = DEFAULT_SEEDS,
    budget: int = DEFAULT_BUDGET,
    timeout_s: float = 600,
    redraw: Any = None,
    verify: Any = None,
) -> tuple[bytes, list[dict[str, Any]]]:
    """Перерисовать спорные места и вклеить принятые; лист для проверок и журнал.

    ``redraw(crop, seed) -> crop`` и ``verify(png, spec) -> отчёт`` — для
    тестов; по умолчанию FLUX.2 в ComfyUI и стадия проверки по листу.
    """
    import numpy as np
    from PIL import Image

    if verify is None:
        from app.ai.cad_recognize.verifiers.stage import verify_spec_against_sheet as verify

    items = [
        item
        for item in verification.get("items") or []
        if item.get("status") == "unmeasurable" and item.get("redraw_box")
    ]
    if not items:
        return content, []
    gray = np.asarray(Image.open(io.BytesIO(content)).convert("L")).copy()
    boxes: list[tuple[tuple[int, int, int, int], list[dict[str, Any]]]] = []
    for item in items:
        box = _box(item, gray.shape)
        if box is None:
            continue
        for known, members in boxes:
            if known == box:
                members.append(item)
                break
        else:
            boxes.append((box, [item]))
    boxes = boxes[: max(0, int(budget))]
    if not boxes:
        return content, []
    comfy = comfy_url.rstrip("/")
    own = redraw is None
    if own:
        from app.ai import gpu_lock

        gpu_lock.unload_ollama()

        def redraw(crop: Any, seed: int) -> Any:
            return run_redraw(comfy, crop, seed, timeout_s=timeout_s)

    from app.ai.cad_recognize.sheet_upscale import main_line_px

    line_px = main_line_px(gray) or 4.0
    log: list[dict[str, Any]] = []
    try:
        for box, members in boxes:
            x0, y0, x1, y1 = box
            source = gray[y0:y1, x0:x1].copy()
            entry: dict[str, Any] = {
                "box_px": list(box),
                "paths": [m["path"] for m in members],
                "seeds": list(seeds),
            }
            variants = []
            for seed in seeds:
                try:
                    drawn = redraw(source, seed)
                except Exception as exc:  # noqa: BLE001 — перерисовка не роняет оцифровку
                    entry["outcome"] = f"перерисовка не удалась: {str(exc)[:200]}"
                    break
                share = precision(source, drawn, line_px)
                entry.setdefault("precision", []).append(round(share, 3))
                if share < _MIN_PRECISION:
                    entry["outcome"] = f"перерисовка выдумала линии (сид {seed}: {share:.2f})"
                    break
                patched = gray.copy()
                patched[y0:y1, x0:x1] = drawn
                variants.append((patched, verify(_png(patched), spec)))
            if len(variants) != len(seeds):
                log.append(entry)
                continue
            by_path = [
                {item["path"]: item for item in report.get("items") or []}
                for _p, report in variants
            ]
            # Уже измеренное по исходнику в этом вырезе не меняется.
            changed = [
                item["path"]
                for item in verification.get("items") or []
                if item.get("status") in ("confirmed", "refuted")
                and _inside(item, box)
                and any(
                    (found.get(item["path"]) or {}).get("status") != item["status"]
                    for found in by_path
                )
            ]
            if changed:
                entry["outcome"] = "перерисовка меняет измеренное по исходнику: " + ", ".join(
                    changed
                )
                log.append(entry)
                continue
            accepted = []
            for member in members:
                results = [found.get(member["path"]) for found in by_path]
                if any(result is None or result["status"] == "unmeasurable" for result in results):
                    continue
                if len({result["status"] for result in results}) != 1:
                    continue
                if not _same_measure(results[0], results[1]):
                    continue
                accepted.append(member["path"])
            if not accepted:
                entry["outcome"] = "сиды разошлись или элемент и на перерисовке не измерить"
                log.append(entry)
                continue
            gray = variants[0][0]
            entry["outcome"] = "принято: " + ", ".join(accepted)
            entry["accepted"] = accepted
            log.append(entry)
    finally:
        if own:
            from app.ai import gpu_lock

            gpu_lock.unload_comfyui()
    if not any(entry.get("accepted") for entry in log):
        return content, log
    return _png(gray), log
