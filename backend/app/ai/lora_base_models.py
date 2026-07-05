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
    "2) задайте токен доступа HuggingFace в настройках студии (Настройки → "
    "ComfyUI → Токен HuggingFace) — он хранится в зашифрованном виде.\n"
    "Модель «FLUX.2 klein 4B» — открытая, её можно обучать без токена."
)


def base_model_info(key: str | None) -> dict:
    return LORA_BASE_MODELS.get(key or DEFAULT_BASE_MODEL,
                                LORA_BASE_MODELS[DEFAULT_BASE_MODEL])


# ── HuggingFace token (gated FLUX.2 models) ──────────────────────────────────
# Stored ENCRYPTED in Redis (same pattern as the Telegram bot token: Fernet on
# app_secret_key via secret_store) and configured through the GUI — NOT the
# .env. The HF_TOKEN env var stays as a legacy fallback only.
_HF_TOKEN_REDIS_KEY = "lora:config:hf_token"


def get_hf_token() -> str | None:
    """Resolve the HF token. Priority: encrypted value set through the UI
    (Redis) → HF_TOKEN env (legacy fallback). Never raises."""
    try:
        from app.utils.redis_client import get_sync_redis
        from app.utils.secret_store import decrypt

        raw = get_sync_redis().get(_HF_TOKEN_REDIS_KEY)
        if raw:
            dec = decrypt(raw)
            if dec:
                return dec
    except Exception:  # noqa: BLE001 — Redis down → fall back to env
        pass
    from app.config import settings

    return getattr(settings, "hf_token", None) or None


def set_hf_token(token: str) -> None:
    """Persist the HF token encrypted in Redis (empty string clears it)."""
    from app.utils.redis_client import get_sync_redis
    from app.utils.secret_store import encrypt

    get_sync_redis().set(_HF_TOKEN_REDIS_KEY, encrypt(token) if token else "")


def hf_token_status() -> dict:
    """UI-safe status: whether a token is configured, a masked preview, and
    where it came from (so the .env fallback is visible)."""
    from app.config import settings
    from app.utils.secret_store import mask

    token = get_hf_token()
    stored_in_ui = False
    try:
        from app.utils.redis_client import get_sync_redis
        from app.utils.secret_store import decrypt

        stored_in_ui = bool(decrypt(get_sync_redis().get(_HF_TOKEN_REDIS_KEY) or ""))
    except Exception:  # noqa: BLE001
        pass
    source = "settings" if stored_in_ui else ("env" if token else None)
    return {"configured": bool(token),
            "masked": mask(token, 4) if token else "",
            "source": source}


def eta_hours(base_model: str | None, steps: int) -> float | None:
    sps = base_model_info(base_model).get("sec_per_step")
    return round(steps * sps / 3600, 1) if sps else None
