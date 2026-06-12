"""GPU telemetry helper — privileged sidecar.

Serves GPU telemetry as JSON on an internal-only HTTP port:
  GET  /health      — liveness probe
  GET  /telemetry   — utilization, temperatures, power, fan, VRAM, clocks
  POST /power-limit — set the GPU power limit (watts, clamped to NVML
                      constraints; requires root, which this container has)

Two data sources, each optional (partial responses are valid):
  1. NVML (nvidia-ml-py) — everything nvidia-smi shows.
  2. PCIe BAR0 register read (mmap) — GDDR6/GDDR6X memory junction
     temperature, which NVML does NOT expose on consumer GPUs.
     Same method as github.com/olealgoritme/gddr6 (requires privileged
     container: /dev/mem or sysfs resource0 mmap).
"""

from __future__ import annotations

import json
import mmap
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

try:
    import pynvml
except Exception:  # pragma: no cover - library missing or broken
    pynvml = None

PORT = int(os.environ.get("PORT", "9966"))
CACHE_TTL_S = float(os.environ.get("CACHE_TTL_S", "2.0"))
# Saved power limits live on a named volume so the chosen limit survives
# container restarts and host reboots (NVML limits reset on reboot).
STATE_FILE = os.environ.get("STATE_FILE", "/data/state.json")

# PCI device id -> BAR0 register offset of the VRAM junction temperature.
# Table from gddr6.c (github.com/olealgoritme/gddr6).
GDDR6_TEMP_OFFSETS: dict[int, int] = {
    # GA102 (GDDR6X)
    0x2203: 0x0000E2A8,  # RTX 3090 Ti
    0x2204: 0x0000E2A8,  # RTX 3090
    0x2208: 0x0000E2A8,  # RTX 3080 Ti
    0x2206: 0x0000E2A8,  # RTX 3080
    0x2216: 0x0000E2A8,  # RTX 3080 LHR
    0x2230: 0x0000E2A8,  # RTX A6000
    0x2231: 0x0000E2A8,  # RTX A5000
    0x2232: 0x0000E2A8,  # RTX A4500
    # AD10x (GDDR6X)
    0x2684: 0x0000E2A8,  # RTX 4090
    0x2702: 0x0000E2A8,  # RTX 4080 Super
    0x2704: 0x0000E2A8,  # RTX 4080
    0x2705: 0x0000E2A8,  # RTX 4070 Ti Super
    0x2782: 0x0000E2A8,  # RTX 4070 Ti
    0x26B1: 0x0000E2A8,  # RTX 6000 Ada
    # GA106 (GDDR6)
    0x2531: 0x0000EE50,  # RTX A2000
    0x2571: 0x0000EE50,  # RTX A2000 12GB
}

_PCI_DEVICES_DIR = "/sys/bus/pci/devices"
_NVIDIA_VENDOR = 0x10DE


def _read_sysfs_hex(path: str) -> int | None:
    try:
        with open(path) as f:
            return int(f.read().strip(), 16)
    except (OSError, ValueError):
        return None


def _read_bar0_base(pci_dir: str) -> int | None:
    """First line of the sysfs `resource` file is BAR0: 'start end flags'."""
    try:
        with open(os.path.join(pci_dir, "resource")) as f:
            start = f.readline().split()[0]
        base = int(start, 16)
        return base or None
    except (OSError, ValueError, IndexError):
        return None


def _read_reg_u32(pci_dir: str, bar0_base: int, offset: int) -> int | None:
    """Read a 32-bit register at BAR0+offset via resource0 mmap or /dev/mem."""
    page = mmap.PAGESIZE
    # Preferred: mmap the BAR directly through sysfs (offset is BAR-relative).
    try:
        fd = os.open(os.path.join(pci_dir, "resource0"), os.O_RDONLY)
        try:
            with mmap.mmap(
                fd, page, mmap.MAP_SHARED, mmap.PROT_READ,
                offset=offset & ~(page - 1),
            ) as mm:
                return int.from_bytes(
                    mm[offset % page:offset % page + 4], "little"
                )
        finally:
            os.close(fd)
    except OSError:
        pass
    # Fallback: physical address through /dev/mem.
    phys = bar0_base + offset
    try:
        fd = os.open("/dev/mem", os.O_RDONLY)
        try:
            with mmap.mmap(
                fd, page, mmap.MAP_SHARED, mmap.PROT_READ,
                offset=phys & ~(page - 1),
            ) as mm:
                return int.from_bytes(mm[phys % page:phys % page + 4], "little")
        finally:
            os.close(fd)
    except OSError:
        return None


def read_junction_temps() -> tuple[dict[str, int], str | None]:
    """Scan NVIDIA PCI devices, return {pci_addr: junction_temp_c}."""
    temps: dict[str, int] = {}
    error: str | None = None
    seen_nvidia = False
    try:
        entries = sorted(os.listdir(_PCI_DEVICES_DIR))
    except OSError as exc:
        return temps, f"pci scan failed: {exc}"
    for addr in entries:
        pci_dir = os.path.join(_PCI_DEVICES_DIR, addr)
        if _read_sysfs_hex(os.path.join(pci_dir, "vendor")) != _NVIDIA_VENDOR:
            continue
        device_id = _read_sysfs_hex(os.path.join(pci_dir, "device"))
        if device_id is None:
            continue
        offset = GDDR6_TEMP_OFFSETS.get(device_id)
        if offset is None:
            # Only report "unsupported" for the GPU itself, not audio functions.
            if addr.endswith(".0"):
                seen_nvidia = True
                error = f"unsupported device id 0x{device_id:04x}"
            continue
        seen_nvidia = True
        bar0 = _read_bar0_base(pci_dir)
        if bar0 is None:
            error = f"{addr}: no BAR0"
            continue
        reg = _read_reg_u32(pci_dir, bar0, offset)
        if reg is None:
            error = f"{addr}: register read failed (need privileged + /dev/mem)"
            continue
        temp = (reg & 0xFFF) // 0x20
        if 0 < temp < 125:
            temps[addr] = temp
            error = None
        else:
            error = f"{addr}: implausible value {temp}"
    if not seen_nvidia and not temps and error is None:
        error = "no nvidia pci device found"
    return temps, error


def _nvml_pci_addr(handle: Any) -> str:
    """Normalize NVML busId ('00000000:01:00.0') to sysfs form ('0000:01:00.0')."""
    info = pynvml.nvmlDeviceGetPciInfo(handle)
    bus_id = info.busId
    if isinstance(bus_id, bytes):
        bus_id = bus_id.decode()
    return bus_id.lower()[-12:]


def _s(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _try(fn, *args) -> Any:
    try:
        return fn(*args)
    except Exception:
        return None


def collect_telemetry() -> dict[str, Any]:
    gpus: list[dict[str, Any]] = []
    errors: dict[str, str | None] = {"nvml": None, "gddr6": None}

    junction_temps, gddr6_err = read_junction_temps()
    errors["gddr6"] = gddr6_err

    if pynvml is None:
        errors["nvml"] = "pynvml not importable"
    else:
        try:
            pynvml.nvmlInit()
            try:
                driver = _s(pynvml.nvmlSystemGetDriverVersion())
                for i in range(pynvml.nvmlDeviceGetCount()):
                    h = pynvml.nvmlDeviceGetHandleByIndex(i)
                    pci_addr = _try(_nvml_pci_addr, h) or ""
                    util = _try(pynvml.nvmlDeviceGetUtilizationRates, h)
                    mem = _try(pynvml.nvmlDeviceGetMemoryInfo, h)
                    power_mw = _try(pynvml.nvmlDeviceGetPowerUsage, h)
                    limit_mw = _try(pynvml.nvmlDeviceGetEnforcedPowerLimit, h)
                    constraints_mw = _try(
                        pynvml.nvmlDeviceGetPowerManagementLimitConstraints, h
                    )
                    default_mw = _try(
                        pynvml.nvmlDeviceGetPowerManagementDefaultLimit, h
                    )
                    gpus.append({
                        "index": i,
                        "name": _s(_try(pynvml.nvmlDeviceGetName, h) or ""),
                        "pci_addr": pci_addr,
                        "driver_version": driver,
                        "utilization_pct": util.gpu if util else None,
                        "temp_gpu_c": _try(
                            pynvml.nvmlDeviceGetTemperature,
                            h, pynvml.NVML_TEMPERATURE_GPU,
                        ),
                        "temp_mem_junction_c": junction_temps.get(pci_addr),
                        "power_draw_w": round(power_mw / 1000, 1) if power_mw else None,
                        "power_limit_w": round(limit_mw / 1000, 1) if limit_mw else None,
                        "power_limit_min_w": (
                            round(constraints_mw[0] / 1000, 1) if constraints_mw else None
                        ),
                        "power_limit_max_w": (
                            round(constraints_mw[1] / 1000, 1) if constraints_mw else None
                        ),
                        "power_limit_default_w": (
                            round(default_mw / 1000, 1) if default_mw else None
                        ),
                        "fan_pct": _try(pynvml.nvmlDeviceGetFanSpeed, h),
                        "vram_total_mb": round(mem.total / 1024**2) if mem else None,
                        "vram_used_mb": round(mem.used / 1024**2) if mem else None,
                        "vram_free_mb": round(mem.free / 1024**2) if mem else None,
                        "clock_sm_mhz": _try(
                            pynvml.nvmlDeviceGetClockInfo, h, pynvml.NVML_CLOCK_SM
                        ),
                        "clock_mem_mhz": _try(
                            pynvml.nvmlDeviceGetClockInfo, h, pynvml.NVML_CLOCK_MEM
                        ),
                    })
            finally:
                pynvml.nvmlShutdown()
        except Exception as exc:
            errors["nvml"] = str(exc)

    # NVML unavailable but junction temps readable — still report something.
    if not gpus and junction_temps:
        for idx, (addr, temp) in enumerate(sorted(junction_temps.items())):
            gpus.append({
                "index": idx,
                "name": None,
                "pci_addr": addr,
                "driver_version": None,
                "temp_mem_junction_c": temp,
            })

    return {
        "ok": bool(gpus),
        "ts": time.time(),
        "gpus": gpus,
        "errors": errors,
    }


def _load_state() -> dict[str, Any]:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except OSError as exc:
        print(f"state save failed: {exc}", flush=True)


def set_power_limit(index: int, watts: float, persist: bool = True) -> dict[str, Any]:
    """Set the GPU power limit (clamped to NVML constraints)."""
    if pynvml is None:
        raise RuntimeError("pynvml not importable")
    pynvml.nvmlInit()
    try:
        h = pynvml.nvmlDeviceGetHandleByIndex(index)
        min_mw, max_mw = pynvml.nvmlDeviceGetPowerManagementLimitConstraints(h)
        requested_mw = int(watts * 1000)
        target_mw = max(min_mw, min(max_mw, requested_mw))
        pynvml.nvmlDeviceSetPowerManagementLimit(h, target_mw)
        applied_mw = pynvml.nvmlDeviceGetEnforcedPowerLimit(h)
        applied_w = round(applied_mw / 1000, 1)
        if persist:
            state = _load_state()
            state.setdefault("power_limits", {})[str(index)] = applied_w
            _save_state(state)
        return {
            "ok": True,
            "index": index,
            "requested_w": round(requested_mw / 1000, 1),
            "power_limit_w": applied_w,
            "clamped": target_mw != requested_mw,
            "min_w": round(min_mw / 1000, 1),
            "max_w": round(max_mw / 1000, 1),
        }
    finally:
        pynvml.nvmlShutdown()


def restore_saved_limits(retries: int = 10, delay_s: float = 3.0) -> None:
    """Re-apply persisted power limits on startup (driver may need a moment)."""
    limits = _load_state().get("power_limits") or {}
    if not limits:
        return
    for attempt in range(1, retries + 1):
        try:
            for index, watts in limits.items():
                result = set_power_limit(int(index), float(watts), persist=False)
                print(
                    f"restored power limit: gpu {index} -> "
                    f"{result['power_limit_w']} W",
                    flush=True,
                )
            return
        except Exception as exc:
            print(f"restore attempt {attempt}/{retries} failed: {exc}", flush=True)
            time.sleep(delay_s)


_cache_lock = threading.Lock()
_cache: tuple[float, dict[str, Any]] | None = None


def get_telemetry_cached() -> dict[str, Any]:
    global _cache
    with _cache_lock:
        now = time.monotonic()
        if _cache is not None and now - _cache[0] < CACHE_TTL_S:
            return _cache[1]
        data = collect_telemetry()
        _cache = (now, data)
        return data


def _invalidate_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path == "/health":
            self._send(200, {"ok": True})
        elif self.path == "/telemetry":
            try:
                self._send(200, get_telemetry_cached())
            except Exception as exc:
                self._send(500, {"ok": False, "error": str(exc)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if self.path != "/power-limit":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            watts = float(body["watts"])
            index = int(body.get("index", 0))
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._send(400, {"ok": False, "error": f"bad request: {exc}"})
            return
        try:
            result = set_power_limit(index, watts)
            _invalidate_cache()  # next /telemetry must show the new limit
            self._send(200, result)
        except Exception as exc:
            self._send(500, {"ok": False, "error": str(exc)})

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # keep container logs quiet on the 5s polling


if __name__ == "__main__":
    threading.Thread(target=restore_saved_limits, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"gpu-temp-helper listening on :{PORT}", flush=True)
    server.serve_forever()
