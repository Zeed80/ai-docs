"""workspace.sql_table through the capability gateway carries its handoff."""

import httpx
import pytest

from app.ai import agent_loop
from app.ai.actor_context import set_acting_user
from app.ai.agent_config import BuiltinAgentConfig
from app.api import capability_router, workspace
from app.auth.work_budget_handoff import WORK_BUDGET_HANDOFF_HEADER, WORKSPACE_SQL_TABLE_PATH
from app.domain.capability_payload import capability_proxy_body
from tests.test_work_budget_direct_text import _install_local_runtime
from tests.test_work_budget_http_recipient import (
    BODY,
    _request,
    _Response,
    _result,
    _rows,
    _setup,
)

GATEWAY_SKILL = {"name": "workspace", "method": "POST", "path": "/api/agent/cap/workspace"}


class _GatewayRequest:
    def __init__(self, headers):
        self.headers = headers


@pytest.mark.asyncio
async def test_chat_sql_table_through_gateway_publishes_on_the_shared_ledger(
    test_engine, monkeypatch
):
    factory, run, parent, _ = await _setup(test_engine, monkeypatch)
    _install_local_runtime(monkeypatch)
    responses = iter(["SELECT id FROM invoices", "Invoices"])
    writes, forwarded = [], []

    async def execute_sql(*_args, **_kwargs):
        return [{"id": "one"}]

    async def publish(_event):
        pass

    monkeypatch.setattr("app.ai.table_sql_pipeline.execute_sql", execute_sql)
    monkeypatch.setattr(workspace.chat_bus, "publish", publish)
    monkeypatch.setattr(
        workspace, "upsert_workspace_block", lambda key, block: writes.append(key) or block
    )

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, url, **kwargs):
            if url.endswith("/api/agent/cap/workspace"):
                # The gateway: same body flattening, service headers re-signed
                # for the acting user, and only the handoff relayed.
                args = dict(kwargs["json"])
                body = capability_proxy_body(args)
                headers = capability_router._service_headers(kwargs["headers"]["X-Acting-User"])
                headers.update(
                    capability_router._relayed_handoff(
                        "workspace", args["action"], _GatewayRequest(kwargs["headers"])
                    )
                )
                forwarded.append(headers)
                response = await workspace.publish_sql_table(
                    workspace.WorkspaceSqlTableRequest(**body), _request(headers, body)
                )
                return httpx.Response(200, json=_result(response))
            return _Response(next(responses))

    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", lambda **_kwargs: Client())
    set_acting_user(parent.expected_owner_key)
    try:
        result = await agent_loop.execute_skill(
            GATEWAY_SKILL,
            {"action": "sql_table", "filters": dict(BODY)},
            BuiltinAgentConfig(),
            budget_context=parent,
        )
    finally:
        set_acting_user(None)

    assert result["status"] == "succeeded"
    assert writes == [BODY["canvas_id"]]
    assert WORK_BUDGET_HANDOFF_HEADER in forwarded[0]
    assert len(await _rows(factory, run["work_order_id"], "recipient-llm:")) == 2


def test_gateway_relays_the_handoff_only_for_workspace_sql_table():
    request = _GatewayRequest({WORK_BUDGET_HANDOFF_HEADER: "token"})
    relay = capability_router._relayed_handoff
    assert relay("workspace", "sql_table", request) == {WORK_BUDGET_HANDOFF_HEADER: "token"}
    assert relay("workspace", "spec_table", request) == {}
    assert relay("email", "sql_table", request) == {}
    assert relay("workspace", "sql_table", _GatewayRequest({})) == {}


def test_proxy_body_matches_the_gateway_flattening():
    args = {"action": "sql_table", "reason": "why", "filters": {"task": "t"}, "body": {"limit": 3}}
    assert capability_proxy_body(args) == {"task": "t", "limit": 3}
    assert args["filters"] == {"task": "t"}  # caller's arguments are not mutated
    assert WORKSPACE_SQL_TABLE_PATH == "/api/workspace/agent/generated/sql-table"
