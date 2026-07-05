#!/usr/bin/env python3
"""VLM captioning + QA for cleanup-LoRA dataset targets.

Captions the CLEAN target (not the degraded control): the description feeds
the training prompt's content part, and the clean render is what the VLM
reads most accurately. Live comparison (2026-07-02, three drawings, RU
captions): qwen3.6:35b is the most precise of the local models — it reads
axis labels and title-block names off the sheet — but its vision encoder
OOMs the 24GB card on large images (HTTP 500), so inputs are downscaled to
800px; gemma4:31b is the fallback (stable at native sizes, slightly less
detailed). Written for the local Ollama node; one-off offline pass.

QA per image: the VLM must produce a non-trivial caption; refusals, empty
answers and boilerplate mark the image as rejected (caption file not
written) so build_dataset.py skips the pair.

Usage:
    python3 caption.py --src <dir with target PNGs> [--model qwen3.6:35b]
                       [--fallback gemma4:31b] [--ollama http://host:11434]
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import pathlib
import sys
import time
import urllib.request

from PIL import Image

PROMPT = (
    "Ты описываешь чертёж для датасета. Ответь по-русски, 1-2 коротких предложения, "
    "строго по содержимому: тип (чертёж детали / сборочный чертёж / план / фасад / "
    "разрез / схема), состав видов, ключевые элементы. "
    "БЕЗ размеров, БЕЗ номеров документов, БЕЗ оценок качества."
)

_REFUSAL_MARKERS = ("не могу", "невозможно", "cannot", "unable", "извини")


def _downscale_b64(path: pathlib.Path, max_px: int) -> str:
    img = Image.open(path).convert("RGB")
    img.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _generate(ollama: str, model: str, image_b64: str, timeout: int = 600) -> str:
    body = {
        "model": model,
        "prompt": PROMPT,
        "images": [image_b64],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.2, "num_predict": 200},
    }
    req = urllib.request.Request(
        f"{ollama}/api/generate",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    return (resp.get("response") or "").strip()


def caption_one(ollama: str, path: pathlib.Path, model: str, fallback: str | None) -> str | None:
    for candidate, max_px in ((model, 800), (model, 640), (fallback, 800)):
        if not candidate:
            continue
        try:
            text = _generate(ollama, candidate, _downscale_b64(path, max_px))
        except Exception as exc:  # noqa: BLE001
            print(f"  {candidate}@{max_px}px failed: {str(exc)[:80]}", file=sys.stderr)
            continue
        cleaned = " ".join(text.split())
        if len(cleaned) < 25 or any(m in cleaned.lower() for m in _REFUSAL_MARKERS):
            print(f"  {candidate}: rejected caption: {cleaned[:80]!r}", file=sys.stderr)
            continue
        return cleaned
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, type=pathlib.Path)
    ap.add_argument("--model", default="qwen3.6:35b")
    ap.add_argument("--fallback", default="gemma4:31b")
    ap.add_argument("--ollama", default="http://localhost:11434")
    ap.add_argument("--force", action="store_true", help="re-caption existing .txt")
    args = ap.parse_args()

    ok = rejected = skipped = 0
    for path in sorted(args.src.glob("*.png")):
        txt_path = path.with_suffix(".txt")
        if txt_path.exists() and not args.force:
            skipped += 1
            continue
        t0 = time.time()
        caption = caption_one(args.ollama, path, args.model, args.fallback)
        if caption is None:
            rejected += 1
            print(f"REJECT {path.name}")
            continue
        txt_path.write_text(caption, encoding="utf-8")
        ok += 1
        print(f"OK {path.name} ({time.time()-t0:.0f}s): {caption[:100]}")
    print(f"done: {ok} captioned, {rejected} rejected, {skipped} already had captions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
