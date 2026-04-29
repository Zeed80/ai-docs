from __future__ import annotations

from pathlib import Path

import yaml

from backend.app.ai.router import AIRouter
from backend.app.ai.schemas import AIRequest, ChatMessage, EvalCase, EvalResult


class ModelEvalHarness:
    def __init__(self, router: AIRouter) -> None:
        self.router = router

    @classmethod
    def load_cases(cls, path: str | Path) -> list[EvalCase]:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return [EvalCase(**item) for item in raw.get("cases", [])]

    async def run_case(self, case: EvalCase, model: str | None = None) -> EvalResult:
        response = await self.router.run(
            AIRequest(
                task=case.task,
                messages=[ChatMessage(role="user", content=case.prompt)],
                confidential=case.confidential,
                preferred_model=model,
            )
        )
        text = response.text or ""
        missing = [needle for needle in case.expected_contains if needle.lower() not in text.lower()]
        passed = not missing
        score = 1.0 if passed else max(0.0, 1.0 - len(missing) / max(len(case.expected_contains), 1))
        reason = "ok" if passed else f"missing expected fragments: {', '.join(missing)}"
        return EvalResult(
            case_id=case.id,
            task=case.task,
            model=response.model,
            passed=passed,
            score=score,
            reason=reason,
            response_text=text,
        )

    async def run(self, cases: list[EvalCase], model: str | None = None) -> list[EvalResult]:
        return [await self.run_case(case, model=model) for case in cases]

