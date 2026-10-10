"""E25 worker process: execute one claimed step, as a Celery worker would."""

import asyncio
import sys
import uuid

from app.tasks.work_orders import execute_claimed_step


async def main(step_id: str, attempt_id: str) -> None:
    await execute_claimed_step(
        uuid.UUID(step_id), uuid.UUID(attempt_id), schedule_verification=False
    )


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
