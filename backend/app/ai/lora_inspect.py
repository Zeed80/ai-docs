"""Inspect a LoRA .safetensors file and check whether it can be continued
(fine-tuned further) on a chosen base model.

Continuing training from an existing LoRA (ours or third-party) only works
when the LoRA matches the base model's architecture AND the training network
dim (rank) equals the LoRA's rank. We read this WITHOUT loading tensors — the
safetensors header (a JSON blob) carries every tensor's shape and an
``__metadata__`` map that ai-toolkit fills with ``ss_base_model_version``
(e.g. ``qwen_image``), ``ss_output_name``, ``training_info`` and ``software``.

Compatibility is layered: a recorded base family that mismatches the chosen
model is a hard error (the run would crash); an unknown family (a bare
third-party LoRA with no metadata) is a warning — we cannot confirm it, the
user proceeds at their own risk and the trainer fails fast if it is wrong.
"""

from __future__ import annotations

import json
import pathlib
import struct

import structlog

logger = structlog.get_logger()

# Map ai-toolkit's ss_base_model_version to our catalog family. Matched by
# substring (lowercased) so minor version suffixes still resolve.
_BASE_VERSION_FAMILY = [
    ("qwen", "qwen"),        # qwen_image, qwen_image_edit, qwen-image-edit-2511
    ("flux2", "flux2"),
    ("flux.2", "flux2"),
    ("flux-2", "flux2"),
    ("klein", "flux2"),
]

_MAX_HEADER_BYTES = 32 * 1024 * 1024  # a LoRA header is KBs; guard against junk


def read_safetensors_header(path: pathlib.Path) -> dict:
    """Return {"tensors": {name: {"shape", "dtype"}}, "metadata": {...}} from
    the safetensors header alone. Raises ValueError on a malformed file."""
    with path.open("rb") as fh:
        raw_len = fh.read(8)
        if len(raw_len) != 8:
            raise ValueError("файл слишком мал для safetensors")
        header_len = struct.unpack("<Q", raw_len)[0]
        if header_len <= 0 or header_len > _MAX_HEADER_BYTES:
            raise ValueError("некорректная длина заголовка safetensors")
        header = json.loads(fh.read(header_len))
    metadata = header.pop("__metadata__", {}) or {}
    return {"tensors": header, "metadata": metadata}


def _detect_family(base_version: str) -> str:
    low = (base_version or "").lower()
    for needle, family in _BASE_VERSION_FAMILY:
        if needle in low:
            return family
    return "unknown"


def _detect_rank(tensors: dict) -> int | None:
    """Rank = the small dim of a LoRA down/A matrix. For a Linear(in,out):
    lora_A/lora_down is (rank, in) → rank = shape[0]; lora_B/lora_up is
    (out, rank) → rank = shape[-1]. Take the most common value."""
    from collections import Counter

    counts: Counter = Counter()
    for name, spec in tensors.items():
        shape = spec.get("shape") or []
        if len(shape) != 2:
            continue
        low = name.lower()
        if "lora_down" in low or ".lora_a" in low:
            counts[int(shape[0])] += 1
        elif "lora_up" in low or ".lora_b" in low:
            counts[int(shape[-1])] += 1
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def inspect_lora(path: pathlib.Path) -> dict:
    """Structured, UI-safe description of a LoRA file. Never raises."""
    try:
        head = read_safetensors_header(path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}

    meta = head["metadata"]
    tensors = head["tensors"]
    base_version = meta.get("ss_base_model_version", "")
    step = None
    try:
        step = json.loads(meta.get("training_info", "{}")).get("step")
    except Exception:  # noqa: BLE001
        pass
    software = ""
    try:
        software = json.loads(meta.get("software", "{}")).get("name", "")
    except Exception:  # noqa: BLE001
        software = meta.get("software", "") if isinstance(meta.get("software"), str) else ""

    return {
        "ok": True,
        "family": _detect_family(base_version),
        "base_model_version": base_version or None,
        "rank": _detect_rank(tensors),
        "step": step,
        "output_name": meta.get("ss_output_name") or None,
        "software": software or None,
        "n_tensors": len(tensors),
        "has_metadata": bool(base_version),
    }


def check_compatibility(info: dict, base_family: str, config_rank: int) -> dict:
    """Compare an inspected LoRA against the chosen base model family and the
    run rank. Levels: ok / warn (unconfirmed) / error (would crash).

    ``suggested_rank`` is the LoRA's own rank — continuation MUST use it, so
    the caller aligns config.rank to it."""
    reasons: list[str] = []
    level = "ok"

    if not info.get("ok"):
        return {"level": "error", "compatible": False,
                "reasons": [f"Не удалось прочитать файл: {info.get('error')}"],
                "suggested_rank": None}

    fam = info.get("family")
    if fam == "unknown":
        level = "warn"
        reasons.append(
            "Не удалось определить базовую модель LoRA (нет метаданных "
            "ai-toolkit). Совместимость не подтверждена — дообучение может "
            "упасть, если LoRA обучалась на другой модели.")
    elif fam != base_family:
        level = "error"
        reasons.append(
            f"LoRA обучена для семейства «{fam}», а выбрана базовая модель "
            f"«{base_family}». Дообучение несовместимо — выберите базовую "
            f"модель того же семейства.")

    rank = info.get("rank")
    if rank and rank != config_rank:
        if level != "error":
            reasons.append(
                f"Rank LoRA — {rank}; он будет использован автоматически "
                f"(значение rank в форме, {config_rank}, игнорируется при "
                "дообучении).")

    if level == "ok" and not reasons:
        reasons.append("Совместима: дообучение продолжит эту LoRA.")

    return {"level": level, "compatible": level != "error",
            "reasons": reasons, "suggested_rank": rank}
