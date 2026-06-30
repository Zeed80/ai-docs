"""Model discovery + automatic resolution for ComfyUI workflows.

Two jobs:

1. ``available_models`` — read which model files a ComfyUI node actually offers
   (from ``/object_info``), grouped by loader class.

2. ``auto_resolve_models`` — make a workflow graph runnable on *this* server: for
   every model-loader node, keep its filename if installed, otherwise substitute
   the best-matching installed model of the same class (token overlap). Anything
   that can't be resolved is reported as ``missing`` so the caller can tell the
   user exactly which model to download.

This lets builtin templates ship with preferred model names and still run on a
server that has different (but equivalent) files installed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import structlog

logger = structlog.get_logger()

# loader class → (input key holding the filename, logical model category)
MODEL_NODE_INPUTS: dict[str, tuple[str, str]] = {
    "CheckpointLoaderSimple": ("ckpt_name", "checkpoints"),
    "CheckpointLoader": ("ckpt_name", "checkpoints"),
    "UNETLoader": ("unet_name", "diffusion_models"),
    "CLIPLoader": ("clip_name", "text_encoders"),
    "DualCLIPLoader": ("clip_name1", "text_encoders"),
    "VAELoader": ("vae_name", "vae"),
    "LoraLoader": ("lora_name", "loras"),
    "LoraLoaderModelOnly": ("lora_name", "loras"),
    "UpscaleModelLoader": ("model_name", "upscale_models"),
}


@dataclass
class MissingModel:
    node: str
    node_class: str
    category: str
    requested: str

    def as_dict(self) -> dict:
        return {
            "node": self.node,
            "node_class": self.node_class,
            "category": self.category,
            "requested": self.requested,
        }


def _node_input_options(object_info: dict, node_class: str, input_key: str) -> list[str]:
    info = object_info.get(node_class)
    if not info:
        return []
    inp = {**info.get("input", {}).get("required", {}), **info.get("input", {}).get("optional", {})}
    spec = inp.get(input_key)
    if spec and isinstance(spec, list) and spec and isinstance(spec[0], list):
        return [str(x) for x in spec[0]]
    return []


def available_models(object_info: dict) -> dict[str, set[str]]:
    """Map loader category → set of installed model filenames (from object_info)."""
    out: dict[str, set[str]] = {}
    for node_class, (input_key, category) in MODEL_NODE_INPUTS.items():
        opts = _node_input_options(object_info, node_class, input_key)
        if opts:
            out.setdefault(category, set()).update(opts)
    return out


_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def _tokens(name: str) -> set[str]:
    base = name.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    return {t for t in _TOKEN_RE.split(base) if t and not t.isdigit() or len(t) > 1}


def _best_match(requested: str, candidates: set[str]) -> str | None:
    """Pick the installed model sharing the most name-tokens with ``requested``."""
    if requested in candidates:
        return requested
    want = _tokens(requested)
    if not want:
        return None
    scored: list[tuple[int, str]] = []
    for cand in candidates:
        overlap = len(want & _tokens(cand))
        if overlap:
            scored.append((overlap, cand))
    if not scored:
        return None
    # Highest overlap wins; tie-break on the shorter (more specific) name.
    scored.sort(key=lambda s: (-s[0], len(s[1])))
    best_overlap, best = scored[0]
    # Require a meaningful overlap (≥2 shared tokens, or the only token).
    if best_overlap >= 2 or (len(want) == 1 and best_overlap == 1):
        return best
    return None


def auto_resolve_models(
    graph: dict, object_info: dict
) -> tuple[dict, list[MissingModel]]:
    """Substitute installed model filenames into a graph; report unresolved ones.

    Returns the (mutated) graph and a list of MissingModel for loaders whose
    requested file is absent and has no close installed substitute.
    """
    avail = available_models(object_info)
    missing: list[MissingModel] = []
    for node_id, node in graph.items():
        if not isinstance(node, dict):
            continue
        node_class = node.get("class_type")
        spec = MODEL_NODE_INPUTS.get(node_class)
        if not spec:
            continue
        input_key, category = spec
        inputs = node.get("inputs") or {}
        requested = inputs.get(input_key)
        if not isinstance(requested, str):
            continue  # wired from another node, not a literal filename
        candidates = avail.get(category, set())
        if requested in candidates:
            continue
        match = _best_match(requested, candidates)
        if match:
            logger.info(
                "comfyui_model_substituted",
                node=node_id, category=category, requested=requested, used=match,
            )
            inputs[input_key] = match
        else:
            missing.append(
                MissingModel(
                    node=str(node_id), node_class=str(node_class),
                    category=category, requested=requested,
                )
            )
    return graph, missing
