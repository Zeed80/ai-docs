#!/usr/bin/env python3
"""Прогнать лестницу деградации по собранному корпусу.

    python scripts/degrade_verify_corpus.py --src ../cad-dataset-out/verify-corpus-v1 \\
        --out ../cad-dataset-out/verify-corpus-v1-ladder

Каждый чистый лист выходит во всех ступенях `degrade.LADDER` — растр и эталон
в его координатах, — плюс `manifest.jsonl` с исходным split. Seed деградации
выводится из имени листа: повторный прогон даёт те же картинки.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.ai.verify_corpus.degrade import LADDER, degrade  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    rows = [
        json.loads(line)
        for line in (args.src / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    built = 0
    with (args.out / "manifest.jsonl").open("w", encoding="utf-8") as manifest:
        for row in rows:
            if row.get("status") != "built":
                continue
            name = row["name"]
            png = (args.src / f"{name}.png").read_bytes()
            truth = json.loads((args.src / f"{name}.json").read_text(encoding="utf-8"))
            for step in LADDER:
                seed = int.from_bytes(
                    hashlib.sha256(f"{name}:{step['name']}".encode()).digest()[:4], "big"
                )
                degraded, moved = degrade(png, truth, step, seed=seed)
                stem = f"{name}@{step['name']}"
                (args.out / f"{stem}.png").write_bytes(degraded)
                (args.out / f"{stem}.json").write_text(
                    json.dumps(moved, ensure_ascii=False), encoding="utf-8"
                )
                manifest.write(
                    json.dumps(
                        {
                            "name": stem,
                            "sheet": name,
                            "kind": row.get("kind"),
                            "split": row.get("split"),
                            "step": step["name"],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                built += 1
    print(f"ступеней выпущено: {built} ({len(LADDER)} на лист)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
