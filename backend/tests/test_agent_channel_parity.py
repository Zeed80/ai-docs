"""E18 channel contract parity.

The adapters below are real HTTP, Telegram and cron intake paths.  The
downstream action/receipt/approval record is deliberately synthetic: the
current runtime has no one common end-to-end recipient action which every
channel can safely execute in a test.  Keeping that boundary explicit avoids
claiming model-to-recipient coverage which this test does not provide.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.chat_runs import ChatRunCreate, submit_chat_run
from app.api.work_orders import cancel_order
from app.auth.models import UserInfo, UserRole
from app.db.agent_runtime_models import (
    ActionReceipt,
    AgentChannelIdentity,
    AgentOutbox,
    ChatLogicalAction,
    DelegationGrant,
    DurableChatRun,
)
from app.db.models import AgentCron, Approval, ApprovalActionType, ChatMessage, User, WorkOrder
from app.domain.action_receipts import RECEIPT_VERSION
from app.domain.agent_intake import (
    AgentIntakeRequest,
    IntakeValidationError,
    VerifiedIntakeIdentity,
    submit_agent_intake,
)
from app.domain.agent_outbox import (
    OutboxClaim,
    OutboxDeliveryAdapter,
    OutboxRecipientUnavailable,
    deliver_outbox_claim,
)
from app.domain.chat_action_journal import digest
from app.domain.work_orders import claim_ready_step
from app.integrations.telegram_bot import SvetaTelegramBot
from app.tasks import agent_cron
from app.tasks.durable_chat import run_durable_chat


def _owner(key: str) -> UserInfo:
    return UserInfo(
        sub=key,
        email=f"{key}@example.test",
        name=key,
        preferred_username=key,
        roles=[UserRole.admin],
    )


def _telegram_update(*, update_id: int, telegram_id: int, text: str):
    update = MagicMock()
    update.update_id = update_id
    update.effective_user.id = telegram_id
    update.effective_chat.type = "private"
    update.message.text = text
    update.message.date = datetime.now(UTC) + timedelta(seconds=1)
    update.message.reply_text = AsyncMock()
    return update


def _telegram_bot(*allowed: int) -> SvetaTelegramBot:
    bot = object.__new__(SvetaTelegramBot)
    bot._allowed = set(allowed)
    return bot


class _TerminalAgent:
    def __init__(self, send):
        self.send = send
        self._executor = SimpleNamespace(total_tokens=12)

    def hydrate_history(self, history):
        self.history = history

    async def on_user_message(self, prompt, **kwargs):
        await self.send({"type": "text", "content": "durable terminal result"})
        await self.send({"type": "done"})


class _UnavailableNotification(OutboxDeliveryAdapter):
    is_internal = True

    async def send(self, **kwargs):
        raise OutboxRecipientUnavailable()


async def _synthetic_downstream_contract(factory, run_id: uuid.UUID, owner: UserInfo) -> dict:
    """Record the uniform persisted contract after intake, then cancel it.

    This does not pretend to execute an external recipient.  It proves that
    channel-specific intake leaves the same owner-bound durable entities for
    the action, receipt, approval and cancellation layers to consume.
    """
    async with factory() as db:
        run = await db.get(DurableChatRun, run_id)
        order, _step, attempt = await claim_ready_step(
            db, worker_id=f"parity-{run.intake_channel}", work_order_id=run.work_order_id
        )
        request = {"name": "agent__mcp", "arguments": {"action": "synthetic_commit"}}
        action = ChatLogicalAction(
            work_order_id=order.id,
            attempt_id=attempt.id,
            call_id="synthetic-commit",
            request=request,
            request_digest=digest(request),
            status="waiting_approval",
            result={"version": 1, "status": "waiting_approval"},
            result_digest=digest({"version": 1, "status": "waiting_approval"}),
        )
        db.add(action)
        await db.flush()
        response = {
            "id": "00000000-0000-0000-0000-00000000e180",
            "updated_at": "2026-09-29T00:00:00+00:00",
        }
        db.add(
            ActionReceipt(
                logical_action_id=action.id,
                work_order_id=order.id,
                owner_key=order.owner_key,
                attempt_id=attempt.id,
                operation="agent.mcp.synthetic_commit",
                request_digest=action.request_digest,
                response=response,
                response_digest=digest(response),
                artifact_id=response["id"],
                artifact_revision=response["updated_at"],
                receipt_version=RECEIPT_VERSION,
                provenance={"source": "synthetic-e18"},
                created_at=datetime.now(UTC),
            )
        )
        approval = Approval(
            action_type=ApprovalActionType.agent_tool_call,
            entity_type="work_order",
            entity_id=order.id,
            requested_by=owner.sub,
            assigned_to=owner.sub,
            context={"logical_action_id": str(action.id)},
        )
        db.add(approval)
        await db.commit()
        await cancel_order(order.id, db, owner)
        await db.refresh(order)
        return {
            "owner": order.owner_key,
            "work_identity": (order.source, order.plan_revision),
            "budgets": order.budgets,
            "logical_action": (action.request, action.status),
            "receipt": (action.request_digest, response, RECEIPT_VERSION),
            "approval": (approval.action_type.value, approval.status.value, approval.assigned_to),
            "cancellation": order.status,
            "initial_result_state": "not_set" if run.result_message_id is None else "saved",
        }


@pytest.mark.asyncio
async def test_http_telegram_and_cron_share_durable_contract_without_model_intake(
    db_session, monkeypatch
):
    """Only the verified adapter and its idempotency key may differ by channel."""
    owner_key = f"channel-parity-{uuid.uuid4().hex}"
    owner = _owner(owner_key)
    telegram_id = 998001
    factory = async_sessionmaker(
        bind=db_session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    monkeypatch.setattr("app.db.session._get_session_factory", lambda: factory)

    calls = []

    class NoModelSession:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("channel intake must not construct AgentSession")

    monkeypatch.setattr("app.ai.agent_loop.AgentSession", NoModelSession)

    async with factory() as db:
        db.add(
            User(
                sub=owner_key,
                email=owner.email,
                name=owner.name,
                preferred_username=owner.preferred_username,
                role="admin",
                is_active=True,
            )
        )
        binding = AgentChannelIdentity(
            owner_key=owner_key, channel="telegram", external_id=str(telegram_id)
        )
        db.add(binding)
        await db.flush()
        prompt = "E18 synthetic channel workflow"
        grant = DelegationGrant(
            owner_key=owner_key,
            title="E18 cron grant",
            actions=["agent.cron.run"],
            constraints={
                "schedule": "* * * * *",
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            },
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add(grant)
        await db.flush()
        cron = AgentCron(
            schedule="* * * * *",
            prompt=prompt,
            owner_key=owner_key,
            delegation_grant_id=grant.id,
        )
        db.add(cron)
        await db.commit()

    async with factory() as db:
        http = await submit_chat_run(
            ChatRunCreate(request_id=uuid.uuid4(), content=prompt), db, owner
        )
    update = _telegram_update(update_id=998002, telegram_id=telegram_id, text=prompt)
    await _telegram_bot(telegram_id)._handle_text(update, MagicMock())
    assert await agent_cron._dispatch(datetime(2026, 9, 29, 12, 0, tzinfo=UTC)) == 1

    async with factory() as db:
        telegram = await db.scalar(
            select(DurableChatRun).where(
                DurableChatRun.owner_key == owner_key,
                DurableChatRun.intake_channel == "telegram",
            )
        )
        cron_run = await db.scalar(
            select(DurableChatRun).where(
                DurableChatRun.owner_key == owner_key,
                DurableChatRun.intake_channel == "cron",
            )
        )
    assert telegram is not None and cron_run is not None

    snapshots = [
        await _synthetic_downstream_contract(factory, http["id"], owner),
        await _synthetic_downstream_contract(factory, telegram.id, owner),
        await _synthetic_downstream_contract(factory, cron_run.id, owner),
    ]
    assert snapshots[0] == snapshots[1] == snapshots[2]
    assert snapshots[0]["owner"] == owner_key
    assert snapshots[0]["budgets"] == {
        "max_replans": 0,
        "max_wall_clock_seconds": 7200,
        "max_tool_calls": 200,
    }
    assert calls == []
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_telegram_and_cron_persist_same_terminal_runtime_result(db_session, monkeypatch):
    """A real durable-chat worker persists the same result contract for every intake."""
    owner_key = f"terminal-channel-parity-{uuid.uuid4().hex}"
    owner = _owner(owner_key)
    telegram_id = 998101
    prompt = "E18 terminal channel workflow"
    factory = async_sessionmaker(
        bind=db_session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    monkeypatch.setattr("app.db.session._get_session_factory", lambda: factory)

    async with factory() as db:
        db.add(
            User(
                sub=owner_key,
                email=owner.email,
                name=owner.name,
                preferred_username=owner.preferred_username,
                role="admin",
                is_active=True,
            )
        )
        binding = AgentChannelIdentity(
            owner_key=owner_key, channel="telegram", external_id=str(telegram_id)
        )
        db.add(binding)
        await db.flush()
        grant = DelegationGrant(
            owner_key=owner_key,
            title="E18 terminal cron grant",
            actions=["agent.cron.run"],
            constraints={
                "schedule": "* * * * *",
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            },
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add(grant)
        await db.flush()
        db.add(
            AgentCron(
                schedule="* * * * *",
                prompt=prompt,
                owner_key=owner_key,
                delegation_grant_id=grant.id,
            )
        )
        await db.commit()

    async with factory() as db:
        http = await submit_chat_run(
            ChatRunCreate(request_id=uuid.uuid4(), content=prompt), db, owner
        )
    update = _telegram_update(update_id=998102, telegram_id=telegram_id, text=prompt)
    await _telegram_bot(telegram_id)._handle_text(update, MagicMock())
    assert await agent_cron._dispatch(datetime(2026, 9, 29, 12, 2, tzinfo=UTC)) == 1

    async with factory() as db:
        telegram_run = await db.scalar(
            select(DurableChatRun).where(
                DurableChatRun.owner_key == owner_key,
                DurableChatRun.intake_channel == "telegram",
            )
        )
        cron_run = await db.scalar(
            select(DurableChatRun).where(
                DurableChatRun.owner_key == owner_key,
                DurableChatRun.intake_channel == "cron",
            )
        )
    assert telegram_run is not None and cron_run is not None

    snapshots = []
    result_message_ids = []
    for run_id in (http["id"], telegram_run.id, cron_run.id):
        async with factory() as db:
            run = await db.get(DurableChatRun, run_id)
            _order, step, attempt = await claim_ready_step(
                db,
                worker_id=f"terminal-parity-{run.intake_channel}",
                work_order_id=run.work_order_id,
            )
            await db.commit()
        result = await run_durable_chat(
            run.work_order_id,
            step.id,
            attempt.id,
            session_factory=factory,
            agent_factory=_TerminalAgent,
        )
        assert result["text"] == "durable terminal result"
        async with factory() as db:
            persisted_run = await db.get(DurableChatRun, run_id)
            order = await db.get(WorkOrder, persisted_run.work_order_id)
            message = await db.get(ChatMessage, persisted_run.result_message_id)
            result_message_ids.append(persisted_run.result_message_id)
            snapshots.append(
                {
                    "work_status": order.status,
                    "has_result_message_id": persisted_run.result_message_id is not None,
                    "result_content": message.content,
                }
            )

    assert (
        snapshots[0]
        == snapshots[1]
        == snapshots[2]
        == {
            # run_durable_chat persists the terminal response; the outer work-step
            # worker, not this function, owns transition from running to completed.
            "work_status": "running",
            "has_result_message_id": True,
            "result_content": "durable terminal result",
        }
    )
    assert len(set(result_message_ids)) == 3


@pytest.mark.asyncio
async def test_inactive_owner_is_rejected_by_http_telegram_and_cron(db_session, monkeypatch):
    """An owner deactivation is checked before every adapter can create a run."""
    factory = async_sessionmaker(
        bind=db_session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    monkeypatch.setattr("app.db.session._get_session_factory", lambda: factory)
    owner_key = f"revoked-channel-owner-{uuid.uuid4().hex}"
    async with factory() as db:
        db.add(
            User(
                sub=owner_key,
                email=f"{owner_key}@example.test",
                name=owner_key,
                preferred_username=owner_key,
                role="admin",
                is_active=False,
            )
        )
        binding = AgentChannelIdentity(
            owner_key=owner_key, channel="telegram", external_id="998003"
        )
        db.add(binding)
        grant = DelegationGrant(
            owner_key=owner_key,
            title="revoked E18 cron grant",
            actions=["agent.cron.run"],
            constraints={
                "schedule": "* * * * *",
                "prompt_sha256": hashlib.sha256(b"must not enter durable runtime").hexdigest(),
            },
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add(grant)
        await db.flush()
        db.add(
            AgentCron(
                schedule="* * * * *",
                prompt="must not enter durable runtime",
                owner_key=owner_key,
                delegation_grant_id=grant.id,
            )
        )
        await db.commit()

    async with factory() as db:
        with pytest.raises(HTTPException, match="Verified owner is inactive"):
            await submit_chat_run(
                ChatRunCreate(request_id=uuid.uuid4(), content="must not enter durable runtime"),
                db,
                _owner(owner_key),
            )
    update = _telegram_update(
        update_id=998004, telegram_id=998003, text="must not enter durable runtime"
    )
    await _telegram_bot(998003)._handle_text(update, MagicMock())
    assert update.message.reply_text.await_count == 1
    assert await agent_cron._dispatch(datetime(2026, 9, 29, 12, 1, tzinfo=UTC)) == 0
    async with factory() as db:
        assert not list(
            await db.scalars(select(DurableChatRun).where(DurableChatRun.owner_key == owner_key))
        )


@pytest.mark.asyncio
async def test_common_intake_does_not_trust_cached_owner_state(db_session):
    """A prior ORM read cannot hide a revoke committed before the locked check."""
    factory = async_sessionmaker(
        bind=db_session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    owner_key = f"stale-channel-owner-{uuid.uuid4().hex}"
    async with factory() as db:
        db.add(
            User(
                sub=owner_key,
                email=f"{owner_key}@example.test",
                name=owner_key,
                preferred_username=owner_key,
                role="admin",
                is_active=True,
            )
        )
        await db.commit()

    async with factory() as intake_db:
        cached_owner = await intake_db.scalar(select(User).where(User.sub == owner_key))
        assert cached_owner is not None and cached_owner.is_active
        async with factory() as revoke_db:
            current_owner = await revoke_db.scalar(
                select(User).where(User.sub == owner_key).with_for_update()
            )
            current_owner.is_active = False
            await revoke_db.commit()

        with pytest.raises(IntakeValidationError, match="Verified owner is inactive"):
            await submit_agent_intake(
                intake_db,
                identity=VerifiedIntakeIdentity(owner_key, "http"),
                request=AgentIntakeRequest(
                    channel="http",
                    external_message_id=f"stale-owner:{uuid.uuid4()}",
                    request_id=uuid.uuid4(),
                    content="must observe the committed revoke",
                ),
            )

    async with factory() as db:
        assert (
            await db.scalar(select(DurableChatRun.id).where(DurableChatRun.owner_key == owner_key))
            is None
        )


@pytest.mark.asyncio
async def test_telegram_notification_outage_does_not_change_persisted_domain_result(db_session):
    """Outbox delivery is a notification concern, never a work-result rollback."""
    factory = async_sessionmaker(
        bind=db_session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    owner_key = f"outage-parity-{uuid.uuid4().hex}"
    async with factory() as db:
        db.add(
            User(
                sub=owner_key,
                email=f"{owner_key}@example.test",
                name=owner_key,
                preferred_username=owner_key,
                role="operator",
                is_active=True,
            )
        )
        binding = AgentChannelIdentity(
            owner_key=owner_key, channel="telegram", external_id="998099"
        )
        db.add(binding)
        await db.flush()
        intake = await submit_agent_intake(
            db,
            identity=VerifiedIntakeIdentity(owner_key, "telegram"),
            request=AgentIntakeRequest(
                channel="telegram",
                external_message_id="update:outage",
                request_id=uuid.uuid4(),
                content="finish despite notification outage",
                source_binding_id=binding.id,
            ),
        )
    async with factory() as db:
        _order, step, attempt = await claim_ready_step(
            db, worker_id="outage-parity", work_order_id=intake.order.id
        )
        await db.commit()
    await run_durable_chat(
        intake.order.id, step.id, attempt.id, session_factory=factory, agent_factory=_TerminalAgent
    )
    async with factory() as db:
        before_order = await db.get(WorkOrder, intake.order.id)
        before_run = await db.get(DurableChatRun, intake.run.id)
        result_status = before_order.status
        result_message_id = before_run.result_message_id
        assert result_message_id is not None
    async with factory() as db:
        outbox = await db.scalar(
            select(AgentOutbox).where(
                AgentOutbox.work_order_id == intake.order.id,
                AgentOutbox.event_type == "chat.reply_ready",
            )
        )
        assert outbox is not None
        lease_token = uuid.uuid4()
        outbox.delivery_state = "leased"
        outbox.lease_token = lease_token
        outbox.lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
        outbox.attempts += 1
        await db.commit()
    claim = OutboxClaim(id=outbox.id, lease_token=lease_token)
    assert (
        await deliver_outbox_claim(
            factory, claim=claim, adapters={"telegram": _UnavailableNotification()}
        )
        == "dead_letter"
    )
    async with factory() as db:
        order = await db.get(WorkOrder, intake.order.id)
        run = await db.get(DurableChatRun, intake.run.id)
        assert order.status == result_status
        assert run.result_message_id == result_message_id
