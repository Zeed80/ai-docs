"""Апскейл листа через локальный ComfyUI (SeedVR2) — для опытов и корпуса.

Тот же рабочий процесс и клиент, что у стадии продукта
(`app.ai.cad_recognize.sheet_upscale`), без предохранителей и решения
«нужно ли»: увеличивает всегда, во сколько сказано.

    python3 scripts/seedvr2_upscale.py --src in.png --out out.png [--scale 4]
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

COMFY = os.environ.get("COMFYUI_URL_HOST", "http://127.0.0.1:8188")


def upscale(src: pathlib.Path, out: pathlib.Path, scale: int = 4, timeout_s: float = 1800) -> float:
    """Увеличить ``src`` в ``scale`` раз и сохранить в ``out``; вернуть время, с."""
    from app.ai.cad_recognize.sheet_upscale import run_comfy_upscale

    started = time.monotonic()
    out.write_bytes(run_comfy_upscale(COMFY, src.read_bytes(), scale, timeout_s=timeout_s))
    return time.monotonic() - started


def free_vram() -> None:
    """Выгрузить модели ComfyUI — GPU общий с Ollama."""
    import json
    import urllib.request

    request = urllib.request.Request(
        f"{COMFY}/free",
        data=json.dumps({"unload_models": True, "free_memory": True}).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=60).read()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--scale", type=int, default=4)
    args = parser.parse_args()
    seconds = upscale(args.src, args.out, args.scale)
    print(f"{args.out} за {seconds:.1f} с")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
