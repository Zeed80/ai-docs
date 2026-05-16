"""Celery tasks for autonomous skill evolution.

Scheduled by Celery beat:
  - evolve_failing_skills: every 2 hours — find and improve underperforming skills
  - evaluate_shadow_tests: every 30 min — check A/B results and promote/rollback
"""
from __future__ import annotations

import asyncio
import logging

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="skill.evolve_failing_skills",
    bind=True,
    max_retries=1,
    queue="scheduler",
    ignore_result=True,
)
def evolve_failing_skills(self) -> None:  # type: ignore[override]
    """Scan Redis for underperforming skills and trigger improvement."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_run_evolution())
    except Exception as exc:
        logger.error("skill_evolution_task_failed", exc_info=exc)
    finally:
        loop.close()


async def _run_evolution() -> None:
    from app.ai.skill_evolver import scan_underperforming_skills, evolve_skill

    candidates = await scan_underperforming_skills()
    if not candidates:
        logger.info("skill_evolver_no_candidates")
        return

    logger.info("skill_evolver_evolving", count=len(candidates), skills=candidates)
    for skill_name in candidates:
        try:
            deployed = await evolve_skill(skill_name)
            logger.info("skill_evolver_result", skill=skill_name, deployed=deployed)
        except Exception as exc:
            logger.warning("skill_evolver_skill_failed", skill=skill_name, error=str(exc))


@celery_app.task(
    name="skill.evaluate_shadow_tests",
    bind=True,
    max_retries=1,
    queue="scheduler",
    ignore_result=True,
)
def evaluate_shadow_tests(self) -> None:  # type: ignore[override]
    """Check A/B shadow test results and promote winners or rollback losers."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_run_evaluation())
    except Exception as exc:
        logger.error("skill_shadow_evaluation_failed", exc_info=exc)
    finally:
        loop.close()


async def _run_evaluation() -> None:
    from app.ai.skill_evolver import evaluate_shadow_results
    await evaluate_shadow_results()
    logger.info("skill_shadow_evaluation_done")
