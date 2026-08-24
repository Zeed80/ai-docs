"""Fan control for the gpu-temp-helper sidecar.

Enumerates every controllable fan on the host, runs the temperature->speed
control loop and enforces the safety layers.  Lives next to `server.py`, which
owns the HTTP surface and injects host services through `bind_host()`.

Two device backends, both optional:

  * `NvmlFanBackend`  — GPU fans.  NVML exposes per-fan manual control
    (`nvmlDeviceSetFanSpeed_v2`) and, crucially, a documented way back to the
    firmware curve (`nvmlDeviceSetDefaultFanSpeed_v2`).
  * `HwmonFanBackend` — motherboard fans through `/sys/class/hwmon`.  Only
    usable when the loaded driver exposes writable `pwmN` *and* `pwmN_enable`.
    The mainline `nct6683` driver does not (it publishes pwm read-only on
    non-Mitac boards); the out-of-tree `nct6687d` DKMS module does.  Channels
    are still enumerated when they are read-only so the UI can explain why.

Safety is the point of this module, not a side concern.  A fan left slow under
load destroys hardware, and a control process that dies must not leave the
hardware in manual mode -- NVML settings outlive the process that made them.
See SAFETY.md notes inline: S1 floor, S2 emergency, S3 stall, S4 sensor loss,
S5 dead-man, S6 hysteresis/slew, S7 write dedup, S8 kill switch, S10 audit.
"""

from __future__ import annotations

import os
import stat
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

try:
    import pynvml
except Exception:  # pragma: no cover - library missing or broken
    pynvml = None


# --- Kill switches (S8): the feature ships disabled ------------------------
def _env_flag(name: str, default: str = "0") -> bool:
    return (os.environ.get(name, default) or "").strip().lower() in {"1", "true", "yes", "on"}


CONTROL_ENABLED = _env_flag("FAN_CONTROL_ENABLED")
ALLOW_HWMON = _env_flag("FAN_CONTROL_ALLOW_HWMON")

TICK_S = float(os.environ.get("FAN_TICK_S", "2.0"))

# --- Safety constants -----------------------------------------------------
# S1: no configuration may drive any fan below this, ever.
HARD_FLOOR_PCT = 20.0
DEFAULT_MOBO_MIN_PCT = 25.0
DEFAULT_MOBO_MAX_PCT = 100.0

# S2: emergency override thresholds, per sensor domain.
DEFAULT_EMERGENCY_C = {"gpu": 83.0, "gpu_mem": 95.0, "cpu": 90.0}
EMERGENCY_HOLD_S = 60.0

# S6: asymmetric response -- heating is answered immediately, cooling is
# rate-limited.  Ramping up fast is always the safe direction.
TEMP_HYSTERESIS_C = 3.0
MAX_STEP_DOWN_PCT = 8.0

STALL_TICKS = 3           # S3: ticks of 0 rpm at a commanded speed before failing
SENSOR_TIMEOUT_TICKS = 5  # S4: ticks without a temperature before reverting

_HWMON_ROOT = "/sys/class/hwmon"
# Thermal-only drivers: they never carry fans, skip them while enumerating.
_HWMON_SKIP = frozenset({"k10temp", "coretemp", "zenpower", "nvme", "amdgpu", "nouveau"})


# --- Host services injected by server.py (avoids a circular import) --------
_host: dict[str, Any] = {"load_state": None, "save_state": None, "cpu_temp": None}


def bind_host(
    load_state: Callable[[], dict],
    save_state: Callable[[dict], None],
    cpu_temp: Callable[[], float | None],
) -> None:
    """Wire the sidecar's state file and CPU thermometer into this module."""
    _host["load_state"] = load_state
    _host["save_state"] = save_state
    _host["cpu_temp"] = cpu_temp


def _load_state() -> dict:
    fn = _host["load_state"]
    return fn() if fn else {}


def _save_state(state: dict) -> None:
    fn = _host["save_state"]
    if fn:
        fn(state)


# --- Channel model --------------------------------------------------------
@dataclass
class FanChannel:
    """One addressable fan.

    `id` must survive reboots: hwmon numbers are assignment order, not identity,
    so the id is keyed on the driver name instead of `hwmonN`.
    """

    id: str
    label: str
    kind: str                       # "gpu" | "mobo"
    controllable: bool
    control_reason: str | None = None
    min_pct: float = HARD_FLOOR_PCT
    max_pct: float = 100.0
    has_tach: bool = False
    default_sensor: str = "cpu"
    rpm: int | None = None
    pwm_pct: float | None = None
    mode: str = "auto"              # "auto" | "manual" | "failed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "controllable": self.controllable,
            "control_reason": self.control_reason,
            "min_pct": self.min_pct,
            "max_pct": self.max_pct,
            "has_tach": self.has_tach,
            "default_sensor": self.default_sensor,
            "rpm": self.rpm,
            "pwm_pct": self.pwm_pct,
            "mode": self.mode,
        }


class FanBackend:
    """Device backend protocol: enumerate / read / set_manual / set_auto."""

    name = "base"

    def enumerate(self) -> list[FanChannel]:
        raise NotImplementedError

    def read(self, channel_id: str) -> tuple[int | None, float | None]:
        """Return (rpm, pwm_pct) for a channel."""
        raise NotImplementedError

    def set_manual(self, channel_id: str, pct: float) -> None:
        raise NotImplementedError

    def set_auto(self, channel_id: str) -> None:
        raise NotImplementedError

    def temperatures(self) -> dict[str, float]:
        return {}


# --- NVML backend ---------------------------------------------------------
class NvmlFanBackend(FanBackend):
    """GPU fans.  Keeps NVML initialised for the lifetime of the process.

    NVML is reference counted, so `collect_telemetry()` in server.py may keep
    doing its own init/shutdown pair without invalidating the handles held here.
    """

    name = "nvml"

    def __init__(self) -> None:
        self._handles: dict[int, Any] = {}
        self._fan_index: dict[str, tuple[int, int]] = {}
        self._limits: dict[str, tuple[float, float]] = {}
        self._ready = False
        if pynvml is None:
            return
        try:
            pynvml.nvmlInit()
            for i in range(pynvml.nvmlDeviceGetCount()):
                self._handles[i] = pynvml.nvmlDeviceGetHandleByIndex(i)
            self._ready = True
        except Exception:
            self._handles.clear()

    def enumerate(self) -> list[FanChannel]:
        channels: list[FanChannel] = []
        if not self._ready:
            return channels
        for gpu_index, handle in self._handles.items():
            try:
                num_fans = int(pynvml.nvmlDeviceGetNumFans(handle))
            except Exception:
                continue
            try:
                gpu_name = pynvml.nvmlDeviceGetName(handle)
                if isinstance(gpu_name, bytes):
                    gpu_name = gpu_name.decode()
            except Exception:
                gpu_name = f"GPU {gpu_index}"
            # The driver reports its own floor (30% on a GeForce 3090); never
            # allow a configuration below it -- NVML would reject or clamp it.
            try:
                lo, hi = pynvml.nvmlDeviceGetMinMaxFanSpeed(handle)
                lo_pct, hi_pct = float(lo), float(hi)
            except Exception:
                lo_pct, hi_pct = 30.0, 100.0
            controllable = hasattr(pynvml, "nvmlDeviceSetFanSpeed_v2") and hasattr(
                pynvml, "nvmlDeviceSetDefaultFanSpeed_v2"
            )
            reason = None if controllable else (
                "версия NVML не умеет управлять вентиляторами "
                "(нужен nvmlDeviceSetFanSpeed_v2)"
            )
            for fan in range(num_fans):
                cid = f"gpu:{gpu_index}:fan{fan}"
                self._fan_index[cid] = (gpu_index, fan)
                self._limits[cid] = (max(lo_pct, HARD_FLOOR_PCT), hi_pct)
                channels.append(
                    FanChannel(
                        id=cid,
                        label=f"{gpu_name} · вентилятор {fan + 1}",
                        kind="gpu",
                        controllable=controllable,
                        control_reason=reason,
                        min_pct=max(lo_pct, HARD_FLOOR_PCT),
                        max_pct=hi_pct,
                        has_tach=False,   # NVML reports duty %, not rpm
                        default_sensor="gpu",
                    )
                )
        return channels

    def read(self, channel_id: str) -> tuple[int | None, float | None]:
        entry = self._fan_index.get(channel_id)
        if entry is None:
            return None, None
        gpu_index, fan = entry
        handle = self._handles.get(gpu_index)
        if handle is None:
            return None, None
        try:
            return None, float(pynvml.nvmlDeviceGetFanSpeed_v2(handle, fan))
        except Exception:
            try:
                return None, float(pynvml.nvmlDeviceGetFanSpeed(handle))
            except Exception:
                return None, None

    def set_manual(self, channel_id: str, pct: float) -> None:
        entry = self._fan_index.get(channel_id)
        if entry is None:
            raise RuntimeError(f"unknown channel {channel_id}")
        gpu_index, fan = entry
        lo, hi = self._limits.get(channel_id, (HARD_FLOOR_PCT, 100.0))
        target = int(round(max(lo, min(hi, pct))))
        pynvml.nvmlDeviceSetFanSpeed_v2(self._handles[gpu_index], fan, target)

    def set_auto(self, channel_id: str) -> None:
        entry = self._fan_index.get(channel_id)
        if entry is None:
            raise RuntimeError(f"unknown channel {channel_id}")
        gpu_index, fan = entry
        pynvml.nvmlDeviceSetDefaultFanSpeed_v2(self._handles[gpu_index], fan)

    def temperatures(self) -> dict[str, float]:
        out: dict[str, float] = {}
        if not self._ready:
            return out
        handle = self._handles.get(0)
        if handle is None:
            return out
        try:
            out["gpu"] = float(
                pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            )
        except Exception:
            pass
        return out


# --- hwmon backend --------------------------------------------------------
def _read_int(path: str) -> int | None:
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _read_str(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _write_int(path: str, value: int) -> None:
    with open(path, "w") as f:
        f.write(str(value))


def _is_writable(path: str) -> bool:
    """Whether the *driver* published this attribute as writable.

    `os.access(W_OK)` is useless here: this sidecar runs as root, and root
    bypasses the permission bits, so it answers True even for a read-only
    sysfs attribute. hwmon drivers encode intent in the mode itself — 0644 for
    a controllable pwm, 0444 for one they refuse to drive.
    """
    try:
        return bool(stat.S_IMODE(os.stat(path).st_mode) & stat.S_IWUSR)
    except OSError:
        return False


class HwmonFanBackend(FanBackend):
    """Motherboard fans via sysfs.

    Enumeration is unconditional -- a read-only channel is worth showing, with
    the reason it cannot be driven.  Writes require both a writable `pwmN` and
    a `pwmN_enable`, which is exactly what separates the mainline `nct6683`
    driver (read-only) from the out-of-tree `nct6687d` module.
    """

    name = "hwmon"

    def __init__(self) -> None:
        self._paths: dict[str, dict[str, str]] = {}

    def enumerate(self) -> list[FanChannel]:
        channels: list[FanChannel] = []
        self._paths.clear()
        try:
            hwmons = sorted(os.listdir(_HWMON_ROOT))
        except OSError:
            return channels
        seen_names: dict[str, int] = {}
        for hwmon in hwmons:
            base = os.path.join(_HWMON_ROOT, hwmon)
            driver = (_read_str(os.path.join(base, "name")) or hwmon).strip()
            if driver in _HWMON_SKIP:
                continue
            # Two chips of the same model would otherwise collide on the id.
            occurrence = seen_names.get(driver, 0)
            seen_names[driver] = occurrence + 1
            prefix = driver if occurrence == 0 else f"{driver}#{occurrence}"
            channels.extend(self._enumerate_chip(base, prefix))
        return channels

    def _enumerate_chip(self, base: str, prefix: str) -> list[FanChannel]:
        out: list[FanChannel] = []
        try:
            entries = os.listdir(base)
        except OSError:
            return out
        indexes = sorted(
            {
                int(e[3:].split("_")[0])
                for e in entries
                if e.startswith("fan") and e[3:].split("_")[0].isdigit()
            }
            | {
                int(e[3:].split("_")[0])
                for e in entries
                if e.startswith("pwm") and e[3:].split("_")[0].isdigit()
            }
        )
        for n in indexes:
            pwm_path = os.path.join(base, f"pwm{n}")
            enable_path = os.path.join(base, f"pwm{n}_enable")
            tach_path = os.path.join(base, f"fan{n}_input")
            has_pwm = os.path.exists(pwm_path)
            has_enable = os.path.exists(enable_path)
            has_tach = os.path.exists(tach_path)
            if not has_pwm and not has_tach:
                continue

            controllable, reason = self._writability(prefix, has_pwm, has_enable, pwm_path)
            label = _read_str(os.path.join(base, f"fan{n}_label")) or (
                f"{prefix.upper()} · канал {n}"
            )
            cid = f"hwmon:{prefix}:pwm{n}" if has_pwm else f"hwmon:{prefix}:fan{n}"
            self._paths[cid] = {
                "pwm": pwm_path if has_pwm else "",
                "enable": enable_path if has_enable else "",
                "tach": tach_path if has_tach else "",
            }
            out.append(
                FanChannel(
                    id=cid,
                    label=label,
                    kind="mobo",
                    controllable=controllable,
                    control_reason=reason,
                    min_pct=DEFAULT_MOBO_MIN_PCT,
                    max_pct=DEFAULT_MOBO_MAX_PCT,
                    has_tach=has_tach,
                    default_sensor="cpu",
                )
            )
        return out

    @staticmethod
    def _writability(
        prefix: str, has_pwm: bool, has_enable: bool, pwm_path: str
    ) -> tuple[bool, str | None]:
        """Report the *hardware* reason first — a kill switch is the least
        informative answer, and the operator needs to know whether flipping it
        would even help."""
        if not has_pwm:
            return False, "только тахометр, PWM-канала нет"
        if not _is_writable(pwm_path):
            # The chip reports itself as nct6687 while the mainline module that
            # publishes it read-only is called nct6683 — match on the family.
            if prefix.startswith("nct66"):
                return False, (
                    "штатный драйвер nct6683 отдаёт pwm только на чтение; "
                    "нужен DKMS-модуль nct6687d — см. docs/cooling-motherboard-fans.md"
                )
            return False, f"{prefix}: pwm доступен только на чтение"
        if not has_enable:
            return False, (
                f"{prefix}: нет pwm_enable — вернуть вентилятор прошивке будет нечем"
            )
        if not ALLOW_HWMON:
            return False, (
                "железо позволяет, но управление платой выключено "
                "(FAN_CONTROL_ALLOW_HWMON=0)"
            )
        return True, None

    def read(self, channel_id: str) -> tuple[int | None, float | None]:
        paths = self._paths.get(channel_id)
        if not paths:
            return None, None
        rpm = _read_int(paths["tach"]) if paths["tach"] else None
        pwm = _read_int(paths["pwm"]) if paths["pwm"] else None
        pct = round(pwm / 255 * 100, 1) if pwm is not None else None
        return rpm, pct

    def set_manual(self, channel_id: str, pct: float) -> None:
        paths = self._paths.get(channel_id)
        if not paths or not paths["pwm"]:
            raise RuntimeError(f"unknown channel {channel_id}")
        target = max(HARD_FLOOR_PCT, min(100.0, pct))
        if paths["enable"]:
            _write_int(paths["enable"], 1)      # 1 = manual
        _write_int(paths["pwm"], int(round(target * 255 / 100)))

    def set_auto(self, channel_id: str) -> None:
        paths = self._paths.get(channel_id)
        if not paths or not paths["enable"]:
            raise RuntimeError(f"no pwm_enable for {channel_id}")
        # 2 = firmware curve.  0 would mean "full speed, no control" -- never
        # written here, and never used as a fallback: a failed revert must be
        # reported, not silently turned into a different hardware state.
        _write_int(paths["enable"], 2)

    def temperatures(self) -> dict[str, float]:
        fn = _host["cpu_temp"]
        if not fn:
            return {}
        try:
            value = fn()
        except Exception:
            return {}
        return {"cpu": float(value)} if value is not None else {}


# --- Curve engine (pure functions: unit-testable without hardware) ---------
def _num(value: Any, default: float) -> float:
    """Fall back only on a genuinely absent value.

    `value or default` would quietly discard a legitimate 0 — a zero hysteresis
    band or a zero slew limit are valid settings, and silently replacing them
    with the default makes the UI lie about what the hardware is doing.
    """
    if value is None:
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def evaluate_curve(points: Iterable[dict], temp: float) -> float:
    """Linear interpolation over [{"t": °C, "pct": %}] sorted by temperature.

    Below the first point the first speed holds; above the last one the last
    speed holds -- an empty curve is a configuration error, not a reason to
    stop a fan, so it reports 100%.
    """
    # Collapse duplicate temperatures to the *higher* speed: a step in the
    # curve must resolve upwards, never to the quieter of the two.
    merged: dict[float, float] = {}
    for p in points:
        t, pct = float(p["t"]), float(p["pct"])
        merged[t] = max(merged.get(t, pct), pct)
    pts = sorted(merged.items())
    if not pts:
        return 100.0
    if temp <= pts[0][0]:
        return pts[0][1]
    if temp >= pts[-1][0]:
        return pts[-1][1]
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        if t0 <= temp <= t1:
            return p0 + (p1 - p0) * (temp - t0) / (t1 - t0)
    return pts[-1][1]


def hysteresis_temp(prev_used: float | None, temp: float, band: float) -> float:
    """S6: react to heating at once, ignore cooling inside the band.

    Asymmetric on purpose -- damping a rise would trade fan noise for heat.
    """
    if prev_used is None or temp > prev_used:
        return temp
    if temp < prev_used - band:
        return temp
    return prev_used


def limit_slew(prev_pct: float | None, target_pct: float, max_step_down: float) -> float:
    """S6: unlimited ramp up, bounded ramp down."""
    if prev_pct is None or target_pct >= prev_pct:
        return target_pct
    return max(target_pct, prev_pct - max_step_down)


def clamp_pct(
    pct: float,
    ch_min: float,
    ch_max: float,
    allow_stop: bool = False,
    temp: float | None = None,
    stop_below_c: float | None = None,
) -> float:
    """S1: the floor no configuration can cross.

    Stopping a fan entirely is only honoured when it was asked for explicitly
    *and* the temperature is known to be below the stop threshold.  An unknown
    temperature never stops a fan.
    """
    if allow_stop and pct <= 0 and temp is not None and stop_below_c is not None:
        if temp < stop_below_c:
            return 0.0
    floor = max(HARD_FLOOR_PCT, ch_min)
    return max(floor, min(ch_max, pct))


BUILTIN_PRESETS: dict[str, dict[str, Any]] = {
    "silent": {
        "label": "Тихий",
        "curves": {
            "gpu": [{"t": 40, "pct": 30}, {"t": 60, "pct": 40},
                    {"t": 75, "pct": 65}, {"t": 83, "pct": 100}],
            "mobo": [{"t": 40, "pct": 25}, {"t": 60, "pct": 40},
                     {"t": 75, "pct": 70}, {"t": 85, "pct": 100}],
        },
    },
    "balanced": {
        "label": "Баланс",
        "curves": {
            "gpu": [{"t": 35, "pct": 30}, {"t": 55, "pct": 45},
                    {"t": 70, "pct": 75}, {"t": 80, "pct": 100}],
            "mobo": [{"t": 35, "pct": 30}, {"t": 55, "pct": 50},
                     {"t": 70, "pct": 80}, {"t": 82, "pct": 100}],
        },
    },
    "max": {
        "label": "Максимум",
        "curves": {
            "gpu": [{"t": 0, "pct": 100}],
            "mobo": [{"t": 0, "pct": 100}],
        },
    },
}


def preset_config(preset: str, channels: Iterable[FanChannel]) -> dict[str, Any]:
    """Materialise a builtin preset into a per-channel configuration."""
    spec = BUILTIN_PRESETS.get(preset) or BUILTIN_PRESETS["balanced"]
    out: dict[str, Any] = {}
    for ch in channels:
        if not ch.controllable:
            continue
        out[ch.id] = {
            "mode": "curve",
            "sensor": ch.default_sensor,
            "curve": [dict(p) for p in spec["curves"].get(ch.kind, spec["curves"]["gpu"])],
            "min_pct": max(HARD_FLOOR_PCT, ch.min_pct),
            "allow_stop": False,
            "stop_below_c": None,
        }
    return out


# --- Controller -----------------------------------------------------------
@dataclass
class _Runtime:
    """Per-channel loop state; never persisted."""

    last_written: float | None = None
    last_temp_used: float | None = None
    no_temp_ticks: int = 0
    zero_rpm_ticks: int = 0
    failed_reason: str | None = None
    commanded: bool = False   # did *we* put this channel into manual mode?


class FanController:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._backends: list[FanBackend] = []
        self._channels: dict[str, FanChannel] = {}
        self._owner: dict[str, FanBackend] = {}
        self._rt: dict[str, _Runtime] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=200)
        self._emergency_until: dict[str, float] = {}
        self._config: dict[str, Any] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_tick_ts: float | None = None
        self._last_error: str | None = None

    # -- setup ------------------------------------------------------------
    def discover(self) -> None:
        with self._lock:
            self._backends = []
            nvml_backend = NvmlFanBackend()
            self._backends.append(nvml_backend)
            self._backends.append(HwmonFanBackend())
            self._channels.clear()
            self._owner.clear()
            for backend in self._backends:
                try:
                    for ch in backend.enumerate():
                        self._channels[ch.id] = ch
                        self._owner[ch.id] = backend
                        self._rt.setdefault(ch.id, _Runtime())
                except Exception as exc:
                    self._log("error", None, "enumerate_failed",
                              f"{backend.name}: {exc}")

    def load_config(self) -> None:
        with self._lock:
            state = _load_state()
            self._config = state.get("fans") or {
                "enabled": False,
                "preset": "balanced",
                "channels": {},
            }

    def _persist_config(self) -> None:
        state = _load_state()
        state["fans"] = self._config
        _save_state(state)

    # -- S5: dead-man -----------------------------------------------------
    def recover_after_unclean_shutdown(self) -> None:
        """NVML manual speeds outlive the process, so an unclean exit leaves the
        hardware pinned.  Absence of the clean-shutdown marker means exactly
        that: hand everything back to firmware before doing anything else.

        Gated on a `fans` section existing in the state file: a run that never
        commanded a fan has nothing to recover, and must not write to hardware
        just to prove it.  Every command path persists the config first, so the
        section is present whenever a pinned channel is possible.
        """
        state = _load_state()
        was_clean = bool(state.pop("fans_clean_shutdown", False))
        ever_commanded = bool(state.get("fans"))
        _save_state(state)
        if was_clean or not ever_commanded:
            return
        reverted = self.restore_all_auto(
            reason="recovery after unclean shutdown", force=True
        )
        if reverted:
            self._log("warn", None, "unclean_recovery",
                      f"после нечистого завершения возвращено в авто: {reverted}")

    def mark_clean_shutdown(self) -> None:
        state = _load_state()
        state["fans_clean_shutdown"] = True
        _save_state(state)

    def restore_all_auto(self, reason: str = "", force: bool = False) -> list[str]:
        """Hand channels back to firmware/NVML.

        Without `force` only channels this process actually commanded are
        touched, so exiting never clobbers a speed somebody else set.  Crash
        recovery passes `force=True`: there the runtime state is empty by
        definition, yet the hardware may still be pinned from the previous run.
        """
        done: list[str] = []
        with self._lock:
            for cid, ch in self._channels.items():
                if not ch.controllable:
                    continue
                rt = self._rt.setdefault(cid, _Runtime())
                if not force and not rt.commanded and ch.mode == "auto":
                    continue
                try:
                    self._owner[cid].set_auto(cid)
                    ch.mode = "auto"
                    rt.last_written = None
                    rt.commanded = False
                    done.append(cid)
                except Exception as exc:
                    self._log("error", cid, "revert_failed", f"{exc}")
        if done and reason:
            self._log("info", None, "revert_all", f"{reason}: {len(done)} каналов")
        return done

    def shutdown(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=TICK_S * 2)
        self.restore_all_auto(reason="shutdown")
        self.mark_clean_shutdown()

    # -- loop -------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="fan-loop")
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
                self._last_error = None
            except Exception as exc:
                self._last_error = str(exc)
                self._log("error", None, "tick_failed", str(exc))
            self._stop.wait(TICK_S)

    def read_temperatures(self) -> dict[str, float]:
        temps: dict[str, float] = {}
        for backend in self._backends:
            try:
                temps.update(backend.temperatures())
            except Exception:
                continue
        return temps

    def tick(self) -> None:
        with self._lock:
            temps = self.read_temperatures()
            now = time.monotonic()
            self._update_emergency(temps, now)
            self._refresh_readings()

            active = CONTROL_ENABLED and bool(self._config.get("enabled"))
            for cid, ch in self._channels.items():
                if not ch.controllable:
                    continue
                rt = self._rt.setdefault(cid, _Runtime())
                if rt.failed_reason:
                    continue
                cfg = (self._config.get("channels") or {}).get(cid)
                emergency = self._emergency_active(ch, now)

                if emergency:
                    self._drive(cid, ch, rt, 100.0, bypass_slew=True)
                    continue
                if not active or not cfg or cfg.get("mode") == "auto":
                    self._ensure_auto(cid, ch, rt)
                    continue
                target = self._target_for(cid, ch, rt, cfg, temps)
                if target is None:
                    continue
                self._drive(cid, ch, rt, target)
            self._last_tick_ts = time.time()

    def _refresh_readings(self) -> None:
        for cid, ch in self._channels.items():
            try:
                rpm, pct = self._owner[cid].read(cid)
            except Exception:
                rpm, pct = None, None
            ch.rpm, ch.pwm_pct = rpm, pct

    # -- S2: emergency override ------------------------------------------
    def _update_emergency(self, temps: dict[str, float], now: float) -> None:
        thresholds = {**DEFAULT_EMERGENCY_C, **(self._config.get("emergency_c") or {})}
        for sensor, limit in thresholds.items():
            value = temps.get(sensor)
            if value is None or value < float(limit):
                continue
            hold = _num(self._config.get("emergency_hold_s"), EMERGENCY_HOLD_S)
            was_active = self._emergency_until.get(sensor, 0.0) > now
            self._emergency_until[sensor] = now + hold
            if not was_active:
                self._log("critical", None, "emergency",
                          f"{sensor} {value:.0f}°C ≥ {limit}°C — все вентиляторы домена на 100%")

    def _emergency_active(self, ch: FanChannel, now: float) -> bool:
        """Which sensors a channel answers to is a property of what it cools,
        not of the curve someone configured for it."""
        sensors = {"gpu": ("gpu", "gpu_mem"), "mobo": ("cpu",)}.get(ch.kind, ())
        return any(self._emergency_until.get(s, 0.0) > now for s in sensors)

    # -- target computation ----------------------------------------------
    def _target_for(
        self,
        cid: str,
        ch: FanChannel,
        rt: _Runtime,
        cfg: dict,
        temps: dict[str, float],
    ) -> float | None:
        if cfg.get("mode") == "manual":
            return clamp_pct(
                _num(cfg.get("pct"), ch.min_pct),
                _num(cfg.get("min_pct"), ch.min_pct),
                ch.max_pct,
                allow_stop=bool(cfg.get("allow_stop")),
                temp=temps.get(cfg.get("sensor") or ch.default_sensor),
                stop_below_c=cfg.get("stop_below_c"),
            )

        temp = temps.get(cfg.get("sensor") or ch.default_sensor)
        if temp is None:
            # S4: an unknown temperature is not a licence to keep spinning slow.
            rt.no_temp_ticks += 1
            if rt.no_temp_ticks >= SENSOR_TIMEOUT_TICKS and rt.commanded:
                self._log("warn", cid, "sensor_lost",
                          f"нет температуры ({cfg.get('sensor')}) — канал возвращён в авто")
                self._ensure_auto(cid, ch, rt)
            return None
        rt.no_temp_ticks = 0

        used = hysteresis_temp(
            rt.last_temp_used, temp,
            _num(self._config.get("temp_hysteresis_c"), TEMP_HYSTERESIS_C),
        )
        rt.last_temp_used = used
        raw = evaluate_curve(cfg.get("curve") or [], used)
        return clamp_pct(
            raw,
            _num(cfg.get("min_pct"), ch.min_pct),
            ch.max_pct,
            allow_stop=bool(cfg.get("allow_stop")),
            temp=used,
            stop_below_c=cfg.get("stop_below_c"),
        )

    # -- actuation --------------------------------------------------------
    def _drive(
        self, cid: str, ch: FanChannel, rt: _Runtime, target: float, bypass_slew: bool = False
    ) -> None:
        if not bypass_slew:
            target = limit_slew(
                rt.last_written, target,
                _num(self._config.get("max_step_down_pct"), MAX_STEP_DOWN_PCT),
            )
        target = round(target, 1)
        # S7: the Super-I/O EC is written over port I/O; repeating an identical
        # value every tick buys nothing.
        if rt.last_written is not None and abs(rt.last_written - target) < 0.5:
            self._check_stall(cid, ch, rt, target)
            return
        try:
            self._owner[cid].set_manual(cid, target)
        except Exception as exc:
            self._fail(cid, ch, rt, f"запись оборотов не удалась: {exc}")
            return
        rt.last_written = target
        rt.commanded = True
        ch.mode = "manual"
        self._check_stall(cid, ch, rt, target)

    def _ensure_auto(self, cid: str, ch: FanChannel, rt: _Runtime) -> None:
        if not rt.commanded and ch.mode == "auto":
            return
        try:
            self._owner[cid].set_auto(cid)
        except Exception as exc:
            self._log("error", cid, "revert_failed", str(exc))
            return
        ch.mode = "auto"
        rt.last_written = None
        rt.last_temp_used = None
        rt.commanded = False

    # -- S3: stall detection ---------------------------------------------
    def _check_stall(self, cid: str, ch: FanChannel, rt: _Runtime, target: float) -> None:
        if not ch.has_tach or target <= 0:
            return
        if ch.rpm is None:
            return
        if ch.rpm > 0:
            rt.zero_rpm_ticks = 0
            return
        rt.zero_rpm_ticks += 1
        if rt.zero_rpm_ticks >= STALL_TICKS:
            self._fail(cid, ch, rt,
                       f"вентилятор не крутится при {target:.0f}% — канал отключён от управления")

    def _fail(self, cid: str, ch: FanChannel, rt: _Runtime, message: str) -> None:
        rt.failed_reason = message
        ch.mode = "failed"
        self._log("error", cid, "channel_failed", message)
        try:
            self._owner[cid].set_auto(cid)
            rt.commanded = False
        except Exception as exc:
            self._log("critical", cid, "revert_failed",
                      f"канал в аварии и не возвращается в авто: {exc}")

    # -- audit (S10) ------------------------------------------------------
    def _log(self, level: str, channel: str | None, code: str, message: str) -> None:
        self._events.append({
            "ts": time.time(),
            "level": level,
            "channel": channel,
            "code": code,
            "message": message,
        })
        if level in {"error", "critical", "warn"}:
            print(f"[fans:{level}] {code} {channel or '-'}: {message}", flush=True)

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(self._events)[-limit:]

    # -- public operations ------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_readings()
            now = time.monotonic()
            channels = []
            for cid, ch in self._channels.items():
                item = ch.to_dict()
                rt = self._rt.get(cid)
                item["failed_reason"] = rt.failed_reason if rt else None
                item["target_pct"] = rt.last_written if rt else None
                item["config"] = (self._config.get("channels") or {}).get(cid)
                channels.append(item)
            return {
                "ok": True,
                "control_enabled": CONTROL_ENABLED,
                "hwmon_allowed": ALLOW_HWMON,
                "config": self._config,
                "presets": {
                    k: {"label": v["label"], "curves": v["curves"]}
                    for k, v in BUILTIN_PRESETS.items()
                },
                "channels": channels,
                "temperatures": self.read_temperatures(),
                "emergency": {
                    s: round(until - now, 1)
                    for s, until in self._emergency_until.items()
                    if until > now
                },
                "loop": {
                    "running": self._thread is not None and self._thread.is_alive(),
                    "tick_s": TICK_S,
                    "last_tick_ts": self._last_tick_ts,
                    "last_error": self._last_error,
                },
                "safety": {
                    "hard_floor_pct": HARD_FLOOR_PCT,
                    "emergency_c": {**DEFAULT_EMERGENCY_C,
                                    **(self._config.get("emergency_c") or {})},
                    "emergency_hold_s": EMERGENCY_HOLD_S,
                    "temp_hysteresis_c": TEMP_HYSTERESIS_C,
                    "max_step_down_pct": MAX_STEP_DOWN_PCT,
                    "stall_ticks": STALL_TICKS,
                    "sensor_timeout_ticks": SENSOR_TIMEOUT_TICKS,
                },
            }

    def _require_controllable(self, cid: str) -> FanChannel:
        ch = self._channels.get(cid)
        if ch is None:
            raise RuntimeError(f"канал {cid} не найден")
        if not ch.controllable:
            raise RuntimeError(ch.control_reason or f"канал {cid} не управляется")
        return ch

    def set_mode_auto(self, scope: str) -> dict[str, Any]:
        with self._lock:
            if scope == "all":
                reverted = self.restore_all_auto(reason="запрос оператора")
                channels_cfg = self._config.setdefault("channels", {})
                for cid in list(channels_cfg):
                    channels_cfg[cid]["mode"] = "auto"
                self._config["enabled"] = False
                self._persist_config()
                return {"ok": True, "reverted": reverted}
            ch = self._require_controllable(scope)
            rt = self._rt.setdefault(scope, _Runtime())
            rt.failed_reason = None
            self._ensure_auto(scope, ch, rt)
            cfg = self._config.setdefault("channels", {}).setdefault(scope, {})
            cfg["mode"] = "auto"
            self._persist_config()
            return {"ok": True, "reverted": [scope]}

    def set_manual(self, cid: str, pct: float) -> dict[str, Any]:
        if not CONTROL_ENABLED:
            raise RuntimeError("управление вентиляторами выключено (FAN_CONTROL_ENABLED=0)")
        with self._lock:
            ch = self._require_controllable(cid)
            rt = self._rt.setdefault(cid, _Runtime())
            rt.failed_reason = None
            requested = float(pct)
            applied = clamp_pct(requested, ch.min_pct, ch.max_pct)
            self._owner[cid].set_manual(cid, applied)
            rt.last_written = applied
            rt.commanded = True
            ch.mode = "manual"
            cfg = self._config.setdefault("channels", {}).setdefault(cid, {})
            cfg.update({"mode": "manual", "pct": applied})
            self._config["enabled"] = True
            self._persist_config()
            self._log("info", cid, "manual", f"вручную {applied:.0f}%")
            return {
                "ok": True,
                "channel": cid,
                "requested_pct": requested,
                "applied_pct": applied,
                "clamped": abs(applied - requested) > 0.01,
                "min_pct": max(HARD_FLOOR_PCT, ch.min_pct),
                "max_pct": ch.max_pct,
            }

    def apply_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not CONTROL_ENABLED:
            raise RuntimeError("управление вентиляторами выключено (FAN_CONTROL_ENABLED=0)")
        with self._lock:
            preset = payload.get("preset")
            channels_cfg = payload.get("channels")
            if channels_cfg is None and preset:
                channels_cfg = preset_config(str(preset), self._channels.values())
            channels_cfg = self._sanitize_channels(channels_cfg or {})
            self._config = {
                "enabled": bool(payload.get("enabled", True)),
                "preset": preset or self._config.get("preset") or "custom",
                "channels": channels_cfg,
                "emergency_c": {**DEFAULT_EMERGENCY_C,
                                **(payload.get("emergency_c") or {})},
                "emergency_hold_s": _num(
                    payload.get("emergency_hold_s"), EMERGENCY_HOLD_S),
                "temp_hysteresis_c": _num(
                    payload.get("temp_hysteresis_c"), TEMP_HYSTERESIS_C),
                "max_step_down_pct": _num(
                    payload.get("max_step_down_pct"), MAX_STEP_DOWN_PCT),
            }
            for rt in self._rt.values():
                rt.failed_reason = None
                rt.last_temp_used = None
            self._persist_config()
            self._log("info", None, "config_applied",
                      f"пресет {self._config['preset']}, каналов {len(channels_cfg)}, "
                      f"enabled={self._config['enabled']}")
            if not self._config["enabled"]:
                self.restore_all_auto(reason="конфигурация выключена")
            return {"ok": True, "config": self._config}

    def _sanitize_channels(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Authoritative validation: the sidecar never trusts the caller's floors."""
        out: dict[str, Any] = {}
        for cid, cfg in raw.items():
            ch = self._channels.get(cid)
            if ch is None or not ch.controllable:
                continue
            mode = cfg.get("mode") or "curve"
            floor = max(HARD_FLOOR_PCT, ch.min_pct, _num(cfg.get("min_pct"), 0.0))
            entry: dict[str, Any] = {
                "mode": mode,
                "sensor": cfg.get("sensor") or ch.default_sensor,
                "min_pct": floor,
                "allow_stop": bool(cfg.get("allow_stop")),
                "stop_below_c": cfg.get("stop_below_c"),
            }
            if mode == "manual":
                entry["pct"] = clamp_pct(
                    _num(cfg.get("pct"), floor), floor, ch.max_pct,
                    allow_stop=entry["allow_stop"],
                    temp=cfg.get("stop_below_c"),
                    stop_below_c=entry["stop_below_c"],
                )
            elif mode == "curve":
                points = []
                for p in cfg.get("curve") or []:
                    try:
                        points.append({
                            "t": float(p["t"]),
                            "pct": clamp_pct(float(p["pct"]), floor, ch.max_pct,
                                             allow_stop=entry["allow_stop"],
                                             temp=float(p["t"]),
                                             stop_below_c=entry["stop_below_c"]),
                        })
                    except (KeyError, TypeError, ValueError):
                        continue
                if not points:
                    continue
                entry["curve"] = sorted(points, key=lambda p: p["t"])
            out[cid] = entry
        return out

    def preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        """S8: dry run -- what the curve would command, with no writes at all."""
        with self._lock:
            channels_cfg = payload.get("channels")
            if channels_cfg is None and payload.get("preset"):
                channels_cfg = preset_config(str(payload["preset"]), self._channels.values())
            channels_cfg = self._sanitize_channels(channels_cfg or {})
            sweep = payload.get("temps") or list(range(30, 101, 5))
            result: dict[str, Any] = {}
            for cid, cfg in channels_cfg.items():
                ch = self._channels[cid]
                floor = cfg["min_pct"]
                if cfg["mode"] == "manual":
                    result[cid] = [{"t": None, "pct": cfg["pct"]}]
                    continue
                result[cid] = [
                    {
                        "t": float(t),
                        "pct": clamp_pct(
                            evaluate_curve(cfg["curve"], float(t)),
                            floor, ch.max_pct,
                            allow_stop=cfg["allow_stop"], temp=float(t),
                            stop_below_c=cfg["stop_below_c"],
                        ),
                    }
                    for t in sweep
                ]
            return {"ok": True, "preview": result, "config": channels_cfg}


_controller: FanController | None = None
_controller_lock = threading.Lock()


def get_controller() -> FanController:
    global _controller
    with _controller_lock:
        if _controller is None:
            _controller = FanController()
        return _controller
