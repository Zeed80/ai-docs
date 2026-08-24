"""Cooling API: proxy behaviour, admin gating and preset storage.

The sidecar is mocked — these tests cover what the backend itself is
responsible for: shape validation, error translation and preset bookkeeping.
The safety layers themselves live in the sidecar and are covered by
`test_cooling.py`.
"""

import pytest

from app.api import cooling_api


@pytest.fixture
def sidecar(monkeypatch):
    """Replace the gpu_manager sidecar client with a recording fake."""

    calls: list[tuple[str, object]] = []
    state = {
        "fans": {
            "ok": True,
            "control_enabled": True,
            "channels": [{"id": "gpu:0:fan0", "controllable": True}],
            "presets": {"silent": {"label": "Тихий", "curves": {"gpu": []}}},
        }
    }

    async def get_fans():
        calls.append(("get_fans", None))
        return dict(state["fans"])

    async def apply_fan_config(payload):
        calls.append(("apply_fan_config", payload))
        return {"ok": True, "config": payload}

    async def set_fan_manual(channel_id, pct):
        calls.append(("set_fan_manual", (channel_id, pct)))
        return {"ok": True, "applied_pct": pct}

    async def set_fan_auto(scope):
        calls.append(("set_fan_auto", scope))
        return {"ok": True, "reverted": [scope]}

    async def preview_fan_config(payload):
        calls.append(("preview_fan_config", payload))
        return {"ok": True, "preview": {}}

    async def get_fan_events():
        return [{"ts": 1.0, "level": "info", "channel": None, "code": "x", "message": "m"}]

    monkeypatch.setattr(cooling_api.gpu_manager, "get_fans", get_fans)
    monkeypatch.setattr(cooling_api.gpu_manager, "apply_fan_config", apply_fan_config)
    monkeypatch.setattr(cooling_api.gpu_manager, "set_fan_manual", set_fan_manual)
    monkeypatch.setattr(cooling_api.gpu_manager, "set_fan_auto", set_fan_auto)
    monkeypatch.setattr(cooling_api.gpu_manager, "preview_fan_config", preview_fan_config)
    monkeypatch.setattr(cooling_api.gpu_manager, "get_fan_events", get_fan_events)
    return calls


@pytest.fixture
def presets(monkeypatch):
    """In-memory stand-in for the Redis preset store."""
    store: dict = {}

    def _set(_key: str, value: dict) -> None:
        store.clear()
        store.update(value)

    monkeypatch.setattr(cooling_api, "_redis_get", lambda key: dict(store))
    monkeypatch.setattr(cooling_api, "_redis_set", _set)
    return store


def _sent(calls: list, name: str) -> dict:
    """First payload the fake sidecar received for `name`."""
    return next(payload for call, payload in calls if call == name)


# --- read paths -----------------------------------------------------------
@pytest.mark.asyncio
async def test_get_fans_merges_custom_presets(sidecar, presets):
    presets["night"] = {"label": "Ночь", "config": {}}
    data = await cooling_api.get_fans()
    assert data["custom_presets"]["night"]["label"] == "Ночь"
    assert data["channels"][0]["id"] == "gpu:0:fan0"


@pytest.mark.asyncio
async def test_sidecar_outage_becomes_503(monkeypatch, presets):
    async def boom():
        raise RuntimeError("gpu-temp-helper unreachable")

    monkeypatch.setattr(cooling_api.gpu_manager, "get_fans", boom)
    with pytest.raises(cooling_api.HTTPException) as exc:
        await cooling_api.get_fans()
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_presets_list_survives_a_dead_sidecar(monkeypatch, presets):
    async def boom():
        raise RuntimeError("down")

    monkeypatch.setattr(cooling_api.gpu_manager, "get_fans", boom)
    presets["night"] = {"label": "Ночь", "config": {}}
    out = await cooling_api.list_presets()
    assert out["builtin"] == {}
    assert "night" in out["custom"]


# --- schema validation ----------------------------------------------------
def test_curve_point_rejects_impossible_speed():
    with pytest.raises(ValueError):
        cooling_api.FanCurvePoint(t=50, pct=140)


def test_manual_update_rejects_out_of_range_percent():
    with pytest.raises(ValueError):
        cooling_api.FanManualUpdate(channel_id="gpu:0:fan0", pct=-5)


def test_channel_config_rejects_unknown_mode():
    with pytest.raises(ValueError):
        cooling_api.FanChannelConfig(mode="turbo")


def test_channel_config_rejects_unknown_sensor():
    with pytest.raises(ValueError):
        cooling_api.FanChannelConfig(mode="curve", sensor="psu")


# --- write paths ----------------------------------------------------------
@pytest.mark.asyncio
async def test_apply_config_forwards_only_provided_fields(sidecar, presets):
    payload = cooling_api.FanConfigUpdate(
        enabled=True,
        preset="silent",
        channels={"gpu:0:fan0": cooling_api.FanChannelConfig(
            mode="curve", curve=[cooling_api.FanCurvePoint(t=40, pct=30)]
        )},
    )
    await cooling_api.apply_fan_config(payload)
    sent = _sent(sidecar, "apply_fan_config")
    assert sent["preset"] == "silent"
    assert "emergency_hold_s" not in sent  # unset stays unset, no silent defaults


@pytest.mark.asyncio
async def test_manual_is_proxied_verbatim(sidecar, presets):
    await cooling_api.set_fan_manual(
        cooling_api.FanManualUpdate(channel_id="gpu:0:fan0", pct=55)
    )
    assert ("set_fan_manual", ("gpu:0:fan0", 55.0)) in sidecar


@pytest.mark.asyncio
async def test_revert_defaults_to_every_channel(sidecar, presets):
    await cooling_api.set_fan_auto(cooling_api.FanModeUpdate())
    assert ("set_fan_auto", "all") in sidecar


# --- presets --------------------------------------------------------------
@pytest.mark.asyncio
async def test_builtin_preset_name_cannot_be_overwritten(sidecar, presets):
    payload = cooling_api.FanPresetSave(
        name="silent", config=cooling_api.FanConfigUpdate()
    )
    with pytest.raises(cooling_api.HTTPException) as exc:
        await cooling_api.save_preset(payload)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_custom_preset_round_trip(sidecar, presets):
    await cooling_api.save_preset(
        cooling_api.FanPresetSave(
            name="night", label="Ночь", config=cooling_api.FanConfigUpdate(preset="night")
        )
    )
    assert presets["night"]["label"] == "Ночь"
    await cooling_api.apply_preset("night")
    sent = _sent(sidecar, "apply_fan_config")
    assert sent["preset"] == "night"


@pytest.mark.asyncio
async def test_deleting_a_missing_preset_is_404(sidecar, presets):
    with pytest.raises(cooling_api.HTTPException) as exc:
        await cooling_api.delete_preset("nope")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_unknown_preset_name_is_passed_to_the_sidecar_as_builtin(sidecar, presets):
    await cooling_api.apply_preset("balanced")
    sent = _sent(sidecar, "apply_fan_config")
    assert sent == {"preset": "balanced", "enabled": True}


# --- policy ---------------------------------------------------------------
def test_cooling_is_a_protected_setting():
    from app.ai.policy_engine import is_protected_setting

    assert is_protected_setting("cooling")
    assert is_protected_setting("cooling.preset")


def test_every_mutating_route_requires_admin():
    from app.api.cooling_api import router

    mutating = [
        r for r in router.routes
        if set(r.methods) & {"POST", "DELETE", "PATCH", "PUT"}
    ]
    assert mutating, "expected mutating routes"
    for route in mutating:
        rendered = [repr(d) for d in route.dependencies]
        assert any("require_role" in text for text in rendered), route.path


# --- runtime switch + setup guide -----------------------------------------
@pytest.mark.asyncio
async def test_control_requires_at_least_one_field(sidecar, presets):
    with pytest.raises(cooling_api.HTTPException) as exc:
        await cooling_api.set_fan_control(cooling_api.FanControlUpdate())
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_control_is_proxied(monkeypatch, presets):
    seen: list = []

    async def set_fan_control(enabled, allow_hwmon):
        seen.append((enabled, allow_hwmon))
        return {"ok": True, "control_enabled": enabled, "hwmon_allowed": allow_hwmon}

    monkeypatch.setattr(cooling_api.gpu_manager, "set_fan_control", set_fan_control)
    await cooling_api.set_fan_control(cooling_api.FanControlUpdate(enabled=True))
    assert seen == [(True, None)]   # an untouched switch stays untouched


@pytest.mark.asyncio
async def test_control_outage_becomes_503(monkeypatch, presets):
    async def boom(*_args):
        raise RuntimeError("gpu-temp-helper unreachable")

    monkeypatch.setattr(cooling_api.gpu_manager, "set_fan_control", boom)
    with pytest.raises(cooling_api.HTTPException) as exc:
        await cooling_api.set_fan_control(cooling_api.FanControlUpdate(enabled=False))
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_setup_guide_serves_the_repository_document():
    out = await cooling_api.get_setup_guide()
    assert out["available"] is True
    assert "NCT6687D" in out["markdown"]
    assert "FAN_CONTROL_ENABLED" in out["markdown"]


@pytest.mark.asyncio
async def test_setup_guide_reports_absence_instead_of_failing(monkeypatch):
    monkeypatch.setattr(cooling_api.Path, "is_file", lambda self: False)
    out = await cooling_api.get_setup_guide()
    assert out == {"available": False, "markdown": ""}
