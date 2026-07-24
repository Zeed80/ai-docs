"""Vision-model conveniences: llama.cpp mmproj auto-download + vLLM model marker."""

import pytest

from app.ai.providers import llamacpp_manager as lc
from app.ai.providers import vllm_manager as vm


def test_vllm_path_translation_backend_to_server():
    # Local vLLM download dir → the server's /models mount.
    assert vm._vllm_backend_to_server_path("/vllm-models/foo/model.safetensors") == (
        "/models/foo/model.safetensors"
    )
    # HuggingFace repo id passes through untouched.
    assert vm._vllm_backend_to_server_path("Qwen/Qwen3-VL-8B-Instruct") == (
        "Qwen/Qwen3-VL-8B-Instruct"
    )
    # A shared llama.cpp GGUF is mounted at the same path in both containers.
    assert vm._vllm_backend_to_server_path("/llamacpp-models/x.gguf") == "/llamacpp-models/x.gguf"


@pytest.mark.asyncio
async def test_mmproj_autodownload_fetches_projector(monkeypatch, tmp_path):
    monkeypatch.setattr(lc, "_MODELS_DIR", tmp_path)

    async def _fake_list(repo_id, source):
        return ["model-Q4_K_M.gguf", "mmproj-F16.gguf", "mmproj-Q8_0.gguf", "README.md"]

    monkeypatch.setattr(lc, "_list_repo_gguf_files", _fake_list)

    grabbed = {}

    async def _fake_download(dl_id, url, dest_name, source="huggingface", repo_id=None):
        grabbed["dest"] = dest_name
        grabbed["url"] = url

    monkeypatch.setattr(lc, "_download_model", _fake_download)

    await lc._maybe_download_mmproj("owner/repo", "huggingface", "model-Q4_K_M.gguf")

    # The F16 projector is preferred over Q8_0.
    assert grabbed["dest"] == "mmproj-F16.gguf"
    assert grabbed["url"].endswith("/owner/repo/resolve/main/mmproj-F16.gguf")


@pytest.mark.asyncio
async def test_mmproj_autodownload_noop_for_text_model(monkeypatch, tmp_path):
    monkeypatch.setattr(lc, "_MODELS_DIR", tmp_path)

    async def _fake_list(repo_id, source):
        return ["model-Q4_K_M.gguf", "README.md"]  # no mmproj → text model

    monkeypatch.setattr(lc, "_list_repo_gguf_files", _fake_list)

    called = {"n": 0}

    async def _fake_download(*a, **k):
        called["n"] += 1

    monkeypatch.setattr(lc, "_download_model", _fake_download)

    await lc._maybe_download_mmproj("owner/repo", "huggingface", "model-Q4_K_M.gguf")
    # Downloading the mmproj file itself must not recurse either.
    await lc._maybe_download_mmproj("owner/repo", "huggingface", "mmproj-F16.gguf")

    assert called["n"] == 0
