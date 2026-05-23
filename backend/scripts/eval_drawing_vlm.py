#!/usr/bin/env python3
"""Drawing VLM Benchmark — Sprint 5 eval script.

Compares VLM models on technical drawing extraction quality.

Usage:
    python scripts/eval_drawing_vlm.py --drawings path/to/drawings/ [--ground-truth path/to/gt.json]

    # Quick smoke test with synthetic fixtures:
    python scripts/eval_drawing_vlm.py --smoke

Output:
    Markdown table: model | avg_features | precision | recall | F1 | latency_s
    Writes results to eval_results_<timestamp>.json

Requirements:
    pip install httpx rich  (httpx for async requests, rich for table output)
    Ollama running locally with at least one VLM model pulled.

Sprint 5 goal: compare qwen3-vl:8b vs qwen3.5:27b vs qwen3.6:35b (already in project).
Winner → status: production in model_registry.yaml.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Config ────────────────────────────────────────────────────────────────────

EVAL_MODELS = [
    "qwen3-vl:8b",          # Sprint 5 candidate A
    "qwen3.5:27b",          # Sprint 5 candidate B
    "qwen3.6:35b",          # Current production model (baseline)
]

OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

# Drawing types to test
DRAWING_TYPES = ["detail", "assembly"]

# Minimum expected extraction per drawing type
MIN_FEATURES = {"detail": 2, "assembly": 1}


# ── Data structures ───────────────────────────────────────────────────────────


@dataclass
class GroundTruth:
    filename: str
    drawing_type: str
    expected_features: list[dict]   # [{feature_type, name, dimensions[{nominal}]}]


@dataclass
class ModelResult:
    model: str
    filename: str
    extracted_features: list[dict]
    latency_s: float
    error: str | None = None


@dataclass
class EvalMetrics:
    model: str
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    avg_features: float = 0.0
    avg_latency_s: float = 0.0
    errors: int = 0
    samples: int = 0


# ── Ground truth loading ──────────────────────────────────────────────────────


def load_ground_truth(gt_path: Path | None, drawings_dir: Path) -> list[GroundTruth]:
    """Load ground truth from JSON file, or infer minimal GT from filenames."""
    if gt_path and gt_path.exists():
        with open(gt_path) as f:
            raw = json.load(f)
        return [
            GroundTruth(
                filename=item["filename"],
                drawing_type=item.get("drawing_type", "detail"),
                expected_features=item.get("features", []),
            )
            for item in raw
        ]

    # Auto-discover: *.dxf, *.pdf, *.png in drawings_dir
    gt = []
    for ext in ("*.dxf", "*.pdf", "*.png", "*.jpg", "*.tiff"):
        for path in sorted(drawings_dir.glob(ext)):
            drawing_type = "assembly" if "assembly" in path.stem.lower() else "detail"
            gt.append(GroundTruth(
                filename=path.name,
                drawing_type=drawing_type,
                expected_features=[],  # no ground truth → measure count only
            ))
    return gt


# ── Smoke test fixtures ───────────────────────────────────────────────────────


def _make_smoke_drawings() -> tuple[Path, list[GroundTruth]]:
    """Generate synthetic test drawings for smoke testing."""
    import io
    import tempfile
    from PIL import Image, ImageDraw, ImageFont

    tmp = Path(tempfile.mkdtemp(prefix="eval_drawings_"))

    def _make_detail_drawing() -> bytes:
        img = Image.new("RGB", (800, 600), (245, 245, 245))
        d = ImageDraw.Draw(img)
        # Title block area
        d.rectangle([0, 500, 800, 600], fill=(230, 230, 230), outline=(0, 0, 0))
        d.text((620, 510), "Вал ведомый", fill=(0, 0, 0))
        d.text((620, 525), "ДП-001-01", fill=(0, 0, 0))
        d.text((620, 540), "Сталь 45 ГОСТ 1050", fill=(0, 0, 0))
        d.text((620, 555), "Масштаб 1:2", fill=(0, 0, 0))
        # Main contour (shaft)
        d.rectangle([100, 150, 600, 350], outline=(0, 0, 0), width=2)
        # Diameter dimensions
        d.line([(100, 350), (100, 400)], fill=(0, 0, 0))
        d.line([(600, 350), (600, 400)], fill=(0, 0, 0))
        d.line([(100, 385), (600, 385)], fill=(0, 0, 0))
        d.text((310, 360), "Ø50h6", fill=(0, 0, 0))
        # Hole
        d.ellipse([320, 180, 380, 240], outline=(0, 0, 0), width=2)
        d.text((320, 245), "Ø12H7", fill=(0, 0, 0))
        # Ra roughness
        d.text([150, 155], "Ra 1.6", fill=(0, 0, 0))
        d.text([450, 155], "Ra 3.2", fill=(0, 0, 0))
        # GD&T
        d.text([120, 450], "⊥ 0.02 A", fill=(0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _make_assembly_drawing() -> bytes:
        img = Image.new("RGB", (1000, 700), (245, 245, 245))
        d = ImageDraw.Draw(img)
        # BOM table (upper right)
        d.rectangle([650, 10, 990, 250], outline=(0, 0, 0))
        d.text((660, 15), "СПЕЦИФИКАЦИЯ", fill=(0, 0, 0))
        d.line([(650, 30), (990, 30)], fill=(0, 0, 0))
        for i, (no, name, qty) in enumerate([
            ("1", "Корпус", "1"), ("2", "Вал", "1"),
            ("3", "Крышка", "2"), ("4", "Болт М8", "4"),
        ]):
            y = 35 + i * 25
            d.text((655, y), no, fill=(0, 0, 0))
            d.text((680, y), name, fill=(0, 0, 0))
            d.text((920, y), qty, fill=(0, 0, 0))
        # Main assembly view
        d.rectangle([50, 50, 550, 450], outline=(0, 0, 0), width=2)
        d.ellipse([200, 150, 400, 350], outline=(0, 0, 0), width=2)
        # Balloons
        for pos, num in [((170, 130), "1"), ((350, 200), "2"), ((100, 400), "3")]:
            cx, cy = pos
            d.ellipse([cx - 15, cy - 15, cx + 15, cy + 15], outline=(0, 0, 0))
            d.text((cx - 5, cy - 8), num, fill=(0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    detail_path = tmp / "shaft_detail.png"
    assembly_path = tmp / "gearbox_assembly.png"
    detail_path.write_bytes(_make_detail_drawing())
    assembly_path.write_bytes(_make_assembly_drawing())

    ground_truth = [
        GroundTruth(
            filename="shaft_detail.png",
            drawing_type="detail",
            expected_features=[
                {"feature_type": "surface", "name": "Ø50h6"},
                {"feature_type": "hole", "name": "Ø12H7"},
                {"feature_type": "surface", "name": "Ra 1.6"},
            ],
        ),
        GroundTruth(
            filename="gearbox_assembly.png",
            drawing_type="assembly",
            expected_features=[
                {"feature_type": "other", "name": "Корпус"},
                {"feature_type": "other", "name": "Вал"},
            ],
        ),
    ]
    return tmp, ground_truth


# ── Model inference ───────────────────────────────────────────────────────────


async def run_model_on_drawing(
    model: str,
    image_bytes: bytes,
    drawing_type: str,
) -> tuple[list[dict], float]:
    """Call Ollama with image and return (features, latency_s)."""
    import base64

    try:
        import httpx
    except ImportError:
        print("Install httpx: pip install httpx", file=sys.stderr)
        sys.exit(1)

    b64 = base64.b64encode(image_bytes).decode()

    prompt = f"""Ты — система анализа технических чертежей. Тип чертежа: {drawing_type}.
Извлеки все конструктивные элементы и верни СТРОГО JSON:
{{"features": [{{"feature_type": "...", "name": "...", "confidence": 0.9, "dimensions": [{{"nominal": 50.0, "fit_system": "h6"}}]}}]}}
Без markdown. Только JSON."""

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "stream": False,
    }

    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(f"{OLLAMA_BASE}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()

        latency = time.perf_counter() - t0
        content = data.get("message", {}).get("content", "")
        features = _parse_features(content)
        return features, latency

    except Exception as exc:
        latency = time.perf_counter() - t0
        print(f"  ⚠  {model} error: {exc}", file=sys.stderr)
        return [], latency


def _parse_features(text: str) -> list[dict]:
    """Extract features list from VLM response."""
    import re
    text = re.sub(r"```(?:json)?", "", text).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
        return data.get("features", []) if isinstance(data, dict) else []
    except Exception:
        return []


# ── Metrics ───────────────────────────────────────────────────────────────────


def compute_metrics(
    results: list[ModelResult],
    ground_truth: list[GroundTruth],
) -> EvalMetrics:
    """Compute precision, recall, F1 for one model."""
    if not results:
        return EvalMetrics(model="", samples=0)

    model = results[0].model
    gt_map = {gt.filename: gt for gt in ground_truth}

    tp = fp = fn = 0
    feature_counts = []
    latencies = []
    errors = 0

    for r in results:
        if r.error:
            errors += 1
            continue

        latencies.append(r.latency_s)
        feature_counts.append(len(r.extracted_features))

        gt = gt_map.get(r.filename)
        if not gt or not gt.expected_features:
            # No ground truth — only count features
            continue

        extracted_names = {
            f.get("name", "").lower().strip()
            for f in r.extracted_features
        }
        extracted_types = {f.get("feature_type", "").lower() for f in r.extracted_features}

        for expected in gt.expected_features:
            exp_name = expected.get("name", "").lower().strip()
            exp_type = expected.get("feature_type", "").lower()

            # Match by name substring or type
            matched = any(exp_name in name for name in extracted_names) or \
                      (exp_type in extracted_types and exp_type not in ("other", ""))
            if matched:
                tp += 1
            else:
                fn += 1

        fp += max(0, len(r.extracted_features) - len(gt.expected_features))

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    return EvalMetrics(
        model=model,
        precision=round(precision, 3),
        recall=round(recall, 3),
        f1=round(f1, 3),
        avg_features=round(sum(feature_counts) / max(1, len(feature_counts)), 1),
        avg_latency_s=round(sum(latencies) / max(1, len(latencies)), 1),
        errors=errors,
        samples=len(results),
    )


# ── Report ────────────────────────────────────────────────────────────────────


def print_report(metrics_list: list[EvalMetrics], results_path: Path) -> None:
    """Print Markdown table and save JSON results."""
    print("\n## Drawing VLM Benchmark Results\n")
    print(f"{'Model':<25} {'Features':<10} {'Precision':<12} {'Recall':<10} {'F1':<8} {'Latency s':<12} {'Errors'}")
    print("-" * 90)

    best_f1 = max((m.f1 for m in metrics_list), default=0)
    for m in sorted(metrics_list, key=lambda x: x.f1, reverse=True):
        marker = " ← WINNER" if m.f1 == best_f1 and best_f1 > 0 else ""
        print(
            f"{m.model:<25} {m.avg_features:<10.1f} {m.precision:<12.3f} "
            f"{m.recall:<10.3f} {m.f1:<8.3f} {m.avg_latency_s:<12.1f} "
            f"{m.errors}{marker}"
        )

    print()

    if best_f1 > 0:
        winner = max(metrics_list, key=lambda x: x.f1)
        print(f"**Рекомендация**: установить `{winner.model}` → `status: production` "
              f"в `model_registry.yaml` (F1={winner.f1:.3f})")

    # Save JSON
    data = [m.__dict__ for m in metrics_list]
    results_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\nРезультаты сохранены: {results_path}")


# ── Main ──────────────────────────────────────────────────────────────────────


async def main_async(args: argparse.Namespace) -> None:
    if args.smoke:
        print("Smoke mode: генерация синтетических чертежей...")
        drawings_dir, ground_truth = _make_smoke_drawings()
    else:
        drawings_dir = Path(args.drawings)
        gt_path = Path(args.ground_truth) if args.ground_truth else None
        ground_truth = load_ground_truth(gt_path, drawings_dir)

    if not ground_truth:
        print("Нет чертежей для оценки. Укажите --drawings или используйте --smoke.")
        sys.exit(1)

    models = args.models if args.models else EVAL_MODELS
    print(f"Модели: {', '.join(models)}")
    print(f"Чертежей: {len(ground_truth)}\n")

    all_results: dict[str, list[ModelResult]] = {m: [] for m in models}

    for gt in ground_truth:
        img_path = drawings_dir / gt.filename
        if not img_path.exists():
            print(f"  Пропуск (не найден): {gt.filename}")
            continue

        image_bytes = img_path.read_bytes()
        print(f"Чертёж: {gt.filename} [{gt.drawing_type}]")

        for model in models:
            print(f"  → {model} ...", end="", flush=True)
            features, latency = await run_model_on_drawing(model, image_bytes, gt.drawing_type)
            print(f" {len(features)} features, {latency:.1f}s")
            all_results[model].append(ModelResult(
                model=model,
                filename=gt.filename,
                extracted_features=features,
                latency_s=latency,
            ))

    # Compute metrics per model
    metrics_list = [
        compute_metrics(results, ground_truth)
        for model, results in all_results.items()
    ]

    import datetime
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    results_path = Path(f"eval_results_{ts}.json")
    print_report(metrics_list, results_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Drawing VLM Benchmark (Sprint 5)")
    parser.add_argument("--drawings", default="tests/fixtures/drawings/",
                        help="Директория с чертежами для оценки")
    parser.add_argument("--ground-truth", default=None,
                        help="JSON-файл с ground truth разметкой")
    parser.add_argument("--models", nargs="+", default=None,
                        help=f"Модели для сравнения (по умолчанию: {EVAL_MODELS})")
    parser.add_argument("--smoke", action="store_true",
                        help="Быстрый тест на синтетических чертежах (без Ollama)")
    args = parser.parse_args()

    if args.smoke:
        # In smoke mode, mock the model calls
        _patch_model_call()

    asyncio.run(main_async(args))


def _patch_model_call() -> None:
    """Replace Ollama call with synthetic responses for CI/smoke testing."""
    import unittest.mock as mock
    import importlib
    import sys

    module = sys.modules[__name__]

    async def _mock_run(model: str, image_bytes: bytes, drawing_type: str):
        await asyncio.sleep(0.05)  # simulate latency
        if drawing_type == "detail":
            features = [
                {"feature_type": "surface", "name": "Ø50h6", "confidence": 0.9,
                 "dimensions": [{"nominal": 50.0, "fit_system": "h6"}]},
                {"feature_type": "hole", "name": "Ø12H7", "confidence": 0.85,
                 "dimensions": [{"nominal": 12.0, "fit_system": "H7"}]},
                {"feature_type": "surface", "name": "Ra 1.6", "confidence": 0.8},
            ]
        else:
            features = [
                {"feature_type": "other", "name": "Корпус", "confidence": 0.9},
                {"feature_type": "other", "name": "Вал", "confidence": 0.85},
                {"feature_type": "other", "name": "Крышка", "confidence": 0.8},
            ]
        # Simulate slight model differences
        if "8b" in model:
            features = features[:-1]  # smaller model gets fewer
        latency = {"qwen3-vl:8b": 3.2, "qwen3.5:27b": 8.1, "qwen3.6:35b": 6.4}.get(model, 5.0)
        return features, latency

    module.run_model_on_drawing = _mock_run


if __name__ == "__main__":
    main()
