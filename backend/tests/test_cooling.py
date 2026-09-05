"""Safety-layer tests for the fan controller.

Everything here is a pure function from `infra/gpu-temp-helper/fans.py` — the
module is imported by path because the sidecar is not part of the backend
package.  No hardware is touched.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_FANS_PATH = Path(__file__).resolve().parents[2] / "infra" / "gpu-temp-helper" / "fans.py"


def _load_fans():
    spec = importlib.util.spec_from_file_location("sidecar_fans", _FANS_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["sidecar_fans"] = module
    spec.loader.exec_module(module)
    return module


fans = _load_fans()


# --- curve interpolation --------------------------------------------------
def test_curve_interpolates_between_points():
    curve = [{"t": 40, "pct": 30}, {"t": 80, "pct": 100}]
    assert fans.evaluate_curve(curve, 40) == 30
    assert fans.evaluate_curve(curve, 80) == 100
    assert fans.evaluate_curve(curve, 60) == pytest.approx(65)


def test_curve_holds_endpoints_outside_range():
    curve = [{"t": 40, "pct": 30}, {"t": 80, "pct": 100}]
    assert fans.evaluate_curve(curve, 5) == 30
    assert fans.evaluate_curve(curve, 200) == 100


def test_curve_is_order_independent():
    shuffled = [{"t": 80, "pct": 100}, {"t": 40, "pct": 30}, {"t": 60, "pct": 65}]
    assert fans.evaluate_curve(shuffled, 60) == pytest.approx(65)


def test_empty_curve_means_full_speed_not_stopped():
    # A broken configuration must fail loud and cold, never quiet and hot.
    assert fans.evaluate_curve([], 50) == 100.0


def test_duplicate_temperature_takes_the_higher_speed():
    curve = [{"t": 60, "pct": 40}, {"t": 60, "pct": 90}]
    assert fans.evaluate_curve(curve, 60) == 90


# --- S1: floor clamping ---------------------------------------------------
def test_clamp_enforces_hard_floor_over_configuration():
    assert fans.clamp_pct(0, ch_min=0, ch_max=100) == fans.HARD_FLOOR_PCT
    assert fans.clamp_pct(5, ch_min=1, ch_max=100) == fans.HARD_FLOOR_PCT


def test_clamp_respects_channel_floor_above_hard_floor():
    # An RTX 3090 reports a 30% minimum through NVML.
    assert fans.clamp_pct(10, ch_min=30, ch_max=100) == 30


def test_clamp_caps_at_channel_max():
    assert fans.clamp_pct(150, ch_min=30, ch_max=100) == 100


def test_stop_requires_explicit_opt_in_and_a_known_cold_temperature():
    assert fans.clamp_pct(0, 25, 100, allow_stop=True, temp=35, stop_below_c=40) == 0.0
    # too hot to stop
    assert fans.clamp_pct(0, 25, 100, allow_stop=True, temp=55, stop_below_c=40) == 25
    # S4: unknown temperature never stops a fan
    assert fans.clamp_pct(0, 25, 100, allow_stop=True, temp=None, stop_below_c=40) == 25
    # not opted in
    assert fans.clamp_pct(0, 25, 100, allow_stop=False, temp=10, stop_below_c=40) == 25


# --- S6: hysteresis and slew ---------------------------------------------
def test_hysteresis_answers_heating_immediately():
    assert fans.hysteresis_temp(60.0, 61.0, band=3.0) == 61.0


def test_hysteresis_ignores_cooling_inside_the_band():
    assert fans.hysteresis_temp(60.0, 58.0, band=3.0) == 60.0


def test_hysteresis_follows_cooling_past_the_band():
    assert fans.hysteresis_temp(60.0, 56.0, band=3.0) == 56.0


def test_first_reading_is_taken_as_is():
    assert fans.hysteresis_temp(None, 42.0, band=3.0) == 42.0


def test_slew_limits_only_the_way_down():
    assert fans.limit_slew(80.0, 30.0, max_step_down=8.0) == 72.0
    assert fans.limit_slew(30.0, 100.0, max_step_down=8.0) == 100.0
    assert fans.limit_slew(None, 45.0, max_step_down=8.0) == 45.0


def test_slew_does_not_overshoot_the_target_downwards():
    assert fans.limit_slew(40.0, 38.0, max_step_down=8.0) == 38.0


# --- presets --------------------------------------------------------------
def test_builtin_presets_never_go_below_the_hard_floor():
    for name, preset in fans.BUILTIN_PRESETS.items():
        for kind, curve in preset["curves"].items():
            for point in curve:
                assert point["pct"] >= fans.HARD_FLOOR_PCT, (name, kind, point)


def test_max_preset_is_full_speed_at_any_temperature():
    curve = fans.BUILTIN_PRESETS["max"]["curves"]["gpu"]
    assert fans.evaluate_curve(curve, 20) == 100
    assert fans.evaluate_curve(curve, 90) == 100


# --- controller-level safety (fake hardware) ------------------------------
class FakeBackend(fans.FanBackend):
    """In-memory fan: records every command and can pretend to be stuck."""

    name = "fake"

    def __init__(self, channels, temps=None, stuck=False):
        self._channels = channels
        self._temps = temps or {}
        self.stuck = stuck
        self.written: list[tuple[str, float]] = []
        self.auto_calls: list[str] = []
        self.speed: dict[str, float] = {}

    def enumerate(self):
        return self._channels

    def read(self, channel_id):
        pct = self.speed.get(channel_id)
        rpm = 0 if self.stuck else int((pct or 0) * 20)
        return rpm, pct

    def set_manual(self, channel_id, pct):
        self.written.append((channel_id, pct))
        self.speed[channel_id] = pct

    def set_auto(self, channel_id):
        self.auto_calls.append(channel_id)
        self.speed.pop(channel_id, None)

    def temperatures(self):
        return dict(self._temps)


def _controller(temps, stuck=False, has_tach=True, kind="mobo", role=None):
    channel = fans.FanChannel(
        id="fake:1",
        label="Fake",
        kind=kind,
        role=role or ("case" if kind == "mobo" else "gpu"),
        controllable=True,
        min_pct=25.0,
        max_pct=100.0,
        has_tach=has_tach,
        default_sensor="cpu" if kind == "mobo" else "gpu",
    )
    backend = FakeBackend([channel], temps, stuck=stuck)
    controller = fans.FanController()
    controller.control_enabled = True  # the switch is per-controller state now
    controller._backends = [backend]
    controller._channels = {channel.id: channel}
    controller._owner = {channel.id: backend}
    controller._rt = {channel.id: fans._Runtime()}
    controller._config = {
        "enabled": True,
        "preset": "test",
        "channels": {
            channel.id: {
                "mode": "curve",
                "sensor": channel.default_sensor,
                "curve": [{"t": 40, "pct": 30}, {"t": 80, "pct": 100}],
                "min_pct": 25.0,
                "allow_stop": False,
                "stop_below_c": None,
            }
        },
    }
    return controller, backend, channel


def test_curve_drives_the_fan_from_temperature():
    controller, backend, _ = _controller({"cpu": 60.0})
    controller.tick()
    assert backend.written == [("fake:1", 65.0)]


def test_emergency_overrides_the_curve_and_latches():
    controller, backend, _ = _controller({"cpu": 95.0})
    controller.tick()
    assert backend.written[-1] == ("fake:1", 100.0)
    # The latch keeps full speed even once the sensor reports a safe value.
    backend._temps = {"cpu": 45.0}
    controller.tick()
    assert backend.speed["fake:1"] == 100.0
    events = [e["code"] for e in controller.events()]
    assert "emergency" in events


def test_emergency_releases_after_the_hold_expires():
    controller, backend, _ = _controller({"cpu": 95.0})
    controller._config["emergency_hold_s"] = 0.0
    controller.tick()
    assert backend.speed["fake:1"] == 100.0
    backend._temps = {"cpu": 45.0}
    controller.tick()
    assert backend.speed["fake:1"] < 100.0


def test_header_without_a_fan_is_reported_once_not_as_a_fault():
    controller, backend, channel = _controller({"cpu": 60.0}, stuck=True)
    for _ in range(fans.STALL_TICKS + 1):
        controller.tick()
    assert channel.mode == "no_fan"
    assert "fake:1" in backend.auto_calls
    notices = [e for e in controller.events() if e["code"] == "no_fan"]
    assert len(notices) == 1
    assert notices[0]["level"] == "info"  # not an error
    assert not any(e["code"] == "channel_failed" for e in controller.events())


def test_empty_header_is_not_driven_again():
    controller, backend, _ = _controller({"cpu": 60.0}, stuck=True)
    for _ in range(fans.STALL_TICKS + 1):
        controller.tick()
    writes_after = len(backend.written)
    controller.tick()
    controller.tick()
    assert len(backend.written) == writes_after


def test_applying_a_preset_does_not_re_announce_an_empty_header(monkeypatch):
    controller, _, _ = _controller({"cpu": 60.0}, stuck=True)
    monkeypatch.setattr(fans, "_load_state", lambda: {})
    monkeypatch.setattr(fans, "_save_state", lambda s: None)
    for _ in range(fans.STALL_TICKS + 1):
        controller.tick()
    controller.apply_config({"preset": "silent", "enabled": True})
    for _ in range(fans.STALL_TICKS + 2):
        controller.tick()
    assert len([e for e in controller.events() if e["code"] == "no_fan"]) == 1


def test_an_explicit_command_re_tests_an_empty_header(monkeypatch):
    controller, backend, _ = _controller({"cpu": 60.0}, stuck=True)
    monkeypatch.setattr(fans, "_load_state", lambda: {})
    monkeypatch.setattr(fans, "_save_state", lambda s: None)
    for _ in range(fans.STALL_TICKS + 1):
        controller.tick()
    controller.set_manual("fake:1", 60)
    assert controller._rt["fake:1"].no_fan is False
    assert ("fake:1", 60.0) in backend.written


def test_sensor_loss_reverts_the_channel_to_automatic():
    controller, backend, channel = _controller({"cpu": 60.0})
    controller.tick()
    assert channel.mode == "manual"
    backend._temps = {}  # thermometer disappears
    for _ in range(fans.SENSOR_TIMEOUT_TICKS + 1):
        controller.tick()
    assert channel.mode == "auto"
    assert "fake:1" in backend.auto_calls


def test_identical_target_is_not_rewritten_every_tick():
    controller, backend, _ = _controller({"cpu": 60.0})
    controller.tick()
    controller.tick()
    controller.tick()
    assert len(backend.written) == 1


def test_disabled_configuration_hands_everything_back():
    controller, backend, channel = _controller({"cpu": 60.0})
    controller.tick()
    assert channel.mode == "manual"
    controller._config["enabled"] = False
    controller.tick()
    assert channel.mode == "auto"
    assert "fake:1" in backend.auto_calls


def test_shutdown_reverts_only_channels_this_process_commanded():
    controller, backend, _ = _controller({"cpu": 60.0})
    controller.restore_all_auto(reason="never commanded")
    assert backend.auto_calls == []
    controller.tick()
    controller.restore_all_auto(reason="after commanding")
    assert backend.auto_calls == ["fake:1"]


def test_sanitize_rejects_floors_below_the_hardware_minimum():
    controller, _, _ = _controller({"cpu": 60.0})
    cleaned = controller._sanitize_channels(
        {
            "fake:1": {
                "mode": "curve",
                "min_pct": 1,
                "curve": [{"t": 30, "pct": 0}, {"t": 90, "pct": 100}],
            },
        }
    )
    assert cleaned["fake:1"]["min_pct"] == 25.0
    assert cleaned["fake:1"]["curve"][0]["pct"] == 25.0


def test_sanitize_drops_channels_that_cannot_be_controlled():
    controller, _, channel = _controller({"cpu": 60.0})
    channel.controllable = False
    assert controller._sanitize_channels({"fake:1": {"mode": "manual", "pct": 50}}) == {}


def test_preview_writes_nothing():
    controller, backend, _ = _controller({"cpu": 60.0})
    result = controller.preview({"channels": controller._config["channels"], "temps": [40, 60, 80]})
    assert backend.written == []
    assert [p["pct"] for p in result["preview"]["fake:1"]] == [30.0, 65.0, 100.0]


# --- runtime on/off switch (replaces editing .env) ------------------------
def test_control_starts_from_the_environment_default(monkeypatch):
    monkeypatch.setattr(fans, "DEFAULT_CONTROL_ENABLED", True)
    monkeypatch.setattr(fans, "DEFAULT_ALLOW_HWMON", False)
    controller = fans.FanController()
    assert controller.control_enabled is True
    assert controller.allow_hwmon is False


def test_disabling_control_hands_the_hardware_back(monkeypatch):
    controller, backend, channel = _controller({"cpu": 60.0})
    controller.control_enabled = True
    state: dict = {}
    monkeypatch.setattr(fans, "_load_state", lambda: state)
    monkeypatch.setattr(fans, "_save_state", lambda s: state.update(s))

    controller.tick()
    assert channel.mode == "manual"

    controller.set_control(enabled=False)
    assert channel.mode == "auto"
    assert "fake:1" in backend.auto_calls
    assert controller.control_enabled is False
    assert state["fan_control"] == {"enabled": False, "allow_hwmon": False}


def test_disabling_board_fans_reverts_only_board_channels(monkeypatch):
    controller, backend, channel = _controller({"cpu": 60.0}, kind="mobo")
    controller.control_enabled = True
    controller.allow_hwmon = True
    state: dict = {}
    monkeypatch.setattr(fans, "_load_state", lambda: state)
    monkeypatch.setattr(fans, "_save_state", lambda s: state.update(s))

    controller.tick()
    assert channel.mode == "manual"

    controller.set_control(allow_hwmon=False)
    assert channel.mode == "auto"
    assert controller.control_enabled is True  # the other switch is untouched


def test_writes_are_refused_while_control_is_off():
    controller, _, _ = _controller({"cpu": 60.0})
    controller.control_enabled = False
    with pytest.raises(RuntimeError, match="выключено"):
        controller.set_manual("fake:1", 50)
    with pytest.raises(RuntimeError, match="выключено"):
        controller.apply_config({"preset": "silent"})


def test_saved_switch_beats_the_environment_default(monkeypatch):
    monkeypatch.setattr(fans, "DEFAULT_CONTROL_ENABLED", False)
    monkeypatch.setattr(fans, "DEFAULT_ALLOW_HWMON", False)
    state = {"fan_control": {"enabled": True, "allow_hwmon": True}}
    monkeypatch.setattr(fans, "_load_state", lambda: state)
    monkeypatch.setattr(fans, "_save_state", lambda s: None)

    controller = fans.FanController()
    controller.load_config()
    assert controller.control_enabled is True
    assert controller.allow_hwmon is True


def test_absent_switch_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setattr(fans, "DEFAULT_CONTROL_ENABLED", True)
    monkeypatch.setattr(fans, "DEFAULT_ALLOW_HWMON", True)
    monkeypatch.setattr(fans, "_load_state", lambda: {})
    monkeypatch.setattr(fans, "_save_state", lambda s: None)

    controller = fans.FanController()
    controller.load_config()
    assert controller.control_enabled is True
    assert controller.allow_hwmon is True


def test_board_channel_is_read_only_without_the_permission():
    controllable, reason = fans.HwmonFanBackend._writability(
        "nct6687", has_pwm=True, has_enable=True, pwm_path="/dev/null", allow=False
    )
    assert controllable is False
    assert "настройках охлаждения" in (reason or "")


def test_driver_limitation_outranks_the_permission():
    # /proc/version is readable-but-not-writable for everyone, including root.
    controllable, reason = fans.HwmonFanBackend._writability(
        "nct6687", has_pwm=True, has_enable=True, pwm_path="/proc/version", allow=True
    )
    assert controllable is False
    assert "nct6687d" in (reason or "")


# --- transient write failures (the NCT6687 EC refuses a write now and then) ---
class FlakyBackend(FakeBackend):
    """Fails `fail_times` consecutive writes, then behaves."""

    def __init__(self, channels, temps=None, fail_times=1):
        super().__init__(channels, temps)
        self.remaining_failures = fail_times

    def set_manual(self, channel_id, pct):
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise OSError(5, "Input/output error")
        super().set_manual(channel_id, pct)


def _flaky(temps, fail_times):
    channel = fans.FanChannel(
        id="fake:1",
        label="Fake",
        kind="mobo",
        role="case",
        controllable=True,
        min_pct=25.0,
        max_pct=100.0,
        has_tach=True,
        default_sensor="cpu",
    )
    backend = FlakyBackend([channel], temps, fail_times=fail_times)
    controller = fans.FanController()
    controller.control_enabled = True
    controller._backends = [backend]
    controller._channels = {channel.id: channel}
    controller._owner = {channel.id: backend}
    controller._rt = {channel.id: fans._Runtime()}
    controller._config = {
        "enabled": True,
        "channels": {
            channel.id: {
                "mode": "manual",
                "pct": 60.0,
                "sensor": "cpu",
                "min_pct": 25.0,
                "allow_stop": False,
                "stop_below_c": None,
            }
        },
    }
    return controller, backend, channel


def test_a_single_write_failure_is_retried_not_fatal():
    controller, backend, channel = _flaky({"cpu": 60.0}, fail_times=1)
    controller.tick()
    assert channel.mode != "failed"
    assert backend.written == []  # first attempt was refused
    controller.tick()
    assert ("fake:1", 60.0) in backend.written  # second attempt got through
    assert controller._rt["fake:1"].write_failures == 0


def test_persistent_write_failures_do_fail_the_channel():
    controller, _, channel = _flaky({"cpu": 60.0}, fail_times=99)
    for _ in range(fans.WRITE_FAILURES_BEFORE_GIVING_UP):
        controller.tick()
    assert channel.mode == "failed"
    assert "не удалась" in (controller._rt["fake:1"].failed_reason or "")


def test_a_refused_write_does_not_count_as_applied():
    # Otherwise the dedup would skip the retry and the fan would silently keep
    # the old speed while the UI showed the new one.
    controller, backend, _ = _flaky({"cpu": 60.0}, fail_times=1)
    controller.tick()
    assert controller._rt["fake:1"].last_written is None


# --- role-based curves ----------------------------------------------------
def test_presets_cover_every_role():
    for name, preset in fans.BUILTIN_PRESETS.items():
        assert set(preset["curves"]) >= {"gpu", "cpu", "pump", "case"}, name


def test_a_pump_keeps_a_higher_floor_than_a_case_fan():
    curves = fans.BUILTIN_PRESETS["silent"]["curves"]
    assert fans.evaluate_curve(curves["pump"], 40) > fans.evaluate_curve(curves["case"], 40)


def test_preset_picks_the_curve_by_role_not_by_kind():
    cpu_ch = fans.FanChannel(
        id="c",
        label="CPU",
        kind="mobo",
        role="cpu",
        controllable=True,
        min_pct=25,
        max_pct=100,
        has_tach=True,
        default_sensor="cpu",
    )
    case_ch = fans.FanChannel(
        id="s",
        label="SYS",
        kind="mobo",
        role="case",
        controllable=True,
        min_pct=25,
        max_pct=100,
        has_tach=True,
        default_sensor="cpu",
    )
    cfg = fans.preset_config("balanced", [cpu_ch, case_ch])
    assert cfg["c"]["curve"] != cfg["s"]["curve"]


def test_unknown_role_falls_back_to_the_gentle_case_curve():
    odd = fans.FanChannel(
        id="x",
        label="X",
        kind="mobo",
        role="mystery",
        controllable=True,
        min_pct=25,
        max_pct=100,
        has_tach=True,
        default_sensor="cpu",
    )
    cfg = fans.preset_config("balanced", [odd])
    assert cfg["x"]["curve"] == fans.BUILTIN_PRESETS["balanced"]["curves"]["case"]


# --- header naming --------------------------------------------------------
def test_nct6687_headers_are_named_after_the_board_layout():
    assert fans._header_of("nct6687", 1) == ("NCT6687 · CPU_FAN", "cpu")
    assert fans._header_of("nct6687", 2) == ("NCT6687 · PUMP_FAN", "pump")
    assert fans._header_of("nct6687", 5)[1] == "case"


def test_an_unknown_chip_gets_a_neutral_name_and_the_safe_role():
    label, role = fans._header_of("it8686", 3)
    assert "канал 3" in label
    assert role == "case"  # never guess that something is the CPU cooler


# --- presets are chosen per hardware domain -------------------------------
def _two_domain_controller(monkeypatch):
    """A GPU fan and a board fan on one controller."""
    gpu = fans.FanChannel(
        id="gpu:0:fan0",
        label="GPU",
        kind="gpu",
        role="gpu",
        controllable=True,
        min_pct=30.0,
        max_pct=100.0,
        has_tach=False,
        default_sensor="gpu",
    )
    mobo = fans.FanChannel(
        id="hwmon:x:pwm1",
        label="SYS",
        kind="mobo",
        role="case",
        controllable=True,
        min_pct=25.0,
        max_pct=100.0,
        has_tach=True,
        default_sensor="cpu",
    )
    backend = FakeBackend([gpu, mobo], {"gpu": 50.0, "cpu": 50.0})
    controller = fans.FanController()
    controller.control_enabled = True
    controller._backends = [backend]
    controller._channels = {gpu.id: gpu, mobo.id: mobo}
    controller._owner = {gpu.id: backend, mobo.id: backend}
    controller._rt = {gpu.id: fans._Runtime(), mobo.id: fans._Runtime()}
    controller._config = {"enabled": True, "presets": {}, "channels": {}}
    monkeypatch.setattr(fans, "_load_state", lambda: {})
    monkeypatch.setattr(fans, "_save_state", lambda s: None)
    return controller, backend


def test_preset_config_can_target_one_domain():
    gpu = fans.FanChannel(
        id="g",
        label="G",
        kind="gpu",
        role="gpu",
        controllable=True,
        min_pct=30,
        max_pct=100,
        has_tach=False,
        default_sensor="gpu",
    )
    mobo = fans.FanChannel(
        id="m",
        label="M",
        kind="mobo",
        role="case",
        controllable=True,
        min_pct=25,
        max_pct=100,
        has_tach=True,
        default_sensor="cpu",
    )
    assert set(fans.preset_config("silent", [gpu, mobo], "gpu")) == {"g"}
    assert set(fans.preset_config("silent", [gpu, mobo], "mobo")) == {"m"}
    assert set(fans.preset_config("silent", [gpu, mobo], "all")) == {"g", "m"}


def test_applying_to_one_domain_keeps_the_other_curve(monkeypatch):
    controller, _ = _two_domain_controller(monkeypatch)
    controller.apply_config({"preset": "silent", "scope": "all"})
    board_before = controller._config["channels"]["hwmon:x:pwm1"]["curve"]

    controller.apply_config({"preset": "max", "scope": "gpu"})
    cfg = controller._config["channels"]
    assert cfg["gpu:0:fan0"]["curve"] == [{"t": 0.0, "pct": 100.0}]
    assert cfg["hwmon:x:pwm1"]["curve"] == board_before  # untouched


def test_each_domain_remembers_its_own_preset(monkeypatch):
    controller, _ = _two_domain_controller(monkeypatch)
    controller.apply_config({"preset": "silent", "scope": "mobo"})
    controller.apply_config({"preset": "max", "scope": "gpu"})
    assert controller._config["presets"] == {"mobo": "silent", "gpu": "max"}


def test_applying_to_all_sets_both_domains(monkeypatch):
    controller, _ = _two_domain_controller(monkeypatch)
    controller.apply_config({"preset": "balanced", "scope": "all"})
    assert controller._config["presets"] == {"gpu": "balanced", "mobo": "balanced"}


def test_scoped_apply_does_not_drop_channels_from_the_other_domain(monkeypatch):
    controller, _ = _two_domain_controller(monkeypatch)
    controller.apply_config({"preset": "balanced", "scope": "all"})
    controller.apply_config({"preset": "silent", "scope": "gpu"})
    assert set(controller._config["channels"]) == {"gpu:0:fan0", "hwmon:x:pwm1"}


def test_an_unknown_scope_is_refused(monkeypatch):
    controller, _ = _two_domain_controller(monkeypatch)
    with pytest.raises(ValueError, match="область"):
        controller.apply_config({"preset": "silent", "scope": "psu"})


def test_preview_honours_the_scope(monkeypatch):
    controller, backend = _two_domain_controller(monkeypatch)
    out = controller.preview({"preset": "max", "scope": "gpu"})
    assert set(out["preview"]) == {"gpu:0:fan0"}
    assert backend.written == []


def test_state_written_before_scoping_is_read_as_both_domains(monkeypatch):
    state = {"fans": {"enabled": True, "preset": "silent", "channels": {}}}
    monkeypatch.setattr(fans, "_load_state", lambda: state)
    monkeypatch.setattr(fans, "_save_state", lambda s: None)
    controller = fans.FanController()
    controller.load_config()
    assert controller._config["presets"] == {"gpu": "silent", "mobo": "silent"}
    assert "preset" not in controller._config
