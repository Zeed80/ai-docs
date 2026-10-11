"""E49: the isolated eval world — its guard and the model setup it runs with.

The live runner writes users, suppliers and invoices and lets a real model act
on them, so it must never run against the production database. The guard
checks the world the process is in, not a flag a caller can pass: the
compose project ``infra/eval-world`` sets ``EMPLOYEE_EVAL_WORLD=isolated`` and
points at its own throwaway Postgres.

The model setup (agent config and task routing) is exported from the stack
being measured and imported into the world, so a result says which model
answered and a run measures what production would do.

    docker exec infra-backend-1 python -m app.ai.evals.live_world export > setup.json
    docker compose -p aiw-eval ... exec -T eval-backend \
        python -m app.ai.evals.live_world import < setup.json
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Any

EVAL_DB_HOST = "eval-postgres"
EVAL_OLLAMA_CPU_URL = "http://eval-ollama-cpu:11434"


def _register_world_node(name: str, base_url: str) -> None:
    """The world's copy of a named Ollama node, so pinned routes resolve."""
    import asyncio

    from sqlalchemy import select

    from app.ai.provider_registry import refresh_cache_from_db
    from app.db.models import ProviderInstance
    from app.db.session import _get_session_factory

    async def register() -> None:
        async with _get_session_factory()() as db:
            node = await db.scalar(
                select(ProviderInstance).where(
                    ProviderInstance.kind == "ollama", ProviderInstance.name == name
                )
            )
            if node is None:
                node = ProviderInstance(kind="ollama", name=name, is_local=True, enabled=True)
                db.add(node)
            node.base_url = base_url
            await db.commit()
            await refresh_cache_from_db(db)

    asyncio.run(register())


class NotAnEvalWorld(RuntimeError):
    pass


def assert_eval_world() -> None:
    from app.config import settings

    problems = []
    if os.environ.get("EMPLOYEE_EVAL_WORLD") != "isolated":
        problems.append("EMPLOYEE_EVAL_WORLD is not 'isolated'")
    if settings.is_production:
        problems.append("APP_ENV is production")
    if settings.postgres_host != EVAL_DB_HOST:
        problems.append(f"database host is {settings.postgres_host!r}, not {EVAL_DB_HOST!r}")
    if "eval" not in settings.postgres_db.casefold():
        problems.append(f"database {settings.postgres_db!r} is not an eval database")
    if problems:
        raise NotAnEvalWorld("Refusing to run outside the eval world: " + "; ".join(problems))


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def export_model_setup() -> dict[str, Any]:
    """The agent config and routing overlay of the stack this runs in."""
    from app.ai.agent_config import get_builtin_agent_config
    from app.ai.gpu_runtime import current_owner
    from app.ai.provider_registry import list_instances
    from app.ai.schemas import ProviderKind
    from app.ai.task_routing import _redis_get

    setup = {
        "agent_config": get_builtin_agent_config().model_dump(mode="json"),
        "task_routing": _redis_get() or {},
        # Who holds the GPU decides whether Strata or GPU Ollama may answer.
        "gpu_owner": current_owner(),
        # Named local nodes a route may pin (the CPU node for embeddings);
        # the world runs its own copy of each under the same name.
        "local_nodes": sorted(
            {inst.name for inst in list_instances(ProviderKind.OLLAMA) if inst.name}
        ),
    }
    return {**setup, "digest": _digest(setup)}


def import_model_setup(setup: dict[str, Any]) -> str:
    """Apply an exported setup inside the eval world; returns its digest."""
    assert_eval_world()
    from app.ai.agent_config import BuiltinAgentConfig, save_builtin_agent_config
    from app.ai.schemas import AITask
    from app.ai.task_routing import TaskRouting, known_model_keys, save_task_routing

    body = {
        "agent_config": setup["agent_config"],
        "task_routing": setup["task_routing"],
        "gpu_owner": setup.get("gpu_owner"),
        "local_nodes": setup.get("local_nodes"),
    }
    digest = _digest(body)
    if setup.get("digest") not in (None, digest):
        raise ValueError("model setup digest does not match its content")
    config = BuiltinAgentConfig.model_validate(setup["agent_config"])
    # The world's own backend: tool calls must land here, never in production.
    config = config.model_copy(update={"backend_url": "http://eval-backend:8000"})
    save_builtin_agent_config(config)
    known = known_model_keys()
    skipped: dict[str, list[str]] = {}
    for task_name, routing in (setup.get("task_routing") or {}).items():
        try:
            task = AITask(task_name)
        except ValueError:
            continue
        models = list(routing.get("models") or [])
        # The tail is stored but never used as a fallback (the router takes
        # models[0]); models the world's catalog lacks are dropped from it.
        # A missing primary would change what answers: refuse instead.
        if models and models[0] not in known:
            raise ValueError(f"{task_name}: primary model {models[0]!r} unknown in the eval world")
        kept = [m for m in models if m in known]
        if len(kept) != len(models):
            skipped[task_name] = [m for m in models if m not in known]
        save_task_routing(task, TaskRouting(**{**routing, "models": kept, "task": task_name}))
    if body["gpu_owner"]:
        from app.ai.gpu_runtime import set_owner

        set_owner(body["gpu_owner"])
    if "ollama-cpu" in (body["local_nodes"] or []):
        _register_world_node("ollama-cpu", EVAL_OLLAMA_CPU_URL)
    if skipped:
        print(f"dropped unknown tail models: {json.dumps(skipped)}", file=sys.stderr)
    return digest


def current_model_identity(setup_digest: str) -> dict[str, Any]:
    from app.ai.agent_config import get_builtin_agent_config

    config = get_builtin_agent_config()
    return {
        "agent_provider": str(config.provider),
        "agent_model": str(config.model),
        "orchestrator_provider": config.orchestrator_provider,
        "orchestrator_model": config.orchestrator_model,
        "setup_digest": setup_digest,
    }


def main(argv: list[str]) -> int:
    if argv[:1] == ["export"]:
        json.dump(export_model_setup(), sys.stdout, ensure_ascii=False, indent=2)
        return 0
    if argv[:1] == ["import"]:
        print(import_model_setup(json.load(sys.stdin)))
        return 0
    print("usage: python -m app.ai.evals.live_world export|import", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
