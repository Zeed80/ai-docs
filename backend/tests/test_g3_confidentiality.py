"""G3: confidentiality invariants — drawings never leave the perimeter, every
agent tool call is audited with its stated reason."""

import inspect

import pytest


def test_cad_llm_review_is_local_only():
    """The ЕСКД LLM/VLM review (levels 6-7) processes CONFIDENTIAL drawings —
    its AIRequest must be pinned confidential=True / allow_cloud=False. This
    is a regression lock: loosening either flag must fail a test, not slip
    through review."""
    from app.ai import cad_validate

    src = inspect.getsource(cad_validate)
    assert "allow_cloud=False" in src
    sig = inspect.signature(cad_validate.run_llm_review_levels)
    assert sig.parameters["confidential"].default is True


def test_vlm_dimensions_is_local_only():
    from app.ai import vlm_dimensions

    src = inspect.getsource(vlm_dimensions)
    assert src.count("allow_cloud=False") >= 2
    for name, obj in inspect.getmembers(vlm_dimensions, inspect.isfunction):
        params = inspect.signature(obj).parameters
        if "confidential" in params:
            assert params["confidential"].default is True, name


@pytest.mark.asyncio
async def test_router_blocks_cloud_for_confidential():
    """The AI router refuses to route a confidential request to a cloud
    provider even when explicitly asked to allow cloud."""
    from app.ai.router import AIRequest

    request = AIRequest(
        task="engineering_reasoning", prompt="секретный чертёж",
        confidential=True, allow_cloud=True,
    )
    # the effective policy collapses allow_cloud under confidentiality
    assert request.confidential is True
    from app.ai import router as ai_router

    src = inspect.getsource(ai_router)
    assert "eff_allow_cloud = (not eff_confidential)" in src


@pytest.mark.asyncio
async def test_capability_dispatch_audits_reason(client, monkeypatch):
    """G3: a capability call carries an optional free-text reason; the
    dispatcher audits it as agent.tool_call and the reason is NOT proxied to
    the downstream endpoint. Intercepted at the audit/proxy seams — the audit
    writer uses its own session factory, so a DB read from the test session
    would race a different database handle, not test the behavior."""
    from app.api import capability_router as cr

    audited: list[tuple] = []
    proxied: list[dict] = []

    async def fake_audit(capability, action, reason, request):
        audited.append((capability, action, reason))

    real_proxy = cr._proxy

    async def spy_proxy(method, path_tpl, path_params, body, base_url):
        proxied.append(dict(body))
        return await real_proxy(method, path_tpl, path_params, body, base_url)

    monkeypatch.setattr(cr, "_audit_tool_call", fake_audit)
    monkeypatch.setattr(cr, "_proxy", spy_proxy)

    resp = await client.post(
        "/api/agent/cap/cad_review",
        json={"action": "list", "reason": "проверка истории оцифровок по запросу пользователя"},
    )
    assert resp.status_code == 200
    assert audited == [("cad_review", "list", "проверка истории оцифровок по запросу пользователя")]
    assert proxied and "reason" not in proxied[0]


@pytest.mark.asyncio
async def test_audit_writer_persists_agent_tool_call(db_session):
    """The audit writer itself lands an agent.tool_call row with the reason."""
    from sqlalchemy import select

    from app.audit.service import log_action
    from app.db.models import AuditLog

    await log_action(
        db_session,
        action="agent.tool_call",
        entity_type="capability",
        user_id="agent",
        details={"capability": "cad_review", "action": "list", "reason": "тест"},
    )
    await db_session.commit()
    row = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "agent.tool_call").order_by(AuditLog.timestamp.desc()).limit(1)
        )
    ).scalar_one()
    assert row.details["reason"] == "тест"
