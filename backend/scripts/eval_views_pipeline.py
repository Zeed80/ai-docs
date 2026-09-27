"""Метод `views` на листах: роли → надписи → тело вращения → ядро → проекции.

    docker exec infra-celery-worker-1 python3 /app/scripts/eval_views_pipeline.py \
        --sheet /tmp/x.png [--sheet ...] --out /tmp/views-out
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
import pathlib
import sys
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def draw_projection(views: dict, path: pathlib.Path) -> None:
    import cv2
    import numpy as np

    panels = []
    for name in ("front", "top", "side"):
        v = views.get(name)
        if not v:
            continue
        b = v["bounds_mm"]
        k = 600.0 / max(1e-6, max(b["u_max"] - b["u_min"], b["v_max"] - b["v_min"]))
        w = int((b["u_max"] - b["u_min"]) * k) + 40
        h = int((b["v_max"] - b["v_min"]) * k) + 40
        img = np.full((h, w, 3), 255, np.uint8)

        def pt(u, vv):
            return int((u - b["u_min"]) * k + 20), int((b["v_max"] - vv) * k + 20)

        for key, col, th in (("hidden", (170, 170, 170), 1), ("visible", (0, 0, 0), 2)):
            for e in v.get(key, []):
                if e.get("type") == "circle":
                    cv2.circle(img, pt(*e["center"]), int(e["radius"] * k), col, th)
                elif e.get("type") == "arc" and "center" in e:
                    c, r = e["center"], e["radius"]
                    a0 = math.atan2(e["points"][0][1] - c[1], e["points"][0][0] - c[0])
                    a1 = math.atan2(e["points"][-1][1] - c[1], e["points"][-1][0] - c[0])
                    am = math.atan2(e["mid"][1] - c[1], e["mid"][0] - c[0])
                    span = (a1 - a0) % (2 * math.pi)
                    if (am - a0) % (2 * math.pi) > span:
                        span -= 2 * math.pi
                    pts = [
                        pt(c[0] + r * math.cos(a0 + span * t), c[1] + r * math.sin(a0 + span * t))
                        for t in np.linspace(0, 1, 40)
                    ]
                    cv2.polylines(img, [np.array(pts, np.int32)], False, col, th)
                elif "points" in e:
                    cv2.polylines(
                        img, [np.array([pt(*q) for q in e["points"]], np.int32)], False, col, th
                    )
        cv2.putText(img, name, (5, 15), 0, 0.5, (0, 0, 200), 1)
        panels.append(img)
    width = max(p.shape[1] for p in panels)
    out = np.vstack(
        [np.pad(p, ((0, 10), (0, width - p.shape[1]), (0, 0)), constant_values=255) for p in panels]
    )
    cv2.imwrite(str(path), out)


def prepare_like_product(gray):
    """Выпрямление фото и увеличение SeedVR2, как стадии 0.9–0.95 `cad_trace`."""
    import cv2
    import numpy as np

    from app.ai.cad_recognize.sheet_upscale import upscale_sheet
    from app.config import settings
    from app.tasks.cad_trace import _dewarp_photo

    ok, encoded = cv2.imencode(".png", gray)
    content = _dewarp_photo(encoded.tobytes())
    note = ""
    try:
        result = upscale_sheet(
            content, comfy_url=settings.comfyui_url, timeout_s=settings.cad_upscale_timeout_s
        )
        if result.applied:
            content, note = result.content, result.reason
    except Exception as exc:  # noqa: BLE001 — без увеличения, как продукт при сбое
        note = f"увеличение недоступно: {exc}"[:120]
    image = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_GRAYSCALE)
    return image, note


async def main() -> int:
    import cv2
    import httpx

    from app.ai.cad_views.pipeline import digitize
    from app.config import settings

    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet", action="append", required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument(
        "--truth",
        type=pathlib.Path,
        default=pathlib.Path(__file__).resolve().parents[1]
        / "tests/fixtures/views_body_truth.json",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="без подготовки продукта (выпрямление фото, увеличение грубого листа)",
    )
    args = parser.parse_args()
    truth = json.loads(args.truth.read_text())["sheets"] if args.truth.exists() else {}
    score = {"верно": 0, "неверно": 0, "отказ": 0}
    args.out.mkdir(parents=True, exist_ok=True)
    kernel = settings.cad_kernel_url.rstrip("/")
    for sheet in args.sheet:
        name = pathlib.Path(sheet).stem
        gray = cv2.imread(sheet, cv2.IMREAD_GRAYSCALE)
        if not args.raw:
            # Тот же путь, что в /cad: грубый лист мерить сырым — мерить не то,
            # что делает продукт (слайд с валом: линии 1–2 px).
            gray, prepared = prepare_like_product(gray)
            if prepared:
                print(f"{name:<20} подготовка: {prepared}", flush=True)
        result, reading, labels = await digitize(gray)
        record = {
            "sheet": name,
            "ok": result.ok,
            "reason": result.reason,
            "labels": labels,
            "main": reading.main,
            "sheet_kind": reading.sheet_kind,
            "regions": [dict(region.__dict__) for region in reading.regions],
            "scales": result.scales,
            "features": result.features,
            "notes": result.notes,
            "coverage": result.coverage,
        }
        if result.ok:
            async with httpx.AsyncClient(timeout=180) as client:
                compiled = await client.post(f"{kernel}/compile", json=result.candidate)
                record["kernel_status"] = compiled.status_code
                if compiled.status_code == 200:
                    report = json.loads(
                        zipfile.ZipFile(io.BytesIO(compiled.content)).read("report.json")
                    )
                    record["volume_mm3"] = report.get("volume_mm3")
                    record["bounds_mm"] = report.get("bounds_mm")
                    projected = await client.post(
                        f"{kernel}/project",
                        json={
                            "candidate": result.candidate["candidate"],
                            "views": ["front", "top", "side"],
                            "confirm_assumptions": True,
                        },
                    )
                    if projected.status_code == 200:
                        draw_projection(projected.json()["views"], args.out / f"{name}_3d.png")
                else:
                    record["kernel_error"] = compiled.text[:300]
            record["profile"] = result.profile
        expected = truth.get(name)
        if expected:
            record["truth"] = verdict = body_verdict(record, expected)
            score[verdict] = score.get(verdict, 0) + 1
        (args.out / f"{name}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1))
        print(
            f"{name:<20} {'тело' if record.get('kernel_status') == 200 else ('ядро ' + str(record.get('kernel_status')) if result.ok else 'нет: ' + result.reason)}"
            f"  {record.get('bounds_mm') or ''}  элементов {len(result.features)}"
            f"  {('эталон: ' + record['truth']) if 'truth' in record else ''}",
            flush=True,
        )
    if truth:
        print("по эталону:", score, flush=True)
    return 0


def body_verdict(record: dict, expected: dict) -> str:
    """верно / неверно / отказ — габарит тела против надписей (допуск 3 %)."""
    bounds = record.get("bounds_mm")
    if not bounds:
        return "верно" if expected.get("expect_refusal") else "отказ"
    if expected.get("expect_refusal"):
        return "неверно"

    def close(value: float, target) -> bool:
        targets = target if isinstance(target, list) else [target]
        return any(abs(value - t) <= 0.03 * t for t in targets)

    if expected.get("bounds_mm"):
        # Выдавливание: габарит по трём осям без учёта их порядка.
        got = sorted(bounds[k] for k in ("x", "y", "z"))
        want = sorted(expected["bounds_mm"])
        return "верно" if all(close(g, w) for g, w in zip(got, want)) else "неверно"
    length, diameter = bounds["z"], max(bounds["x"], bounds["y"])
    for value, target in (
        (length, expected.get("length_mm")),
        (diameter, expected.get("max_diameter_mm")),
    ):
        if target is not None and not close(value, target):
            return "неверно"
    return "верно"


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
