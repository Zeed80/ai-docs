"""Durable intake, ownership, event replay and conservative worker-loss behavior."""

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.chat_runs import ChatRunCreate, submit_chat_run
from app.auth.jwt import _DEV_USER
from app.db.agent_runtime_models import AgentChannelIdentity, AgentOutbox, DurableChatRun
from app.db.models import ChatMessage, ChatSession, User, WorkEvent, WorkOrder, WorkStep
from app.db.work_budget_models import WorkBudgetLedger, WorkBudgetReservation
from app.domain.agent_intake import (
    AgentIntakeRequest,
    IntakeAttachment,
    IntakeNotFoundError,
    IntakeValidationError,
    VerifiedIntakeIdentity,
    submit_agent_intake,
)
from app.domain.work_orders import claim_ready_step, reclaim_expired_leases
from app.tasks.durable_chat import run_durable_chat


def request(**kwargs):
    return {"request_id": str(uuid.uuid4()), "content": "Return a short answer", **kwargs}


@pytest.mark.asyncio
async def test_intake_idempotency_and_conflicts(client, db_session):
    body = request()
    first = await client.post("/api/agent/chat-runs", json=body)
    assert first.status_code == 202, first.text
    run = first.json()
    retry = await client.post("/api/agent/chat-runs", json=body)
    assert retry.json() == run
    conflict = await client.post("/api/agent/chat-runs", json={**body, "content": "Different"})
    assert conflict.status_code == 409
    busy = await client.post("/api/agent/chat-runs", json=request(session_id=run["session_id"]))
    assert busy.status_code == 409
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DurableChatRun)
            .where(
                DurableChatRun.request_id == uuid.UUID(body["request_id"]),
                DurableChatRun.owner_key == _DEV_USER.sub,
            )
        )
        == 1
    )
    step = await db_session.scalar(
        select(WorkStep).where(WorkStep.work_order_id == uuid.UUID(run["work_order_id"]))
    )
    assert step.max_attempts == 1
    order = await db_session.get(WorkOrder, uuid.UUID(run["work_order_id"]))
    assert order.budgets["max_replans"] == 0
    assert order.budget_ledger_id is not None
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(WorkBudgetLedger)
            .where(WorkBudgetLedger.root_work_order_id == order.id)
        )
        == 1
    )
    assert (await client.post(f"/api/work-orders/{run['work_order_id']}/run")).status_code == 409
    assert (
        await client.post(
            f"/api/work-orders/{run['work_order_id']}/instructions",
            json={"instruction": "Repeat the action"},
        )
    ).status_code == 409
    events = (await client.get(f"/api/agent/chat-runs/{run['id']}/events?limit=1")).json()
    assert len(events["items"]) == 1
    cursor = events["next_cursor"]
    later = (await client.get(f"/api/agent/chat-runs/{run['id']}/events?after={cursor}")).json()
    assert all(e["sequence"] > cursor for e in later["items"])


@pytest.mark.asyncio
async def test_http_retry_matches_pre_namespace_digest(client, db_session):
    body = request()
    first = await client.post("/api/agent/chat-runs", json=body)
    assert first.status_code == 202
    run = await db_session.get(DurableChatRun, uuid.UUID(first.json()["id"]))
    # This is exactly the digest persisted before E12's channel namespace.
    run.request_digest = hashlib.sha256(
        ChatRunCreate(**body).model_dump_json().encode()
    ).hexdigest()
    await db_session.commit()

    retry = await client.post("/api/agent/chat-runs", json=body)
    assert retry.status_code == 202
    assert retry.json() == first.json()


@pytest.mark.asyncio
async def test_http_attachment_metadata_remains_part_of_idempotency(client, db_session):
    from app.db.models import Document

    document = Document(
        owner_sub=_DEV_USER.sub,
        file_name="real.pdf",
        file_hash="b" * 64,
        file_size=1,
        mime_type="application/pdf",
        storage_path="test/metadata",
    )
    db_session.add(document)
    await db_session.commit()
    body = request(
        attachments=[
            {
                "document_id": str(document.id),
                "file_name": "first-name.pdf",
                "mime_type": "application/pdf",
                "size_bytes": 1,
            }
        ]
    )
    assert (await client.post("/api/agent/chat-runs", json=body)).status_code == 202
    changed = {
        **body,
        "attachments": [{**body["attachments"][0], "file_name": "changed-name.pdf"}],
    }
    assert (await client.post("/api/agent/chat-runs", json=changed)).status_code == 409


@pytest.mark.asyncio
async def test_foreign_owner_and_service_are_denied(client):
    from app.auth.jwt import get_current_user
    from app.main import app

    run = (await client.post("/api/agent/chat-runs", json=request())).json()
    original = app.dependency_overrides.copy()
    try:
        app.dependency_overrides[get_current_user] = lambda: _DEV_USER.model_copy(
            update={"sub": "other"}
        )
        assert (await client.get(f"/api/agent/chat-runs/{run['id']}")).status_code == 404
        assert (await client.get(f"/api/agent/chat-runs/{run['id']}/events")).status_code == 404
        assert (
            await client.post("/api/agent/chat-runs", json=request(session_id=run["session_id"]))
        ).status_code == 404
        app.dependency_overrides[get_current_user] = lambda: _DEV_USER.model_copy(
            update={"via_agent": True}
        )
        assert (await client.post("/api/agent/chat-runs", json=request())).status_code == 403
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original)


@pytest.mark.asyncio
async def test_concurrent_retry_has_one_committed_request(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    body = ChatRunCreate(**request())

    async def submit():
        async with factory() as db:
            return await submit_chat_run(body, db, _DEV_USER)

    a, b = await asyncio.gather(submit(), submit())
    assert a["id"] == b["id"]
    async with factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(DurableChatRun)
                .where(DurableChatRun.request_id == body.request_id)
            )
            == 1
        )


@pytest.mark.asyncio
async def test_cross_channel_external_ids_are_namespaced_by_verified_identity(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    request_id = uuid.uuid4()
    async with factory() as db:
        binding = AgentChannelIdentity(
            owner_key=_DEV_USER.sub, channel="telegram", external_id="770001"
        )
        db.add(binding)
        await db.commit()

    async def submit(channel):
        async with factory() as db:
            return await submit_agent_intake(
                db,
                identity=VerifiedIntakeIdentity(account_key=_DEV_USER.sub, channel=channel),
                request=AgentIntakeRequest(
                    channel=channel,
                    # A Telegram update can legitimately have the same text
                    # as an HTTP client's UUID.  Channel is part of the key.
                    external_message_id=str(request_id),
                    request_id=request_id,
                    content="Same external identifier on another channel",
                    source_binding_id=binding.id if channel == "telegram" else None,
                ),
            )

    http, telegram = await asyncio.gather(submit("http"), submit("telegram"))
    assert http.run.id != telegram.run.id
    async with factory() as db:
        runs = list(
            (
                await db.scalars(
                    select(DurableChatRun).where(
                        DurableChatRun.external_message_id == str(request_id)
                    )
                )
            ).all()
        )
    assert {run.intake_channel for run in runs} == {"http", "telegram"}


@pytest.mark.asyncio
async def test_service_rejects_unverified_channel_and_invalid_namespace(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        for identity, intake in (
            (
                VerifiedIntakeIdentity(account_key=_DEV_USER.sub, channel="telegram"),
                AgentIntakeRequest(
                    channel="http",
                    external_message_id="1",
                    request_id=uuid.uuid4(),
                    content="Wrong verified channel",
                ),
            ),
            (
                VerifiedIntakeIdentity(account_key=" " * 200, channel="telegram"),
                AgentIntakeRequest(
                    channel="telegram",
                    external_message_id=" ",
                    request_id=uuid.uuid4(),
                    content="Invalid namespace",
                ),
            ),
            (
                VerifiedIntakeIdentity(account_key=_DEV_USER.sub, channel="telegram"),
                AgentIntakeRequest(
                    channel="telegram",
                    external_message_id="untrusted-digest",
                    request_id=uuid.uuid4(),
                    content="Digest must be derived from the intake request",
                    input_digest="0" * 64,
                ),
            ),
        ):
            with pytest.raises(IntakeValidationError):
                await submit_agent_intake(db, identity=identity, request=intake)


@pytest.mark.asyncio
async def test_intake_failure_rolls_back_message_order_and_run(test_engine, monkeypatch):
    from app.domain import agent_intake

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        binding = AgentChannelIdentity(
            owner_key=_DEV_USER.sub, channel="telegram", external_id="770002"
        )
        db.add(binding)
        await db.commit()

    async with factory() as db:
        ledger_count_before = await db.scalar(select(func.count()).select_from(WorkBudgetLedger))

    async def broken_plan(*args, **kwargs):
        raise RuntimeError("write failed")

    monkeypatch.setattr(agent_intake, "create_single_step_plan", broken_plan)
    async with factory() as db:
        with pytest.raises(RuntimeError, match="write failed"):
            await submit_agent_intake(
                db,
                identity=VerifiedIntakeIdentity(account_key=_DEV_USER.sub, channel="telegram"),
                request=AgentIntakeRequest(
                    channel="telegram",
                    external_message_id="rollback-1",
                    request_id=uuid.uuid4(),
                    content="Must be atomic",
                    source_binding_id=binding.id,
                ),
            )
    async with factory() as db:
        assert (
            await db.scalar(select(func.count()).select_from(WorkBudgetLedger))
            == ledger_count_before
        )
        assert (
            await db.scalar(
                select(DurableChatRun.id).where(DurableChatRun.external_message_id == "rollback-1")
            )
            is None
        )
        assert (
            await db.scalar(select(WorkOrder.id).where(WorkOrder.objective == "Must be atomic"))
            is None
        )
        assert (
            await db.scalar(select(ChatMessage.id).where(ChatMessage.content == "Must be atomic"))
            is None
        )


@pytest.mark.asyncio
async def test_service_rejects_foreign_attachment_before_writing_turn(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        from app.db.models import Document

        foreign = Document(
            owner_sub="document-owner",
            file_name="private.pdf",
            file_hash="a" * 64,
            file_size=1,
            mime_type="application/pdf",
            storage_path="test/private",
        )
        db.add(foreign)
        binding = AgentChannelIdentity(
            owner_key="not-document-owner", channel="telegram", external_id="770003"
        )
        db.add(binding)
        await db.commit()
        with pytest.raises(IntakeNotFoundError, match="Owned attachment not found"):
            await submit_agent_intake(
                db,
                identity=VerifiedIntakeIdentity(
                    account_key="not-document-owner", channel="telegram"
                ),
                request=AgentIntakeRequest(
                    channel="telegram",
                    external_message_id="foreign-document",
                    request_id=uuid.uuid4(),
                    content="Foreign attachment",
                    attachments=(IntakeAttachment(document_id=foreign.id),),
                    source_binding_id=binding.id,
                ),
            )
    async with factory() as db:
        assert (
            await db.scalar(
                select(DurableChatRun.id).where(
                    DurableChatRun.external_message_id == "foreign-document"
                )
            )
            is None
        )


async def claimed_run(factory):
    async with factory() as db:
        run = await submit_chat_run(ChatRunCreate(**request()), db, _DEV_USER)
    async with factory() as db:
        order, step, attempt = await claim_ready_step(
            db, worker_id="chat-test", work_order_id=run["work_order_id"]
        )
        await db.commit()
        return run, step.id, attempt.id


class FakeAgent:
    def __init__(self, send):
        self.send = send
        self._executor = SimpleNamespace(total_tokens=12)

    def hydrate_history(self, history):
        self.history = history

    async def on_user_message(self, prompt, **kwargs):
        await self.send({"type": "text", "content": "Answer"})
        await self.send({"type": "done"})


@pytest.mark.asyncio
async def test_nonterminal_checkpoint_signal_propagates_without_runtime_wrapping(monkeypatch):
    from app.ai.chat_checkpoint import ChatNonterminalToolResult
    from app.tasks import durable_chat

    result = {
        "version": 1,
        "status": "partial",
        "data": {"recipient": "accepted"},
        "error_code": "job_queued",
        "retryable": False,
        "evidence": {"adapter_contract": "test_v1"},
        "checkpoint": {"receipt": "r-1"},
    }

    async def stopped(*args, **kwargs):
        raise ChatNonterminalToolResult(result)

    monkeypatch.setattr(durable_chat, "_run_durable_chat", stopped)
    with pytest.raises(ChatNonterminalToolResult) as exc_info:
        await run_durable_chat(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), session_factory=object())
    assert exc_info.value.result == result


@pytest.mark.asyncio
async def test_worker_persists_result_without_http_connection(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step, attempt = await claimed_run(factory)
    result = await run_durable_chat(
        run["work_order_id"], step, attempt, session_factory=factory, agent_factory=FakeAgent
    )
    assert result["text"] == "Answer"
    async with factory() as db:
        saved = await db.get(DurableChatRun, run["id"])
        message = await db.get(ChatMessage, saved.result_message_id)
        assert message.content == "Answer"
        assert message.metadata_["verified"] is False
        assert await db.scalar(
            select(WorkEvent.id).where(
                WorkEvent.work_order_id == run["work_order_id"],
                WorkEvent.event_type == "chat.response_saved",
            )
        )


@pytest.mark.asyncio
async def test_telegram_worker_persists_terminal_reply_outbox(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    owner = "telegram-terminal-owner"
    async with factory() as db:
        db.add(
            User(
                sub=owner,
                email="telegram-terminal@example.test",
                name="Telegram terminal",
                preferred_username="telegram-terminal",
                role="operator",
                is_active=True,
            )
        )
        binding = AgentChannelIdentity(owner_key=owner, channel="telegram", external_id="880001")
        db.add(binding)
        await db.flush()
        intake = await submit_agent_intake(
            db,
            identity=VerifiedIntakeIdentity(account_key=owner, channel="telegram"),
            request=AgentIntakeRequest(
                channel="telegram",
                external_message_id="update:terminal",
                request_id=uuid.uuid4(),
                content="Return a terminal answer",
                source_binding_id=binding.id,
            ),
        )
    async with factory() as db:
        _, step, attempt = await claim_ready_step(
            db, worker_id="telegram-chat-test", work_order_id=intake.order.id
        )
        await db.commit()

    await run_durable_chat(
        intake.order.id, step.id, attempt.id, session_factory=factory, agent_factory=FakeAgent
    )

    async with factory() as db:
        outbox = await db.scalar(
            select(AgentOutbox).where(
                AgentOutbox.work_order_id == intake.order.id,
                AgentOutbox.event_type == "chat.reply_ready",
            )
        )
        assert outbox is not None
        assert outbox.owner_key == owner
        assert outbox.destination_binding_id == binding.id
        assert outbox.payload == {
            "resource_type": "work_order",
            "resource_id": str(intake.order.id),
        }


@pytest.mark.asyncio
async def test_telegram_reply_uses_exact_source_binding_not_another_owner_binding(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    owner = "telegram-multi-binding-owner"
    async with factory() as db:
        first = AgentChannelIdentity(owner_key=owner, channel="telegram", external_id="880101")
        other = AgentChannelIdentity(owner_key=owner, channel="telegram", external_id="880102")
        db.add_all([first, other])
        await db.flush()
        intake = await submit_agent_intake(
            db,
            identity=VerifiedIntakeIdentity(account_key=owner, channel="telegram"),
            request=AgentIntakeRequest(
                channel="telegram",
                external_message_id="update:exact-binding",
                request_id=uuid.uuid4(),
                content="Reply only to the inbound binding",
                source_binding_id=other.id,
            ),
        )
    async with factory() as db:
        _, step, attempt = await claim_ready_step(
            db, worker_id="telegram-exact-binding", work_order_id=intake.order.id
        )
        await db.commit()
    await run_durable_chat(
        intake.order.id, step.id, attempt.id, session_factory=factory, agent_factory=FakeAgent
    )
    async with factory() as db:
        outbox = await db.scalar(
            select(AgentOutbox).where(AgentOutbox.work_order_id == intake.order.id)
        )
        assert outbox.destination_binding_id == other.id
        assert outbox.destination_binding_id != first.id


@pytest.mark.asyncio
async def test_revoked_and_rebound_source_binding_never_receives_old_reply(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    alice, bob = "telegram-old-owner", "telegram-new-owner"
    async with factory() as db:
        source = AgentChannelIdentity(owner_key=alice, channel="telegram", external_id="880201")
        db.add(source)
        await db.flush()
        intake = await submit_agent_intake(
            db,
            identity=VerifiedIntakeIdentity(account_key=alice, channel="telegram"),
            request=AgentIntakeRequest(
                channel="telegram",
                external_message_id="update:revoked-source",
                request_id=uuid.uuid4(),
                content="Do not send this reply after rebind",
                source_binding_id=source.id,
            ),
        )
        source.is_active = False
        rebound = AgentChannelIdentity(owner_key=bob, channel="telegram", external_id="880201")
        db.add(rebound)
        await db.commit()
    async with factory() as db:
        _, step, attempt = await claim_ready_step(
            db, worker_id="telegram-revoked-binding", work_order_id=intake.order.id
        )
        await db.commit()
    await run_durable_chat(
        intake.order.id, step.id, attempt.id, session_factory=factory, agent_factory=FakeAgent
    )
    async with factory() as db:
        assert (
            await db.scalar(
                select(AgentOutbox.id).where(AgentOutbox.work_order_id == intake.order.id)
            )
            is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["approval", "error", "expired", "canceled"])
async def test_fail_closed_before_effect_or_result(test_engine, mode):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step_id, attempt_id = await claimed_run(factory)
    effects = []
    async with factory() as db:
        order = await db.get(WorkOrder, run["work_order_id"])
        if mode == "expired":
            step = await db.get(WorkStep, step_id)
            step.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        if mode == "canceled":
            order.status = "canceled"
        await db.commit()

    class Agent(FakeAgent):
        async def on_user_message(self, prompt, **kwargs):
            if mode == "approval":
                await self._executor._request_approval("invoices.approve", {"id": "one"})
            elif mode == "error":
                await self.send({"type": "error", "error_code": "provider_down"})
                await self.send({"type": "text", "content": "Fallback text"})
                return
            await self.send({"type": "tool_call", "tool": "test"})
            effects.append("effect")

    with pytest.raises(RuntimeError):
        await run_durable_chat(
            run["work_order_id"], step_id, attempt_id, session_factory=factory, agent_factory=Agent
        )
    assert effects == []
    async with factory() as db:
        saved = await db.get(DurableChatRun, run["id"])
        assert saved.result_message_id is None


@pytest.mark.asyncio
async def test_tool_call_event_is_audit_not_physical_attempt_counter(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step_id, attempt_id = await claimed_run(factory)
    async with factory() as db:
        order = await db.get(WorkOrder, run["work_order_id"])
        order.budgets = {**order.budgets, "max_tool_calls": 0}
        await db.commit()

    class Agent(FakeAgent):
        async def on_user_message(self, prompt, **kwargs):
            await self.send({"type": "tool_call", "tool": "audit-only", "args": {}})
            await super().on_user_message(prompt, **kwargs)

    result = await run_durable_chat(
        run["work_order_id"],
        step_id,
        attempt_id,
        session_factory=factory,
        agent_factory=Agent,
    )

    assert result["text"] == "Answer"
    async with factory() as db:
        assert await db.scalar(
            select(WorkEvent.id).where(
                WorkEvent.work_order_id == run["work_order_id"],
                WorkEvent.event_type == "chat.tool_call",
            )
        )


@pytest.mark.asyncio
async def test_worker_loss_blocks_instead_of_replaying(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step_id, _ = await claimed_run(factory)
    async with factory() as db:
        step = await db.get(WorkStep, step_id)
        step.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    async with factory() as db:
        await reclaim_expired_leases(db)
        await db.commit()
    async with factory() as db:
        order = await db.get(WorkOrder, run["work_order_id"])
        assert order.status == "blocked"
        assert await claim_ready_step(db, worker_id="replacement", work_order_id=order.id) is None


@pytest.mark.asyncio
async def test_event_is_committed_before_tool_effect(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step, attempt = await claimed_run(factory)

    class Agent(FakeAgent):
        async def on_user_message(self, prompt, **kwargs):
            assert self._executor._session_id == str(run["session_id"])
            assert self.history == []
            await self.send({"type": "tool_call", "tool": "test.read", "args": {}})
            async with factory() as db:
                assert await db.scalar(
                    select(WorkEvent.id).where(
                        WorkEvent.work_order_id == run["work_order_id"],
                        WorkEvent.event_type == "chat.tool_call",
                    )
                )
            await super().on_user_message(prompt, **kwargs)

    await run_durable_chat(
        run["work_order_id"], step, attempt, session_factory=factory, agent_factory=Agent
    )


@pytest.mark.asyncio
async def test_persistence_failure_crosses_model_recovery_handlers(test_engine, monkeypatch):
    from app.tasks import durable_chat

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step, attempt = await claimed_run(factory)
    effects = []

    async def broken(*args, **kwargs):
        raise ConnectionError("Database unavailable")

    monkeypatch.setattr(durable_chat, "append_event", broken)

    class Agent(FakeAgent):
        async def on_user_message(self, prompt, **kwargs):
            try:
                await self.send({"type": "tool_call", "tool": "test"})
            except Exception:
                effects.append("unsafe recovery")
            effects.append("effect")

    with pytest.raises(RuntimeError, match="persistence failed"):
        await run_durable_chat(
            run["work_order_id"], step, attempt, session_factory=factory, agent_factory=Agent
        )
    assert effects == []


@pytest.mark.asyncio
async def test_worker_dispatch_settles_durable_turn(test_engine, monkeypatch):
    from unittest.mock import AsyncMock

    from app.ai import orchestrator
    from app.tasks import work_orders

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step, attempt = await claimed_run(factory)
    monkeypatch.setattr(orchestrator, "AgentOrchestrator", FakeAgent)
    verifier = AsyncMock()
    monkeypatch.setattr(work_orders, "verify_completed_step", verifier)
    assert await work_orders.execute_claimed_step(
        step, attempt, schedule_verification=False, session_factory=factory
    )
    verifier.assert_awaited_once()
    assert not await work_orders.execute_claimed_step(
        step, attempt, schedule_verification=False, session_factory=factory
    )
    async with factory() as db:
        saved = await db.get(WorkStep, step)
        assert saved.state == "succeeded"
        assert saved.output["text"] == "Answer"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatMessage)
                .where(ChatMessage.session_id == run["session_id"], ChatMessage.role == "assistant")
            )
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["max_zero", "finite_tokens", "finite_cost", "legacy"])
async def test_worker_persists_budget_stop_as_nonretryable_blocker(test_engine, monkeypatch, mode):
    from app.ai import agent_loop, orchestrator
    from app.ai.agent_config import BuiltinAgentConfig
    from app.db.models import WorkStepAttempt, WorkToolCall
    from app.tasks import work_orders

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step_id, attempt_id = await claimed_run(factory)
    async with factory() as db:
        order = await db.get(WorkOrder, run["work_order_id"])
        ledger = await db.get(WorkBudgetLedger, order.budget_ledger_id)
        if mode == "max_zero":
            ledger.max_llm_calls = 0
        elif mode == "finite_tokens":
            ledger.max_tokens = 100
        elif mode == "finite_cost":
            ledger.max_cost_usd = 1
        else:
            order.budget_ledger_id = None
        await db.commit()

    provider_calls = []

    async def provider(*args, **kwargs):
        provider_calls.append("called")
        return {"role": "assistant", "content": "must not run"}

    class Agent:
        def __init__(self, send):
            self.send = send
            self._executor = self
            self.total_tokens = 0
            self.context = None

        def set_work_budget_context(self, context):
            self.context = context

        def hydrate_history(self, history):
            self.history = history

        async def on_user_message(self, prompt, **kwargs):
            config = BuiltinAgentConfig(department_enabled=False, provider="ollama")
            await agent_loop._call_provider_streaming(
                [{"role": "user", "content": prompt}],
                [],
                None,
                config,
                lambda _token: asyncio.sleep(0),
                budget_context=self.context,
            )

    monkeypatch.setattr(orchestrator, "AgentOrchestrator", Agent)
    monkeypatch.setattr(agent_loop, "_call_ollama_streaming", provider)

    assert not await work_orders.execute_claimed_step(
        step_id, attempt_id, schedule_verification=False, session_factory=factory
    )
    assert provider_calls == []
    async with factory() as db:
        order = await db.get(WorkOrder, run["work_order_id"])
        step = await db.get(WorkStep, step_id)
        attempt = await db.get(WorkStepAttempt, attempt_id)
        call = await db.scalar(select(WorkToolCall).where(WorkToolCall.attempt_id == attempt_id))
        saved_run = await db.get(DurableChatRun, run["id"])
        assert order.status == "blocked"
        assert order.blocker["code"] in {
            "llm_call_budget_exceeded",
            "token_budget_enforcement_unavailable",
            "cost_budget_enforcement_unavailable",
            "legacy_budget_baseline_required",
        }
        assert step.state == "failed"
        assert step.lease_owner is None
        assert step.lease_expires_at is None
        assert step.next_attempt_at is None
        assert attempt.status == "failed"
        assert call.status == "failed"
        assert saved_run.result_message_id is None


@pytest.mark.asyncio
async def test_two_workers_for_same_attempt_dispatch_only_one_provider(test_engine, monkeypatch):
    from unittest.mock import AsyncMock

    from app.ai import agent_loop, orchestrator
    from app.ai.agent_config import BuiltinAgentConfig
    from app.tasks import work_orders

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step_id, attempt_id = await claimed_run(factory)
    provider_calls = []

    async def provider(*args, **kwargs):
        provider_calls.append(kwargs.get("provider", "ollama"))
        await asyncio.sleep(0.05)
        return {"role": "assistant", "content": "one answer"}

    class Agent:
        def __init__(self, send):
            self.send = send
            self._executor = self
            self.total_tokens = 0
            self.context = None

        def set_work_budget_context(self, context):
            self.context = context

        def hydrate_history(self, history):
            self.history = history

        async def on_user_message(self, prompt, **kwargs):
            config = BuiltinAgentConfig(department_enabled=False, provider="ollama")
            result = await agent_loop._call_provider_streaming(
                [{"role": "user", "content": prompt}],
                [],
                None,
                config,
                lambda _token: asyncio.sleep(0),
                budget_context=self.context,
            )
            await self.send({"type": "text", "content": result["content"]})

    monkeypatch.setattr(agent_loop, "_call_ollama_streaming", provider)
    monkeypatch.setattr(agent_loop, "_call_openai_streaming", provider)
    monkeypatch.setattr(orchestrator, "AgentOrchestrator", Agent)
    verifier = AsyncMock()
    monkeypatch.setattr(work_orders, "verify_completed_step", verifier)
    outcomes = await asyncio.gather(
        work_orders.execute_claimed_step(
            step_id,
            attempt_id,
            session_factory=factory,
            schedule_verification=False,
        ),
        work_orders.execute_claimed_step(
            step_id,
            attempt_id,
            session_factory=factory,
            schedule_verification=False,
        ),
    )
    assert sorted(outcomes) == [False, True]
    verifier.assert_awaited_once()
    assert len(provider_calls) == 1
    async with factory() as db:
        reservations = list(
            await db.scalars(
                select(WorkBudgetReservation).where(
                    WorkBudgetReservation.work_order_id == run["work_order_id"]
                )
            )
        )
    assert len([row for row in reservations if row.operation_key.startswith("execution:")]) == 1
    assert len([row for row in reservations if row.operation_key.startswith("llm:")]) == 1
    async with factory() as db:
        step = await db.get(WorkStep, step_id)
        assert step.state == "succeeded"
        assert step.last_error is None


@pytest.mark.asyncio
async def test_durable_schema_migration_round_trip(db_session):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    path = Path(__file__).resolve().parents[1] / "migrations/versions/20260911_0001_durable_chat.py"
    spec = importlib.util.spec_from_file_location("durable_chat_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    schema = "chat_migration_" + uuid.uuid4().hex
    connection = await db_session.connection()
    await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    await connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))

    def verify(sync):
        module.op = Operations(MigrationContext.configure(sync))
        module.upgrade()
        assert inspect(sync).get_table_names(schema=schema) == ["durable_chat_runs"]
        assert len(inspect(sync).get_foreign_keys("durable_chat_runs", schema=schema)) == 4
        module.downgrade()
        assert not inspect(sync).get_table_names(schema=schema)

    await connection.run_sync(verify)


@pytest.mark.asyncio
async def test_intake_namespace_migration_round_trip(db_session):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    versions = Path(__file__).resolve().parents[1] / "migrations/versions"
    durable_spec = importlib.util.spec_from_file_location(
        "durable_chat_migration", versions / "20260911_0001_durable_chat.py"
    )
    intake_spec = importlib.util.spec_from_file_location(
        "agent_intake_migration", versions / "20260929_0001_agent_intake_namespace.py"
    )
    durable = importlib.util.module_from_spec(durable_spec)
    intake = importlib.util.module_from_spec(intake_spec)
    durable_spec.loader.exec_module(durable)
    intake_spec.loader.exec_module(intake)
    legacy_session = ChatSession(user_key="migration-owner")
    db_session.add(legacy_session)
    await db_session.flush()
    legacy_message = ChatMessage(session_id=legacy_session.id, role="user", content="Legacy intake")
    legacy_order = WorkOrder(owner_key="migration-owner", objective="Legacy intake")
    db_session.add_all([legacy_message, legacy_order])
    await db_session.flush()
    legacy_run_id = uuid.uuid4()
    legacy_request_id = uuid.uuid4()
    schema = "intake_migration_" + uuid.uuid4().hex
    connection = await db_session.connection()
    await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    await connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))

    def verify(sync):
        context = MigrationContext.configure(sync)
        durable.op = Operations(context)
        intake.op = Operations(context)
        durable.upgrade()
        sync.execute(
            text(
                """
                INSERT INTO durable_chat_runs
                    (id, owner_key, request_id, request_digest, work_order_id, session_id, user_message_id)
                VALUES
                    (:id, :owner_key, :request_id, :request_digest, :work_order_id, :session_id, :message_id)
                """
            ),
            {
                "id": legacy_run_id,
                "owner_key": "migration-owner",
                "request_id": legacy_request_id,
                "request_digest": "0" * 64,
                "work_order_id": legacy_order.id,
                "session_id": legacy_session.id,
                "message_id": legacy_message.id,
            },
        )
        intake.upgrade()
        columns = {
            column["name"]
            for column in inspect(sync).get_columns("durable_chat_runs", schema=schema)
        }
        assert {"intake_channel", "external_message_id"} <= columns
        uniques = inspect(sync).get_unique_constraints("durable_chat_runs", schema=schema)
        assert any(item["name"] == "uq_chat_run_intake_message" for item in uniques)
        migrated = sync.execute(
            text(
                "SELECT intake_channel, external_message_id FROM durable_chat_runs WHERE id = :id"
            ),
            {"id": legacy_run_id},
        ).one()
        assert migrated == ("http", str(legacy_request_id))
        intake.downgrade()
        durable.downgrade()
        assert not inspect(sync).get_table_names(schema=schema)

    await connection.run_sync(verify)


@pytest.mark.asyncio
async def test_channel_binding_migration_round_trip(db_session):
    import importlib.util
    from pathlib import Path

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    path = (
        Path(__file__).resolve().parents[1]
        / "migrations/versions/20260929_0003_agent_channel_soft_revoke.py"
    )
    spec = importlib.util.spec_from_file_location("channel_binding_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    schema = "channel_binding_migration_" + uuid.uuid4().hex
    connection = await db_session.connection()
    await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    await connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))

    def verify(sync):
        op = Operations(MigrationContext.configure(sync))
        op.create_table(
            "agent_channel_identities",
            sa.Column("id", sa.UUID(), primary_key=True),
            sa.Column("channel", sa.String(30), nullable=False),
            sa.Column("external_id", sa.String(200), nullable=False),
            sa.UniqueConstraint(
                "channel", "external_id", name="agent_channel_identities_channel_external_id_key"
            ),
        )
        op.create_table("durable_chat_runs", sa.Column("id", sa.UUID(), primary_key=True))
        module.op = op
        module.upgrade()
        inspector = inspect(sync)
        columns = {
            item["name"] for item in inspect(sync).get_columns("durable_chat_runs", schema=schema)
        }
        assert "source_binding_id" in columns
        indexes = inspector.get_indexes("agent_channel_identities", schema=schema)
        assert any(item["name"] == "uq_active_agent_channel_identity" for item in indexes)
        module.downgrade()
        columns = {
            item["name"] for item in inspect(sync).get_columns("durable_chat_runs", schema=schema)
        }
        assert "source_binding_id" not in columns

    await connection.run_sync(verify)


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatch", [False, True])
async def test_cancel_interrupts_running_agent_without_later_effect(
    test_engine, monkeypatch, dispatch
):
    from app.ai import orchestrator
    from app.api.work_orders import cancel_order
    from app.db.models import WorkStepAttempt, WorkToolCall
    from app.tasks.work_orders import execute_claimed_step

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step, attempt = await claimed_run(factory)
    started, stopped = asyncio.Event(), asyncio.Event()
    effects = []

    class Agent(FakeAgent):
        async def on_user_message(self, prompt, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
                effects.append("must not run after cancellation")
            finally:
                stopped.set()

    monkeypatch.setattr(orchestrator, "AgentOrchestrator", Agent)
    execution = asyncio.create_task(
        execute_claimed_step(step, attempt, session_factory=factory)
        if dispatch
        else run_durable_chat(
            run["work_order_id"], step, attempt, session_factory=factory, agent_factory=Agent
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        async with factory() as db:
            await cancel_order(run["work_order_id"], db, _DEV_USER)
        if dispatch:
            assert await asyncio.wait_for(execution, timeout=5) is False
        else:
            with pytest.raises(RuntimeError, match="Execution stopped"):
                await asyncio.wait_for(execution, timeout=5)
        assert stopped.is_set()
        assert effects == []
        async with factory() as db:
            assert (await db.get(DurableChatRun, run["id"])).result_message_id is None
            assert (await db.get(WorkOrder, run["work_order_id"])).status == "canceled"
            assert (await db.get(WorkStep, step)).state == "canceled"
            assert (await db.get(WorkStepAttempt, attempt)).status == "canceled"
            if dispatch:
                call = await db.scalar(
                    select(WorkToolCall).where(WorkToolCall.attempt_id == attempt)
                )
                assert call.status == "outcome_unknown"
            assert await reclaim_expired_leases(db) >= 0
            assert (await db.get(WorkOrder, run["work_order_id"])).status == "canceled"
    finally:
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_and_prepare_use_root_first_locks_without_deadlock_or_provider(
    test_engine, monkeypatch
):
    from unittest.mock import AsyncMock

    from app.api import work_orders as work_order_api
    from app.db.models import WorkToolCall
    from app.tasks import work_orders

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step_id, attempt_id = await claimed_run(factory)
    cancel_holds_order = asyncio.Event()
    release_cancel = asyncio.Event()
    original_get_owned_order = work_order_api._get_owned_order

    async def hold_cancel_after_root_lock(db, work_order_id, user, *, lock=False):
        order = await original_get_owned_order(db, work_order_id, user, lock=lock)
        if lock:
            cancel_holds_order.set()
            await release_cancel.wait()
        return order

    provider = AsyncMock(side_effect=AssertionError("Canceled work must not reach provider"))
    monkeypatch.setattr(work_order_api, "_get_owned_order", hold_cancel_after_root_lock)
    monkeypatch.setattr("app.tasks.durable_chat.run_durable_chat", provider)

    async def cancel():
        async with factory() as db:
            return await work_order_api.cancel_order(run["work_order_id"], db, _DEV_USER)

    cancel_task = asyncio.create_task(cancel())
    execute_task = None
    try:
        await asyncio.wait_for(cancel_holds_order.wait(), timeout=5)
        execute_task = asyncio.create_task(
            work_orders.execute_claimed_step(
                step_id,
                attempt_id,
                schedule_verification=False,
                session_factory=factory,
            )
        )
        # On PostgreSQL the worker is now waiting for the root WorkOrder lock.
        # The former attempt-first order instead held the attempt here, making
        # cancel wait for attempt while prepare waited for WorkOrder.
        await asyncio.sleep(0.05)
        assert not execute_task.done()
        release_cancel.set()
        canceled, executed = await asyncio.wait_for(
            asyncio.gather(cancel_task, execute_task), timeout=5
        )
        assert canceled.status == "canceled"
        assert executed is False
        provider.assert_not_awaited()
        async with factory() as db:
            assert (await db.get(WorkOrder, run["work_order_id"])).status == "canceled"
            assert (await db.get(WorkStep, step_id)).state == "canceled"
            assert (
                await db.scalar(
                    select(WorkToolCall.id).where(WorkToolCall.attempt_id == attempt_id)
                )
                is None
            )
    finally:
        release_cancel.set()
        for task in (cancel_task, execute_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (cancel_task, execute_task) if task is not None),
            return_exceptions=True,
        )


@pytest.mark.asyncio
async def test_legacy_history_cannot_be_claimed_by_durable_intake(client, db_session):
    from app.chat.store import append_chat_message, create_chat_session

    session = await create_chat_session(db_session, user_key=_DEV_USER.sub)
    await append_chat_message(db_session, session_id=session.id, role="user", content="Legacy turn")
    await db_session.commit()
    response = await client.post("/api/agent/chat-runs", json=request(session_id=str(session.id)))
    assert response.status_code == 409
    assert "Legacy conversation migration" in response.json()["detail"]
    assert (
        await db_session.scalar(
            select(DurableChatRun.id).where(DurableChatRun.session_id == session.id)
        )
        is None
    )


@pytest.mark.asyncio
async def test_oversized_event_stops_before_effect(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step, attempt = await claimed_run(factory)
    effects = []

    class Agent(FakeAgent):
        async def on_user_message(self, prompt, **kwargs):
            await self.send(
                {"type": "tool_call", "tool": "test", "args": {"text": "x" * 1_000_001}}
            )
            effects.append("effect")

    with pytest.raises(RuntimeError, match="storage limit"):
        await run_durable_chat(
            run["work_order_id"], step, attempt, session_factory=factory, agent_factory=Agent
        )
    assert effects == []


@pytest.mark.asyncio
async def test_cancel_between_preparation_and_dispatch_preserves_fence(test_engine, monkeypatch):
    from unittest.mock import AsyncMock

    from app.api.work_orders import cancel_order
    from app.db.models import WorkToolCall
    from app.tasks import work_orders

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    run, step, attempt = await claimed_run(factory)
    original = work_orders.append_event

    async def cancel_after_prepare(db, order_id, event_type, **kwargs):
        result = await original(db, order_id, event_type, **kwargs)
        if event_type == "tool_call.prepared":
            await cancel_order(order_id, db, _DEV_USER)
        return result

    runner = AsyncMock(side_effect=AssertionError("Canceled work must not execute"))
    monkeypatch.setattr(work_orders, "append_event", cancel_after_prepare)
    monkeypatch.setattr("app.tasks.durable_chat.run_durable_chat", runner)
    assert not await work_orders.execute_claimed_step(step, attempt, session_factory=factory)
    runner.assert_not_awaited()
    async with factory() as db:
        call = await db.scalar(select(WorkToolCall).where(WorkToolCall.attempt_id == attempt))
        assert call.status == "outcome_unknown"
        assert (await db.get(WorkOrder, run["work_order_id"])).status == "canceled"


@pytest.mark.asyncio
async def test_latest_run_is_scoped_to_owned_session(client):
    from app.auth.jwt import get_current_user
    from app.main import app

    run = (await client.post("/api/agent/chat-runs", json=request())).json()
    response = await client.get(f"/api/agent/chat-runs?session_id={run['session_id']}")
    assert response.json()["run"]["id"] == run["id"]
    original = app.dependency_overrides.copy()
    try:
        app.dependency_overrides[get_current_user] = lambda: _DEV_USER.model_copy(
            update={"sub": "other"}
        )
        assert (
            await client.get(f"/api/agent/chat-runs?session_id={run['session_id']}")
        ).status_code == 404
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original)


@pytest.mark.asyncio
async def test_foreign_attachment_rejects_entire_intake(client, db_session):
    body = request(attachments=[{"document_id": str(uuid.uuid4()), "file_name": "forged.pdf"}])
    response = await client.post("/api/agent/chat-runs", json=body)
    assert response.status_code == 404
    assert (
        await db_session.scalar(
            select(DurableChatRun.id).where(
                DurableChatRun.request_id == uuid.UUID(body["request_id"])
            )
        )
        is None
    )


@pytest.mark.asyncio
async def test_owned_attachment_uses_server_metadata(client, db_session):
    from app.db.models import ChatMessageAttachment, Document

    doc = Document(
        owner_sub=_DEV_USER.sub,
        file_name="real.pdf",
        file_hash="0" * 64,
        file_size=10,
        mime_type="application/pdf",
        storage_path="test/attachment",
    )
    db_session.add(doc)
    await db_session.flush()
    response = await client.post(
        "/api/agent/chat-runs",
        json=request(
            attachments=[{"document_id": str(doc.id), "file_name": "forged.exe"}],
            workspace_context={"active_tabular_surface": {"id": "surface"}},
        ),
    )
    assert response.status_code == 202, response.text
    run = await db_session.get(DurableChatRun, uuid.UUID(response.json()["id"]))
    attachment = await db_session.scalar(
        select(ChatMessageAttachment).where(ChatMessageAttachment.message_id == run.user_message_id)
    )
    assert attachment.file_name == "real.pdf"
    step = await db_session.scalar(
        select(WorkStep).where(WorkStep.work_order_id == run.work_order_id)
    )
    assert step.input_["workspace_context"]["active_tabular_surface"]["id"] == "surface"
