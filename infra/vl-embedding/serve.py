#!/usr/bin/env python3
"""Multimodal embedding service (Qwen3-VL-Embedding, Apache-2.0).

Why a sidecar and not the existing embedding path: the project's embeddings go
through Ollama, and Ollama's ``/api/embed`` accepts an ``images`` field and
silently ignores it — the vector comes back describing the text alone. There is
no image search to be had that way, so the model that can actually do it runs
here, in its own torch/CUDA stack (same arrangement as technical-vectorizer).

The single property everything downstream depends on: text and images land in
ONE vector space. A photo of a tool and the catalog line describing it are
comparable directly, so one Qdrant collection serves photo-search, text-search
and "more like this" without a second index.

Endpoints
  GET  /health   liveness; never loads the model (it is the healthcheck)
  GET  /info     model name, dimension, device, whether weights are resident
  POST /embed    embed a batch of inputs: text, image, or text+image

VRAM: this stand has one 3090 shared with the LLMs, so the weights are dropped
after ``VL_EMBEDDING_IDLE_UNLOAD_SECONDS`` of inactivity and re-loaded on the
next request (a few seconds from page cache) rather than parked for good.
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
import os
import threading
import time
from typing import Any

import torch
from fastapi import FastAPI, HTTPException
from PIL import Image
from pydantic import BaseModel, Field, model_validator

app = FastAPI(title="vl-embedding")
logger = logging.getLogger("vl_embedding")
logging.basicConfig(level=logging.INFO)

_MODEL_NAME = os.environ.get("VL_EMBEDDING_MODEL", "Qwen/Qwen3-VL-Embedding-2B")
_IDLE_UNLOAD = float(os.environ.get("VL_EMBEDDING_IDLE_UNLOAD_SECONDS", "600"))
# 2048 native. Truncating is supported by the model (MRL) and halves both the
# Qdrant footprint and the search cost; keep it a deployment choice, and note
# that CHANGING it invalidates an existing collection — the dimension is part
# of the collection's schema, so a change means a re-index, not a restart.
_DIM = int(os.environ.get("VL_EMBEDDING_DIM", "0")) or None
_MAX_BATCH = int(os.environ.get("VL_EMBEDDING_MAX_BATCH", "16"))
_MAX_PIXELS = int(os.environ.get("VL_EMBEDDING_MAX_PIXELS", "1024"))

_model: Any = None
_lock = threading.Lock()
_last_used = 0.0
_device = "cuda" if torch.cuda.is_available() else "cpu"
# Set when CUDA had no room and the weights went to system RAM instead. The
# card on this stand is shared with the LLMs, and a 27B model resident in VRAM
# leaves nothing for a 2B encoder — refusing the search outright would make
# picture search look broken exactly when someone is using the assistant.
# CPU is slow (seconds per image), not wrong, and /info says which one is live.
_fell_back_to_cpu = False


class EmbedItem(BaseModel):
    """One thing to embed: text, an image, or both together."""

    text: str | None = None
    # base64 (with or without a data: prefix). Bytes, not a URL: the service is
    # internal-only and must never fetch from the network on someone's input.
    image_base64: str | None = None

    @model_validator(mode="after")
    def _needs_content(self) -> "EmbedItem":
        if not self.text and not self.image_base64:
            raise ValueError("item must carry text, image_base64, or both")
        return self


class EmbedRequest(BaseModel):
    items: list[EmbedItem] = Field(..., min_length=1)
    # The model is instruction-aware; the caller states the retrieval intent.
    prompt: str | None = None


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]
    dim: int
    model: str
    elapsed_ms: int


def _decode_image(raw: str) -> Image.Image:
    payload = raw.split(",", 1)[1] if raw.startswith("data:") else raw
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"image_base64 is not valid base64: {exc}")
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:  # noqa: BLE001 — any decoder failure is a bad request
        raise HTTPException(status_code=400, detail=f"cannot decode image: {exc}")
    image = image.convert("RGB")
    # A catalog page render is ~1240x1670; the encoder gains nothing from that
    # resolution and pays for every pixel in tokens.
    if max(image.size) > _MAX_PIXELS:
        scale = _MAX_PIXELS / max(image.size)
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.LANCZOS,
        )
    return image


def _build(device: str):
    from sentence_transformers import SentenceTransformer

    kwargs: dict[str, Any] = {"device": device}
    if device == "cuda":
        kwargs["model_kwargs"] = {"torch_dtype": torch.float16}
    if _DIM:
        kwargs["truncate_dim"] = _DIM
    return SentenceTransformer(_MODEL_NAME, **kwargs)


def _load_model():
    global _model, _fell_back_to_cpu, _last_used
    # Loading IS using: without this the idle watcher measured the gap since
    # the epoch and threw the weights away ~30 s after they finished loading —
    # every /info paid a full re-load.
    _last_used = time.time()
    if _model is not None:
        return _model

    started = time.time()
    logger.info("vl_embedding_loading model=%s device=%s", _MODEL_NAME, _device)
    try:
        _model = _build(_device)
        _fell_back_to_cpu = False
    except torch.OutOfMemoryError:
        if _device != "cuda":
            raise
        logger.warning("vl_embedding_cuda_oom_falling_back_to_cpu")
        torch.cuda.empty_cache()
        _model = _build("cpu")
        _fell_back_to_cpu = True
    _last_used = time.time()
    logger.info(
        "vl_embedding_loaded seconds=%.1f device=%s",
        time.time() - started,
        "cpu" if _fell_back_to_cpu else _device,
    )
    return _model


def _unload_model() -> None:
    global _model, _fell_back_to_cpu
    with _lock:
        if _model is None:
            return
        _model = None
        _fell_back_to_cpu = False
        if _device == "cuda":
            torch.cuda.empty_cache()
        logger.info("vl_embedding_unloaded reason=idle")


def _idle_watch() -> None:
    while True:
        time.sleep(30)
        if _model is not None and _IDLE_UNLOAD > 0 and time.time() - _last_used > _IDLE_UNLOAD:
            _unload_model()


threading.Thread(target=_idle_watch, daemon=True).start()


@app.get("/health")
def health() -> dict:
    """Liveness only — deliberately does NOT load the model.

    The healthcheck runs every 30 s; loading weights here would pin 4.5 GB of
    VRAM forever and defeat the idle unload.
    """
    return {
        "status": "ok",
        "model": _MODEL_NAME,
        "loaded": _model is not None,
        "device": "cpu" if _fell_back_to_cpu else _device,
    }


@app.get("/info")
def info() -> dict:
    model = _load_model()
    return {
        "model": _MODEL_NAME,
        "dim": model.get_sentence_embedding_dimension(),
        # The device actually in use, not the one we wanted — a caller timing
        # a bulk index needs to know it is on CPU.
        "device": "cpu" if _fell_back_to_cpu else _device,
        "cuda_available": torch.cuda.is_available(),
        "max_batch": _MAX_BATCH,
        "idle_unload_seconds": _IDLE_UNLOAD,
    }


@app.post("/embed", response_model=EmbedResponse)
def embed(request: EmbedRequest) -> EmbedResponse:
    global _last_used, _model, _fell_back_to_cpu
    if len(request.items) > _MAX_BATCH:
        raise HTTPException(
            status_code=400,
            detail=f"batch of {len(request.items)} exceeds max_batch={_MAX_BATCH}",
        )

    inputs: list[Any] = []
    for item in request.items:
        image = _decode_image(item.image_base64) if item.image_base64 else None
        if image is not None and item.text:
            inputs.append({"text": item.text, "image": image})
        elif image is not None:
            inputs.append({"image": image})
        else:
            inputs.append(item.text)

    started = time.time()
    # One request at a time: a single GPU, and concurrent encodes only queue up
    # behind each other while multiplying peak VRAM.
    with _lock:
        model = _load_model()
        kwargs: dict[str, Any] = {"batch_size": len(inputs), "convert_to_numpy": True}
        if request.prompt:
            kwargs["prompt"] = request.prompt
        try:
            vectors = model.encode(inputs, **kwargs)
        except torch.OutOfMemoryError:
            # Ran out mid-encode (another process grew): drop to CPU and answer
            # slowly rather than fail the person's search.
            logger.warning("vl_embedding_oom_during_encode_retrying_on_cpu")
            _model = None
            torch.cuda.empty_cache()
            _model = _build("cpu")
            _fell_back_to_cpu = True
            vectors = _model.encode(inputs, **kwargs)
        _last_used = time.time()

    return EmbedResponse(
        embeddings=[[float(x) for x in vector] for vector in vectors],
        dim=int(vectors.shape[1]),
        model=_MODEL_NAME,
        elapsed_ms=int((time.time() - started) * 1000),
    )
