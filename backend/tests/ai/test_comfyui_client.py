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


def test_build_workflow_fallback_injects_prompt_when_map_missing():
    template = {
        "1": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {"prompt": "template prompt"},
        },
        "2": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {"prompt": "negative blur"},
        },
    }

    graph = build_workflow(template, {}, {"prompt": "user edit instruction"})

    assert graph["1"]["inputs"]["prompt"] == "user edit instruction"
    assert graph["2"]["inputs"]["prompt"] == "negative blur"
    assert template["1"]["inputs"]["prompt"] == "template prompt"


def test_build_workflow_replaces_template_prompt_even_when_map_hits_wrong_text_node():
    template = {
        "1": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "template positive prompt"},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "template negative blur"},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "unused side prompt"},
        },
    }

    graph = build_workflow(
        template,
        {
            "prompt": {"node": "3", "input": "text"},
            "negative": {"node": "2", "input": "text"},
        },
        {"prompt": "user prompt", "negative": None},
    )

    assert graph["1"]["inputs"]["text"] == "user prompt"
    assert graph["3"]["inputs"]["text"] == "user prompt"
    assert graph["2"]["inputs"]["text"] == "template negative blur"


def test_build_workflow_normalizes_old_qwen_text_alias_to_prompt():
    template = {
        "76": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {"text": "old imported positive"},
        },
        "77": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {"text": "negative blur"},
        },
    }

    graph = build_workflow(
        template,
        {
            "prompt": {"node": "76", "input": "text"},
            "negative": {"node": "77", "input": "text"},
        },
        {"prompt": "new user prompt", "negative": "new negative"},
    )

    assert graph["76"]["inputs"]["prompt"] == "new user prompt"
    assert "text" not in graph["76"]["inputs"]
    assert graph["77"]["inputs"]["prompt"] == "new negative"
    assert "text" not in graph["77"]["inputs"]


def test_build_workflow_single_flux_text_node_is_positive_even_if_old_map_marks_negative():
    template = {
        "123": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "clip": ["124", 0],
                "text": (
                    "Restore a scanned drawing. Remove blur, noise, artifacts, shadows "
                    "and low quality scan defects while preserving geometry."
                ),
            },
        },
        "124": {
            "class_type": "CLIPLoader",
            "inputs": {"type": "flux2", "clip_name": "mistral_3_small_flux2_bf16.safetensors"},
        },
    }

    graph = build_workflow(
        template,
        {"negative": {"node": "123", "input": "text"}},
        {"prompt": "user edit instruction", "negative": None},
    )

    assert graph["123"]["inputs"]["text"] == "user edit instruction"


def test_build_workflow_injects_generic_prompt_like_text_inputs():
    template = {
        "10": {
            "class_type": "Flux2AdvancedTextEncoder",
            "inputs": {"clip_l": "template prompt", "clip": ["11", 0]},
        },
    }

    graph = build_workflow(template, {}, {"prompt": "new user prompt"})

    assert graph["10"]["inputs"]["clip_l"] == "new user prompt"


def test_build_workflow_ignores_bad_prompt_map_to_sampler_positive():
    template = {
        "3": {
            "class_type": "KSampler",
            "inputs": {"positive": ["111", 0], "negative": ["110", 0], "seed": 1},
        },
        "110": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {"prompt": "", "image1": ["93", 0]},
        },
        "111": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {"prompt": "template positive", "image1": ["93", 0]},
        },
    }

    graph = build_workflow(
        template,
        {
            "prompt": {"node": "3", "input": "positive"},
            "negative": {"node": "110", "input": "prompt"},
        },
        {"prompt": "new user prompt", "negative": None},
    )

    assert graph["3"]["inputs"]["positive"] == ["111", 0]
    assert graph["111"]["inputs"]["prompt"] == "new user prompt"
    assert graph["110"]["inputs"]["prompt"] == ""


def test_build_workflow_detects_qwen_positive_negative_from_graph_links_without_map():
    template = {
        "3": {
            "class_type": "KSampler",
            "inputs": {"positive": ["111", 0], "negative": ["110", 0], "seed": 1},
        },
        "110": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {"prompt": "", "image1": ["93", 0]},
        },
        "111": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {"prompt": "template positive", "image1": ["93", 0]},
        },
    }

    graph = build_workflow(template, {}, {"prompt": "new user prompt", "negative": None})

    assert graph["111"]["inputs"]["prompt"] == "new user prompt"
    assert graph["110"]["inputs"]["prompt"] == ""


def test_build_workflow_detects_qwen_roles_through_intermediate_conditioning_nodes():
    template = {
        "3": {
            "class_type": "KSampler",
            "inputs": {"positive": ["86", 0], "negative": ["87", 0], "seed": 1},
        },
        "76": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {"prompt": "", "image1": ["78", 0]},
        },
        "77": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {"prompt": "", "image1": ["78", 0]},
        },
        "86": {
            "class_type": "FluxKontextMultiReferenceLatentMethod",
            "inputs": {"conditioning": ["76", 0]},
        },
        "87": {
            "class_type": "FluxKontextMultiReferenceLatentMethod",
            "inputs": {"conditioning": ["77", 0]},
        },
    }

    graph = build_workflow(template, {}, {"prompt": "new user prompt", "negative": None})

    assert graph["76"]["inputs"]["prompt"] == "new user prompt"
    assert graph["77"]["inputs"]["prompt"] == ""


def test_build_workflow_does_not_treat_sampler_or_kontext_nodes_as_text_nodes():
    template = {
        "3": {
            "class_type": "KSampler",
            "inputs": {"positive": ["86", 0], "negative": ["87", 0], "seed": 1},
        },
        "76": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {"prompt": "", "image1": ["78", 0]},
        },
        "77": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {"prompt": "", "image1": ["78", 0]},
        },
        "86": {
            "class_type": "FluxKontextMultiReferenceLatentMethod",
            "inputs": {"conditioning": ["76", 0]},
        },
        "87": {
            "class_type": "FluxKontextMultiReferenceLatentMethod",
            "inputs": {"conditioning": ["77", 0]},
        },
    }

    graph = build_workflow(
        template,
        {"prompt": {"node": "3", "input": "positive"}},
        {"prompt": "new user prompt", "negative": None},
    )

    assert graph["3"]["inputs"]["positive"] == ["86", 0]
    assert "prompt" not in graph["3"]["inputs"]
    assert "prompt" not in graph["86"]["inputs"]
    assert graph["76"]["inputs"]["prompt"] == "new user prompt"


def test_build_workflow_ignores_negative_map_when_it_points_to_positive_text_node():
    template = {
        "2": {
            "class_type": "KSampler",
            "inputs": {"positive": ["3", 0], "negative": ["4", 0], "seed": 1},
        },
        "3": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {"prompt": "template positive", "image1": ["13", 0]},
        },
        "4": {
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {"prompt": "negative template"},
        },
    }

    graph = build_workflow(
        template,
        {
            "prompt": {"node": "2", "input": "positive"},
            "negative": {"node": "3", "input": "prompt"},
        },
        {"prompt": "new user prompt", "negative": None},
    )

    assert graph["2"]["inputs"]["positive"] == ["3", 0]
    assert graph["3"]["inputs"]["prompt"] == "new user prompt"
    assert graph["4"]["inputs"]["prompt"] == "negative template"


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


def test_build_workflow_honors_explicit_negative_map_on_single_text_node():
    """A single-CLIPTextEncode template with an explicit negative map entry
    must actually receive the negative text — not silently drop it because
    the graph-wide heuristic only compares roles across >=2 text nodes."""
    template = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "template positive"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "template negative"}},
    }

    graph = build_workflow(
        template,
        {
            "prompt": {"node": "1", "input": "text"},
            "negative": {"node": "2", "input": "text"},
        },
        {"prompt": "user prompt", "negative": "user negative"},
    )

    assert graph["1"]["inputs"]["text"] == "user prompt"
    assert graph["2"]["inputs"]["text"] == "user negative"


def test_build_workflow_drops_negative_safely_with_no_target_node():
    """A single text node with no explicit negative mapping has nowhere to
    put a negative prompt — it must not clobber the positive prompt node."""
    template = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "template prompt"}},
    }

    graph = build_workflow(
        template,
        {"prompt": {"node": "1", "input": "text"}},
        {"prompt": "user prompt", "negative": "blurry, low quality"},
    )

    assert graph["1"]["inputs"]["text"] == "user prompt"


def test_build_workflow_honors_explicit_prompt_map_over_heuristic_guess():
    """An admin-configured prompt target that correctly names a text node
    must be honored, not silently discarded in favor of the heuristic."""
    template = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "template a"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "template b"}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "template c"}},
    }

    graph = build_workflow(
        template,
        {"prompt": {"node": "2", "input": "text"}},
        {"prompt": "user prompt", "negative": None},
    )

    assert graph["2"]["inputs"]["text"] == "user prompt"


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
            targets = target if isinstance(target, list) else [target]
            for item in targets:
                node = str(item["node"])
                assert node in graph, f"{path.name}: inject '{key}' -> unknown node {node}"
