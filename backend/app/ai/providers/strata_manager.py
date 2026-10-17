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
INSTALL_ENV_FILE_NAME = "aiw-install.env"

# Model families Strata's setup offers (setup.py FAMILIES + OWN_PATH_FAMILIES,
# 0.1.45). "kind" is how setup lays a family out: "pack" — GSQ-RCO files with
# an expert pack and the low-RAM mode; "budget" — Unsloth's GGUF read in
# place, a RAM budget of experts and the rest from the SSD
# (--resident-budget-gib); "own" — a model with its own engine path
# (Qwen3.6-35B-A3B): no pack, no MTP download, every expert it can in VRAM and
# a RAM copy of the rest. "tag" is setup's prefix of the pack, config and
# model-folder names; the entrypoint derives it as "<family>-" too.
# catalog_key is the model_registry.yaml entry slots point at while that
# family is loaded: Unsloth's files are the same model as the original, so
# they share its catalog entry.
FAMILIES: dict[str, dict] = {
    "qwen": {
        "title": "Qwen3.8-Flash-Next",
        "about": "исходная модель, 125B MoE",
        "tag": "",
        "kind": "pack",
        "catalog_key": "strata_qwen3_8_flash_next",
        "served_name": "qwen3.8-flash-next",
    },
    "swift": {
        "title": "Swift 1.5",
        "about": "дообучение Flash-Next: рассуждает на ~63% короче, отвечает в ~1,8 раза раньше",
        "tag": "swift-",
        "kind": "pack",
        "catalog_key": "strata_swift_1_5",
        "served_name": "swift-1.5",
    },
    "coder": {
        "title": "Qwen3.8-Flash-Next Coder",
        "about": "половина экспертов (код, инструменты, картинки): ~32 ГБ ОЗУ, быстрее, слабее вне кода",
        "tag": "coder-",
        "kind": "pack",
        "catalog_key": "strata_qwen3_8_flash_next_coder",
        "served_name": "qwen3.8-flash-next-coder",
    },
    "unsloth": {
        "title": "Qwen3.8-Flash-Next (Unsloth)",
        "about": "4-битные файлы Unsloth: точнее, но при ОЗУ меньше ~80 ГБ часть экспертов читается с SSD",
        "tag": "unsloth-",
        "kind": "budget",
        "catalog_key": "strata_qwen3_8_flash_next",
        "served_name": "qwen3.8-flash-next",
    },
    "qwen36": {
        "title": "Qwen3.6-35B-A3B",
        "about": "35B MoE, 3B активных: почти целиком на видеокарте, быстрая; читает картинки",
        "tag": "qwen36-",
        "kind": "own",
        "catalog_key": "strata_qwen3_6_35b",
        "served_name": "qwen3.6-35b-a3b",
        # Setup knows the family from 0.1.44; pictures for it came in 0.1.45.
        "min_version": "0.1.45",
    },
}
DEFAULT_FAMILY = "qwen"

# The sizes (setup.py MODELS / OWN_PATH_FAMILIES[*]["models"]). experts_gb is
# what must sit in RAM+GPU expert cache (setup's arena_gb; for Qwen3.6 the
# file itself); ram_gb is setup's own "comfortable" RAM figure. On a PC below
# it a "pack" family switches to the low-RAM mode by itself (the GPU holds the
# most-used experts, the rest is copied into RAM), which is how IQ3_S runs
# with a 24 GB card and ~60 GB RAM.
_SIZES: list[tuple[str, str, dict]] = [
    ("qwen", "Q2_0", {"label": "Q2_0 — самая быстрая", "download_gb": 66.4, "experts_gb": 34.0, "ram_gb": 48}),
    ("qwen", "IQ2_XS", {"label": "IQ2_XS — рекомендуемая", "download_gb": 68.0, "experts_gb": 35.5, "ram_gb": 48}),
    ("qwen", "IQ3_XXS", {"label": "IQ3_XXS — точнее, медленнее", "download_gb": 75.8, "experts_gb": 42.9, "ram_gb": 60}),
    ("qwen", "IQ3_S", {"label": "IQ3_S — лучшее качество", "download_gb": 83.6, "experts_gb": 50.3, "ram_gb": 62}),
    ("swift", "IQ2_XS", {"label": "IQ2_XS — быстрая", "download_gb": 68.0, "experts_gb": 35.5, "ram_gb": 48}),
    ("swift", "IQ3_XXS", {"label": "IQ3_XXS — точнее", "download_gb": 75.8, "experts_gb": 42.9, "ram_gb": 60}),
    ("swift", "IQ3_S", {"label": "IQ3_S — лучшее качество", "download_gb": 83.6, "experts_gb": 50.3, "ram_gb": 62}),
    ("coder", "IQ1_M", {"label": "IQ1_M — единственный размер", "download_gb": 58.4, "experts_gb": 23.4, "ram_gb": 32}),
    ("unsloth", "UD-IQ4_XS", {"label": "UD-IQ4_XS — ~4 бита", "download_gb": 93.7, "experts_gb": 59.5, "ram_gb": 48}),
    ("unsloth", "UD-Q4_K_XL", {"label": "UD-Q4_K_XL — 4 бита, K-кванты", "download_gb": 111.3, "experts_gb": 77.0, "ram_gb": 48}),
    ("qwen36", "UD-Q4_K_XL", {"label": "UD-Q4_K_XL — 4 бита, самая лёгкая", "download_gb": 22.9, "experts_gb": 21.0, "ram_gb": 32}),
    ("qwen36", "UD-Q6_K_XL", {"label": "UD-Q6_K_XL — 6 бит, ближе к полной", "download_gb": 32.6, "experts_gb": 30.0, "ram_gb": 44}),
    ("qwen36", "Q8_0", {"label": "Q8_0 — 8 бит, ближе всего к полной", "download_gb": 37.8, "experts_gb": 35.0, "ram_gb": 52}),
]  # fmt: skip


def variant_id(family: str, model: str) -> str:
    """Setup's name of a family+size ("IQ3_S", "qwen36-UD-Q4_K_XL"): model folder, pack, config."""
    return FAMILIES[family]["tag"] + model


# Keyed by variant id. The original family's ids are the bare size names, so
# everything written before families existed ("IQ2_XS") keeps its meaning.
QUANTS: dict[str, dict] = {
    variant_id(fam, model): {**meta, "family": fam, "model": model} for fam, model, meta in _SIZES
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
# setup.py: LOW_RAM_HEADROOM_GB (RAM beside the experts for the OS, the
# engine's other buffers and the server) and UNSLOTH_RAM_LEFT_GB (beside an
# Unsloth file's RAM budget, which also feeds the file cache it reads through).
LOW_RAM_HEADROOM_GB = 10
BUDGET_RAM_LEFT_GB = 24


def resolve_variant(family: str | None, model: str) -> str:
    """The variant id for an API's (family, model); a bare id is accepted as is."""
    if family is None:
        if model in QUANTS:
            return model
        family = DEFAULT_FAMILY
    if family not in FAMILIES:
        raise StrataError(f"Неизвестное семейство моделей Strata: {family}")
    vid = variant_id(family, model)
    if vid not in QUANTS:
        raise StrataError(f"Неизвестное квантование: {model} ({FAMILIES[family]['title']})")
    return vid


def catalog_key(vid: str) -> str:
    return FAMILIES[QUANTS[vid]["family"]]["catalog_key"]


def catalog_keys() -> set[str]:
    return {f["catalog_key"] for f in FAMILIES.values()}


def served_name(vid: str) -> str:
    return FAMILIES[QUANTS[vid]["family"]]["served_name"]


def check_version(vid: str, version: str | None) -> None:
    """Refuse a variant this Strata version cannot set up (setup would fail at its family)."""
    from app.ai.providers.strata_lifecycle import version_tuple

    need = FAMILIES[QUANTS[vid]["family"]].get("min_version")
    if need and version and version_tuple(version) < version_tuple(need):
        raise StrataError(
            f"{FAMILIES[QUANTS[vid]['family']]['title']} работает со Strata {need} и новее, "
            f"установлена {version}. Сначала обновите Strata."
        )


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
    # GB of RAM Strata must leave to the rest of the server (0: no limit). It
    # becomes the container's memory limit and the expert placement setup is
    # told to use (ram_plan).
    "ram_reserve_gb": 0,
}
RESERVE_CHOICES = (0, 4, 8, 12, 16, 24, 32)


class StrataError(RuntimeError):
    pass


# ── Desired settings (aiw.env on the shared volume) ─────────────────────────


def data_dir() -> Path:
    return Path(os.environ.get("STRATA_DATA_DIR", settings.strata_data_dir))


def _env_path() -> Path:
    return data_dir() / ENV_FILE_NAME


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return values


def read_desired() -> dict:
    values = _read_env_file(_env_path())
    family = values.get("FAMILY", DEFAULT_FAMILY)
    model = values.get("MODEL", DEFAULT_QUANT)
    vid = variant_id(family, model) if family in FAMILIES else model
    if vid not in QUANTS:
        vid = DEFAULT_QUANT
    try:
        context = int(values.get("CONTEXT", DEFAULT_CONTEXT))
    except ValueError:
        context = DEFAULT_CONTEXT
    return {
        "id": vid,
        "family": QUANTS[vid]["family"],
        "model": QUANTS[vid]["model"],
        "context": context,
        "vision": values.get("VISION", "yes") != "no",
        "reinstall_pending": values.get("REINSTALL") == "1",
        # The container only downloads/sets up the quant and exits; it does
        # not serve and does not take the GPU from Ollama.
        "install_only": values.get("INSTALL_ONLY") == "1",
        "low_ram": values.get("LOW_RAM", "auto"),
    }


def _env_lines(vid: str, context: int, vision: bool, ram: dict | None) -> list[str]:
    if vid not in QUANTS:
        raise StrataError(f"Неизвестное квантование: {vid}")
    if context not in CONTEXTS:
        raise StrataError(f"Контекст должен быть одним из {', '.join(map(str, CONTEXTS))}")
    q = QUANTS[vid]
    ram = ram or ram_plan(vid, context)
    lines = [
        "# Written by the AI workspace backend (strata_manager). Sourced by the",
        "# strata container before Strata's docker-entrypoint.sh.",
        f"FAMILY={q['family']}",
        f"MODEL={q['model']}",
        f"CONTEXT={context}",
        f"VISION={'yes' if vision else 'no'}",
        # setup.py reads the host's RAM, not the container's limit: with a
        # reserve the mode it must pick is spelled out (see ram_plan).
        f"LOW_RAM={ram['low_ram']}",
    ]
    if ram.get("resident_budget_gib"):
        lines.append(f"RESIDENT_BUDGET_GIB={ram['resident_budget_gib']}")
    return lines


def write_desired(
    *,
    model: str,
    context: int,
    vision: bool,
    reinstall: bool,
    install_only: bool = False,
    family: str | None = None,
    ram: dict | None = None,
) -> None:
    vid = resolve_variant(family, model)
    lines = _env_lines(vid, context, vision, ram)
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
    _publish_served(vid)


def write_install_env(vid: str, context: int, vision: bool) -> str:
    """The env file of a side download (strata_lifecycle's installer container).

    A separate file, so a download never changes what the server starts with.
    Returns its path inside the container.
    """
    lines = [*_env_lines(vid, context, vision, None), "INSTALL_ONLY=1"]
    _atomic_write(data_dir() / INSTALL_ENV_FILE_NAME, "\n".join(lines) + "\n")
    return f"/data/{INSTALL_ENV_FILE_NAME}"


def _publish_served(vid: str) -> None:
    """Tell every process (workers too) which model Strata serves: gpu_runtime refuses others."""
    try:
        gpu_runtime.set_strata_model(served_name(vid))
    except Exception as exc:  # noqa: BLE001 — the guard then refuses nothing, as without an owner
        logger.warning("strata_served_model_unpublished", error=str(exc)[:200])


# ── RAM kept for the system ─────────────────────────────────────────────────


def _vram_gb() -> float:
    try:
        return float(os.environ.get("GPU_TOTAL_VRAM_GB") or 24.0)
    except ValueError:
        return 24.0


def ram_plan(
    vid: str,
    context: int,
    *,
    reserve_gb: float | None = None,
    total_gb: float | None = None,
    vram_gb: float | None = None,
) -> dict:
    """How the variant must be set up so Strata stays within RAM − reserve.

    setup.py measures MemTotal (the host's, even in a capped container) and
    sizes the expert placement from it, so a cap alone would get Strata
    OOM-killed at load. With a reserve the placement is decided here with
    setup's own rules (setup.py low_ram_needed / low_ram_resident /
    resident_budget_gib) for the RAM that is left, and the container gets a
    hard memory limit of that size:

    * "off"      — every expert in RAM (the fastest);
    * "resident" — the GPU's cache holds the hottest experts, the rest is
      copied into RAM once (the low-RAM mode);
    * "mmap"     — the rest is read through the OS file cache (slowest:
      measured 2026-10-16, IQ3_XXS 28 tok/s instead of 52);
    * Unsloth files: the RAM budget of experts (--resident-budget-gib);
    * Qwen3.6: no such settings — only a warning when it does not fit.
    """
    q = QUANTS[vid]
    kind = FAMILIES[q["family"]]["kind"]
    total = ram_total_gb() if total_gb is None else total_gb
    reserve = read_runtime()["ram_reserve_gb"] if reserve_gb is None else reserve_gb
    vram = _vram_gb() if vram_gb is None else vram_gb
    limit = round(total - reserve, 1) if (reserve and total) else None
    ram = limit if limit is not None else total
    out = {
        "reserve_gb": reserve,
        "limit_gb": limit,
        "low_ram": "auto",
        "resident_budget_gib": None,
        "mode": "auto",
        "fits": True,
    }
    if ram is None:
        return out
    arena = q["experts_gb"]
    if kind == "own":
        out["mode"] = "own"
        out["fits"] = ram >= q["ram_gb"]
        return out
    if kind == "budget":
        out["mode"] = "budget"
        if limit is not None:
            out["resident_budget_gib"] = max(
                8, min(int(ram - BUDGET_RAM_LEFT_GB), int(arena / 1.0737))
            )
            out["fits"] = ram - BUDGET_RAM_LEFT_GB >= 8
        return out
    # setup.py low_ram_gpu_gb: VRAM minus ~5 GB (dense weights, buffers, a 32K
    # KV cache) minus the KV of a longer context (13 layers × 1056 B/token).
    gpu = max(0.0, min(arena, vram - 5 - max(0, context - 32768) * 13 * 1056 / 1e9))
    if ram >= arena + LOW_RAM_HEADROOM_GB:
        mode = "off"
    elif ram >= arena - gpu + LOW_RAM_HEADROOM_GB:
        mode = "resident"
    else:
        mode = "mmap"
        out["fits"] = ram - 6 + max(0.0, vram - 5) >= arena  # setup.py low_ram_fits
    out["mode"] = mode
    if limit is not None:
        out["low_ram"] = mode
    return out


def config_ram_mode(cfg: dict) -> str | None:
    """The expert placement a run config was set up with (None: unknown/none)."""
    args = list(cfg.get("args") or [])
    if "--resident-budget-gib" in args:
        return "budget"
    if "--resident-experts" in args:
        return "resident"
    if "--mmap-experts" in args:
        return "mmap"
    return "off" if args else None


def ram_setup_needed(vid: str, plan: dict) -> bool:
    """Does the installed config place experts differently than ``plan`` wants?

    A setup pass (no new download) rewrites it. Without a reserve setup's own
    "auto" decides; only a config left in a low-RAM mode by an earlier
    reserve, for a model that now fits RAM whole, is set up again.
    """
    cfg = _installed_config(vid)
    current = config_ram_mode(cfg)
    if current is None:
        return False
    if plan["mode"] == "budget":
        want = plan.get("resident_budget_gib")
        if not want:
            return False
        args = list(cfg.get("args") or [])
        try:
            have = float(args[args.index("--resident-budget-gib") + 1])
        except (ValueError, IndexError):
            return True
        return abs(have - want) >= 1
    if plan["mode"] not in ("off", "resident", "mmap"):
        return False
    if plan["low_ram"] == "auto":
        return current in ("resident", "mmap") and plan["mode"] == "off"
    return current != plan["mode"]


def installed_settings(vid: str) -> dict:
    """Context and images an installed variant was set up with (None: unknown)."""
    cfg = _installed_config(vid)
    args = list(cfg.get("args") or [])
    context = None
    if "--max-context" in args:
        try:
            context = int(args[args.index("--max-context") + 1])
        except (ValueError, IndexError):
            context = None
    vision = None
    if cfg:
        vision = bool(cfg.get("vision")) or "--vision" in args
    return {"context": context, "vision": vision}


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
    ram_reserve_gb: int | None = None,
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
    if ram_reserve_gb is not None:
        if ram_reserve_gb not in RESERVE_CHOICES:
            raise StrataError("Запас ОЗУ: " + ", ".join(f"{c} ГБ" for c in RESERVE_CHOICES))
        total = ram_total_gb()
        if ram_reserve_gb and total and total - ram_reserve_gb < 24:
            raise StrataError(
                f"После запаса {ram_reserve_gb} ГБ Strata останется {total - ram_reserve_gb:.0f} ГБ "
                f"из {total:.0f}: меньше 24 ГБ не хватит ни одной модели"
            )
        runtime["ram_reserve_gb"] = ram_reserve_gb
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
        rows = [
            row for row in r.json() if not any("-retired-" in n for n in row.get("Names") or [])
        ]
        return rows[0] if rows else None


async def _container_action(action: str, timeout: int = 30) -> None:
    c = await _find_container()
    if not c:
        raise StrataError(NOT_INSTALLED)
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


async def log_tail(lines: int = 60, container_id: str | None = None) -> list[str]:
    """The last log lines of the Strata server (or of ``container_id``)."""
    if container_id is None:
        c = await _find_container()
        if not c:
            return []
        container_id = c["Id"]
    async with _docker() as client:
        r = await client.get(
            f"/containers/{container_id}/logs",
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
    runtime = read_runtime()
    active_cfg = _installed_config(desired["id"]) if desired["id"] in installed else {}
    quants = []
    for vid, meta in QUANTS.items():
        plan = ram_plan(vid, desired["context"], reserve_gb=runtime["ram_reserve_gb"], total_gb=ram)
        quants.append(
            {
                "id": vid,
                "family": meta["family"],
                "model": meta["model"],
                "label": meta["label"],
                "download_gb": meta["download_gb"],
                "disk_need_gb": disk_need_gb(vid),
                "experts_gb": meta["experts_gb"],
                "ram_gb": meta["ram_gb"],
                "installed": vid in installed,
                # Model files + the expert pack built from them.
                "size_on_disk_gb": _size_on_disk_gb(vid),
                # Below setup's own figure the model runs in the low-RAM mode
                # (slower: the GPU holds the hot experts, the rest is in RAM).
                "low_ram_mode": plan["mode"] in ("resident", "mmap"),
                "ram_mode": plan["mode"],
                "fits_ram": plan["fits"],
                "catalog_key": catalog_key(vid),
                "min_version": FAMILIES[meta["family"]].get("min_version"),
                "installed_settings": installed_settings(vid) if vid in installed else None,
            }
        )
    owner = gpu_runtime.current_owner()
    from app.ai.providers import strata_lifecycle

    try:
        server = await strata_lifecycle.update_info()
    except Exception as exc:  # noqa: BLE001 — the panel still shows the rest
        server = {"error": str(exc)[:200]}
    try:
        installer = await strata_lifecycle.installer_status()
    except Exception as exc:  # noqa: BLE001
        installer = {"stage": "failed", "variant": None, "log_tail": [str(exc)[:200]]}
    return {
        "owner": owner,
        "server": server,
        "installer": installer,
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
        "runtime": runtime,
        "families": [
            {"family": k, "title": v["title"], "about": v["about"], "catalog_key": v["catalog_key"]}
            for k, v in FAMILIES.items()
        ],
        "served_catalog_key": catalog_key(desired["id"]),
        "ram_plan": ram_plan(
            desired["id"], desired["context"], reserve_gb=runtime["ram_reserve_gb"], total_gb=ram
        ),
        "reserve_choices": list(RESERVE_CHOICES),
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
    """Download + what setup builds next to it.

    "pack": the expert pack (~experts_gb) + MTP/encoder — measured on IQ3_S:
    79 GB of model files, a 49 GB pack, 6.5 GB of MTP. "budget": the GGUF is
    read in place, + MTP/encoder. "own" (Qwen3.6): the file + its encoder.
    """
    meta = QUANTS[model]
    kind = FAMILIES[meta["family"]]["kind"]
    if kind == "own":
        return round(meta["download_gb"] + 1.0, 1)
    if kind == "budget":
        return round(meta["download_gb"] + EXTRA_DOWNLOAD_GB, 1)
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
    from app.ai.providers import strata_lifecycle

    desired = read_desired()
    downloading = await strata_lifecycle.installer_variant()
    if downloading == desired["id"]:
        raise StrataError(
            f"{desired['id']} ещё скачивается. Дождитесь окончания — иначе Strata начнёт "
            "качать те же файлы вторым потоком."
        )
    check_quant_fits_disk(desired["id"])
    container = await _find_container()
    if container is None:
        raise StrataError(NOT_INSTALLED)
    if desired["install_only"]:
        if container.get("State") == "running":
            raise StrataError(
                "Идёт скачивание Strata. Дождитесь окончания (или остановите его) и переключите снова."
            )
        write_desired(
            family=desired["family"],
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
        _publish_served(desired["id"])
        await strata_lifecycle.start_server()
    except Exception:
        gpu_runtime.set_owner(previous or gpu_runtime.OLLAMA)
        raise
    logger.info("strata_started", model=desired["id"], ollama_unloaded=unloaded)
    return unloaded


NOT_INSTALLED = (
    "Strata не установлена: контейнера нет. Установите её кнопкой «Установить Strata» "
    "в «Модели → Провайдеры»."
)


async def delete_quant(model: str) -> float:
    """Remove one quant's files from the volume; returns the GB freed.

    Only what belongs to that quant goes: its model files, the pack setup built
    from them and its run config. The image encoder, the MTP draft layer and
    the expert profile are shared by every quant and stay. The selected quant
    cannot be deleted (the next start would download it again), nor one that
    is being downloaded right now.
    """
    from app.ai.providers import strata_lifecycle

    if model not in QUANTS:
        raise StrataError(f"Неизвестное квантование: {model}")
    desired = read_desired()
    if model == desired["id"]:
        raise StrataError(f"{model} выбрана для запуска. Сначала загрузите в Strata другую модель.")
    if await strata_lifecycle.installer_variant() == model:
        raise StrataError(f"{model} сейчас скачивается. Остановите скачивание, затем удаляйте.")
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


async def install_strata(
    vid: str | None = None, *, context: int | None = None, vision: bool | None = None
) -> str:
    """Download and set up a variant without serving it, beside a running Strata.

    A separate one-shot container (strata_lifecycle's installer) runs Strata's
    setup only, from its own env file, and exits: the server keeps serving
    whatever it serves, and the GPU stays with whoever has it, so an
    hour-long download costs no downtime. Returns the variant id.
    """
    from app.ai.providers import strata_lifecycle

    desired = read_desired()
    vid = vid or desired["id"]
    if vid not in QUANTS:
        raise StrataError(f"Неизвестное квантование: {vid}")
    if vid in installed_quants():
        raise StrataError(f"{vid} уже скачана")
    running = await strata_lifecycle.installer_variant()
    if running:
        raise StrataError(f"Уже скачивается {running}. Дождитесь окончания или остановите его.")
    check_quant_fits_disk(vid)
    container = await _find_container()
    current = await strata_lifecycle.server_version(container) if container else None
    check_version(vid, (current or {}).get("version"))
    if container and container.get("State") == "running" and vid == desired["id"]:
        raise StrataError(
            f"Strata запущена с {vid} и скачивает её сама. Дождитесь, пока она загрузится."
        )
    env_file = write_install_env(
        vid,
        context or desired["context"],
        desired["vision"] if vision is None else vision,
    )
    await strata_lifecycle.run_installer(vid, env_file)
    logger.info("strata_install_started", model=vid)
    return vid


async def stop_strata() -> None:
    """Give the GPU back to Ollama: stop the container (frees VRAM and RAM)."""
    c = await _find_container()
    if c and c.get("State") == "running":
        # Strata ends the engine on SIGTERM; give it time to release pinned RAM.
        await _container_action("stop", timeout=60)
    gpu_runtime.set_owner(gpu_runtime.OLLAMA)
    logger.info("strata_stopped")


async def restart_if_running() -> bool:
    from app.ai.providers import strata_lifecycle

    c = await _find_container()
    if c and c.get("State") == "running":
        if not read_desired()["install_only"]:
            apply_runtime_keys()
        await strata_lifecycle.restart_server()
        return True
    return False


async def reconcile_owner() -> str | None:
    """At startup: the container's state is the truth about who holds the GPU."""
    try:
        c = await _find_container()
    except Exception as exc:  # noqa: BLE001
        logger.warning("strata_reconcile_failed", error=str(exc)[:200])
        return None
    desired = read_desired()
    serving = c and c.get("State") == "running" and not desired["install_only"]
    owner = gpu_runtime.STRATA if serving else gpu_runtime.OLLAMA
    _publish_served(desired["id"])
    try:
        if gpu_runtime.current_owner() != owner:
            gpu_runtime.set_owner(owner)
    except Exception as exc:  # noqa: BLE001
        logger.warning("strata_reconcile_owner_write_failed", error=str(exc)[:200])
        return None
    return owner
