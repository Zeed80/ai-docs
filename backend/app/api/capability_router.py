"""Capability dispatcher — unified /api/agent/cap/{capability} endpoint.

Routes agent capability calls to the appropriate backend endpoints.
Each capability accepts an `action` field plus context parameters.
"""

from __future__ import annotations

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.ai.agent_config import get_builtin_agent_config
from app.ai.capability_manifest import load_capability_manifest
from app.ai.policy_engine import check_tool_execution, classify_capability_action_risk
from app.auth.jwt import get_current_user
from app.auth.models import UserInfo, UserRole
from app.config import settings


async def _bind_actor(request: Request, user: UserInfo = Depends(get_current_user)) -> None:
    request.state.execution_user = user
    from app.ai.actor_context import set_acting_user

    set_acting_user(user.sub)


router = APIRouter(tags=["capabilities"], dependencies=[Depends(_bind_actor)])
logger = structlog.get_logger()

# Maps capability → action → (method, path_template, path_params)
# path_params: list of arg keys that get interpolated into the URL
from app.ai.tool_catalog import dispatch_routes

_DISPATCH = dispatch_routes()


def capability_action_map() -> dict[str, list[str]]:
    """Action enum per capability — the single source of truth for tool schemas.

    The agent's tool catalog injects these as JSON-schema ``enum`` on the
    ``action`` field, so the model can only emit a valid action and the dispatcher
    never has to reject a guessed string. Drift is structurally impossible because
    both the schema and the routing read this same ``_DISPATCH`` table.
    """
    return {cap: sorted(actions.keys()) for cap, actions in _DISPATCH.items()}


# Capabilities handled by dedicated routes outside the generic _DISPATCH table.
_SPECIAL_CAPABILITIES = {"vault", "mcp"}


def validate_gateway_grants() -> list[str]:
    """Ф6.9 — the direction nobody checked.

    ``gateway.yml`` grants "capability.action" names to roles. Three e-mail
    template actions were granted while existing in no dispatch table at all,
    so an agent explicitly given the right got "unknown action" — indis-
    tinguishable, from the model's side, from its own mistake.

    Kept separate from validate_capability_catalog(): that one asserts
    manifest↔dispatch and is expected to be clean, while this surfaces a
    pre-existing backlog in other capabilities that is not this subsystem's to
    fix. The e-mail slice of it is asserted empty by the tests.
    """
    problems: list[str] = []
    try:
        from app.ai.gateway_config import gateway_config

        granted: set[str] = set(getattr(gateway_config, "exposed_skills", None) or ())
    except Exception:  # noqa: BLE001 — gateway config is optional in some tests
        return problems

    for entry in sorted(granted):
        if "." not in entry:
            continue
        cap, _, action = entry.partition(".")
        if cap not in _DISPATCH or not action:
            continue
        if action not in _DISPATCH[cap]:
            problems.append(
                f"gateway grants '{entry}' but '{action}' is not an action of "
                f"capability '{cap}' — the agent would get 'unknown action'"
            )
    return problems


def validate_capability_catalog() -> list[str]:
    """Fail-closed consistency check: capabilities.yml ↔ _DISPATCH.

    Returns a list of human-readable problems (empty = consistent). Run as a
    test and optionally at startup so the hand-curated manifest can never drift
    from the dispatcher's real routing table.
    """
    manifest = load_capability_manifest()
    problems: list[str] = []
    declared = {c.name for c in manifest.capabilities}

    # Every declared capability must be routable (or a known special route).
    for name in declared:
        if name not in _DISPATCH and name not in _SPECIAL_CAPABILITIES:
            problems.append(f"capability '{name}' declared in manifest but absent from _DISPATCH")

    # Every routable capability should be declared so the model can see it.
    for name in _DISPATCH:
        if name not in declared:
            problems.append(f"capability '{name}' in _DISPATCH but not declared in manifest")

    # Gate actions must reference real actions of their capability.
    for cap in manifest.capabilities:
        if cap.name in _SPECIAL_CAPABILITIES:
            continue
        actions = set(_DISPATCH.get(cap.name, {}).keys())
        for gate in cap.gate_actions:
            if gate not in actions:
                problems.append(
                    f"gate_action '{cap.name}.{gate}' has no matching action in _DISPATCH"
                )
        for action in cap.non_recipeable_actions:
            if action not in actions:
                problems.append(
                    f"non_recipeable_action '{cap.name}.{action}' has no matching action in _DISPATCH"
                )

    # Направление, которого не хватало: путь маршрута требует path-параметр, а
    # схема capability его не объявляет — модель не может его заполнить, и
    # действие возвращает 422 missing_args, оставаясь при этом «объявленным».
    # Так были недостижимы email.read (email_id), get_attachment (filename) и
    # весь набор шаблонов (template_id). Ровно тот класс молчаливых потерь,
    # что и невидимая capability: заявлено, но не вызывается.
    for cap in manifest.capabilities:
        if cap.name in _SPECIAL_CAPABILITIES:
            continue
        properties = set(((cap.parameters or {}).get("properties") or {}).keys())
        if not properties:
            continue
        for action, (_m, _path, path_params) in _DISPATCH.get(cap.name, {}).items():
            for param in path_params:
                if param not in properties:
                    problems.append(
                        f"action '{cap.name}.{action}' needs path parameter "
                        f"'{param}', which the capability schema does not declare"
                    )
    return problems


def _acting_user(request: Request) -> str | None:
    """Identity established by authentication, never a caller-supplied header."""
    user = getattr(request.state, "execution_user", None)
    return user.sub if user and user.sub != "agent-service" else None


def _service_headers(acting_user: str | None = None) -> dict:
    """Auth headers for internal service-to-service calls.

    ``X-Acting-User`` is relayed verbatim from the agent's call so downstream
    endpoints can scope per-user data (app.auth.acting.get_effective_user)
    instead of seeing the full-admin service account. Dropping it degrades to
    "service account only", never to "somebody else's data".
    """
    from app.config import settings

    headers: dict = {}
    if settings.agent_service_key:
        headers["X-API-Key"] = settings.agent_service_key
    if acting_user:
        headers["X-Acting-User"] = acting_user
        from app.auth.execution_context import sign_execution_context

        headers["X-Execution-Context"] = sign_execution_context(acting_user)
    return headers


_QUERY_PARAM_CACHE: dict[tuple[str, str], frozenset[str]] = {}


def _route_query_params(method: str, path_tpl: str) -> frozenset[str]:
    """Names the target route expects in the QUERY STRING, not the body.

    The dispatcher used to send every non-path argument of a POST/PATCH as
    JSON. For endpoints whose parameters are query-only (FastAPI's default for
    scalars) that meant the argument silently never arrived: the agent's
    document search landed with an empty query and failed validation, and
    flags like `force`/`received_by`/`batch_qty` were quietly ignored.

    Resolved from the live route table — same process, so it always matches the
    real signatures instead of a hand-maintained list that drifts.
    """
    import re as _re

    key = (method, path_tpl)
    cached = _QUERY_PARAM_CACHE.get(key)
    if cached is not None:
        return cached

    def _shape(path: str) -> str:
        return _re.sub(r"\{[^}]+\}", "{}", path or "")

    names: frozenset[str] = frozenset()
    try:
        from app.main import app as _app

        want = _shape(path_tpl)
        for route in _app.routes:
            if _shape(getattr(route, "path", "")) != want:
                continue
            if method not in (getattr(route, "methods", None) or []):
                continue
            dependant = getattr(route, "dependant", None)
            if dependant is not None:
                names = frozenset(
                    n
                    for p in dependant.query_params
                    for n in (p.name, getattr(p, "alias", None))
                    if n
                )
            break
    except Exception:  # noqa: BLE001 — routing must never fail on introspection
        names = frozenset()
    _QUERY_PARAM_CACHE[key] = names
    return names


async def _proxy(
    method: str,
    path: str,
    path_params: list[str],
    body: dict,
    base_url: str,
    acting_user: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Interpolate path params, split remaining args into query/body, proxy request."""
    query: dict = {}
    payload: dict = {}

    query_only = _route_query_params(method, path) if method != "GET" else frozenset()
    for k, v in body.items():
        if k in path_params:
            path = path.replace(f"{{{k}}}", str(v))
        elif method == "GET" or k in query_only:
            query[k] = v
        else:
            payload[k] = v

    url = base_url.rstrip("/") + path
    headers = _service_headers(acting_user)
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    # Web research/browse read many live pages (+ PDF OCR) and legitimately take
    # minutes — the default 30s would time out and trigger wasteful retries that
    # re-run the whole search. Give these paths a generous budget.
    # Catalog ingestion fetches a live page or PDF (with OCR) and then runs an
    # LLM extraction over it — same minutes-scale shape as web research, and it
    # is not retry-safe: a 30s timeout here left half-created draft entries.
    _LONG_RUNNING = (
        "/api/web-search/",
        "/attach-web-catalog",
        "/ingest-web-source",
        "/discover-catalogs",
    )
    timeout = 900.0 if any(marker in path for marker in _LONG_RUNNING) else 30.0
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if method == "GET":
                resp = await client.get(url, params=query, headers=headers)
            elif method == "POST":
                resp = await client.post(url, params=query or None, json=payload, headers=headers)
            elif method == "PATCH":
                resp = await client.patch(url, params=query or None, json=payload, headers=headers)
            elif method == "DELETE":
                resp = await client.delete(url, params=query or None, headers=headers)
            else:
                return {"error": f"Unsupported method: {method}"}

        if resp.status_code < 400:
            try:
                data = resp.json()
                # Normalise bare list responses to {"items": [...], "total": N}
                if isinstance(data, list):
                    return {"items": data, "total": len(data)}
                return data
            except Exception:
                return {"text": resp.text[:2000]}
        if resp.status_code == 422:
            # A 422 on a capability call is usually a NAME mismatch between what
            # the capability declares and what the endpoint accepts (live: the
            # agent sent `query`, the endpoint wanted `q`). Say so explicitly —
            # otherwise it reads as "the agent called it wrong".
            logger.warning(
                "capability_argument_contract_mismatch",
                method=method,
                path=path,
                sent=sorted(set(query) | set(payload)),
                detail=resp.text[:300],
            )
        raise HTTPException(
            resp.status_code, {"error": f"HTTP {resp.status_code}", "detail": resp.text[:300]}
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, "Downstream service unavailable") from exc


def _validate_capability_contract(
    capability_name: str,
    action: str,
    path_params: list[str],
    body: dict,
) -> None:
    """Fail closed when dispatcher and the reviewed capability contract drift."""
    try:
        manifest = load_capability_manifest()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Capability contract unavailable: {exc}",
        ) from exc

    capability = manifest.by_name.get(capability_name)
    if capability is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "contract_unavailable",
                "message": f"Capability '{capability_name}' is not declared in the active manifest",
            },
        )

    if classify_capability_action_risk(action) == "high" and action not in capability.gate_actions:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "gate_missing",
                "message": (
                    f"Risky action '{capability_name}.{action}' is blocked because "
                    "it is missing from gate_actions"
                ),
            },
        )

    missing = [
        name
        for name in path_params
        if name not in body or body[name] is None or str(body[name]).strip() == ""
    ]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "missing_args",
                "message": f"Missing required path parameters: {missing}",
                "missing": missing,
            },
        )


def _capability_gate_actions(capability_name: str) -> set[str]:
    """Return approval-gated actions from the reviewed manifest."""
    manifest = load_capability_manifest()
    capability = manifest.by_name.get(capability_name)
    if capability is None:
        return set()
    return set(capability.gate_actions or [])


def _request_has_internal_approval(request: Request, raw_body: dict | None = None) -> bool:
    """Accept approval proof only from the internal agent transport.

    In production the service key is the trust boundary. The internal marker is
    kept for local/dev environments where AGENT_SERVICE_KEY may be empty.

    ``X-Agent-Approval-Digest`` привязывает одобрение к аргументам вызова: без
    него заголовок означал лишь «цикл что-то одобрил» и подходил к любому
    другому вызову той же capability — человек подтверждал одно письмо, а уйти
    могло другое. Заголовок обязателен для чат-пути (app.ai.agent_loop его
    всегда ставит); долговечный путь (app.tasks.work_orders) проверяет
    соответствие аргументов своим digest'ом до вызова и приходит без него.
    """
    if request.headers.get("X-Agent-Approval") != "granted":
        return False
    if settings.agent_service_key:
        if request.headers.get("X-API-Key") != settings.agent_service_key:
            return False
    elif request.headers.get("X-Internal-Agent") != "1":
        return False

    claimed = (request.headers.get("X-Agent-Approval-Digest") or "").strip()
    if not claimed:
        return False
    from app.ai.agent_loop import capability_args_digest

    return claimed == capability_args_digest(raw_body or {})


def _enforce_capability_policy(
    capability_name: str,
    action: str,
    body: dict,
    request: Request,
    raw_body: dict | None = None,
    *,
    approval_authorized: bool = False,
) -> None:
    """Apply the same risk/approval policy at the HTTP dispatcher boundary."""
    config = get_builtin_agent_config()
    from app.ai.tool_catalog import get_tool

    definition = get_tool(capability_name, action)
    user = getattr(request.state, "execution_user", None)
    if user is not None:
        if user.sub == "agent-service":
            raise HTTPException(403, "Agent execution requires a human owner")
        if definition and definition.admin_only and UserRole.admin not in user.roles:
            raise HTTPException(403, "This operation requires administrator rights")
        if set(user.roles) <= {UserRole.viewer} and (
            definition is None or definition.effect != "read"
        ):
            raise HTTPException(403, "Viewer cannot execute write operations")
    gate_actions = _capability_gate_actions(capability_name)
    approval_gates = set(config.approval_gates)

    # "*" gates every action of the capability — used by "mcp" (Б17): the
    # trustworthiness of an individual MCP tool is unknown at manifest-authoring
    # time (tool set depends on which external servers are connected), so every
    # MCP tool call requires approval by default rather than enumerating names.
    if action in gate_actions or "*" in gate_actions:
        approval_gates.add(capability_name)
        if not approval_authorized and not _request_has_internal_approval(request, raw_body):
            raise HTTPException(
                status_code=423,
                detail={
                    "error_code": "approval_required",
                    "message": (
                        f"Action '{capability_name}.{action}' requires an "
                        "approved internal agent execution."
                    ),
                    "capability": capability_name,
                    "action": action,
                    "required_approval": True,
                },
            )

    decision = check_tool_execution(
        skill_name=capability_name,
        args={"action": action, **body},
        config=config,
        approval_gates=approval_gates,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": "policy_blocked",
                "message": decision.reason,
                "capability": capability_name,
                "action": action,
                "risk_level": decision.risk_level,
                "required_approval": decision.required_approval,
            },
        )


@router.post("/cap/vault")
async def dispatch_vault(request: Request) -> JSONResponse:
    """Vault capability: retrieve paginated data from a previous large tool result.

    The agent calls this when it needs data beyond the 3-item preview in the
    compact envelope. Prefer workspace.* for display — vault is for iterating.
    """
    from app.ai.turn_vault import vault_get

    try:
        body: dict = await request.json()
    except Exception:
        body = {}
    vault_ref = body.get("vault_ref") or ""
    if not vault_ref:
        raise HTTPException(status_code=400, detail="'vault_ref' is required")
    offset = int(body.get("offset") or 0)
    limit = min(int(body.get("limit") or 20), 100)
    result = await vault_get(vault_ref, offset=offset, limit=limit)
    if result is None:
        raise HTTPException(status_code=404, detail="Vault ref expired or not found (TTL 15 min)")
    return JSONResponse(content=result)


@router.post("/cap/mcp")
async def dispatch_mcp(request: Request) -> JSONResponse:
    """Route a capability call to an MCP tool (built-in or external server).

    Same policy/approval/audit path as every other capability
    (_enforce_capability_policy, _audit_tool_call) — "mcp" is gate_actions:
    ["*"] in capabilities.yml (see Б17), so every call requires approval by
    default; there is no per-tool trust signal available at manifest-authoring
    time since the tool set depends on which external servers are connected.

    Dispatches in-process to the cached MCP handler (mcp_capability.py)
    instead of proxying to an internal REST path (unlike the generic
    _DISPATCH-based capabilities) — MCP tools are reached over a live
    protocol connection (stdio subprocess / external HTTP server), not a
    FastAPI route of this backend.
    """
    from app.ai.mcp_capability import get_mcp_tool_handler, list_mcp_tool_names

    try:
        body: dict = await request.json()
    except Exception:
        body = {}
    raw_body = dict(body)

    action = body.pop("action", None)
    if not action:
        raise HTTPException(
            status_code=400,
            detail={"error_code": "missing_action", "message": "'action' field is required"},
        )

    handler = await get_mcp_tool_handler(action)
    if handler is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "unknown_action",
                "message": f"Unknown MCP tool '{action}'.",
                "available": await list_mcp_tool_names(),
            },
        )

    reason = body.pop("reason", None)
    if reason is not None and not isinstance(reason, str):
        reason = str(reason)
    arguments = body.pop("arguments") if isinstance(body.get("arguments"), dict) else body

    _enforce_capability_policy("mcp", action, arguments, request, raw_body)
    await _audit_tool_call("mcp", action, reason, request)

    try:
        result = await handler(arguments)
    except Exception as exc:
        logger.warning("mcp_tool_call_failed", tool=action, error=str(exc))
        return JSONResponse(content={"error": str(exc)}, status_code=502)
    return JSONResponse(content=result if isinstance(result, dict) else {"result": result})


@router.get("/cap/mcp/tools")
async def list_mcp_tools() -> JSONResponse:
    """Tool names currently reachable through the mcp capability.

    capability_action_map() can't enumerate these statically like other
    capabilities' actions (the set depends on which MCP servers are
    connected) — the planner and operators read this endpoint instead.
    """
    from app.ai.mcp_capability import list_mcp_tool_names

    return JSONResponse(content={"tools": await list_mcp_tool_names()})


@router.post("/cap/{capability_name}")
async def dispatch_capability(capability_name: str, request: Request) -> JSONResponse:
    """Route a capability call to the appropriate backend endpoint."""
    actions = _DISPATCH.get(capability_name)
    if actions is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "unknown_capability",
                "message": f"Unknown capability: {capability_name}",
                "available": sorted(_DISPATCH.keys()),
            },
        )

    try:
        body: dict = await request.json()
    except Exception:
        body = {}

    # Снимок тела ДО того, как action/reason выдёргиваются, а filters/body
    # расплющиваются: одобрение привязано именно к тому, что прислал агент.
    raw_body = dict(body)

    action = body.pop("action", None)
    if not action:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "missing_action",
                "message": "'action' field is required",
                "available": sorted(actions.keys()),
            },
        )

    route = actions.get(action)
    if route is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "unknown_action",
                "message": f"Unknown action '{action}' for capability '{capability_name}'.",
                "available": sorted(actions.keys()),
            },
        )

    method, path_tpl, path_params = route
    _validate_capability_contract(capability_name, action, path_params, body)
    from app.ai.gateway_config import gateway_config

    base_url = gateway_config.backend_url

    # G3: the agent may attach a free-text `reason` ("зачем этот вызов") to
    # any capability call. It is audit metadata, never proxied downstream.
    reason = body.pop("reason", None)
    if reason is not None and not isinstance(reason, str):
        reason = str(reason)

    # Flatten nested 'filters' and 'body' into top-level args for proxying
    if "filters" in body and isinstance(body["filters"], dict):
        body.update(body.pop("filters"))
    if "body" in body and isinstance(body["body"], dict):
        body.update(body.pop("body"))

    # A grant never bypasses role, mode or tool policy. Validate these first,
    # then atomically reserve standing authority before any downstream effect.
    _enforce_capability_policy(
        capability_name, action, body, request, raw_body, approval_authorized=True
    )
    delegated = None
    if action in _capability_gate_actions(capability_name) and not _request_has_internal_approval(
        request, raw_body
    ):
        from app.domain.delegations import matching_delegation

        delegated = await matching_delegation(
            _acting_user(request),
            f"{capability_name}.{action}",
            body,
            consume=True,
        )
    _enforce_capability_policy(
        capability_name, action, body, request, raw_body, approval_authorized=delegated is not None
    )
    request.state.delegation_id = str(delegated) if delegated else None

    await _audit_tool_call(capability_name, action, reason, request)

    result = await _proxy(
        method,
        path_tpl,
        path_params,
        body,
        base_url,
        acting_user=_acting_user(request),
        idempotency_key=request.headers.get("X-Agent-Idempotency-Key"),
    )
    return JSONResponse(content=result)


async def _audit_tool_call(
    capability_name: str, action: str, reason: str | None, request: Request
) -> None:
    """G3: persist every capability tool call with its stated reason.

    Best-effort — an audit-write failure must not sink the tool call itself,
    but it is logged loudly so a silent audit gap can't go unnoticed."""
    logger.info(
        "capability_tool_call",
        capability=capability_name,
        action=action,
        reason=reason,
    )
    try:
        from app.audit.service import log_action
        from app.db.session import _get_session_factory

        actor = _acting_user(request) or "agent"
        async with _get_session_factory()() as db:
            await log_action(
                db,
                action="agent.tool_call",
                entity_type="capability",
                user_id=actor[:255],
                details={
                    "capability": capability_name,
                    "action": action,
                    "reason": (reason or "")[:1000] or None,
                    "delegation_id": getattr(request.state, "delegation_id", None),
                },
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("capability_audit_failed", capability=capability_name, error=str(exc))
        from app.ai.tool_catalog import get_tool

        definition = get_tool(capability_name, action)
        if definition is None or definition.effect != "read":
            raise HTTPException(503, "Durable audit unavailable; operation not executed") from exc
