"""Unit tests for ComfyUI model discovery + auto-resolution."""

from __future__ import annotations

from app.ai.comfyui_models import auto_resolve_models, available_models


def _object_info(unets, clips, vaes, ckpts=None):
    def node(key, opts):
        return {"input": {"required": {key: [opts]}}}

    info = {
        "UNETLoader": node("unet_name", unets),
        "CLIPLoader": node("clip_name", clips),
        "VAELoader": node("vae_name", vaes),
    }
    if ckpts is not None:
        info["CheckpointLoaderSimple"] = node("ckpt_name", ckpts)
    return info


def test_available_models_groups_by_category():
    info = _object_info(["a.safetensors"], ["b.safetensors"], ["c.safetensors"])
    avail = available_models(info)
    assert avail["diffusion_models"] == {"a.safetensors"}
    assert avail["text_encoders"] == {"b.safetensors"}
    assert avail["vae"] == {"c.safetensors"}


def test_keeps_exact_installed_model():
    info = _object_info(["qwen_image_edit_2511_fp8mixed.safetensors"], ["x"], ["y"])
    graph = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen_image_edit_2511_fp8mixed.safetensors"}},
    }
    out, missing = auto_resolve_models(graph, info)
    assert not missing
    assert out["1"]["inputs"]["unet_name"] == "qwen_image_edit_2511_fp8mixed.safetensors"


def test_substitutes_best_token_match():
    # Requested file absent; an equivalent Qwen edit unet is installed.
    info = _object_info(["qwen_image_edit_2511_bf16.safetensors"], ["x"], ["y"])
    graph = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen_image_edit_fp8_e4m3fn.safetensors"}},
    }
    out, missing = auto_resolve_models(graph, info)
    assert not missing
    assert out["1"]["inputs"]["unet_name"] == "qwen_image_edit_2511_bf16.safetensors"


def test_reports_missing_when_no_match():
    info = _object_info(["flux2_dev.safetensors"], ["x"], ["y"])
    graph = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen_image_edit.safetensors"}},
    }
    out, missing = auto_resolve_models(graph, info)
    assert len(missing) == 1
    assert missing[0].category == "diffusion_models"
    assert missing[0].requested == "qwen_image_edit.safetensors"


def test_ignores_wired_inputs():
    # A model input wired from another node (list ref) must be left alone.
    info = _object_info(["a.safetensors"], ["x"], ["y"])
    graph = {
        "2": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": "z.safetensors", "strength_model": 1.0}},
    }
    out, missing = auto_resolve_models(graph, info)
    # lora 'z' not installed and no loras advertised → reported missing,
    # but the wired 'model' input is untouched.
    assert out["2"]["inputs"]["model"] == ["1", 0]


def test_controlnet_category_exact_match():
    info = _object_info(["a.safetensors"], ["x"], ["y"])
    info["ControlNetLoader"] = {
        "input": {"required": {"control_net_name": [["qwen-image/instantx/union.safetensors"]]}}
    }
    graph = {
        "9": {"class_type": "ControlNetLoader", "inputs": {"control_net_name": "qwen-image/instantx/union.safetensors"}},
    }
    out, missing = auto_resolve_models(graph, info)
    assert not missing
    assert out["9"]["inputs"]["control_net_name"] == "qwen-image/instantx/union.safetensors"


def test_controlnet_category_substitutes_by_basename_tokens():
    # Installed file lives in a subfolder the requested literal doesn't know about.
    info = _object_info(["a.safetensors"], ["x"], ["y"])
    info["ControlNetLoader"] = {
        "input": {"required": {"control_net_name": [["qwen-image/instantx/Qwen-Image-InstantX-ControlNet-Union.safetensors"]]}}
    }
    graph = {
        "9": {"class_type": "ControlNetLoader", "inputs": {"control_net_name": "Qwen-Image-InstantX-ControlNet-Union.safetensors"}},
    }
    out, missing = auto_resolve_models(graph, info)
    assert not missing
    assert out["9"]["inputs"]["control_net_name"] == (
        "qwen-image/instantx/Qwen-Image-InstantX-ControlNet-Union.safetensors"
    )


def test_controlnet_category_reports_missing():
    info = _object_info(["a.safetensors"], ["x"], ["y"])
    info["ControlNetLoader"] = {"input": {"required": {"control_net_name": [[]]}}}
    graph = {
        "9": {"class_type": "ControlNetLoader", "inputs": {"control_net_name": "some_other_controlnet.safetensors"}},
    }
    out, missing = auto_resolve_models(graph, info)
    assert len(missing) == 1
    assert missing[0].category == "controlnet"
