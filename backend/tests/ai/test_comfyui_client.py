"""Unit tests for the ComfyUI client (workflow injection + on-prem guard)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ai import comfyui_client
from app.ai.comfyui_client import ComfyUIError, build_workflow, resolve_node
from app.ai.provider_registry import ResolvedProvider
from app.ai.schemas import ProviderKind


def test_build_workflow_injects_only_present_keys():
    template = {
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["38", 0]}},
        "3": {"class_type": "KSampler", "inputs": {"seed": 0, "steps": 20}},
        "78": {"class_type": "LoadImage", "inputs": {"image": "input.png"}},
    }
    inject_map = {
        "prompt": {"node": "6", "input": "text"},
        "seed": {"node": "3", "input": "seed"},
        "image": {"node": "78", "input": "image"},
        "mask": {"node": "99", "input": "image"},  # missing node → skipped
    }
    graph = build_workflow(
        template,
        inject_map,
        {"prompt": "draw a flange", "seed": 42, "image": "src.png", "mask": None},
    )
    assert graph["6"]["inputs"]["text"] == "draw a flange"
    assert graph["3"]["inputs"]["seed"] == 42
    assert graph["78"]["inputs"]["image"] == "src.png"
    # Original template untouched (deep copy).
    assert template["6"]["inputs"]["text"] == ""


def test_build_workflow_skips_missing_node_gracefully():
    template = {"1": {"class_type": "X", "inputs": {}}}
    graph = build_workflow(
        template, {"prompt": {"node": "404", "input": "text"}}, {"prompt": "hi"}
    )
    assert graph == {"1": {"class_type": "X", "inputs": {}}}


def test_build_workflow_controlnet_image_optional():
    """A workflow declaring controlnet_image in inject_map must stay valid
    whether or not the caller supplies it — the ControlNet path is opt-in."""
    template = {
        "96": {"class_type": "ControlNetApplyAdvanced", "inputs": {"strength": 0.6}},
        "97": {"class_type": "LoadImage", "inputs": {"image": "placeholder.png"}},
    }
    inject_map = {
        "controlnet_image": {"node": "97", "input": "image"},
        "controlnet_strength": {"node": "96", "input": "strength"},
    }

    without = build_workflow(template, inject_map, {})
    assert without["97"]["inputs"]["image"] == "placeholder.png"  # untouched
    assert without["96"]["inputs"]["strength"] == 0.6  # untouched

    with_cn = build_workflow(
        template, inject_map, {"controlnet_image": "edges.png", "controlnet_strength": 0.8}
    )
    assert with_cn["97"]["inputs"]["image"] == "edges.png"
    assert with_cn["96"]["inputs"]["strength"] == 0.8


def test_resolve_node_rejects_non_local(monkeypatch):
    monkeypatch.setattr(
        comfyui_client.provider_registry,
        "select_instance",
        lambda *a, **k: ResolvedProvider(
            kind=ProviderKind.COMFYUI, base_url="http://cloud:8188", is_local=False
        ),
    )
    with pytest.raises(ComfyUIError):
        resolve_node()


def test_resolve_node_requires_base_url(monkeypatch):
    monkeypatch.setattr(
        comfyui_client.provider_registry,
        "select_instance",
        lambda *a, **k: ResolvedProvider(
            kind=ProviderKind.COMFYUI, base_url="", is_local=True
        ),
    )
    with pytest.raises(ComfyUIError):
        resolve_node()


def test_resolve_node_accepts_local(monkeypatch):
    monkeypatch.setattr(
        comfyui_client.provider_registry,
        "select_instance",
        lambda *a, **k: ResolvedProvider(
            kind=ProviderKind.COMFYUI, base_url="http://comfyui:8188", is_local=True
        ),
    )
    node = resolve_node()
    assert node.base_url == "http://comfyui:8188"


def _repo_root() -> Path:
    # tests/ai/this_file → backend/ → repo root
    return Path(__file__).resolve().parents[3]


def test_builtin_workflow_templates_are_consistent():
    """Every shipped template's inject_map must reference nodes in its graph."""
    wf_dir = _repo_root() / "aiagent/config/comfyui_workflows"
    files = list(wf_dir.glob("*.json"))
    assert files, "no builtin workflow templates found"
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data.get("key") and data.get("graph") and data.get("inject_map")
        graph = data["graph"]
        for key, target in data["inject_map"].items():
            node = str(target["node"])
            assert node in graph, f"{path.name}: inject '{key}' → unknown node {node}"
