"""Во что обошлись облачные вызовы.

Токены телеметрия копит с самого начала, цена лежит в каталоге — но вместе их
никто не сводил, и вопрос «сколько мы тратим на облако» оставался без ответа.

Ключевое требование к отчёту: не выдавать неизвестное за нулевое. У
большинства моделей `cost_per_1k_*` не заполнена, и «$0» там означало бы
«ничего не потрачено», хотя значит «не знаем сколько».
"""

import pytest

from app.api import providers_api as p


@pytest.fixture
def telemetry(monkeypatch):
    def _set(rows):
        monkeypatch.setattr("app.ai.telemetry.get_summary", lambda: {"by_model": rows})

    return _set


@pytest.mark.asyncio
async def test_priced_cloud_model_is_counted(telemetry):
    telemetry(
        [
            {
                "task": "email_drafting",
                "model": "claude_sonnet_anthropic",
                "calls": 10,
                "tokens_in": 100_000,
                "tokens_out": 20_000,
            }
        ]
    )
    report = await p.cost_report()

    row = next(r for r in report.by_model if r.model == "claude_sonnet_anthropic")
    assert row.priced is True
    # 100k входных по $0.003/1k + 20k выходных по $0.015/1k
    assert row.cost_usd == pytest.approx(0.3 + 0.3, rel=1e-3)
    assert report.total_usd > 0


@pytest.mark.asyncio
async def test_one_model_used_by_several_tasks_is_summed_once(telemetry):
    """Телеметрия хранит строку на пару (задача, модель); расход считается по
    модели, иначе одна и та же трата попала бы в отчёт дважды."""
    telemetry(
        [
            {
                "task": "email_drafting",
                "model": "claude_sonnet_anthropic",
                "calls": 5,
                "tokens_in": 50_000,
                "tokens_out": 0,
            },
            {
                "task": "tool_calling",
                "model": "claude_sonnet_anthropic",
                "calls": 3,
                "tokens_in": 50_000,
                "tokens_out": 0,
            },
        ]
    )
    report = await p.cost_report()

    rows = [r for r in report.by_model if r.model == "claude_sonnet_anthropic"]
    assert len(rows) == 1
    assert rows[0].calls == 8
    assert rows[0].tokens_in == 100_000


@pytest.mark.asyncio
async def test_unknown_price_is_never_reported_as_zero(telemetry):
    telemetry(
        [
            {
                "task": "email_drafting",
                "model": "нет_такой_модели_в_каталоге",
                "calls": 12,
                "tokens_in": 9_000,
                "tokens_out": 1_000,
            }
        ]
    )
    report = await p.cost_report()

    row = next(r for r in report.by_model if r.model == "нет_такой_модели_в_каталоге")
    assert row.priced is False
    assert row.cost_usd is None
    # И модель попадает в список того, что мешает считать точно.
    assert "нет_такой_модели_в_каталоге" in report.unpriced_models


@pytest.mark.asyncio
async def test_local_models_are_not_listed_as_unpriced(telemetry):
    """Локальная модель денег не стоит — в списке «цена неизвестна» ей не
    место, иначе он превратится в шум из всего локального каталога."""
    telemetry(
        [
            {
                "task": "embedding",
                "model": "qwen3_embedding_4b_ollama",
                "calls": 1000,
                "tokens_in": 500_000,
                "tokens_out": 0,
            }
        ]
    )
    report = await p.cost_report()

    assert "qwen3_embedding_4b_ollama" not in report.unpriced_models
    assert all(r.model != "qwen3_embedding_4b_ollama" for r in report.by_model)


@pytest.mark.asyncio
async def test_empty_telemetry_gives_an_empty_report(telemetry):
    telemetry([])
    report = await p.cost_report()
    assert report.total_usd == 0
    assert report.by_model == []
    assert report.unpriced_models == []


@pytest.mark.asyncio
async def test_telemetry_failure_does_not_break_the_report(monkeypatch):
    def boom():
        raise RuntimeError("redis is down")

    monkeypatch.setattr("app.ai.telemetry.get_summary", boom)
    report = await p.cost_report()
    assert report.total_usd == 0
