"""Strata server manager — container state, quantization, GPU switch.

Strata (https://github.com/Niko1221/Strata) serves ONE model,
Qwen3.8-Flash-Next (125B MoE), in several quantizations. It runs as the
optional compose service ``strata`` (profile ``strata``) whose entrypoint reads
its setup choices (MODEL, CONTEXT, VISION, ...) from the environment. The
backend never recreates that container: it writes the choices into
``aiw.env`` on the shared ``strata_data`` volume, and the compose command
sources that file before Strata's own entrypoint. Changing the quantization is
therefore "write the file, restart the container"; a quantization that is not
on the volume yet is downloaded by that start (66-84 GB).

The GPU switch lives here too: Strata running ⇔ the GPU belongs to Strata
(``gpu_runtime``). Stopping the container is the "off" — it frees both the
VRAM and the 30-50 GB of experts held in RAM, which an unload through the
server's API would not guarantee for the RAM side.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
from pathlib import Path

import httpx
import structlog

from app.ai import gpu_runtime
from app.config import settings

logger = structlog.get_logger()

_DOCKER_SOCK = "/var/run/docker.sock"
ENV_FILE_NAME = "aiw.env"

# The original model's four GSQ-RCO sizes (Strata setup.py MODELS, family
# "qwen"). experts_gb is what must sit in RAM+GPU expert cache; ram_gb is
# setup's own "comfortable" RAM figure. On a PC below it setup switches to the
# low-RAM mode by itself (the GPU holds the most-used experts, the rest is
# copied into RAM), which is how IQ3_S runs with a 24 GB card and ~60 GB RAM.
QUANTS: dict[str, dict] = {
    "Q2_0": {
        "label": "Q2_0 — самая быстрая",
        "download_gb": 66.4,
        "experts_gb": 34.0,
        "ram_gb": 48,
    },
    "IQ2_XS": {
        "label": "IQ2_XS — рекомендуемая",
        "download_gb": 68.0,
        "experts_gb": 35.5,
        "ram_gb": 48,
    },
    "IQ3_XXS": {
        "label": "IQ3_XXS — точнее, медленнее",
        "download_gb": 75.8,
        "experts_gb": 42.9,
        "ram_gb": 60,
    },
    "IQ3_S": {
        "label": "IQ3_S — лучшее качество",
        "download_gb": 83.6,
        "experts_gb": 50.3,
        "ram_gb": 62,
    },
}
# IQ2_XS: measured 2026-10-06 on the RTX 3090 + 59 GB, ~1.5x IQ3_S's decode
# speed with the same results on known-answer tasks, half the disk (66 GB).
DEFAULT_QUANT = "IQ2_XS"
# 262144 is the model's trained length (no rope scaling); its KV cache takes
# ~3.6 GB more than 128K, which comes out of the GPU's expert cache.
CONTEXTS = (32768, 65536, 131072, 262144)
DEFAULT_CONTEXT = 65536
# The MTP draft layer (~6 GB) and, with images, the encoder (~1 GB) come on
# top of the model download on the first start.
EXTRA_DOWNLOAD_GB = 7.0

_SWITCH_REVISION_KEY = "strata:switch_revision"

# Ollama-like residency: Strata unloads itself after this much idle time (its
# own `idle_unload_s`; the next request loads it again — ~30 s from a warm
# file cache), so ComfyUI can have the card between agent turns. Before a
# reload it asks ComfyUI to free its models (`before_load`) and loads only into
# enough free VRAM (`min_free_vram_mib`), else answers 503 "the GPU is in use
# by another program" instead of starting into a full card.
RUNTIME_FILE_NAME = "aiw-runtime.json"
IDLE_CHOICES = (0, 60, 300, 600, 1800)
# Measured 2026-10-06 (IQ2_XS, 128K, RTX 3090 + 59 GB): "parallel": 2 costs a
# solo request ~0-4% and the GPU ~9% of its expert cache, gives two concurrent
# requests +20% and lets a short request through in 6.6 s instead of waiting
# 24 s behind a 60K-token read. The conversation cache parks up to 4 chats in
# RAM: an agent turn after a foreign request read 0.5 s instead of re-reading
# 60K tokens for 28 s (4 parked chats took 3 GB of the 6 GB budget).
PARALLEL_CHOICES = (1, 2)
CONVERSATION_CACHE_CHOICES = (0, 2048, 4096, 6144, 8192)
CONVERSATION_CACHE_SLOTS = 4
DEFAULT_RUNTIME = {
    "idle_unload_s": 300,
    "free_comfyui": True,
    "min_free_vram_mib": 12000,
    "parallel": 2,
    "conversation_cache_mib": 6144,
}


class StrataError(RuntimeError):
    pass


# ── Desired settings (aiw.env on the shared volume) ─────────────────────────


def data_dir() -> Path:
    return Path(os.environ.get("STRATA_DATA_DIR", settings.strata_data_dir))


def _env_path() -> Path:
    return data_dir() / ENV_FILE_NAME


def read_desired() -> dict:
    values: dict[str, str] = {}
    try:
        for line in _env_path().read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    model = values.get("MODEL", DEFAULT_QUANT)
    try:
        context = int(values.get("CONTEXT", DEFAULT_CONTEXT))
    except ValueError:
        context = DEFAULT_CONTEXT
    return {
        "model": model if model in QUANTS else DEFAULT_QUANT,
        "context": context,
        "vision": values.get("VISION", "yes") != "no",
        "reinstall_pending": values.get("REINSTALL") == "1",
        # The container only downloads/sets up the quant and exits; it does
        # not serve and does not take the GPU from Ollama.
        "install_only": values.get("INSTALL_ONLY") == "1",
    }


def write_desired(
    *, model: str, context: int, vision: bool, reinstall: bool, install_only: bool = False
) -> None:
    if model not in QUANTS:
        raise StrataError(f"Неизвестное квантование: {model}")
    if context not in CONTEXTS:
        raise StrataError(f"Контекст должен быть одним из {', '.join(map(str, CONTEXTS))}")
    lines = [
        "# Written by the AI workspace backend (strata_manager). Sourced by the",
        "# strata container before Strata's docker-entrypoint.sh.",
        "FAMILY=qwen",
        f"MODEL={model}",
        f"CONTEXT={context}",
        f"VISION={'yes' if vision else 'no'}",
    ]
    if reinstall:
        # One-shot: the compose command drops this line before starting setup.
        lines.append("REINSTALL=1")
    if install_only:
        lines.append("INSTALL_ONLY=1")
    path = _env_path()
    if not path.parent.is_dir():
        raise StrataError(
            f"Том Strata не подключён к бэкенду ({path.parent}). Пересоберите стек с сервисом strata."
        )
    _atomic_write(path, "\n".join(lines) + "\n")


def read_runtime() -> dict:
    try:
        stored = json.loads((data_dir() / RUNTIME_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    out = dict(DEFAULT_RUNTIME)
    if isinstance(stored, dict):
        out.update({k: stored[k] for k in DEFAULT_RUNTIME if k in stored})
    return out


def write_runtime(
    *,
    idle_unload_s: int | None = None,
    free_comfyui: bool | None = None,
    parallel: int | None = None,
    conversation_cache_mib: int | None = None,
) -> dict:
    """Change some runtime settings; None leaves a setting as it is."""
    runtime = read_runtime()
    if idle_unload_s is not None:
        if idle_unload_s not in IDLE_CHOICES:
            raise StrataError(f"Простой должен быть одним из {', '.join(map(str, IDLE_CHOICES))} с")
        runtime["idle_unload_s"] = idle_unload_s
    if free_comfyui is not None:
        runtime["free_comfyui"] = bool(free_comfyui)
    if parallel is not None:
        if parallel not in PARALLEL_CHOICES:
            raise StrataError("Параллельных запросов может быть 1 или 2")
        runtime["parallel"] = parallel
    if conversation_cache_mib is not None:
        if conversation_cache_mib not in CONVERSATION_CACHE_CHOICES:
            raise StrataError(
                "Кэш разговоров: "
                + ", ".join(f"{c // 1024} ГБ" for c in CONVERSATION_CACHE_CHOICES)
            )
        runtime["conversation_cache_mib"] = conversation_cache_mib
    _save_runtime(runtime)
    return runtime


def _save_runtime(runtime: dict) -> None:
    """Persist the settings plus the ready-made config keys.

    The container merges ``config_keys`` into the run config on every start
    (see the strata service command), including right after a setup pass that
    rewrote the config — a context change would otherwise drop them.
    """
    path = data_dir() / RUNTIME_FILE_NAME
    if not path.parent.is_dir():
        raise StrataError(f"Том Strata не подключён к бэкенду ({path.parent}).")
    body = {k: runtime[k] for k in DEFAULT_RUNTIME}
    body["config_keys"] = _config_keys(body)
    body["config_args"] = _config_args(body)
    _atomic_write(path, json.dumps(body, indent=1))


def _config_keys(runtime: dict) -> dict:
    """Run-config keys for these settings; None means "remove the key"."""
    idle = int(runtime["idle_unload_s"])
    return {
        "idle_unload_s": idle or None,
        "min_free_vram_mib": int(runtime["min_free_vram_mib"]) if idle else None,
        "before_load": _comfyui_free_command() if idle and runtime["free_comfyui"] else None,
        "parallel": int(runtime["parallel"]) if int(runtime["parallel"]) > 1 else None,
    }


def _config_args(runtime: dict) -> dict:
    """Engine flags in the run config's "args"; None means "remove the flag and its value"."""
    mib = int(runtime["conversation_cache_mib"])
    return {
        "--conversation-cache-mib": str(mib) if mib else None,
        "--conversation-cache-slots": str(CONVERSATION_CACHE_SLOTS) if mib else None,
    }


def merge_run_config(cfg: dict, keys: dict, args: dict) -> dict:
    """The run config with these keys/flags applied (None removes).

    The strata service command applies the same rules from aiw-runtime.json at
    every container start; keep the two in step.
    """
    new = dict(cfg)
    for key, value in keys.items():
        if value is None:
            new.pop(key, None)
        else:
            new[key] = value
    argv = list(new.get("args") or [])
    for flag, value in args.items():
        if flag in argv:
            i = argv.index(flag)
            del argv[i : i + 2]
        if value is not None:
            argv += [flag, value]
    if "args" in new or argv:
        new["args"] = argv
    return new


def _comfyui_free_command() -> list[str]:
    url = settings.comfyui_url.rstrip("/") + "/free"
    return [
        "curl", "-s", "-m", "20", "-X", "POST",
        "-H", "Content-Type: application/json",
        "-d", '{"unload_models": true, "free_memory": true}',
        url,
    ]  # fmt: skip


def apply_runtime_keys() -> list[str]:
    """Write the residency settings into every installed quant's run config.

    Strata reads them at start, so this runs before each container start or
    restart. Returns the quants whose config changed.
    """
    runtime = read_runtime()
    keys = _config_keys(runtime)
    args = _config_args(runtime)
    try:
        _save_runtime(runtime)  # the container re-applies these at every start
    except StrataError as exc:
        logger.warning("strata_runtime_file_unwritable", error=str(exc)[:200])
    changed: list[str] = []
    for model in installed_quants():
        path = data_dir() / "config" / f"strata-{_tag(model)}.json"
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("strata_config_unreadable", path=str(path), error=str(exc)[:200])
            continue
        new = merge_run_config(cfg, keys, args)
        if new != cfg:
            try:
                _atomic_write(path, json.dumps(new, indent=1))
            except StrataError as exc:
                logger.warning("strata_config_unwritable", path=str(path), error=str(exc)[:200])
                continue
            changed.append(model)
    if changed:
        logger.info("strata_runtime_keys_applied", quants=changed, **runtime)
    return changed


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file; a permission problem becomes a readable StrataError.

    The volume is created by the strata container as root, the backend runs as
    appuser: without the group grant in the strata service command this was a
    bare PermissionError — a 500 and "could not save settings" in the panel.
    """
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except PermissionError as exc:
        raise StrataError(
            f"Нет прав на запись в том Strata ({path.parent}). Перезапустите контейнер "
            "strata — он выдаёт бэкенду права на том при старте."
        ) from exc


def _tag(model: str) -> str:
    return model.lower()


def installed_quants() -> list[str]:
    """Quantizations whose setup finished on the volume (their run config exists)."""
    cfg_dir = data_dir() / "config"
    found = []
    for model in QUANTS:
        if (cfg_dir / f"strata-{_tag(model)}.json").is_file():
            found.append(model)
    return found


def _installed_config(model: str) -> dict:
    try:
        return json.loads(
            (data_dir() / "config" / f"strata-{_tag(model)}.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return {}


def _dir_size_gb(path: Path) -> float | None:
    if not path.is_dir():
        return None
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return round(total / 1e9, 1)


def _size_on_disk_gb(model: str) -> float | None:
    parts = [
        _dir_size_gb(data_dir() / "models" / model),
        _dir_size_gb(data_dir() / "packs" / _tag(model)),
    ]
    found = [p for p in parts if p is not None]
    return round(sum(found), 1) if found else None


def disk_free_gb() -> float | None:
    try:
        return round(shutil.disk_usage(data_dir()).free / 1e9, 1)
    except OSError:
        return None


def ram_total_gb() -> float | None:
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / 1024 / 1024, 1)
    except OSError:
        pass
    return None


# ── Docker ──────────────────────────────────────────────────────────────────


def _docker() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=_DOCKER_SOCK), base_url="http://docker", timeout=30.0
    )


async def _find_container() -> dict | None:
    if not os.path.exists(_DOCKER_SOCK):
        return None
    filters = json.dumps({"label": [f"com.docker.compose.service={settings.strata_service_name}"]})
    async with _docker() as client:
        r = await client.get("/containers/json", params={"filters": filters, "all": "true"})
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None


async def _container_action(action: str, timeout: int = 30) -> None:
    c = await _find_container()
    if not c:
        raise StrataError(
            "Контейнер Strata не создан. Один раз выполните на сервере: "
            "docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml "
            "--env-file infra/.env --profile strata up -d strata"
        )
    async with _docker() as client:
        r = await client.post(f"/containers/{c['Id']}/{action}", params={"t": str(timeout)})
        if r.status_code not in (204, 304):
            raise StrataError(f"docker {action}: HTTP {r.status_code} {r.text[:200]}")


def _demux_docker_logs(raw: bytes) -> str:
    """Docker's non-TTY log stream: 8-byte frame headers before each chunk."""
    out = bytearray()
    i = 0
    while i + 8 <= len(raw):
        size = struct.unpack(">I", raw[i + 4 : i + 8])[0]
        out += raw[i + 8 : i + 8 + size]
        i += 8 + size
    if i == 0 and raw:  # TTY container: plain stream
        return raw.decode("utf-8", "replace")
    return out.decode("utf-8", "replace")


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


async def log_tail(lines: int = 60) -> list[str]:
    c = await _find_container()
    if not c:
        return []
    async with _docker() as client:
        r = await client.get(
            f"/containers/{c['Id']}/logs",
            params={"stdout": "1", "stderr": "1", "tail": str(lines)},
        )
        if r.status_code != 200:
            return []
    text = _ANSI.sub("", _demux_docker_logs(r.content))
    # Progress bars redraw with \r: keep the last state of each line.
    return [ln.split("\r")[-1] for ln in text.splitlines() if ln.split("\r")[-1].strip()]


# ── Server ──────────────────────────────────────────────────────────────────


def _base_url() -> str:
    from app.ai.provider_registry import select_instance
    from app.ai.schemas import ProviderKind

    try:
        return select_instance(ProviderKind.STRATA).base_url.rstrip("/").removesuffix("/v1")
    except Exception:  # noqa: BLE001
        return settings.strata_url.rstrip("/")


async def health() -> dict | None:
    """Strata's /health (answered before the API-key gate). None while it is down/loading."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{_base_url()}/health")
            if r.status_code == 200:
                return r.json()
    except Exception:  # noqa: BLE001
        return None
    return None


async def ensure_loaded(*, timeout_s: float = 300.0, poll_s: float = 3.0) -> bool:
    """Wait until Strata's model is loaded; start the load when it is unloaded.

    After ``idle_unload_s`` Strata reloads on the next request, which took 86 s
    live (39 GB of experts into RAM) — longer than the 45 s turn router, so the
    chat turn ended with "model unavailable" two seconds before the model was
    ready (2026-10-07). True when loaded; False when it did not come up in time.
    """
    import asyncio
    import time

    h = await health()
    if h is not None and h.get("loaded", True):
        return True
    from app.ai.provider_registry import select_instance
    from app.ai.schemas import ProviderKind

    headers: dict[str, str] = {}
    try:
        node = select_instance(ProviderKind.STRATA)
        if node.api_key:
            headers["Authorization"] = f"Bearer {node.api_key}"
    except Exception:  # noqa: BLE001 — the load request is best effort, health decides
        pass

    async def _load() -> None:
        # Blocks until loaded; 409 means a request is already loading it.
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                await client.post(f"{_base_url()}/v1/load", headers=headers, json={})
        except Exception as exc:  # noqa: BLE001
            logger.info("strata_load_request_failed", error=str(exc)[:200])

    loader = asyncio.create_task(_load())
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            h = await health()
            if h is not None and h.get("loaded", True):
                return True
            await asyncio.sleep(poll_s)
        return False
    finally:
        if not loader.done():
            loader.cancel()


def _phase(
    container_state: str | None, h: dict | None, logs: list[str], install_only: bool = False
) -> str:
    if container_state is None:
        return "not_created"
    if container_state != "running":
        return "stopped"
    if install_only:
        return "installing"
    if h is not None:
        return "ready" if h.get("loaded", True) else "unloaded"
    tail = "\n".join(logs[-15:]).lower()
    if "download" in tail or "setting up" in tail or "%|" in tail:
        return "installing"
    return "loading"


async def status() -> dict:
    container = None
    docker_error = None
    try:
        container = await _find_container()
    except Exception as exc:  # noqa: BLE001
        docker_error = str(exc)[:200]
    state = container.get("State") if container else None
    desired = read_desired()
    h = await health() if state == "running" and not desired["install_only"] else None
    logs: list[str] = []
    if state == "running" and h is None:
        try:
            logs = await log_tail(40)
        except Exception:  # noqa: BLE001
            logs = []
    installed = installed_quants()
    ram = ram_total_gb()
    active_cfg = _installed_config(desired["model"]) if desired["model"] in installed else {}
    quants = []
    for model, meta in QUANTS.items():
        quants.append(
            {
                "model": model,
                "label": meta["label"],
                "download_gb": meta["download_gb"],
                "disk_need_gb": disk_need_gb(model),
                "experts_gb": meta["experts_gb"],
                "ram_gb": meta["ram_gb"],
                "installed": model in installed,
                # Model files + the expert pack built from them.
                "size_on_disk_gb": _size_on_disk_gb(model),
                # Below setup's own figure the model runs in the low-RAM mode
                # (slower: the GPU holds the hot experts, the rest is in RAM).
                "low_ram_mode": bool(ram and ram < meta["experts_gb"] + 10),
            }
        )
    owner = gpu_runtime.current_owner()
    return {
        "owner": owner,
        "container": state,
        "docker_error": docker_error,
        "phase": _phase(state, h, logs, desired["install_only"]),
        # The GPU is Strata's but its container is down (crash, host reboot):
        # nothing serves the moved slots until the operator switches back.
        "down_while_owner": owner == gpu_runtime.STRATA and state != "running",
        "health": h,
        "desired": desired,
        "installed": installed,
        "installed_context": active_cfg.get("context") or active_cfg.get("max_context"),
        "quants": quants,
        "contexts": list(CONTEXTS),
        "disk_free_gb": disk_free_gb(),
        "ram_total_gb": ram,
        "log_tail": logs[-12:],
        "runtime": read_runtime(),
        "idle_choices": list(IDLE_CHOICES),
        "parallel_choices": list(PARALLEL_CHOICES),
        "conversation_cache_choices": list(CONVERSATION_CACHE_CHOICES),
        "external_url": settings.strata_public_url or None,
        "switch_revision": _get_switch_revision() or ("tasks" if get_switch_tasks() else None),
    }


# ── GPU switch ──────────────────────────────────────────────────────────────


def _get_switch_revision() -> str | None:
    try:
        from app.utils.redis_client import get_sync_redis

        raw = get_sync_redis().get(_SWITCH_REVISION_KEY)
        return (raw.decode() if isinstance(raw, bytes) else raw) or None
    except Exception:  # noqa: BLE001
        return None


def set_switch_revision(revision_id: str | None) -> None:
    from app.utils.redis_client import get_sync_redis

    r = get_sync_redis()
    if revision_id:
        r.set(_SWITCH_REVISION_KEY, revision_id)
    else:
        r.delete(_SWITCH_REVISION_KEY)


def disk_need_gb(model: str) -> float:
    """Download + the expert pack setup builds from it (~experts_gb) + MTP/encoder.

    Measured on IQ3_S: 79 GB of model files, a 49 GB pack, 6.5 GB of MTP.
    """
    meta = QUANTS[model]
    return round(meta["download_gb"] + meta["experts_gb"] + EXTRA_DOWNLOAD_GB, 1)


_SWITCH_TASKS_KEY = "strata:switch_tasks"


def get_switch_tasks() -> dict[str, list[str]]:
    """Original chains of the slot-less tasks the last switch moved to Strata."""
    try:
        from app.utils.redis_client import get_sync_redis

        raw = get_sync_redis().get(_SWITCH_TASKS_KEY)
        value = json.loads(raw) if raw else {}
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def set_switch_tasks(chains: dict[str, list[str]]) -> None:
    from app.utils.redis_client import get_sync_redis

    r = get_sync_redis()
    if chains:
        r.set(_SWITCH_TASKS_KEY, json.dumps(chains))
    else:
        r.delete(_SWITCH_TASKS_KEY)


def check_quant_fits_disk(model: str) -> None:
    if model in installed_quants():
        return
    need = disk_need_gb(model)
    free = disk_free_gb()
    if free is not None and free < need + 20:
        raise StrataError(
            f"Мало места для {model}: нужно ~{need:.0f} ГБ (+20 ГБ запаса), свободно {free:.0f} ГБ"
        )


async def start_strata() -> list[str]:
    """Give the GPU to Strata: block GPU-Ollama, unload its models, start the container.

    The owner flips FIRST so no new Ollama load can sneak in between the unload
    and Strata sizing its expert cache from the free VRAM. Returns the unloaded
    Ollama models.
    """
    desired = read_desired()
    check_quant_fits_disk(desired["model"])
    container = await _find_container()
    if container is None:
        await _container_action("start")  # raises the "create it once" hint
    if desired["install_only"]:
        if container and container.get("State") == "running":
            raise StrataError(
                "Идёт скачивание Strata. Дождитесь окончания (или остановите его) и переключите снова."
            )
        write_desired(
            model=desired["model"],
            context=desired["context"],
            vision=desired["vision"],
            reinstall=desired["reinstall_pending"],
        )
    previous = gpu_runtime.current_owner()
    gpu_runtime.set_owner(gpu_runtime.STRATA)
    try:
        from app.ai.gpu_manager import unload_all_ollama_models

        unloaded = await unload_all_ollama_models(exclude_pinned=False)
        apply_runtime_keys()
        await _container_action("start")
    except Exception:
        gpu_runtime.set_owner(previous or gpu_runtime.OLLAMA)
        raise
    logger.info("strata_started", model=desired["model"], ollama_unloaded=unloaded)
    return unloaded


async def delete_quant(model: str) -> float:
    """Remove one quant's files from the volume; returns the GB freed.

    Only what belongs to that quant goes: its model files, the pack setup built
    from them and its run config. The image encoder, the MTP draft layer and
    the expert profile are shared by every quant and stay. The selected quant
    cannot be deleted (the next start would download it again), nor one that
    is being downloaded right now.
    """
    if model not in QUANTS:
        raise StrataError(f"Неизвестное квантование: {model}")
    desired = read_desired()
    if model == desired["model"]:
        raise StrataError(
            f"{model} выбрана для запуска. Сначала выберите и примените другое квантование."
        )
    targets = [
        data_dir() / "models" / model,
        data_dir() / "packs" / _tag(model),
        data_dir() / "config" / f"strata-{_tag(model)}.json",
    ]
    if not any(t.exists() for t in targets):
        raise StrataError(f"{model} не скачана — удалять нечего")
    freed = _size_on_disk_gb(model) or 0.0
    try:
        for target in targets:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
    except PermissionError as exc:
        raise StrataError(
            f"Нет прав на удаление файлов {model} в томе Strata. Перезапустите контейнер "
            "strata — он выдаёт бэкенду права на том при старте."
        ) from exc
    logger.info("strata_quant_deleted", model=model, freed_gb=freed)
    return freed


async def install_strata() -> None:
    """Download and set up the chosen quant without serving it.

    The container runs Strata's setup only and exits; the GPU stays with
    Ollama the whole time, so an hour-long download costs no downtime.
    """
    desired = read_desired()
    if desired["model"] in installed_quants() and not desired["reinstall_pending"]:
        raise StrataError(f"{desired['model']} уже скачана")
    check_quant_fits_disk(desired["model"])
    container = await _find_container()
    if container and container.get("State") == "running":
        raise StrataError(
            "Strata сейчас запущена. Верните видеокарту Ollama, затем скачивайте — "
            "или нажмите «Применить», и Strata скачает квантование при перезапуске."
        )
    write_desired(
        model=desired["model"],
        context=desired["context"],
        vision=desired["vision"],
        reinstall=desired["reinstall_pending"],
        install_only=True,
    )
    await _container_action("start")
    logger.info("strata_install_started", model=desired["model"])


async def stop_strata() -> None:
    """Give the GPU back to Ollama: stop the container (frees VRAM and RAM)."""
    c = await _find_container()
    if c and c.get("State") == "running":
        # Strata ends the engine on SIGTERM; give it time to release pinned RAM.
        await _container_action("stop", timeout=60)
    gpu_runtime.set_owner(gpu_runtime.OLLAMA)
    logger.info("strata_stopped")


async def restart_if_running() -> bool:
    c = await _find_container()
    if c and c.get("State") == "running":
        if not read_desired()["install_only"]:
            apply_runtime_keys()
        await _container_action("restart", timeout=60)
        return True
    return False


async def reconcile_owner() -> str | None:
    """At startup: the container's state is the truth about who holds the GPU."""
    try:
        c = await _find_container()
    except Exception as exc:  # noqa: BLE001
        logger.warning("strata_reconcile_failed", error=str(exc)[:200])
        return None
    serving = c and c.get("State") == "running" and not read_desired()["install_only"]
    owner = gpu_runtime.STRATA if serving else gpu_runtime.OLLAMA
    try:
        if gpu_runtime.current_owner() != owner:
            gpu_runtime.set_owner(owner)
    except Exception as exc:  # noqa: BLE001
        logger.warning("strata_reconcile_owner_write_failed", error=str(exc)[:200])
        return None
    return owner
