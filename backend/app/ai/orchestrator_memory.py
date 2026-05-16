"""Orchestrator feedback memory — adaptive skill routing based on outcome history.

Records tool call outcomes after each turn and provides preference hints to the
planner so it learns which skills work for which intents over time.

Storage: Redis keys with 30-day TTL (no DB writes — operational data only).
  orchestrator:skill:{skill} → JSON {success, fail, total_ms, count_ms, last_at}
  orchestrator:intent:{hash} → JSON list[{skill, outcome, ms, ts}] (last 30 entries)
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()

_TTL_SECONDS = 30 * 24 * 3600  # 30 days
_SKILL_KEY_PREFIX = "orchestrator:skill:"
_INTENT_KEY_PREFIX = "orchestrator:intent:"
_USER_RATING_KEY_PREFIX = "user:skill_rating:"
_MAX_INTENT_ENTRIES = 30


# ── Redis helpers ─────────────────────────────────────────────────────────────

def _redis():
    """Return a Redis client or None if unavailable."""
    try:
        from app.utils.redis_client import get_sync_redis
        return get_sync_redis()
    except Exception:
        return None


# ── Feedback recording ────────────────────────────────────────────────────────

@dataclass
class TurnFeedback:
    intent_text: str              # raw user message (first 300 chars)
    intent_category: str          # from plan.worker.role / intent field
    skills_planned: list[str]     # from plan.worker.recommended_skills
    skills_used: list[str]        # from trace.tool_calls (sanitised names)
    audit_passed: bool
    retries: int = 0
    duration_ms: int = 0
    errors: list[str] = field(default_factory=list)


def record_turn_feedback(feedback: TurnFeedback) -> None:
    """Write outcome data to Redis. Fire-and-forget — exceptions are swallowed."""
    try:
        r = _redis()
        if r is None:
            return
        now = time.time()

        # Update per-skill stats
        for skill in feedback.skills_used:
            key = _SKILL_KEY_PREFIX + skill
            raw = r.get(key)
            stats: dict[str, Any] = json.loads(raw) if raw else {
                "success": 0, "fail": 0,
                "total_ms": 0, "count_ms": 0, "last_at": 0,
            }
            if feedback.audit_passed:
                stats["success"] += 1
            else:
                stats["fail"] += 1
            if feedback.duration_ms:
                stats["total_ms"] = stats.get("total_ms", 0) + feedback.duration_ms
                stats["count_ms"] = stats.get("count_ms", 0) + 1
            stats["last_at"] = now
            r.setex(key, _TTL_SECONDS, json.dumps(stats))

        # Update intent→skills mapping (circular buffer of last N entries)
        intent_hash = _hash_intent(feedback.intent_text, feedback.intent_category)
        key = _INTENT_KEY_PREFIX + intent_hash
        raw = r.get(key)
        entries: list[dict] = json.loads(raw) if raw else []
        for skill in feedback.skills_used:
            entries.append({
                "skill": skill,
                "outcome": "success" if feedback.audit_passed else "fail",
                "ms": feedback.duration_ms,
                "ts": now,
                "retries": feedback.retries,
            })
        # Keep last N entries
        entries = entries[-_MAX_INTENT_ENTRIES:]
        r.setex(key, _TTL_SECONDS, json.dumps(entries))

        logger.info(
            "orchestrator_feedback_recorded",
            category=feedback.intent_category,
            skills_used=feedback.skills_used,
            audit_passed=feedback.audit_passed,
            retries=feedback.retries,
            duration_ms=feedback.duration_ms,
        )
    except Exception as exc:
        logger.warning("orchestrator_feedback_write_failed", error=str(exc))


# ── Preference reading ────────────────────────────────────────────────────────

@dataclass
class SkillScore:
    name: str
    success: int
    fail: int
    avg_ms: int
    last_at: float

    @property
    def success_rate(self) -> float:
        total = self.success + self.fail
        return self.success / total if total else 0.0

    @property
    def total(self) -> int:
        return self.success + self.fail


def get_skill_scores(skills: list[str]) -> list[SkillScore]:
    """Return SkillScore objects for the given skill names."""
    try:
        r = _redis()
        if r is None:
            return []
        scores: list[SkillScore] = []
        for skill in skills:
            raw = r.get(_SKILL_KEY_PREFIX + skill)
            if not raw:
                continue
            d = json.loads(raw)
            count_ms = d.get("count_ms", 0)
            avg_ms = int(d["total_ms"] / count_ms) if count_ms else 0
            scores.append(SkillScore(
                name=skill,
                success=d.get("success", 0),
                fail=d.get("fail", 0),
                avg_ms=avg_ms,
                last_at=d.get("last_at", 0),
            ))
        return scores
    except Exception:
        return []


def get_intent_history(intent_text: str, intent_category: str) -> list[dict]:
    """Return recent skill outcome entries for this intent category."""
    try:
        r = _redis()
        if r is None:
            return []
        key = _INTENT_KEY_PREFIX + _hash_intent(intent_text, intent_category)
        raw = r.get(key)
        return json.loads(raw) if raw else []
    except Exception:
        return []


def build_tool_preference_hint(
    intent_text: str,
    intent_category: str,
    candidate_skills: list[str] | None = None,
) -> str:
    """Return a compact preference hint string for the orchestrator prompt.

    Returns empty string if there is no history or Redis is unavailable.
    """
    try:
        # 1. Intent-specific history (recent 30 entries for this category)
        history = get_intent_history(intent_text, intent_category)
        if not history:
            return ""

        # Aggregate: skill → {success, fail}
        agg: dict[str, dict[str, int]] = {}
        for entry in history:
            skill = entry.get("skill", "")
            if not skill:
                continue
            if skill not in agg:
                agg[skill] = {"success": 0, "fail": 0, "total_retries": 0}
            if entry.get("outcome") == "success":
                agg[skill]["success"] += 1
            else:
                agg[skill]["fail"] += 1
            agg[skill]["total_retries"] = agg[skill].get("total_retries", 0) + entry.get("retries", 0)

        if not agg:
            return ""

        # 2. Sort: prefer high success_rate, penalise high retries
        def score(stat: dict) -> float:
            total = stat["success"] + stat["fail"]
            if total == 0:
                return 0.0
            rate = stat["success"] / total
            retry_penalty = min(stat.get("total_retries", 0) / max(total, 1) * 0.1, 0.3)
            return rate - retry_penalty

        sorted_skills = sorted(agg.items(), key=lambda kv: score(kv[1]), reverse=True)

        preferred = [s for s, st in sorted_skills if score(st) >= 0.6][:5]
        avoid = [s for s, st in sorted_skills if score(st) < 0.3 and (st["success"] + st["fail"]) >= 2][:3]

        lines: list[str] = ["История использования инструментов для похожих задач:"]
        if preferred:
            lines.append(f"  Предпочтительные (высокий процент успеха): {', '.join(preferred)}")
        if avoid:
            lines.append(f"  Избегать (частые сбои): {', '.join(avoid)}")

        # 3. Global skill stats for additional context (if candidate_skills given)
        if candidate_skills:
            global_scores = get_skill_scores(candidate_skills)
            never_failed = [s.name for s in global_scores if s.fail == 0 and s.success >= 3]
            if never_failed:
                lines.append(f"  Глобально надёжные (≥3 успехов, 0 сбоев): {', '.join(never_failed[:5])}")

        # 4. User thumbs up/down ratings
        if candidate_skills:
            user_hint = get_user_rating_hint(candidate_skills)
            if user_hint:
                lines.append(user_hint)

        return "\n".join(lines)
    except Exception as exc:
        logger.warning("orchestrator_preference_hint_failed", error=str(exc))
        return ""


# ── User rating feedback ──────────────────────────────────────────────────────


def record_user_rating(tools_used: list[str], rating: int, session_id: str = "") -> None:
    """Persist a thumbs-up (+1) or thumbs-down (-1) vote for the tools used in a turn."""
    try:
        r = _redis()
        if r is None or not tools_used:
            return
        now = time.time()
        for tool in tools_used:
            key = _USER_RATING_KEY_PREFIX + tool
            raw = r.get(key)
            stats: dict[str, Any] = json.loads(raw) if raw else {"up": 0, "down": 0, "last_at": 0}
            if rating > 0:
                stats["up"] = stats.get("up", 0) + 1
            elif rating < 0:
                stats["down"] = stats.get("down", 0) + 1
            stats["last_at"] = now
            r.setex(key, _TTL_SECONDS, json.dumps(stats))
        logger.info(
            "user_rating_recorded",
            tools=tools_used,
            rating=rating,
            session_id=session_id,
        )
    except Exception as exc:
        logger.warning("user_rating_write_failed", error=str(exc))


def get_user_rating_hint(candidate_skills: list[str]) -> str:
    """Return a compact hint about user ratings for the given skills."""
    try:
        r = _redis()
        if r is None or not candidate_skills:
            return ""
        loved: list[str] = []
        disliked: list[str] = []
        for skill in candidate_skills:
            raw = r.get(_USER_RATING_KEY_PREFIX + skill)
            if not raw:
                continue
            d = json.loads(raw)
            up = d.get("up", 0)
            down = d.get("down", 0)
            total = up + down
            if total < 2:
                continue
            rate = up / total
            if rate >= 0.7:
                loved.append(skill)
            elif rate <= 0.35:
                disliked.append(skill)
        if not loved and not disliked:
            return ""
        lines = ["Оценки пользователя:"]
        if loved:
            lines.append(f"  Нравится (👍 чаще): {', '.join(loved[:5])}")
        if disliked:
            lines.append(f"  Не нравится (👎 чаще): {', '.join(disliked[:3])}")
        return "\n".join(lines)
    except Exception:
        return ""


# ── Internal helpers ──────────────────────────────────────────────────────────

def _hash_intent(text: str, category: str) -> str:
    """Stable short hash for intent text + category."""
    raw = f"{category}:{text[:200].lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
