"""Self-refinement loop — critique → revise for any LLM generation task.

Especially effective for:
- Skill code generation (CapabilityBuilder)
- Table construction
- Email drafting
- Complex planning

The critique model scores the output and lists specific problems.
The revise step fixes only the identified problems.
Both steps can use the same or different models.

Reference: "Self-Refine: Iterative Refinement with Self-Feedback" (Madaan et al. 2023)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

import structlog

logger = structlog.get_logger()

# ── Prompts ────────────────────────────────────────────────────────────────────

_CRITIQUE_SYSTEM = """Ты критик-рецензент вывода AI-системы.
Твоя задача — объективно оценить качество ответа и выявить конкретные проблемы.
Отвечай строго в JSON."""

_CRITIQUE_PROMPT = """\
Задача, которую выполнял AI: {task}

Ответ AI:
{response}

Оцени ответ по следующим критериям и верни JSON:
{{
  "score": <число 1-10; 10 = идеально>,
  "issues": [
    "<конкретная проблема 1>",
    "<конкретная проблема 2>"
  ],
  "strengths": ["<что сделано хорошо>"],
  "can_improve": <true если score < {threshold}>
}}

Будь конкретным: указывай строки кода, поля, логические ошибки — не общие слова."""

_REVISE_PROMPT = """\
Твой предыдущий ответ на задачу:
{task}

Содержал следующие проблемы:
{issues}

Исправь ТОЛЬКО эти проблемы. Не переписывай то, что работало правильно.
Верни исправленную версию ответа (без объяснений, только результат):"""

_CODE_CRITIQUE_SYSTEM = """Ты старший Python-разработчик, проверяешь сгенерированный AI код.
Отвечай строго в JSON."""

_CODE_CRITIQUE_PROMPT = """\
Задача для кода: {task}

Сгенерированный Python-модуль:
```python
{code}
```

Проверь по критериям:
1. Синтаксис и запускаемость
2. Наличие `async def execute(args: dict) -> dict`
3. Наличие `SKILL_META` словаря
4. Обработка ошибок (try/except)
5. Возврат структурированного dict (status, message, data)
6. Нет заглушек `raise NotImplementedError`
7. Корректность импортов

Верни JSON:
{{
  "score": <1-10>,
  "issues": ["<проблема>"],
  "has_execute": <bool>,
  "has_skill_meta": <bool>,
  "has_error_handling": <bool>,
  "is_stub": <bool>,
  "can_improve": <bool>
}}"""


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class CritiqueResult:
    score: float
    issues: list[str]
    strengths: list[str]
    can_improve: bool
    raw: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.score >= 8.0 and not self.can_improve


@dataclass
class RefineResult:
    final_response: str
    rounds_used: int
    final_score: float
    critique_history: list[CritiqueResult]

    @property
    def improved(self) -> bool:
        if len(self.critique_history) >= 2:
            return self.critique_history[-1].score > self.critique_history[0].score
        return False


# ── Core refinement loop ──────────────────────────────────────────────────────

async def refine(
    response: str,
    task: str,
    generate_fn: Callable[[str, str | None], Coroutine[Any, Any, str]],
    *,
    max_rounds: int = 3,
    target_score: float = 8.0,
    mode: str = "general",
) -> RefineResult:
    """Run the self-refinement loop on an initial response.

    Args:
        response: Initial LLM output to refine.
        task: Description of the original task (for critique context).
        generate_fn: async (prompt, system_prompt) -> str
        max_rounds: Maximum critique-revise cycles.
        target_score: Stop early if critique score reaches this threshold.
        mode: "general" | "code" — selects appropriate critique prompts.
    """
    history: list[CritiqueResult] = []
    current = response

    for round_n in range(max_rounds):
        # Critique current response
        critique = await _critique(current, task, generate_fn, mode=mode)
        history.append(critique)

        logger.info(
            "self_refine_round",
            round=round_n + 1,
            score=critique.score,
            issues_count=len(critique.issues),
            can_improve=critique.can_improve,
        )

        if critique.passed or critique.score >= target_score:
            break

        if not critique.issues:
            break

        # Revise
        issues_text = "\n".join(f"- {issue}" for issue in critique.issues)
        revise_prompt = _REVISE_PROMPT.format(
            task=task, issues=issues_text
        )
        current = await generate_fn(revise_prompt, None)

    final_score = history[-1].score if history else 0.0
    return RefineResult(
        final_response=current,
        rounds_used=len(history),
        final_score=final_score,
        critique_history=history,
    )


async def refine_code(
    code: str,
    task: str,
    generate_fn: Callable[[str, str | None], Coroutine[Any, Any, str]],
    *,
    max_rounds: int = 3,
    target_score: float = 8.5,
) -> RefineResult:
    """Specialized refinement for Python skill code."""
    return await refine(
        code, task, generate_fn,
        max_rounds=max_rounds,
        target_score=target_score,
        mode="code",
    )


# ── Critique helper ────────────────────────────────────────────────────────────

async def _critique(
    response: str,
    task: str,
    generate_fn: Callable[[str, str | None], Coroutine[Any, Any, str]],
    *,
    mode: str = "general",
    target_score: float = 8.0,
) -> CritiqueResult:
    if mode == "code":
        system = _CODE_CRITIQUE_SYSTEM
        prompt = _CODE_CRITIQUE_PROMPT.format(task=task, code=response)
    else:
        system = _CRITIQUE_SYSTEM
        prompt = _CRITIQUE_PROMPT.format(
            task=task, response=response, threshold=int(target_score)
        )

    raw_text = await generate_fn(prompt, system)

    from app.ai.structured_output import parse_json_output
    parsed = parse_json_output(raw_text, default={})

    return CritiqueResult(
        score=float(parsed.get("score", 5.0)),
        issues=parsed.get("issues", []),
        strengths=parsed.get("strengths", []),
        can_improve=bool(parsed.get("can_improve", True)),
        raw=parsed,
    )
