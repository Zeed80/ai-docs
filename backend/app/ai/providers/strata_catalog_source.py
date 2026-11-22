"""Read the official model catalog from the installed Strata image."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from app.ai.providers import strata_lifecycle, strata_manager
from app.ai.providers.strata_catalog import StrataCatalogV1, StrataFamily, StrataVariant
from app.ai.providers.strata_catalog_store import current_catalog

logger = structlog.get_logger()

_PY = "/opt/strata/terminal/python/bin/python3"
_MCP_CATALOG = (
    "import json; from tools.strata_mcp import Strata, Tools; "
    "print(json.dumps(Tools(Strata()).strata_models(), ensure_ascii=False, default=str))"
)
_MAX_OUTPUT = 8 * 1024 * 1024


class StrataCatalogSourceError(RuntimeError):
    pass


@dataclass
class StrataSourceResult:
    catalog: StrataCatalogV1
    warnings: list[str]


def source_revision(server: dict[str, Any]) -> str:
    return json.dumps(
        {
            "version": server.get("version"),
            "commit": server.get("commit"),
            "image_id": server.get("image_id"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _read_only_host_config(info: dict, *, write_data: bool = False) -> dict:
    host = info.get("HostConfig") or {}
    mounts = []
    for original in host.get("Mounts") or []:
        mount = dict(original)
        if mount.get("Target") == "/data":
            mount["ReadOnly"] = not write_data
            mounts.append(mount)
    config: dict[str, Any] = {
        "ReadonlyRootfs": True,
        "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=67108864"},
        "Memory": 1024**3,
        "NetworkMode": "none",
    }
    if mounts:
        config["Mounts"] = mounts
    else:
        binds = []
        for binding in host.get("Binds") or []:
            parts = binding.split(":")
            if len(parts) >= 2 and parts[1] == "/data":
                binds.append(":".join([*parts[:2], "rw" if write_data else "ro"]))
        config["Binds"] = binds
    return config


async def _run_python_json(code: str, *, write_data: bool = False) -> tuple[dict, dict]:
    server = await strata_manager._find_container()
    if server is None:
        raise StrataCatalogSourceError(strata_manager.NOT_INSTALLED)
    info = await strata_lifecycle._inspect(server["Id"])
    image = (info.get("Config") or {}).get("Image")
    name = f"aiw-strata-catalog-{uuid.uuid4().hex[:12]}"
    spec = {
        "Image": image,
        "Entrypoint": [_PY],
        "Cmd": ["-c", code],
        "WorkingDir": "/opt/strata/terminal",
        "Env": [
            "PYTHONPATH=/opt/strata/terminal",
            "PYTHONDONTWRITEBYTECODE=1",
            "PYTHONNOUSERSITE=1",
            "STRATA_CONTROL_INSTALL=1",
            "STRATA_INSTALL_DIR=/data/install",
        ],
        "Labels": {"aiw.strata.role": "catalog"},
        "HostConfig": _read_only_host_config(info, write_data=write_data),
    }
    created = strata_lifecycle._ok(
        await strata_lifecycle._post("/containers/create", json_body=spec, name=name),
        "create catalog reader",
    ).json()
    container_id = created["Id"]
    try:
        strata_lifecycle._ok(
            await strata_lifecycle._post(f"/containers/{container_id}/start"),
            "start catalog reader",
            204,
            304,
        )
        async with strata_manager._docker() as client:
            response = await client.post(f"/containers/{container_id}/wait", timeout=120.0)
        if response.status_code != 200:
            raise StrataCatalogSourceError(f"Docker wait: HTTP {response.status_code}")
        state = response.json() or {}
        logs = await strata_manager.log_tail(30, container_id)
        if state.get("StatusCode") != 0:
            raise StrataCatalogSourceError(
                f"Официальный catalog reader завершился с кодом {state.get('StatusCode')}: "
                + " | ".join(logs[-5:])
            )
        payload = None
        for line in reversed(logs):
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                    break
                except ValueError:
                    continue
        if not isinstance(payload, dict):
            raise StrataCatalogSourceError("Официальный catalog reader не вернул JSON")
        if len(json.dumps(payload, ensure_ascii=False)) > _MAX_OUTPUT:
            raise StrataCatalogSourceError("Каталог Strata превышает допустимый размер")
        current = await strata_lifecycle.server_version(server)
        return payload, current or {}
    finally:
        await strata_lifecycle._remove(container_id)


def parse_model_table(
    payload: dict,
    *,
    previous: StrataCatalogV1,
    revision: str,
) -> StrataSourceResult:
    """Normalize MCP data, retaining only runtime profiles this backend can execute."""
    previous_families = {family.id: family for family in previous.families}
    previous_variants = {variant.id: variant for variant in previous.variants}
    families = []
    variants = []
    warnings = []
    for raw_family in payload.get("families") or []:
        family_id = raw_family.get("family")
        family = previous_families.get(family_id)
        if family is None:
            if not isinstance(family_id, str) or not family_id:
                warnings.append("Семейство без корректного id пропущено")
                continue
            family = StrataFamily(
                id=family_id,
                title=str(raw_family.get("title") or family_id),
                about="Обнаружено в Strata; runtime profile приложения ещё не подтверждён",
                tag=f"{family_id}-",
                kind="unknown",
                catalog_key=f"strata_discovered_{family_id}",
                served_name=f"unsupported-{family_id}",
                runtime_profile="unknown",
                setup_policy="unknown",
                disk_layout="unknown",
                min_version=None,
                upstream_present=True,
                origin="official",
                capability={},
            )
            warnings.append(
                f"Семейство {family_id} показано только для чтения: runtime profile не подтверждён"
            )
        families.append(family.model_copy(update={"upstream_present": True, "origin": "official"}))
        for size in raw_family.get("sizes") or []:
            model = size.get("model")
            if not isinstance(model, str):
                warnings.append(f"{family_id}: вариант без корректного model пропущен")
                continue
            setup_id = family.tag + model
            old = previous_variants.get(setup_id)
            about = size.get("about")
            label = old.label if old else f"{model} — {about or 'официальный вариант Strata'}"
            variants.append(
                StrataVariant(
                    id=old.id if old else setup_id,
                    setup_id=old.setup_id if old else setup_id,
                    family=family_id,
                    model=model,
                    label=label,
                    download_gb=size.get("download_gb"),
                    experts_gb=size.get("experts_gb"),
                    ram_gb=size.get("ram_needed_gb"),
                    default_context=old.default_context if old else 65536,
                    recommended_context=old.recommended_context if old else None,
                    min_version=size.get("engine_min") or family.min_version,
                    upstream_present=True,
                    origin="official",
                )
            )
    if not families or not variants:
        raise StrataCatalogSourceError("В ответе Strata нет поддерживаемых приложением моделей")
    catalog = StrataCatalogV1(
        schema_version=1,
        source="official_mcp",
        source_revision=revision,
        fetched_at=datetime.now(UTC).isoformat(),
        feed_revision=None,
        families=families,
        variants=variants,
    )
    return StrataSourceResult(catalog=catalog, warnings=warnings)


async def load_official_catalog() -> StrataSourceResult:
    payload, server = await _run_python_json(_MCP_CATALOG)
    return parse_model_table(
        payload,
        previous=current_catalog(),
        revision=source_revision(server),
    )


def _remove_code(setup_id: str, *, confirm: bool) -> str:
    # Values come only from the validated local catalog, never directly from HTTP/upstream.
    return (
        "import json; from tools.strata_mcp import Strata, Tools; "
        f"print(json.dumps(Tools(Strata()).strata_remove(model={setup_id!r}, "
        f"confirm={confirm!r}), ensure_ascii=False, default=str))"
    )


async def validate_install_plan(variant: dict) -> dict:
    """Ask the installed release to validate a newly discovered variant without starting it."""
    args = [
        "strata",
        "launch",
        "--plan",
        "--family",
        variant["family"],
        "--model",
        variant["model"],
        "--output",
        "json",
    ]
    code = (
        "import json, subprocess; "
        f"r=subprocess.run({args!r}, capture_output=True, text=True, timeout=110); "
        "print(json.dumps({'returncode': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr}))"
    )
    result, _server = await _run_python_json(code)
    if result.get("returncode") != 0:
        raise StrataCatalogSourceError(
            f"Strata не подтвердила установку {variant['id']}: {result.get('stderr') or result.get('stdout')}"
        )
    try:
        plan = json.loads(result.get("stdout") or "{}")
    except ValueError as exc:
        raise StrataCatalogSourceError("Strata launch --plan вернул невалидный JSON") from exc
    if not isinstance(plan, dict) or plan.get("error"):
        raise StrataCatalogSourceError(f"Strata отклонила установку {variant['id']}: {plan}")
    return plan


async def remove_official_variant(variant: dict) -> dict:
    """Run Strata's own plan/apply removal; never interpret or execute returned paths."""
    setup_id = variant.get("setup_id") or variant["id"]
    plan_result, _server = await _run_python_json(_remove_code(setup_id, confirm=False))
    plan = plan_result.get("plan") or {}
    if plan_result.get("error") or plan.get("model") != setup_id.lower():
        raise StrataCatalogSourceError(f"Strata запретила удаление {variant['id']}: {plan_result}")
    result, _server = await _run_python_json(_remove_code(setup_id, confirm=True), write_data=True)
    if result.get("error") or result.get("failed"):
        raise StrataCatalogSourceError(f"Strata не удалила {variant['id']} полностью: {result}")
    return result
