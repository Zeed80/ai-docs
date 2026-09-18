"""Точечный переспрос спорного Ø ступени: принимается только согласное с замером."""

from __future__ import annotations

import asyncio
import io

from PIL import Image

from app.ai.cad_recognize.verifiers.reask import reask_disputed


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("L", (1200, 800), 255).save(buffer, format="PNG")
    return buffer.getvalue()


SPEC = {
    "main_view": {
        "outer": [
            {"diameter_mm": 25.0, "length_mm": 12.0},
            {"diameter_mm": 40.0, "length_mm": 30.0},  # на листе Ø28
            {"diameter_mm": 40.0, "length_mm": 80.0},
        ]
    },
    "dimensions": [{"value": "Ø25"}, {"value": "Ø40"}],
}
REPORT = {
    "frame": {"origin_px": [100.0, 400.0], "mm_per_px": 0.2},
    "items": [
        {
            "kind": "shaft_step",
            "path": "main_view.outer[1]",
            "read": {"diameter_mm": 40.0, "length_mm": 30.0},
            "measured": {"diameter_mm": 27.917, "length_mm": 29.95},
            "status": "refuted",
            "reason": "",
            "tolerance_mm": {"diameter": 0.3, "length": 0.5},
        }
    ],
    "summary": {"checked": 1, "confirmed": 0, "refuted": 1, "unmeasurable": 0},
}
DECISION = {
    "kind": "shaft_step",
    "path": "main_view.outer[1]",
    "field": "diameter_mm",
    "read": 40.0,
    "measured": 27.917,
    "action": "ask_human",
    "reason": "прочитанное 40 тоже есть на листе",
}


def _run(answer: dict):
    seen = []

    async def ask(prompt, crop):
        seen.append((prompt, crop.size))
        return answer

    result = asyncio.run(reask_disputed(_png(), SPEC, REPORT, [DECISION], ask=ask))
    return result, seen


def test_an_answer_agreeing_with_the_measurement_is_adopted():
    (spec, report, decisions, log), seen = _run({"value": 28})

    assert spec["main_view"]["outer"][1]["diameter_mm"] == 28.0
    assert decisions[0]["action"] == "adopt" and decisions[0]["value"] == 28.0
    assert decisions[0]["source"] == "reask"
    assert report["items"][0]["status"] == "confirmed"
    assert "согласен с замером" in log[0]["outcome"]
    # Модели не показывают ни прочитанное, ни замер.
    prompt = seen[0][0]
    assert "40" not in prompt.split("Слева")[0] and "27.9" not in prompt and "28" not in prompt


def test_repeating_the_read_value_leaves_it_to_a_person():
    (spec, _report, decisions, log), _ = _run({"value": 40})

    assert spec["main_view"]["outer"][1]["diameter_mm"] == 40.0
    assert decisions[0]["action"] == "ask_human"
    assert "повторила прочитанное" in log[0]["outcome"]


def test_no_answer_leaves_it_to_a_person():
    (spec, _report, decisions, log), _ = _run({"value": None})

    assert spec["main_view"]["outer"][1]["diameter_mm"] == 40.0
    assert decisions[0]["action"] == "ask_human"
    assert log[0]["outcome"] == "модель не ответила"


def test_the_crop_is_around_the_disputed_step():
    _result, seen = _run({"value": None})

    # Ступень 12…42 мм при 0,2 мм/px — фрагмент ~ (30 + 30) мм / 0,2 по ширине.
    width, height = seen[0][1]
    assert 250 <= width <= 350
    assert height < 800


def _two_disputes() -> list[dict]:
    second = {**DECISION, "path": "main_view.outer[2]", "read": 40.0, "measured": 35.0}
    return [DECISION, second]


def _report_two() -> dict:
    item = {**REPORT["items"][0], "path": "main_view.outer[2]"}
    item["measured"] = {"diameter_mm": 35.0, "length_mm": 80.0}
    return {**REPORT, "items": [REPORT["items"][0], item]}


def test_a_second_round_only_follows_a_round_that_settled_something():
    """Ф8, правило остановки: круг, ничего не решивший, не повторяется —
    тот же вопрос даст тот же ответ, а вызов модели локально ~15 с."""
    from app.ai.cad_recognize.verifiers.reask import reask_until_settled

    answers = iter([{"value": 28}, {"value": 40}, {"value": 35}])
    sizes = []

    async def ask(prompt, crop):
        sizes.append(crop.size)
        return next(answers)

    spec, _report, decisions, log, reason = asyncio.run(
        reask_until_settled(_png(), SPEC, _report_two(), _two_disputes(), ask=ask)
    )

    # Круг 1: Ø28 принят, Ø35 модель повторила прочитанное → расхождений меньше.
    # Круг 2 — по вырезу шире: Ø35 принят.
    assert [entry["round"] for entry in log] == [1, 1, 2]
    assert sizes[2][0] > sizes[1][0]
    assert spec["main_view"]["outer"][2]["diameter_mm"] == 35.0
    assert all(decision["action"] == "adopt" for decision in decisions)
    assert reason == "расхождений не осталось"


def test_a_round_that_settles_nothing_stops_the_questions():
    from app.ai.cad_recognize.verifiers.reask import reask_until_settled

    calls = []

    async def ask(prompt, crop):
        calls.append(prompt)
        return {"value": 40}

    *_rest, log, reason = asyncio.run(
        reask_until_settled(_png(), SPEC, _report_two(), _two_disputes(), ask=ask)
    )

    assert len(calls) == 2 and all(entry["round"] == 1 for entry in log)
    assert "не уменьшил" in reason


def test_the_question_budget_per_sheet_is_kept():
    from app.ai.cad_recognize.verifiers.reask import reask_until_settled

    calls = []

    async def ask(prompt, crop):
        calls.append(prompt)
        return {"value": 28}

    *_rest, reason = asyncio.run(
        reask_until_settled(_png(), SPEC, _report_two(), _two_disputes(), ask=ask, budget=1)
    )

    assert len(calls) == 1
    assert "бюджет" in reason
