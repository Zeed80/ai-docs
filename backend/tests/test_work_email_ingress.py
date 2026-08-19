"""Б16: email as an agent-instruction channel — marker detection and the
3-step (answer -> draft_reply -> send_reply) WorkOrder it creates.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy import select

from app.db.models import WorkStep
from app.domain.work_email_ingress import (
    create_work_order_from_email,
    is_agent_instruction_email,
)


@dataclass
class _FakeParsedEmail:
    message_id: str | None
    from_address: str
    subject: str
    body_text: str


def test_is_agent_instruction_email_matches_marker_case_insensitively():
    assert is_agent_instruction_email(
        _FakeParsedEmail("m1", "a@b.test", "Поручение: собрать отчёт", "текст")
    )
    assert is_agent_instruction_email(
        _FakeParsedEmail("m1", "a@b.test", "  ПОРУЧЕНИЕ:  собрать отчёт", "текст")
    )


def test_is_agent_instruction_email_false_for_unrelated_subject():
    assert not is_agent_instruction_email(
        _FakeParsedEmail("m1", "a@b.test", "Счёт №42 от поставщика", "текст")
    )
    assert not is_agent_instruction_email(
        _FakeParsedEmail("m1", "a@b.test", "", "текст")
    )


@pytest.mark.asyncio
async def test_create_work_order_from_email_builds_three_step_plan(db_session):
    parsed = _FakeParsedEmail(
        message_id="<abc@mail.test>",
        from_address="ivan@customer.test",
        subject="Поручение: собрать отчёт по счетам за август",
        body_text="Собери, пожалуйста, отчёт по счетам за август.",
    )

    order = await create_work_order_from_email(db_session, parsed)
    await db_session.flush()

    assert order.owner_key == "email:ivan@customer.test"
    assert order.objective == "собрать отчёт по счетам за август"
    assert order.source == "email"
    assert order.metadata_["email_from"] == "ivan@customer.test"
    assert order.metadata_["email_message_id"] == "<abc@mail.test>"

    steps = list(
        (
            await db_session.execute(
                select(WorkStep).where(WorkStep.work_order_id == order.id)
            )
        ).scalars()
    )
    by_key = {s.step_key: s for s in steps}
    assert set(by_key) == {"answer", "draft_reply", "send_reply"}

    assert by_key["answer"].kind == "agent_turn"
    assert by_key["answer"].depends_on == []
    assert "отчёт по счетам" in by_key["answer"].input_["prompt"]

    draft = by_key["draft_reply"]
    assert draft.kind == "capability"
    assert draft.capability == "email"
    assert draft.action == "draft"
    assert draft.depends_on == ["answer"]
    assert draft.input_["to_addresses"] == ["ivan@customer.test"]
    assert draft.input_["subject"] == "Re: Поручение: собрать отчёт по счетам за август"
    assert draft.input_["body_text"] == "${steps.answer.output.text}"

    send = by_key["send_reply"]
    assert send.kind == "capability"
    assert send.capability == "email"
    assert send.action == "send"
    assert send.depends_on == ["draft_reply"]
    assert send.input_["draft_id"] == "${steps.draft_reply.output.id}"
    # gate_actions: [send, ...] in capabilities.yml — no bypass, this step
    # is approval-gated exactly like any other agent-composed email send.
    assert send.risk_level == "high"


@pytest.mark.asyncio
async def test_create_work_order_from_email_falls_back_to_body_when_subject_empty(db_session):
    parsed = _FakeParsedEmail(
        message_id="<x@mail.test>",
        from_address="a@b.test",
        subject="",
        body_text="Собери отчёт по остаткам склада, пожалуйста, самое главное сделай быстро",
    )

    order = await create_work_order_from_email(db_session, parsed)

    assert order.objective.startswith("Собери отчёт по остаткам склада")


@pytest.mark.asyncio
async def test_create_work_order_from_email_reply_subject_avoids_double_re(db_session):
    parsed = _FakeParsedEmail(
        message_id="<x@mail.test>",
        from_address="a@b.test",
        subject="Re: Поручение: свериться с поставщиком",
        body_text="текст",
    )

    order = await create_work_order_from_email(db_session, parsed)
    await db_session.flush()

    draft = (
        await db_session.execute(
            select(WorkStep).where(
                WorkStep.work_order_id == order.id, WorkStep.step_key == "draft_reply"
            )
        )
    ).scalar_one()
    assert draft.input_["subject"] == "Re: Поручение: свериться с поставщиком"
