"""Catalog of base models available for LoRA training in the studio.

Single source of truth shared by the API (validation, UI list, ETA) and the
trainer task (ai-toolkit config). Keys are stable identifiers stored in
``LoraTrainingRun.config["base_model"]``.

Families:
- ``qwen``  — Qwen-Image-Edit (arch ``qwen_image_edit_plus``); the measured
  production setup: uint3+ARA quantization (fp8 OOMs the 3090), see project
  memory project-lora-training-run.
- ``flux2`` — FLUX.2 (arch ``flux2`` in ai-toolkit, control images supported
  via ctrl_img_*). klein-4B/9B train on 24GB with quantization; dev is 32B
  and needs a data-center GPU — kept in the catalog with an explicit warning
  so the UI can show (and block) it honestly on this hardware.

``sec_per_step`` drives the pre-run ETA shown to the user; None = unknown
yet (the UI falls back to live speed measured from progress history).
"""

from __future__ import annotations

DEFAULT_BASE_MODEL = "qwen_image_edit_2511"

LORA_BASE_MODELS: dict[str, dict] = {
    "qwen_image_edit_2511": {
        "family": "qwen",
        "arch": "qwen_image_edit_plus",
        "hf": "Qwen/Qwen-Image-Edit-2511",
        "label": "Qwen-Image-Edit 2511 (проверено на этом GPU)",
        "sec_per_step": 30.5,  # measured on the RTX 3090
        "fits_24gb": True,
        "quantize": {
            "quantize": True,
            "qtype": "uint3|ostris/accuracy_recovery_adapters/"
                     "qwen_image_edit_2511_torchao_uint3.safetensors",
            "quantize_te": True,
            "qtype_te": "qfloat8",
        },
    },
    "flux2_klein_9b": {
        "family": "flux2",
        # ai-toolkit registers klein under a size-specific arch (its own
        # text-encoder: Qwen3-8B); plain "flux2" is the 32B dev model.
        "arch": "flux2_klein_9b",
        "hf": "black-forest-labs/FLUX.2-klein-base-9B",
        "label": "FLUX.2 klein 9B",
        "sec_per_step": None,
        "fits_24gb": True,
        # Gated on HF (confirmed live 2026-07-05: 401 GatedRepoError) — needs
        # an HF_TOKEN whose account accepted the license. klein-4B is open.
        "gated": True,
        "quantize": {"quantize": True, "qtype": "qfloat8",
                     "quantize_te": True, "qtype_te": "qfloat8"},
    },
    "flux2_klein_4b": {
        "family": "flux2",
        "arch": "flux2_klein_4b",  # text-encoder Qwen3-4B
        "hf": "black-forest-labs/FLUX.2-klein-base-4B",
        "label": "FLUX.2 klein 4B (быстрее, проще, без токена HF)",
        "sec_per_step": None,
        "fits_24gb": True,
        "gated": False,
        "quantize": {"quantize": True, "qtype": "qfloat8",
                     "quantize_te": True, "qtype_te": "qfloat8"},
    },
    "flux2_dev": {
        "family": "flux2",
        "arch": "flux2",
        "hf": "black-forest-labs/FLUX.2-dev",
        "label": "FLUX.2 dev 32B",
        "sec_per_step": None,
        "fits_24gb": False,
        "gated": True,
        "vram_note": "требует ≥80GB GPU — на текущей карте (24GB) обучение упадёт",
        "quantize": {"quantize": True, "qtype": "qfloat8",
                     "quantize_te": True, "qtype_te": "qfloat8"},
    },
}


HF_GATED_HELP = (
    "Модель {hf} на HuggingFace закрытая (gated). Чтобы обучать на ней:\n"
    "1) на странице https://huggingface.co/{hf} нажмите «Agree and access "
    "repository» под своим аккаунтом HuggingFace;\n"
    "2) задайте токен доступа HuggingFace в Настройки → Модели (🤗 HuggingFace) "
    "— он используется и для загрузки моделей, и для обучения.\n"
    "Модель «FLUX.2 klein 4B» — открытая, её можно обучать без токена."
)


def base_model_info(key: str | None) -> dict:
    return LORA_BASE_MODELS.get(key or DEFAULT_BASE_MODEL,
                                LORA_BASE_MODELS[DEFAULT_BASE_MODEL])


# ── HuggingFace token (gated FLUX.2 models) ──────────────────────────────────
# Reuses the SHARED token configured in Настройки → Модели
# (/api/local-models/tokens, stored in Redis under "llamacpp_tokens" and used
# for gated model downloads across providers) — no separate LoRA token to
# avoid duplication. HF_TOKEN env stays as a legacy fallback only.


def _shared_hf_token() -> str:
    try:
        from app.ai.providers.llamacpp_manager import _load_tokens

        return (_load_tokens() or {}).get("huggingface") or ""
    except Exception:  # noqa: BLE001 — Redis down / import issue → no token
        return ""


def get_hf_token() -> str | None:
    """Resolve the HF token: shared token from Настройки → Модели → HF_TOKEN
    env (legacy fallback). Never raises."""
    shared = _shared_hf_token()
    if shared:
        return shared
    from app.config import settings

    return getattr(settings, "hf_token", None) or None


def hf_token_status() -> dict:
    """UI-safe status: whether a token is configured and where it came from
    (shared settings vs legacy env). Never returns the token itself."""
    token = get_hf_token()
    source = "settings" if _shared_hf_token() else ("env" if token else None)
    return {"configured": bool(token), "source": source}


def eta_hours(base_model: str | None, steps: int) -> float | None:
    sps = base_model_info(base_model).get("sec_per_step")
    return round(steps * sps / 3600, 1) if sps else None
