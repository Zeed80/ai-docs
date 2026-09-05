"""Сгенерированный моделью код должен доходить до файла.

`build_capability` читал ответ роутера как `response.content`. Такого поля у
`AIResponse` нет — текст лежит в `.text`. Обращение бросало AttributeError,
его ловил общий `except`, писал в лог `capability_builder_llm_failed` и
подставлял заглушку. То есть генерация не срабатывала НИ РАЗУ, а в журнале
это выглядело как «модель не справилась»: ошибка называлась не своим именем,
и чинить шли не туда.
"""

from __future__ import annotations

import pytest

from app.ai import capability_builder as cb
from app.ai.schemas import AIResponse, AITask, ProviderKind

_CODE = '''"""Заглушка теста."""

async def execute(args: dict) -> dict:
    return {"ok": True, "marker": "СГЕНЕРИРОВАНО-МОДЕЛЬЮ"}
'''


class _Router:
    def __init__(self, text: str | None):
        self._text = text

    async def run(self, request):
        return AIResponse(
            task=AITask.CODE_GENERATION,
            provider=ProviderKind.OLLAMA,
            model="mock",
            text=self._text,
        )


@pytest.fixture(autouse=True)
def _no_refine(monkeypatch):
    """Цикл самокритики — отдельная история; здесь проверяется первый проход."""

    async def _refine(code, gap, gen, max_rounds=2):
        raise RuntimeError("refine отключён в этом тесте")

    monkeypatch.setattr("app.ai.self_refine.refine_code", _refine)


def _patch_router(monkeypatch, text: str | None) -> None:
    class _AIRouter:
        def __init__(self, *a, **k):
            pass

        async def run(self, request):
            return await _Router(text).run(request)

    monkeypatch.setattr("app.ai.router.AIRouter", _AIRouter)


async def test_generated_code_reaches_the_file(monkeypatch, tmp_path):
    _patch_router(monkeypatch, f"```python\n{_CODE}```")
    monkeypatch.setattr(cb, "_GENERATED_ROOT", tmp_path)

    result = await cb.build_capability("нужна выгрузка по КПЭ", skill_name="reports.kpi")

    written = (tmp_path / "reports_kpi.py").read_text(encoding="utf-8")
    assert "СГЕНЕРИРОВАНО-МОДЕЛЬЮ" in written, (
        "в файл попала заглушка вместо ответа модели — значит текст ответа снова "
        "читается не из того поля"
    )
    assert result.skill_name == "reports.kpi"


async def test_empty_answer_still_falls_back_to_a_stub(monkeypatch, tmp_path):
    """Пустой ответ — законный повод для заглушки, в отличие от опечатки в поле."""
    _patch_router(monkeypatch, None)
    monkeypatch.setattr(cb, "_GENERATED_ROOT", tmp_path)

    await cb.build_capability("нужна выгрузка по КПЭ", skill_name="reports.kpi")

    written = (tmp_path / "reports_kpi.py").read_text(encoding="utf-8")
    assert "agent_generated_stub" in written
