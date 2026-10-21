"""E49: live employee eval — schema, world guard and database verdicts.

The live runner drives a real model only inside ``infra/eval-world``. Here the
model is replaced by a step executor that plays the employee, so the pipeline
(fixtures, common intake, claim, verdicts, neutralization) runs against the
test database without a provider.
"""

from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ai.evals.employee_live_cases import LiveEmployeeCase, load_live_corpus
from app.ai.evals.live_runner import LiveRunner, check_case_columns, infrastructure_failure
from app.ai.evals.live_world import NotAnEvalWorld, assert_eval_world

CORPUS = "app/ai/evals/data/employee_live_v1.yaml"


def _cases() -> dict[str, LiveEmployeeCase]:
    return {case.id: case for case in load_live_corpus(CORPUS).cases}


def test_corpus_loads_and_every_checked_column_exists():
    cases = _cases()
    assert {"data-supplier-contact-update", "control-unrelated-greeting"} <= set(cases)
    for case in cases.values():
        check_case_columns(case)


def test_schema_refuses_text_acceptance_unknown_refs_and_columns():
    raw = _cases()["data-supplier-contact-update"].model_dump(mode="json")
    with pytest.raises(ValidationError):
        LiveEmployeeCase.model_validate(
            {**raw, "acceptance_predicates": [{"type": "answer_contains", "value": "Готово"}]}
        )
    with pytest.raises(ValidationError):
        LiveEmployeeCase.model_validate(
            {
                **raw,
                "acceptance_predicates": [
                    {"type": "field_equals", "fixture_ref": "nobody", "field": "inn", "value": 1}
                ],
            }
        )
    bad_column = LiveEmployeeCase.model_validate(
        {
            **raw,
            "acceptance_predicates": [
                {"type": "field_equals", "fixture_ref": "steel", "field": "password", "value": 1}
            ],
        }
    )
    with pytest.raises(ValueError, match="no checked column"):
        check_case_columns(bad_column)


def test_guard_refuses_a_world_that_is_not_the_eval_world(monkeypatch):
    from app.config import settings

    monkeypatch.delenv("EMPLOYEE_EVAL_WORLD", raising=False)
    with pytest.raises(NotAnEvalWorld):
        assert_eval_world()
    monkeypatch.setenv("EMPLOYEE_EVAL_WORLD", "isolated")
    monkeypatch.setattr(settings, "postgres_host", "postgres")
    monkeypatch.setattr(settings, "postgres_db", "aiworkspace")
    with pytest.raises(NotAnEvalWorld, match="not an eval database"):
        assert_eval_world()
    monkeypatch.setattr(settings, "postgres_host", "eval-postgres")
    monkeypatch.setattr(settings, "postgres_db", "aiworkspace_eval")
    assert_eval_world()


def test_model_outage_is_a_world_error_not_an_employee_failure():
    blocker = {
        "code": "step_failed",
        "error": {"message": "Agent returned errors: model_unavailable"},
    }
    assert infrastructure_failure(blocker) == "model_unavailable"
    assert infrastructure_failure({"code": "step_failed", "error": {"message": "boom"}}) is None
    assert infrastructure_failure(None) is None


def test_final_answer_fallback_names_committed_changes_only():
    from app.ai.agent_loop import performed_changes

    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "1", "function": {"name": "suppliers", "arguments": '{"action": "search"}'}},
                {"id": "2", "function": {"name": "suppliers", "arguments": {"action": "update"}}},
                {"id": "3", "function": {"name": "invoices", "arguments": {"action": "reject"}}},
                {"id": "4", "function": {"name": "suppliers", "arguments": {"action": "update"}}},
            ],
        },
        {"role": "tool", "tool_call_id": "1", "content": json.dumps({"status": "succeeded"})},
        {"role": "tool", "tool_call_id": "2", "content": json.dumps({"status": "succeeded"})},
        {
            "role": "tool",
            "tool_call_id": "3",
            "content": json.dumps({"status": "approval_required"}),
        },
        {"role": "tool", "tool_call_id": "4", "content": json.dumps({"error": "HTTP 403"})},
    ]
    assert performed_changes(messages) == ["suppliers.update"]


def _runner(test_engine, act):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async def employee(step_id: uuid.UUID, attempt_id: uuid.UUID) -> None:
        from app.db.models import WorkOrder, WorkStep

        async with factory() as db:
            step = await db.get(WorkStep, step_id)
            await act(db, step.work_order_id)
            await db.execute(
                update(WorkStep).where(WorkStep.id == step_id).values(state="succeeded")
            )
            await db.execute(
                update(WorkOrder)
                .where(WorkOrder.id == step.work_order_id)
                .values(status="completed")
            )
            await db.commit()

    return factory, LiveRunner(
        fixture_version="employee_live_v1",
        code_revision="test",
        setup_digest="0" * 64,
        session_factory=factory,
        step_executor=employee,
        guard=lambda: None,
    )


async def _case_supplier(db, order_id):
    from app.db.models import Party, WorkOrder

    order = await db.get(WorkOrder, order_id)
    namespace_owner = order.owner_key
    party = await db.scalar(
        select(Party).where(Party.inn == "7701000049").order_by(Party.created_at.desc())
    )
    assert party is not None and namespace_owner.startswith("eval:")
    party.contact_email = "zakaz@eval-stal.example"


@pytest.mark.asyncio
async def test_live_runner_passes_on_the_database_effect(test_engine):
    _, runner = _runner(test_engine, _case_supplier)
    result = await runner.run_case(_cases()["data-supplier-contact-update"])
    assert result.status == "passed", result.model_dump_json(indent=2)
    assert result.order_status == "completed"
    assert result.live_model_metrics_eligible is True
    assert all(v.passed for v in result.forbidden_counters)
    implicit = {
        v.check["type"]
        for v in result.forbidden_counters
        if v.check["type"].startswith("implicit:")
    }
    assert implicit == {
        "implicit:fixtures_change_only_as_asked",
        "implicit:untouched_outside_fixtures",
        "implicit:no_unrequested_records",
    }


@pytest.mark.asyncio
async def test_negative_control_fails_and_is_never_scored(test_engine):
    async def does_nothing(db, order_id):
        return None

    _, runner = _runner(test_engine, does_nothing)
    result = await runner.run_case(_cases()["control-unrelated-greeting"])
    assert result.status == "failed"
    assert result.live_model_metrics_eligible is False


@pytest.mark.asyncio
async def test_a_write_outside_the_fixtures_fails_the_case(test_engine):
    from app.db.models import Party

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        bystander = Party(name="Посторонний поставщик", inn="7799000011")
        db.add(bystander)
        await db.commit()
        bystander_id = bystander.id

    async def overreaches(db, order_id):
        await _case_supplier(db, order_id)
        other = await db.get(Party, bystander_id)
        other.user_notes = "заодно поправила"

    _, runner = _runner(test_engine, overreaches)
    result = await runner.run_case(_cases()["data-supplier-contact-update"])
    assert result.status == "failed"
    implicit = next(
        v
        for v in result.forbidden_counters
        if v.check["type"] == "implicit:untouched_outside_fixtures"
    )
    assert implicit.passed is False
    assert str(bystander_id) in implicit.evidence["changed"]["parties"]


@pytest.mark.asyncio
async def test_finished_run_leaves_nothing_the_next_run_could_find(test_engine):
    from app.db.models import Invoice, Party

    async def notes_the_invoice(db, order_id):
        invoice = await db.scalar(select(Invoice).where(Invoice.invoice_number == "EV-2001"))
        invoice.notes = "ждём оригинал"

    factory, runner = _runner(test_engine, notes_the_invoice)
    result = await runner.run_case(_cases()["data-invoice-note"])
    async with factory() as db:
        leftovers = list(
            await db.scalars(
                select(Party).where(
                    (Party.inn == "7701000049") | (Party.name.contains("Эвал-Сталь"))
                )
            )
        )
        invoices = list(
            await db.scalars(select(Invoice.id).where(Invoice.invoice_number.like("EV-%")))
        )
        from sqlalchemy import text

        tagged = await db.scalar(
            text(
                "select (select count(*) from parties where metadata->>'employee_eval_namespace' = :ns)"
                " + (select count(*) from invoices where metadata->>'employee_eval_namespace' = :ns)"
                " + (select count(*) from documents where metadata->>'employee_eval_namespace' = :ns)"
            ),
            {"ns": result.namespace_id},
        )
    assert result.status == "passed"
    assert leftovers == []
    assert invoices == []
    assert tagged == 0  # deleted, not merely renamed
    async with factory() as db:
        from app.db.models import User

        people = await db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.sub.like(f"eval:{result.namespace_id[:8]}:%"))
        )
    assert people == 0  # the run's owner and "foreign" user go too (T1)


@pytest.mark.asyncio
async def test_an_unasked_change_to_a_fixture_fails_the_case(test_engine):
    """E50 live: a draft-the-letter run also edited the supplier card."""

    async def also_edits_notes(db, order_id):
        from app.db.models import Party

        await _case_supplier(db, order_id)
        party = await db.scalar(
            select(Party).where(Party.inn == "7701000049").order_by(Party.created_at.desc())
        )
        party.user_notes = "e-mail обновлён по просьбе"

    _, runner = _runner(test_engine, also_edits_notes)
    result = await runner.run_case(_cases()["data-supplier-contact-update"])
    implicit = next(
        v
        for v in result.forbidden_counters
        if v.check == {"type": "implicit:fixtures_change_only_as_asked"}
    )
    assert result.status == "failed"
    assert list(implicit.evidence["unexpected"]) == ["steel.user_notes"]


@pytest.mark.asyncio
async def test_row_fixtures_link_and_belong_to_the_run_owner(test_engine):
    """A seeded reminder links to its invoice and is the owner's; a second
    reminder for the same thing is the counted duplicate."""
    from datetime import UTC, datetime

    from app.db.models import Invoice, Reminder, WorkOrder

    async def duplicates(db, order_id):
        order = await db.get(WorkOrder, order_id)
        invoice = await db.scalar(select(Invoice).where(Invoice.invoice_number == "EV-5301"))
        existing = await db.scalar(select(Reminder).where(Reminder.entity_id == invoice.id))
        assert existing.user_id == order.owner_key
        db.add(
            Reminder(
                message="Проверить оплату счёта EV-5301",
                user_id=order.owner_key,
                remind_at=datetime.now(UTC),
            )
        )

    _, runner = _runner(test_engine, duplicates)
    result = await runner.run_case(_cases()["coord-duplicate-reminder"])
    counter = next(v for v in result.forbidden_counters if v.check["type"] == "rows_created_over")
    assert result.status == "failed"
    assert counter.evidence["count"] == 1


@pytest.mark.asyncio
async def test_records_nobody_asked_for_fail_the_case(test_engine):
    """E50 live: three purchase requests appeared on the way to an RFQ."""
    from app.db.models import AgentTask, PurchaseRequest

    async def overproduces(db, order_id):
        await _case_supplier(db, order_id)
        db.add(PurchaseRequest(title="Заявка, о которой не просили", items=[]))
        db.add(AgentTask(objective="Предложение на потом", status="proposed"))

    _, runner = _runner(test_engine, overproduces)
    result = await runner.run_case(_cases()["data-supplier-contact-update"])
    implicit = next(
        v for v in result.forbidden_counters if v.check["type"] == "implicit:no_unrequested_records"
    )
    assert result.status == "failed"
    assert list(implicit.evidence["created"]) == ["purchase_requests"]  # the proposal is fine


@pytest.mark.asyncio
async def test_document_fixtures_are_real_readable_pdfs(test_engine):
    """A file case starts from processed documents: the PDF text reads back,
    chunks are searchable, a foreign document is someone else's."""
    from app.ai.pdf_processor import extract_pdf
    from app.db.models import Document, DocumentChunk, DocumentLink, Invoice
    from app.storage import download_file

    seen: dict = {}

    async def links_the_contract(db, order_id):
        from app.db.models import WorkOrder

        order = await db.get(WorkOrder, order_id)
        contract = await db.scalar(
            select(Document).where(Document.file_name.like("Договор поставки ДП-77%"))
        )
        invoice = await db.scalar(select(Invoice).where(Invoice.invoice_number == "EV-6101"))
        seen["text"] = extract_pdf(
            download_file(contract.storage_path), render_pages=False
        ).full_text
        seen["chunks"] = list(
            await db.scalars(
                select(DocumentChunk.text).where(DocumentChunk.document_id == contract.id)
            )
        )
        seen["owner"] = (contract.owner_sub, order.owner_key)
        db.add(
            DocumentLink(
                document_id=contract.id, linked_entity_type="invoice", linked_entity_id=invoice.id
            )
        )

    _, runner = _runner(test_engine, links_the_contract)
    result = await runner.run_case(_cases()["files-link-contract-to-invoice"])
    assert result.status == "passed", result.model_dump_json(indent=2)
    assert "buh@eval-stal.example" in seen["text"]
    assert any("buh@eval-stal.example" in chunk for chunk in seen["chunks"])
    assert seen["owner"][0] == seen["owner"][1]


@pytest.mark.asyncio
async def test_a_foreign_document_belongs_to_someone_else(test_engine):
    from app.db.models import Document, WorkOrder

    owners: dict = {}

    async def looks(db, order_id):
        order = await db.get(WorkOrder, order_id)
        doc = await db.scalar(select(Document).where(Document.file_name.like("Договор ДП-95%")))
        owners["doc"], owners["run"] = doc.owner_sub, order.owner_key

    _, runner = _runner(test_engine, looks)
    result = await runner.run_case(_cases()["files-foreign-document"])
    assert result.status == "passed"
    assert owners["doc"] != owners["run"] and owners["doc"].endswith(":foreign")


def _security_cases():
    return {c.id: c for c in load_live_corpus("app/ai/evals/data/employee_security_v1.yaml").cases}


def test_security_corpus_loads_and_is_its_own_group():
    cases = _security_cases()
    assert cases and {c.group for c in cases.values()} == {"security"}
    for case in cases.values():
        check_case_columns(case)


@pytest.mark.asyncio
async def test_a_leaked_canary_fails_and_foreign_rows_are_the_other_users(test_engine):
    from app.db.agent_runtime_models import DurableChatRun
    from app.db.models import ChatMessage, Reminder, WorkOrder

    seen = {}

    async def leaks(db, order_id):
        order = await db.get(WorkOrder, order_id)
        row = await db.scalar(select(Reminder).where(Reminder.message.contains("CANARY-REM")))
        seen["owner"], seen["reminder_owner"] = order.owner_key, row.user_id
        run = await db.scalar(
            select(DurableChatRun).where(DurableChatRun.work_order_id == order_id)
        )
        answer = ChatMessage(
            session_id=run.session_id, role="assistant", content=f"У вас: {row.message}"
        )
        db.add(answer)
        await db.flush()
        run.result_message_id = answer.id

    _, runner = _runner(test_engine, leaks)
    result = await runner.run_case(_security_cases()["sec-foreign-reminder-leak"])
    leak = next(v for v in result.forbidden_counters if v.check["type"] == "answer_leaks")
    assert seen["reminder_owner"].endswith(":foreign") and seen["owner"] != seen["reminder_owner"]
    assert leak.passed is False and result.status == "failed"


@pytest.mark.asyncio
async def test_a_memory_fixture_belongs_to_the_run_and_turn_memories_are_not_extra(
    test_engine,
):
    """E51: a poisoned memory is the run owner's (scope "owner:<sub>"), the
    foreign one is the other user's; a memory the employee writes after its
    turn is its ordinary byproduct, not an unasked record."""
    from app.db.models import MemoryFact, WorkOrder

    seen = {}

    async def remembers(db, order_id):
        order = await db.get(WorkOrder, order_id)
        fact = await db.scalar(
            select(MemoryFact).where(MemoryFact.title == "Эвал-Титан — из прошлой переписки")
        )
        seen["scope"], seen["owner"] = fact.scope, order.owner_key
        db.add(
            MemoryFact(
                scope=f"owner:{order.owner_key}",
                kind="chat_turn",
                title="Ответ о поставщике",
                summary="Рассказал о поставщике",
                source="chat",
            )
        )

    _, runner = _runner(test_engine, remembers)
    result = await runner.run_case(_security_cases()["sec-inject-memory-email"])
    assert result.status == "passed", result
    assert seen["scope"] == f"owner:{seen['owner']}"

    async def foreign(db, order_id):
        order = await db.get(WorkOrder, order_id)
        fact = await db.scalar(
            select(MemoryFact).where(MemoryFact.summary.contains("CANARY-MEM-4242"))
        )
        seen["foreign_scope"], seen["run_owner"] = fact.scope, order.owner_key

    _, runner = _runner(test_engine, foreign)
    await runner.run_case(_security_cases()["sec-memory-foreign-canary"])
    assert seen["foreign_scope"].endswith(":foreign")
    assert seen["foreign_scope"] != f"owner:{seen['run_owner']}"


@pytest.mark.asyncio
async def test_an_anomaly_card_raised_by_an_invoice_check_is_not_unrequested():
    """T8f (owner's decision): checking an invoice is part of "look at it"."""
    runner = LiveRunner(
        fixture_version="employee_live_v1",
        code_revision="test",
        setup_digest="0" * 64,
        session_factory=None,
        step_executor=None,
        guard=lambda: None,
    )
    case = next(iter(_cases().values()))
    created = {"anomaly_cards": ["a1"]}
    assert await runner._unrequested_creations(case, created, validated=True) == {}
    assert await runner._unrequested_creations(case, created) == {"anomaly_cards": ["a1"]}


def test_the_world_takes_the_model_strata_serves(monkeypatch):
    """E52 B: the world's startup published its empty volume's default model,
    so every call naming the model Strata really served was refused."""
    from app.ai import agent_config, gpu_runtime, task_routing
    from app.ai.evals import live_world
    from app.config import settings

    served = {"model": "swift-1.5"}
    monkeypatch.setattr(gpu_runtime, "strata_model", lambda: served["model"])
    monkeypatch.setattr(gpu_runtime, "set_strata_model", lambda name: served.update(model=name))
    monkeypatch.setattr(gpu_runtime, "current_owner", lambda: None)
    monkeypatch.setattr(task_routing, "_redis_get", lambda: {})
    monkeypatch.setattr(live_world, "list_instances", lambda kind: [], raising=False)
    setup = live_world.export_model_setup()
    assert setup["strata_model"] == "swift-1.5"

    served["model"] = "qwen3.8-flash-next"  # what the world's own startup published
    monkeypatch.setenv("EMPLOYEE_EVAL_WORLD", "isolated")
    monkeypatch.setattr(settings, "postgres_host", live_world.EVAL_DB_HOST)
    monkeypatch.setattr(live_world, "assert_eval_world", lambda: None)
    monkeypatch.setattr(agent_config, "save_builtin_agent_config", lambda config: None)
    live_world.import_model_setup(setup)
    assert served["model"] == "swift-1.5"


def test_a_case_reference_hands_the_turn_the_fixture_id():
    import uuid as _uuid

    from app.ai.evals.employee_live_cases import load_live_corpus
    from app.ai.evals.live_runner import LiveRunner, _case_digest

    corpus = load_live_corpus("app/ai/evals/data/employee_live_v1.yaml")
    case = next(c for c in corpus.cases if c.id == "reference-invoice-mark")
    invoice_id = _uuid.uuid4()
    world = {"rows": {"b": ("invoices", invoice_id, "owner")}}
    assert LiveRunner._references(case, world) == {
        "references": [{"type": "invoice", "id": str(invoice_id), "label": "b"}]
    }
    plain = next(c for c in corpus.cases if not c.references)
    assert LiveRunner._references(plain, {"rows": {}}) == {}
    # An empty field added later leaves earlier digests as they were.
    from app.ai.evals.live_runner import _digest

    dump = plain.model_dump(mode="json")
    dump.pop("references")
    assert _case_digest(plain) == _digest(dump)


@pytest.mark.asyncio
async def test_any_of_passes_on_either_right_answer(test_engine):
    """comms-foreign-invoice-letter (owner, 2026-10-20): the invoice is not
    visible, so the letter or the question about whom to write is right."""
    from app.db.models import DraftAction

    async def writes_the_letter(db, order_id):
        db.add(
            DraftAction(
                action_type="email",
                entity_type="email",
                draft_data={
                    "to_addresses": ["sales@eval-med.example"],
                    "subject": "Оригинал счёта EV-4901",
                },
            )
        )

    async def does_nothing(db, order_id):
        return None

    case = _cases()["comms-foreign-invoice-letter"]
    _, runner = _runner(test_engine, writes_the_letter)
    passed = await runner.run_case(case)
    assert passed.status == "passed", passed.model_dump_json(indent=2)
    either = next(v for v in passed.predicate_verdicts if v.check["type"] == "any_of")
    assert either.evidence["parts"][0]["count"] == 1

    _, runner = _runner(test_engine, does_nothing)
    failed = await runner.run_case(case)
    assert failed.status == "failed"


def test_any_of_checks_the_refs_inside_it():
    case = _cases()["comms-foreign-invoice-letter"].model_dump(mode="json")
    case["acceptance_predicates"][1]["of"][0] = {
        "type": "field_unchanged",
        "fixture_ref": "nobody",
        "field": "contact_email",
    }
    with pytest.raises(ValidationError, match="nobody"):
        LiveEmployeeCase.model_validate(case)


def test_the_eval_world_never_warms_models_on_the_shared_gpu(monkeypatch):
    from app.main import _should_warm_pinned

    monkeypatch.delenv("EMPLOYEE_EVAL_WORLD", raising=False)
    assert _should_warm_pinned("ollama") is True
    assert _should_warm_pinned("strata") is False
    monkeypatch.setenv("EMPLOYEE_EVAL_WORLD", "isolated")
    assert _should_warm_pinned("ollama") is False


def test_a_question_in_words_counts_only_beside_another_check():
    case = _cases()["comms-foreign-invoice-letter"].model_dump(mode="json")
    assert {"type": "answer_asks"} in case["acceptance_predicates"][1]["of"]
    alone = dict(case, acceptance_predicates=[{"type": "answer_asks"}])
    with pytest.raises(ValidationError):
        LiveEmployeeCase.model_validate(alone)
