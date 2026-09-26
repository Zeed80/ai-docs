"""Живой гейт реальных листов: полный конвейер «по описанию», по одному листу.

Офлайн-гейт (`eval_real_sheets.py`) работает на замороженном чтении ридера и
регрессию самого ридера не видит (2026-09-26: вопрос о сечениях сломал сборку
z4-r4, офлайн-гейт был зелёным). Здесь каждый лист проходит весь `cad_trace`
на текущем образе; итог — собрано ли тело и чем заблокировано. Лист, который
собирался (``built: true`` в манифесте), не должен перестать.

Запуск в контейнере воркера (листы идут ПО ОДНОМУ: одна модель на стенде,
параллельные прогоны уходят в тайм-ауты вопросов):

    docker exec infra-celery-worker-1 python3 /app/scripts/live_real_gate.py [--only z4-r4,...]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

MANIFEST = (
    pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "real_sheets_live.json"
)
OWNER = "c8e001c36ec87957c2f7714d3243e46c206e2b86aca6872fb8c36689fd999809"  # claude@test.com


async def _run(sheet: dict, timeout_s: float) -> dict:
    from sqlalchemy import select

    from app.db.models import ImageGeneration, ImageGenStatus
    from app.db.session import _get_session_factory
    from app.services import studio_queue
    from app.tasks.cad_trace import run_cad_trace

    factory = _get_session_factory()
    async with factory() as db:
        gen = ImageGeneration(
            owner_sub=OWNER,
            operation="vectorize",
            status=ImageGenStatus.queued,
            prompt=f"live gate {sheet['name']}",
            params={"vectorize_method": "spec", "digitization_type": sheet.get("type", "auto")},
            source_image_paths=[sheet["path"]],
        )
        db.add(gen)
        await db.flush()
        job = await studio_queue.create_image_job(db, gen, title=gen.prompt)
        await db.commit()
        await db.refresh(gen)
        await db.refresh(job)
        task = run_cad_trace.apply_async(args=[str(gen.id)], queue="celery")
        job.celery_task_id = task.id
        gen.celery_task_id = task.id
        await db.commit()
        gen_id = gen.id
    started = time.monotonic()
    while time.monotonic() - started < timeout_s:
        await asyncio.sleep(20)
        async with factory() as db:
            row = (
                await db.execute(select(ImageGeneration).where(ImageGeneration.id == gen_id))
            ).scalar_one()
            status = getattr(row.status, "value", str(row.status))
            if status in ("done", "failed"):
                params = row.params or {}
                solid = params.get("solid_3d") or {}
                return {
                    "id": str(gen_id),
                    "status": status,
                    "build_status": solid.get("build_status"),
                    "built": solid.get("build_status") == "built_unverified",
                    "blockers": [str(b)[:200] for b in solid.get("blockers") or []],
                    "seconds": round(time.monotonic() - started),
                }
    return {"id": str(uuid.UUID(str(gen_id))), "status": "timeout", "built": False, "blockers": []}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="")
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument(
        "--report", type=pathlib.Path, default=pathlib.Path("/tmp/live_real_gate.json")
    )
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text())
    only = {name for name in args.only.split(",") if name}
    results = {}
    worse = []
    for sheet in manifest["sheets"]:
        if only and sheet["name"] not in only:
            continue
        got = await _run(sheet, args.timeout)
        results[sheet["name"]] = got
        mark = "собрано" if got["built"] else (got.get("build_status") or got["status"])
        print(
            f"{sheet['name']:<16} {mark:<24} {got.get('seconds', '?')} с  блокеров {len(got['blockers'])}",
            flush=True,
        )
        for line in got["blockers"][:5]:
            print(f"    {line[:140]}", flush=True)
        if sheet.get("built") and not got["built"]:
            worse.append(sheet["name"])
    args.report.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    built = sum(1 for r in results.values() if r["built"])
    print(f"собрано {built} из {len(results)}")
    for name in worse:
        print(f"ХУЖЕ: {name} собирался и перестал")
    print("живой гейт", "не пройден" if worse else "пройден")
    return 1 if worse else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
