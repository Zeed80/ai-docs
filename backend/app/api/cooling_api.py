"""Fan control API.

A typed, admin-gated proxy over the `gpu-temp-helper` sidecar plus a
server-side preset store.

The backend deliberately holds no control logic: the sidecar owns the loop and
every safety layer (floor clamping, emergency override, stall detection,
dead-man revert) because it must keep working when the backend, Celery or
Redis are down.  Bounds submitted here are re-validated there — the values in
this module are for early, readable rejection, not for safety.

Presets live in Redis (key ``cooling_presets``) rather than in browser storage
like the older GPU power presets: a cooling profile describes the machine, not
one person's browser.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.ai import gpu_manager
from app.auth.jwt import require_role
from app.auth.models import UserRole

logger = structlog.get_logger()
router = APIRouter()

_PRESETS_KEY = "cooling_presets"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class FanCurvePoint(BaseModel):
    t: float = Field(..., ge=-20, le=150, description="Температура, °C")
    pct: float = Field(..., ge=0, le=100, description="Обороты, % от максимума")


class FanChannelConfig(BaseModel):
    mode: str = Field("curve", pattern="^(auto|manual|curve)$")
    sensor: str | None = Field(None, pattern="^(gpu|gpu_mem|cpu)$")
    curve: list[FanCurvePoint] | None = None
    pct: float | None = Field(None, ge=0, le=100)
    min_pct: float | None = Field(None, ge=0, le=100)
    allow_stop: bool = False
    stop_below_c: float | None = Field(None, ge=0, le=100)


class FanConfigUpdate(BaseModel):
    enabled: bool = True
    preset: str | None = None
    channels: dict[str, FanChannelConfig] | None = None
    emergency_c: dict[str, float] | None = None
    emergency_hold_s: float | None = Field(None, ge=0, le=600)
    temp_hysteresis_c: float | None = Field(None, ge=0, le=20)
    max_step_down_pct: float | None = Field(None, ge=1, le=100)


class FanManualUpdate(BaseModel):
    channel_id: str = Field(..., min_length=1, max_length=128)
    pct: float = Field(..., ge=0, le=100)


class FanModeUpdate(BaseModel):
    scope: str = Field("all", min_length=1, max_length=128)


class FanPreviewRequest(BaseModel):
    preset: str | None = None
    channels: dict[str, FanChannelConfig] | None = None
    temps: list[float] | None = None


class FanControlUpdate(BaseModel):
    """At least one field must be set; omitted fields are left alone."""

    enabled: bool | None = None
    allow_hwmon: bool | None = None


class FanPresetSave(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    label: str | None = Field(None, max_length=128)
    config: FanConfigUpdate


# ---------------------------------------------------------------------------
# Preset store (Redis, mirroring app/ai/parameter_profiles.py)
# ---------------------------------------------------------------------------
def _redis_get(key: str) -> dict | None:
    try:
        from app.utils.redis_client import get_sync_redis

        raw = get_sync_redis().get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _redis_set(key: str, value: dict) -> None:
    try:
        from app.utils.redis_client import get_sync_redis

        get_sync_redis().set(key, json.dumps(value, ensure_ascii=False))
    except Exception as exc:
        logger.warning("cooling_presets_redis_write_failed", key=key, error=str(exc))
        raise HTTPException(503, detail="не удалось сохранить пресет: Redis недоступен")


def _custom_presets() -> dict[str, Any]:
    return _redis_get(_PRESETS_KEY) or {}


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
@router.get("/fans")
async def get_fans() -> dict:
    """Channels, capabilities, active config, loop health and safety limits."""
    try:
        data = await gpu_manager.get_fans()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    data["custom_presets"] = _custom_presets()
    return data


@router.get("/fans/events")
async def get_fan_events() -> dict:
    try:
        return {"events": await gpu_manager.get_fan_events()}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/presets")
async def list_presets() -> dict:
    """Builtin presets come from the sidecar so both sides agree on the curves."""
    builtin: dict[str, Any] = {}
    try:
        builtin = (await gpu_manager.get_fans()).get("presets") or {}
    except RuntimeError:
        pass  # the sidecar may be down; custom presets are still listable
    return {"builtin": builtin, "custom": _custom_presets()}


@router.get("/setup-guide")
async def get_setup_guide() -> dict:
    """The motherboard-fan instruction, served so the GUI can show it in place.

    Single source of truth: the same file the repository ships. The docs
    directory is mounted read-only into the container; when it is missing the
    UI falls back to a short summary rather than showing a broken link.
    """
    for candidate in (Path("/app/docs"), Path(__file__).resolve().parents[3] / "docs"):
        path = candidate / "cooling-motherboard-fans.md"
        if path.is_file():
            try:
                return {"available": True, "markdown": path.read_text(encoding="utf-8")}
            except OSError as exc:
                logger.warning("cooling_guide_unreadable", path=str(path), error=str(exc))
                break
    return {"available": False, "markdown": ""}


# ---------------------------------------------------------------------------
# Write (admin only — fan control is not delegated to the agent)
# ---------------------------------------------------------------------------
_admin = [Depends(require_role(UserRole.admin))]


@router.post("/control", dependencies=_admin)
async def set_fan_control(payload: FanControlUpdate) -> dict:
    """Turn fan control (and board fans) on or off. Replaces editing .env.

    Switching control off is a safety operation, not just a flag: the sidecar
    hands every channel it was driving back to firmware before the flag flips.
    """
    if payload.enabled is None and payload.allow_hwmon is None:
        raise HTTPException(status_code=422, detail="нужно указать enabled или allow_hwmon")
    try:
        return await gpu_manager.set_fan_control(payload.enabled, payload.allow_hwmon)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/fans/mode", dependencies=_admin)
async def set_fan_auto(payload: FanModeUpdate) -> dict:
    """Hand a channel — or everything — back to firmware/NVML control."""
    try:
        return await gpu_manager.set_fan_auto(payload.scope)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/fans/manual", dependencies=_admin)
async def set_fan_manual(payload: FanManualUpdate) -> dict:
    try:
        return await gpu_manager.set_fan_manual(payload.channel_id, payload.pct)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/fans/config", dependencies=_admin)
async def apply_fan_config(payload: FanConfigUpdate) -> dict:
    try:
        return await gpu_manager.apply_fan_config(
            payload.model_dump(exclude_none=True)
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/fans/preview", dependencies=_admin)
async def preview_fan_config(payload: FanPreviewRequest) -> dict:
    """Dry run: what the curve would command. Touches no hardware."""
    try:
        return await gpu_manager.preview_fan_config(
            payload.model_dump(exclude_none=True)
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/presets", dependencies=_admin)
async def save_preset(payload: FanPresetSave) -> dict:
    """Store a named configuration. Builtin names stay reserved."""
    builtin: dict[str, Any] = {}
    try:
        builtin = (await gpu_manager.get_fans()).get("presets") or {}
    except RuntimeError:
        pass
    if payload.name in builtin:
        raise HTTPException(
            status_code=409,
            detail=f"«{payload.name}» — встроенный пресет, его нельзя перезаписать",
        )
    presets = _custom_presets()
    presets[payload.name] = {
        "label": payload.label or payload.name,
        "config": payload.config.model_dump(exclude_none=True),
    }
    _redis_set(_PRESETS_KEY, presets)
    logger.info("cooling_preset_saved", name=payload.name)
    return {"ok": True, "presets": presets}


@router.delete("/presets/{name}", dependencies=_admin)
async def delete_preset(name: str) -> dict:
    presets = _custom_presets()
    if name not in presets:
        raise HTTPException(status_code=404, detail=f"пресет «{name}» не найден")
    presets.pop(name)
    _redis_set(_PRESETS_KEY, presets)
    logger.info("cooling_preset_deleted", name=name)
    return {"ok": True, "presets": presets}


@router.post("/presets/{name}/apply", dependencies=_admin)
async def apply_preset(name: str) -> dict:
    """Apply a builtin preset by name, or a stored custom one."""
    custom = _custom_presets().get(name)
    payload = dict(custom["config"]) if custom else {"preset": name}
    payload.setdefault("enabled", True)
    try:
        return await gpu_manager.apply_fan_config(payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
