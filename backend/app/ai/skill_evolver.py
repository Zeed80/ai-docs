"""Autonomous Skill Evolver — self-improving agent system.

Continuously monitors skill performance and automatically improves underperforming skills:

  1. Every EVOLUTION_INTERVAL minutes (Celery beat): scan skill success rates
  2. Skills below FAILURE_THRESHOLD: collect failure examples + error patterns
  3. Call CapabilityBuilder with enhanced context (anti-patterns, examples, original code)
  4. Validate improved version in sandbox
  5. Deploy in shadow mode (SHADOW_TRAFFIC_PCT % of calls go to v2)
  6. After MIN_AB_CALLS calls: if v2 win_rate > v1 → promote, else rollback
  7. Log all decisions to audit trail

This creates a positive feedback loop where the system gets smarter over time.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

# ── Configuration ──────────────────────────────────────────────────────────────

FAILURE_THRESHOLD = 0.25      # skills failing > 25% → evolve
MIN_CALLS_TO_EVOLVE = 10      # need at least 10 calls for reliable stats
SHADOW_TRAFFIC_PCT = 0.10     # route 10% to new version during A/B test
MIN_AB_CALLS = 50             # minimum calls before declaring A/B winner
WIN_MARGIN = 0.05             # v2 must beat v1 by 5pp to be promoted
MAX_EVOLUTIONS_PER_RUN = 3    # don't evolve more than N skills per scheduled run

# ── A/B state (in-memory, backed by Redis) ────────────────────────────────────

_SHADOW_KEY_PREFIX = "skill_evolver:shadow:"
_AUDIT_KEY = "skill_evolver:audit"


@dataclass
class ShadowConfig:
    skill_name: str
    v2_name: str          # full path to v2 module
    started_at: float
    v1_calls: int = 0
    v1_success: int = 0
    v2_calls: int = 0
    v2_success: int = 0

    @property
    def v1_rate(self) -> float:
        return self.v1_success / max(1, self.v1_calls)

    @property
    def v2_rate(self) -> float:
        return self.v2_success / max(1, self.v2_calls)

    @property
    def total_calls(self) -> int:
        return self.v1_calls + self.v2_calls

    def to_dict(self) -> dict:
        return {
            "skill_name": self.skill_name,
            "v2_name": self.v2_name,
            "started_at": self.started_at,
            "v1_calls": self.v1_calls, "v1_success": self.v1_success,
            "v2_calls": self.v2_calls, "v2_success": self.v2_success,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ShadowConfig":
        return cls(**d)


# ── Skill scanner ──────────────────────────────────────────────────────────────

async def scan_underperforming_skills() -> list[str]:
    """Return skill names that are below the failure threshold."""
    from app.ai.orchestrator_memory import get_skill_scores, _SKILL_KEY_PREFIX
    from app.utils.redis_client import get_async_redis

    r = get_async_redis()
    keys = await r.keys(f"{_SKILL_KEY_PREFIX}*")
    if not keys:
        return []

    underperforming: list[str] = []
    for key in keys:
        skill_name = key.replace(_SKILL_KEY_PREFIX, "")
        raw = await r.get(key)
        if not raw:
            continue
        stats = json.loads(raw)
        total = stats.get("success", 0) + stats.get("fail", 0)
        if total < MIN_CALLS_TO_EVOLVE:
            continue
        failure_rate = stats.get("fail", 0) / max(1, total)
        if failure_rate >= FAILURE_THRESHOLD:
            underperforming.append(skill_name)
            logger.info(
                "skill_evolver_candidate",
                skill=skill_name,
                failure_rate=round(failure_rate, 3),
                total_calls=total,
            )

    return underperforming[:MAX_EVOLUTIONS_PER_RUN]


# ── Context builder for evolution ─────────────────────────────────────────────

async def _build_evolution_context(skill_name: str) -> str:
    """Build rich context for CapabilityBuilder: include failures, original code."""
    parts: list[str] = [f"Улучши существующий навык: **{skill_name}**"]

    # Load original skill code
    try:
        from app.ai.capability_builder import _sanitize_skill_filename, _GENERATED_ROOT
        safe = _sanitize_skill_filename(skill_name)
        skill_path = _GENERATED_ROOT / f"{safe}.py"
        if skill_path.exists():
            code = skill_path.read_text(encoding="utf-8")
            parts.append(f"\n## Текущий код навыка\n```python\n{code[:3000]}\n```")
    except Exception:
        pass

    # Load failure examples from Redis
    try:
        from app.ai.orchestrator_memory import _SKILL_KEY_PREFIX, _INTENT_KEY_PREFIX
        from app.utils.redis_client import get_async_redis
        r = get_async_redis()

        # Skill stats
        raw = await r.get(_SKILL_KEY_PREFIX + skill_name)
        if raw:
            stats = json.loads(raw)
            failure_rate = stats.get("fail", 0) / max(1, stats.get("success", 0) + stats.get("fail", 0))
            parts.append(
                f"\n## Статистика использования\n"
                f"- Успехов: {stats.get('success', 0)}\n"
                f"- Сбоев: {stats.get('fail', 0)}\n"
                f"- Частота сбоев: {failure_rate:.1%}\n"
                f"- Среднее время: {int(stats.get('total_ms', 0) / max(1, stats.get('count_ms', 1)))} мс"
            )
    except Exception:
        pass

    # Common failure patterns
    parts.append(
        "\n## Задача по улучшению\n"
        "1. Исправь причины сбоев (добавь обработку ошибок, проверку входных данных)\n"
        "2. Верни `{'status': 'error', 'message': '...'}` вместо исключений\n"
        "3. Добавь retry для нестабильных операций (БД, внешние API)\n"
        "4. Логируй ошибки через structlog\n"
        "5. Не меняй сигнатуру `async def execute(args: dict) -> dict`\n"
        "6. Верни улучшенный ПОЛНЫЙ код модуля"
    )

    return "\n".join(parts)


# ── Evolution orchestration ────────────────────────────────────────────────────

async def evolve_skill(skill_name: str) -> bool:
    """Attempt to improve a skill. Returns True if a new version was deployed."""
    logger.info("skill_evolver_start", skill=skill_name)

    try:
        gap_context = await _build_evolution_context(skill_name)

        # Generate improved version with CapabilityBuilder + self_refine
        from app.ai.capability_builder import build_capability
        v2_name = f"{skill_name}.v2.{int(time.time())}"

        result = await build_capability(
            gap_description=gap_context,
            skill_name=v2_name,
        )

        if not result.ok:
            logger.warning("skill_evolver_build_failed", skill=skill_name, errors=result.errors)
            return False

        # Register shadow deployment
        config = ShadowConfig(
            skill_name=skill_name,
            v2_name=v2_name,
            started_at=time.time(),
        )
        await _save_shadow_config(config)

        logger.info(
            "skill_evolver_shadow_deployed",
            skill=skill_name,
            v2=v2_name,
            path=result.skill_path,
        )
        await _audit_log("shadow_deployed", skill_name, {"v2_name": v2_name})
        return True

    except Exception as exc:
        logger.error("skill_evolver_exception", skill=skill_name, error=str(exc))
        return False


# ── A/B decision ───────────────────────────────────────────────────────────────

async def evaluate_shadow_results() -> None:
    """Check all active shadow deployments and promote or rollback."""
    configs = await _load_all_shadow_configs()

    for config in configs:
        if config.total_calls < MIN_AB_CALLS:
            continue  # not enough data yet

        v2_wins = (config.v2_rate - config.v1_rate) >= WIN_MARGIN
        if v2_wins:
            await _promote_v2(config)
        else:
            await _rollback_v2(config)


async def _promote_v2(config: ShadowConfig) -> None:
    """Replace v1 with v2 as the canonical skill."""
    logger.info(
        "skill_evolver_promote",
        skill=config.skill_name,
        v1_rate=round(config.v1_rate, 3),
        v2_rate=round(config.v2_rate, 3),
    )
    try:
        from app.ai.capability_builder import _GENERATED_ROOT, _sanitize_skill_filename
        v1_path = _GENERATED_ROOT / f"{_sanitize_skill_filename(config.skill_name)}.py"
        v2_path = _GENERATED_ROOT / f"{_sanitize_skill_filename(config.v2_name)}.py"
        if v2_path.exists():
            v1_path.write_text(v2_path.read_text(encoding="utf-8"), encoding="utf-8")
            v2_path.unlink()
            logger.info("skill_evolver_v2_installed", skill=config.skill_name)
    except Exception as exc:
        logger.error("skill_evolver_promote_failed", skill=config.skill_name, error=str(exc))

    await _remove_shadow_config(config.skill_name)
    await _audit_log(
        "promoted",
        config.skill_name,
        {"v1_rate": config.v1_rate, "v2_rate": config.v2_rate},
    )


async def _rollback_v2(config: ShadowConfig) -> None:
    """Remove v2 (v1 stays)."""
    logger.info(
        "skill_evolver_rollback",
        skill=config.skill_name,
        v1_rate=round(config.v1_rate, 3),
        v2_rate=round(config.v2_rate, 3),
    )
    try:
        from app.ai.capability_builder import _GENERATED_ROOT, _sanitize_skill_filename
        v2_path = _GENERATED_ROOT / f"{_sanitize_skill_filename(config.v2_name)}.py"
        if v2_path.exists():
            v2_path.unlink()
    except Exception as exc:
        logger.warning("skill_evolver_rollback_cleanup", error=str(exc))

    await _remove_shadow_config(config.skill_name)
    await _audit_log(
        "rolled_back",
        config.skill_name,
        {"v1_rate": config.v1_rate, "v2_rate": config.v2_rate},
    )


# ── Shadow routing (call-time) ────────────────────────────────────────────────

async def maybe_route_to_shadow(skill_name: str, args: dict) -> str | None:
    """Return shadow skill name if this call should be routed to v2, else None.

    Called by the skill dispatcher before executing a skill.
    """
    import random
    if random.random() > SHADOW_TRAFFIC_PCT:
        return None

    config = await _load_shadow_config(skill_name)
    if config is None:
        return None

    return config.v2_name


async def record_shadow_outcome(skill_name: str, *, is_v2: bool, success: bool) -> None:
    """Update A/B stats after a skill call completes."""
    config = await _load_shadow_config(skill_name)
    if config is None:
        return
    if is_v2:
        config.v2_calls += 1
        if success:
            config.v2_success += 1
    else:
        config.v1_calls += 1
        if success:
            config.v1_success += 1
    await _save_shadow_config(config)


# ── Redis persistence ─────────────────────────────────────────────────────────

async def _save_shadow_config(config: ShadowConfig) -> None:
    try:
        from app.utils.redis_client import get_async_redis
        key = _SHADOW_KEY_PREFIX + config.skill_name
        await get_async_redis().setex(key, 7 * 24 * 3600, json.dumps(config.to_dict()))
    except Exception:
        pass


async def _load_shadow_config(skill_name: str) -> ShadowConfig | None:
    try:
        from app.utils.redis_client import get_async_redis
        raw = await get_async_redis().get(_SHADOW_KEY_PREFIX + skill_name)
        if raw:
            return ShadowConfig.from_dict(json.loads(raw))
    except Exception:
        pass
    return None


async def _load_all_shadow_configs() -> list[ShadowConfig]:
    try:
        from app.utils.redis_client import get_async_redis
        r = get_async_redis()
        keys = await r.keys(f"{_SHADOW_KEY_PREFIX}*")
        configs = []
        for key in keys:
            raw = await r.get(key)
            if raw:
                configs.append(ShadowConfig.from_dict(json.loads(raw)))
        return configs
    except Exception:
        return []


async def _remove_shadow_config(skill_name: str) -> None:
    try:
        from app.utils.redis_client import get_async_redis
        await get_async_redis().delete(_SHADOW_KEY_PREFIX + skill_name)
    except Exception:
        pass


async def _audit_log(action: str, skill: str, details: dict) -> None:
    try:
        from app.utils.redis_client import get_async_redis
        r = get_async_redis()
        entry = json.dumps({
            "ts": time.time(), "action": action,
            "skill": skill, **details,
        })
        await r.lpush(_AUDIT_KEY, entry)
        await r.ltrim(_AUDIT_KEY, 0, 999)  # keep last 1000 entries
    except Exception:
        pass


async def get_evolution_audit(limit: int = 50) -> list[dict]:
    """Return recent evolution audit log entries."""
    try:
        from app.utils.redis_client import get_async_redis
        raws = await get_async_redis().lrange(_AUDIT_KEY, 0, limit - 1)
        return [json.loads(r) for r in raws]
    except Exception:
        return []
