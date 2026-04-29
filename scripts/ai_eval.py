from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.ai.providers.base import AIProvider
from backend.app.ai.evals.harness import ModelEvalHarness
from backend.app.ai.model_registry import ModelRegistry
from backend.app.ai.router import AIRouter
from backend.app.ai.schemas import AIRequest, AIResponse, AITask, ProviderConfig, ProviderKind


REGISTRY_PATH = ROOT / "backend" / "app" / "ai" / "config" / "model_registry.yaml"
CASES_PATH = ROOT / "backend" / "app" / "ai" / "evals" / "cases.yaml"


class MockEvalProvider(AIProvider):
    kind = ProviderKind.OLLAMA

    def __init__(self) -> None:
        super().__init__(ProviderConfig(kind=ProviderKind.OLLAMA, base_url="mock://eval"))

    async def chat(self, request: AIRequest, model: str) -> AIResponse:
        text = (
            "supplier invoice total material tolerance question confirm delivery price "
            "operation inspection warning"
        )
        return AIResponse(task=request.task, provider=self.kind, model=model, text=text)

    async def vision(self, request: AIRequest, model: str) -> AIResponse:
        return await self.chat(request, model)

    async def embedding(self, request: AIRequest, model: str) -> AIResponse:
        return AIResponse(task=request.task, provider=self.kind, model=model, embedding=[0.0])


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run AI model regression checks.")
    parser.add_argument("--model", default=None, help="Registry model name to evaluate.")
    parser.add_argument("--task", default=None, help="Optional task filter, e.g. invoice_ocr.")
    parser.add_argument("--mock", action="store_true", help="Use deterministic mock provider for CI.")
    args = parser.parse_args()

    registry = ModelRegistry.from_yaml(REGISTRY_PATH)
    use_mock = args.mock or os.getenv("AI_EVAL_MOCK", "").lower() in {"1", "true", "yes"}
    providers = {ProviderKind.OLLAMA: MockEvalProvider()} if use_mock else None
    router = AIRouter(registry, providers=providers)
    harness = ModelEvalHarness(router)
    cases = harness.load_cases(CASES_PATH)
    task_name = args.task or None
    model_name = args.model or None
    if task_name:
        task = AITask(task_name)
        cases = [case for case in cases if case.task == task]
    if not cases:
        print("No eval cases selected.")
        return 2

    results = await harness.run(cases, model=model_name)
    passed = 0
    for result in results:
        marker = "PASS" if result.passed else "FAIL"
        print(f"{marker} {result.case_id} model={result.model} score={result.score:.2f} {result.reason}")
        passed += int(result.passed)
    print(f"Summary: {passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
