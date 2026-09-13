"""Опытный корпус «лист, улучшенный перед оцифровкой» (SeedVR2 через ComfyUI).

Берёт листы dev с грубых ступеней лестницы (75 и 100 dpi), увеличивает их
до 300 dpi рабочим процессом оператора (`seedvr2_upscale`) и пишет корпус той
же раскладки: ``<имя>@sr-75.png`` + эталон, пересчитанный на новый размер тем
же `_map_points`, что и лестница деградации. Исходные грубые листы тех же
имён копируются рядом (``@dpi-75``), чтобы сравнение шло на ОДНИХ И ТЕХ ЖЕ
листах. Прерываемо: готовые листы пропускаются.

    python3 scripts/build_sr_corpus.py --src ../cad-dataset-out/verify-corpus-v9-ladder \\
        --out ../cad-dataset-out/verify-corpus-v9-sr --per-kind 8
"""

from __future__ import annotations

import argparse
import copy
import json
import pathlib
import shutil
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# Ступень лестницы → множитель до 300 dpi.
_STEPS = {"dpi-75": 4, "dpi-100": 3}


def scaled_truth(truth: dict, factor_x: float, factor_y: float) -> dict:
    """Эталон на увеличенный лист: точки и рамки — тем же преобразованием, что у лестницы."""
    import numpy as np

    from app.ai.verify_corpus.degrade import _map_points

    out = copy.deepcopy(truth)
    factors = np.asarray([factor_x, factor_y], dtype=float)
    _map_points(out, lambda points: points * factors)
    if out.get("px_per_part_mm"):
        out["px_per_part_mm"] = float(out["px_per_part_mm"]) * (factor_x + factor_y) / 2.0
    return out


def main() -> int:
    from PIL import Image
    from seedvr2_upscale import free_vram, upscale

    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--per-kind", type=int, default=8)
    parser.add_argument("--steps", nargs="*", default=sorted(_STEPS))
    args = parser.parse_args()

    rows = [
        json.loads(line)
        for line in (args.src / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    args.out.mkdir(parents=True, exist_ok=True)
    manifest = args.out / "manifest.jsonl"
    done = (
        {json.loads(line)["name"] for line in manifest.read_text().splitlines() if line.strip()}
        if manifest.exists()
        else set()
    )
    sheets: dict[str, list[str]] = {}
    for row in rows:
        if row.get("split") == "dev" and row["step"] == "clean-300":
            sheets.setdefault(row["kind"], []).append(row["sheet"])
    chosen = [
        (kind, sheet) for kind, names in sorted(sheets.items()) for sheet in names[: args.per_kind]
    ]
    started = time.monotonic()
    with manifest.open("a", encoding="utf-8") as sink:
        for kind, sheet in chosen:
            for step in args.steps:
                source = args.src / f"{sheet}@{step}"
                raw_name = f"{sheet}@{step}"
                sr_name = f"{sheet}@sr-{step.split('-')[1]}"
                if raw_name not in done:
                    shutil.copy(f"{source}.png", args.out / f"{raw_name}.png")
                    shutil.copy(f"{source}.json", args.out / f"{raw_name}.json")
                    sink.write(
                        json.dumps(
                            {
                                "name": raw_name,
                                "sheet": sheet,
                                "kind": kind,
                                "split": "dev",
                                "step": step,
                            }
                        )
                        + "\n"
                    )
                if sr_name in done:
                    continue
                target = args.out / f"{sr_name}.png"
                seconds = upscale(pathlib.Path(f"{source}.png"), target, _STEPS[step])
                before = Image.open(f"{source}.png").size
                after = Image.open(target).convert("L")
                after.save(target)
                truth = json.loads(pathlib.Path(f"{source}.json").read_text(encoding="utf-8"))
                truth = scaled_truth(truth, after.size[0] / before[0], after.size[1] / before[1])
                (args.out / f"{sr_name}.json").write_text(
                    json.dumps(truth, ensure_ascii=False), encoding="utf-8"
                )
                sink.write(
                    json.dumps(
                        {
                            "name": sr_name,
                            "sheet": sheet,
                            "kind": kind,
                            "split": "dev",
                            "step": f"sr-{step.split('-')[1]}",
                        }
                    )
                    + "\n"
                )
                sink.flush()
                print(
                    f"{sr_name}: {seconds:.0f} с ({time.monotonic() - started:.0f} с всего)",
                    flush=True,
                )
    free_vram()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
