"""vLLM image update: tag normalization + container-recreate payload."""

import pytest

from app.ai.providers import vllm_manager as vm


def test_normalize_image_ref_bare_tag():
    assert vm._normalize_image_ref("v0.25.1") == "vllm/vllm-openai:v0.25.1"
    assert vm._normalize_image_ref(":v0.25.1") == "vllm/vllm-openai:v0.25.1"
    assert vm._normalize_image_ref("  v0.25.1 ") == "vllm/vllm-openai:v0.25.1"


def test_normalize_image_ref_full_ref_passthrough():
    assert vm._normalize_image_ref("vllm/vllm-openai:v0.25.1") == "vllm/vllm-openai:v0.25.1"
    assert vm._normalize_image_ref("mymirror.io/vllm/vllm-openai:v0.25.1") == (
        "mymirror.io/vllm/vllm-openai:v0.25.1"
    )
    # A repo without a tag defaults to :latest.
    assert vm._normalize_image_ref("myrepo/vllm").endswith("/vllm:latest")


def test_normalize_image_ref_rejects_bad_input():
    for bad in ("", "   ", "vllm/vllm openai:v1"):
        with pytest.raises(ValueError):
            vm._normalize_image_ref(bad)


def test_recreate_body_preserves_labels_gpu_and_networks():
    info = {
        "Name": "/infra-vllm-server-1",
        "Config": {
            "Env": ["VLLM_MODEL=Qwen/Qwen3-VL-8B-Instruct", "VLLM_KV_CACHE_DTYPE=fp8"],
            "Cmd": ["/bin/sh", "-c", "exec vllm ..."],
            "Entrypoint": ["/bin/sh", "-c"],
            "Labels": {
                "com.docker.compose.project": "infra",
                "com.docker.compose.service": "vllm-server",
            },
            "Healthcheck": {"Test": ["CMD-SHELL", "curl -sf http://localhost:8000/health"]},
            "Image": "vllm/vllm-openai:v0.22.0",
        },
        "HostConfig": {
            "Binds": ["vllm_models:/models"],
            "DeviceRequests": [{"Driver": "nvidia", "Count": 1, "Capabilities": [["gpu"]]}],
            "NetworkMode": "infra_default",
        },
        "NetworkSettings": {
            "Networks": {
                "infra_default": {"Aliases": ["vllm-server", "abc123def456"]},
            }
        },
    }
    name, body = vm._build_recreate_body(info, "vllm/vllm-openai:v0.25.1", "abc123def456789")

    assert name == "infra-vllm-server-1"
    assert body["Image"] == "vllm/vllm-openai:v0.25.1"  # new image
    # compose labels preserved → container stays compose-managed
    assert body["Labels"]["com.docker.compose.service"] == "vllm-server"
    # GPU device requests preserved
    assert body["HostConfig"]["DeviceRequests"][0]["Driver"] == "nvidia"
    assert body["HostConfig"]["Binds"] == ["vllm_models:/models"]
    # the auto-generated container-id alias is dropped, the stable one kept
    assert body["NetworkingConfig"]["EndpointsConfig"]["infra_default"]["Aliases"] == ["vllm-server"]
    # env carried over verbatim
    assert "VLLM_KV_CACHE_DTYPE=fp8" in body["Env"]
