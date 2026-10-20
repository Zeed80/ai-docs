"""Procedures the agent proposes on its own after finished work.

hermes-agent writes a skill after a successful multi-step task; here the same
reflection only proposes: the procedure reaches the prompt once the owner
approves it. Off unless ``procedure_autopropose_enabled``. The call yields the
GPU to people's turns like the semantic verifier.
"""

from __future__ import annotations

import json
import uuid

import structlog
from sqlalchemy import select

logger = structlog.get_logger()

MIN_CALLS = 4

_SYSTEM = """Ты решаешь, стоит ли записать порядок работы как процедуру для ИИ-сотрудника.
Тебе дана выполненная задача: что просили, какие инструменты вызывались и чем кончилось,
а также список уже существующих процедур.
Предлагай, только если это повторяющаяся работа с неочевидным порядком: какие данные
проверить, где их взять, что спросить у человека, в каком порядке действовать. Не предлагай
для разовой просьбы, для простого поиска или если тема уже есть в списке процедур.
Текст — правила для сотрудника по шагам, без данных этой задачи (имён, сумм, номеров).
Верни JSON: {"propose": bool, "name": "латиница-через-дефис", "description": "одна строка
до 150 знаков", "text": "markdown по шагам", "reason": "из какой работы это выяснилось"}.
Только JSON."""


async def _trace(db, work_order_id: uuid.UUID) -> dict | None:
    from app.db.agent_runtime_models import ChatLogicalAction, DurableChatRun
    from app.db.models import ChatMessage, WorkOrder

    order = await db.get(WorkOrder, work_order_id)
    if order is None or order.status != "completed":
        return None
    actions = (
        await db.scalars(
            select(ChatLogicalAction)
            .where(
                ChatLogicalAction.work_order_id == work_order_id,
                ChatLogicalAction.status == "result_recorded",
            )
            .order_by(ChatLogicalAction.created_at)
        )
    ).all()
    calls = []
    for action in actions:
        request = action.request or {}
        arguments = request.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {}
        arguments = arguments if isinstance(arguments, dict) else {}
        calls.append(
            {
                "tool": f"{request.get('name')}.{arguments.get('action') or ''}",
                "arguments": json.dumps(arguments, ensure_ascii=False, default=str)[:300],
            }
        )
    run = await db.scalar(
        select(DurableChatRun).where(DurableChatRun.work_order_id == work_order_id)
    )
    answer = None
    if run is not None and run.result_message_id is not None:
        message = await db.get(ChatMessage, run.result_message_id)
        answer = message.content if message is not None else None
    return {
        "order": order,
        "request": order.objective,
        "calls": calls,
        "answer": (answer or order.result_summary or "")[:3000],
    }


async def suggest_procedure(work_order_id: uuid.UUID, *, session_factory=None) -> str | None:
    """Ask the model whether this work is worth a procedure; propose it if so.

    Returns the proposed name, or None (switched off, too short, nothing new).
    """
    from app.ai.agent_config import get_builtin_agent_config
    from app.ai.model_resolver import get_reasoning_model
    from app.ai.ollama_client import generate_json
    from app.ai.procedures import ProcedureError, list_procedures, propose, refresh_learned
    from app.audit.service import log_action
    from app.db.session import _get_session_factory

    if not get_builtin_agent_config().procedure_autopropose_enabled:
        return None
    factory = session_factory or _get_session_factory()
    async with factory() as db:
        trace = await _trace(db, work_order_id)
        await refresh_learned(db)
    if trace is None or len(trace["calls"]) < MIN_CALLS:
        return None
    existing = [{"name": p.name, "description": p.description} for p in list_procedures()]
    prompt = json.dumps(
        {
            "request": trace["request"],
            "calls": trace["calls"][:30],
            "answer": trace["answer"],
            "existing_procedures": existing,
        },
        ensure_ascii=False,
    )
    model = get_reasoning_model(confidential=True)
    try:
        verdict = await generate_json(
            prompt,
            model=model.model,
            provider=model.provider,
            system=_SYSTEM,
            temperature=0.0,
            max_tokens=2048,
            timeout_seconds=120,
        )
    except Exception as exc:  # noqa: BLE001 — a proposal is optional, the work is done
        logger.info("procedure_suggest_failed", work_order_id=str(work_order_id), error=str(exc))
        return None
    if not isinstance(verdict, dict) or verdict.get("propose") is not True:
        return None
    async with factory() as db:
        try:
            row = await propose(
                db,
                name=str(verdict.get("name") or ""),
                description=str(verdict.get("description") or ""),
                text=str(verdict.get("text") or ""),
                reason=str(verdict.get("reason") or "")[:2000] or None,
                actor="agent:auto",
                origin="agent",
                work_order_id=work_order_id,
            )
        except ProcedureError as exc:
            logger.info(
                "procedure_suggest_rejected", work_order_id=str(work_order_id), error=str(exc)
            )
            return None
        await log_action(
            db,
            action="procedure.proposed",
            entity_type="agent_procedure",
            entity_id=row.id,
            user_id="agent:auto",
            details={"name": row.name, "work_order_id": str(work_order_id)},
        )
        await db.commit()
        logger.info("procedure_proposed", name=row.name, work_order_id=str(work_order_id))
        return row.name


async def curate_procedures(*, session_factory=None) -> dict[str, int]:
    from app.ai.procedures import curate
    from app.db.session import _get_session_factory

    factory = session_factory or _get_session_factory()
    async with factory() as db:
        counts = await curate(db)
        await db.commit()
    if any(counts.values()):
        logger.info("procedures_curated", **counts)
    return counts
