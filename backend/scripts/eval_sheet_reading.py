"""Этап A метода `views` на корпусе реальных листов: роли областей листа.

    docker exec infra-celery-worker-1 python3 /app/scripts/eval_sheet_reading.py \
        --corpus /data/real-web [--only id,...]

Сохраняет ответы и листы с подписанными ролями в ``<corpus>/reading/``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

COLORS = {
    "view": (0, 140, 255),
    "section": (0, 90, 220),
    "cross_section": (160, 0, 200),
    "detail_view": (200, 0, 120),
    "isometric": (120, 120, 0),
    "title_block": (0, 0, 255),
    "specification": (0, 160, 0),
    "table": (0, 120, 60),
    "notes": (90, 90, 90),
    "label": (180, 180, 180),
    "other": (200, 200, 200),
}


async def main() -> int:
    import cv2

    from app.ai.cad_views.sheet_reading import read_sheet

    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=pathlib.Path, required=True)
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    manifest = json.loads((args.corpus / "manifest.json").read_text())
    out = args.corpus / "reading"
    out.mkdir(exist_ok=True)
    only = {x for x in args.only.split(",") if x}
    for sheet in manifest["sheets"]:
        if only and sheet["id"] not in only:
            continue
        gray = cv2.imread(str(args.corpus / sheet["file"]), cv2.IMREAD_GRAYSCALE)
        started = time.monotonic()
        reading = await read_sheet(gray)
        seconds = round(time.monotonic() - started, 1)
        record = {
            "id": sheet["id"],
            "seconds": seconds,
            "sheet_kind": reading.sheet_kind,
            "main": reading.main,
            "regions": [r.__dict__ for r in reading.regions],
        }
        (out / f"{sheet['id']}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1))
        image = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        for r in reading.regions:
            x0, y0, x1, y1 = r.box
            color = COLORS.get(r.role, (200, 200, 200))
            cv2.rectangle(image, (x0, y0), (x1, y1), color, 8)
            text = (
                f"{r.n}:{r.role}"
                + (f" {r.name}" if r.name else "")
                + (f" of{r.of}" if r.of else "")
            )
            cv2.putText(
                image, text.encode("ascii", "replace").decode(), (x0 + 8, y0 + 50), 0, 1.6, color, 4
            )
        cv2.imwrite(str(out / f"{sheet['id']}.png"), image)
        views = [
            r
            for r in reading.regions
            if r.role in ("view", "section", "cross_section", "detail_view")
        ]
        print(
            f"{sheet['id']:<16} {seconds:6.1f} с  {reading.sheet_kind:<11} main={reading.main} "
            f"видов {len(views)}: "
            + "; ".join(f"{r.n} {r.role} {r.name or ''} [{r.part or ''}]" for r in views),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
