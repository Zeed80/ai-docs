"""Explicit, owner-scoped standing authority; never inferred from conversation."""

import json
from datetime import UTC, datetime

from sqlalchemy import select

from app.db.agent_runtime_models import DelegationGrant


def normalized_arguments(arguments: dict) -> dict:
    body = {k: v for k, v in arguments.items() if k not in {"action", "reason"}}
    for key in ("filters", "body"):
        nested = body.get(key)
        if isinstance(nested, dict):
            body.pop(key)
            body.update(nested)
    return body


def arguments_match(constraints: dict, arguments: dict) -> bool:
    """Exact typed values only. No patterns, expressions or executable predicates."""
    body = normalized_arguments(arguments)
    return bool(constraints) and all(
        key in body and json.dumps(body[key], sort_keys=True) == json.dumps(value, sort_keys=True)
        for key, value in constraints.items()
    )


async def matching_delegation(
    owner: str,
    action: str,
    arguments: dict,
    *,
    consume: bool = False,
):
    from app.audit.service import log_action
    from app.db.session import _get_session_factory

    if not owner or owner in {"agent-service", "anonymous"}:
        return None
    now = datetime.now(UTC)
    async with _get_session_factory()() as db:
        query = (
            select(DelegationGrant)
            .where(
                DelegationGrant.owner_key == owner,
                DelegationGrant.revoked_at.is_(None),
                DelegationGrant.expires_at > now,
                DelegationGrant.used_actions < DelegationGrant.max_actions,
            )
            .order_by(DelegationGrant.expires_at, DelegationGrant.id)
        )
        if consume:
            query = query.with_for_update()
        for grant in (await db.scalars(query)).all():
            if action not in grant.actions or not arguments_match(grant.constraints, arguments):
                continue
            if consume:
                grant.used_actions += 1
                await log_action(
                    db,
                    action="agent.delegation.used",
                    entity_type="delegation",
                    entity_id=grant.id,
                    user_id=owner,
                    details={"tool": action, "used_actions": grant.used_actions},
                )
                await db.commit()
            return grant.id
    return None


# ── E46: typed constraint fields, from the action's own endpoint ────────────

_SCALAR_TYPES = {"string", "integer", "number", "boolean"}
_WILDCARDS = {"*", "%", "all", "any", "всё", "все", "любой"}


def _resolve(schema: dict, components: dict) -> dict:
    while isinstance(schema, dict) and "$ref" in schema:
        schema = components.get(schema["$ref"].rsplit("/", 1)[-1], {})
    if isinstance(schema, dict) and "anyOf" in schema:
        options = [s for s in schema["anyOf"] if s.get("type") != "null"]
        if len(options) == 1:
            return _resolve(options[0], components)
    return schema if isinstance(schema, dict) else {}


def _openapi() -> dict:
    from app.main import app

    return app.openapi()


def delegation_fields(action: str, spec: dict | None = None) -> list[dict]:
    """The scalar arguments of an action a delegation may pin, with types.

    Taken from the action's endpoint (path, query, top-level body fields), so
    the form offers exactly what the call carries and the server can check a
    constraint's name and type instead of trusting free JSON.
    """
    from app.ai.tool_catalog import TOOLS

    tool = TOOLS.get(action)
    if tool is None:
        return []
    spec = spec or _openapi()
    components = spec.get("components", {}).get("schemas", {})
    operation = spec.get("paths", {}).get(tool.path, {}).get(tool.method.lower(), {})
    fields: dict[str, dict] = {}
    for param in operation.get("parameters", []):
        if param.get("in") not in {"path", "query"}:
            continue
        schema = _resolve(param.get("schema", {}), components)
        if schema.get("type") in _SCALAR_TYPES:
            fields[param["name"]] = {
                "name": param["name"],
                "type": schema["type"],
                "format": schema.get("format"),
                "required": bool(param.get("required")),
                "where": param["in"],
            }
    body = operation.get("requestBody", {}).get("content", {}).get("application/json", {})
    body_schema = _resolve(body.get("schema", {}), components)
    required = set(body_schema.get("required", []))
    for name, prop in (body_schema.get("properties") or {}).items():
        prop = _resolve(prop, components)
        if prop.get("type") in _SCALAR_TYPES and name not in {"reason", "action"}:
            fields.setdefault(
                name,
                {
                    "name": name,
                    "type": prop["type"],
                    "format": prop.get("format"),
                    "required": name in required,
                    "where": "body",
                },
            )
    return sorted(fields.values(), key=lambda f: (not f["required"], f["name"]))


def check_constraints(actions: list[str], constraints: dict, spec: dict | None = None) -> None:
    """Raise ValueError unless every constraint is a known, typed, exact value.

    A field must exist on every delegated action (a scope that one action
    cannot carry would never match it — or worse, match nothing it says).
    Empty values, wildcard words and collections are refused: an exact
    scope only, never "all".
    """
    import uuid as _uuid

    if not constraints:
        raise ValueError("Укажите хотя бы одно точное ограничение")
    per_action = {a: {f["name"]: f for f in delegation_fields(a, spec)} for a in actions}
    for name, value in constraints.items():
        for action, fields in per_action.items():
            field = fields.get(name)
            if field is None:
                raise ValueError(f"Поле «{name}» не передаётся действием {action}")
            kind = field["type"]
            if kind == "boolean":
                ok = isinstance(value, bool)
            elif kind == "integer":
                ok = isinstance(value, int) and not isinstance(value, bool)
            elif kind == "number":
                ok = isinstance(value, int | float) and not isinstance(value, bool)
            else:
                ok = isinstance(value, str) and bool(value.strip())
                if ok and value.strip().lower() in _WILDCARDS:
                    raise ValueError(f"«{name}»: подстановочное значение не допускается")
                if ok and field.get("format") == "uuid":
                    try:
                        _uuid.UUID(value)
                    except ValueError:
                        ok = False
            if not ok:
                raise ValueError(
                    f"«{name}»: ожидается {kind}"
                    + (f"/{field['format']}" if field.get("format") else "")
                )
