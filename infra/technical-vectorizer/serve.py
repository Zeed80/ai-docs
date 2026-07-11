#!/usr/bin/env python3
"""FastAPI inference server wrapping the vendored Deep Vectorization of
Technical Drawings line-primitive model (Egiazarian et al., ECCV 2020,
MPL-2.0, github.com/Vahe1994/Deep-Vectorization-of-Technical-Drawings —
vendored under ./vendor, unmodified, LICENSE preserved).

Replaces the from-scratch Drawing2CAD-style seq2seq model
(infra/cad-vectorizer): that model was trained ONLY on synthetic data and
never beat CV on real photos (recall ~0, see project notes). This service
wraps an ALREADY-TRAINED, openly licensed model built specifically for
raster-technical-drawing → vector-primitive recognition — the task class
the project's own research explicitly recommended, not the seq2seq/
command-generation class Drawing2CAD-style models target (those expect
vector input, not raster).

Zero-shot validated live against real test photos (2026-07-11): recall
+3.8..+23.5 points over the CV baseline on all 3 tested files, modest
precision cost. Only the "line" model is served — the companion "curve"
model was tested and found to hurt precision more than it helps recall on
this project's drawings (duplicate noisy Bezier approximations of straight
lines); circles/arcs stay on the CV path for now.

POST /vectorize takes a binarized ink PNG (same contract as
cad_recognize.cv.CvRecognizer.recognize — uint8, 255=ink) and returns line
segments, greedy patch-based detection rescaled back to the input's own
resolution. Never treated as ground truth by the caller — arbitrate against
CV exactly like the model this replaces.
"""

from __future__ import annotations

import io
import logging
import os
import pathlib
import sys
from itertools import product
from typing import List

import numpy as np
import torch
from fastapi import FastAPI, File, HTTPException, UploadFile
from PIL import Image
from pydantic import BaseModel

sys.path.insert(0, str(pathlib.Path(__file__).parent / "vendor"))

from vectorization import load_model  # noqa: E402
from util_files.patchify import patchify  # noqa: E402
from merging.utils.merging_functions import assemble_vector_patches_lines  # noqa: E402
from util_files.geometric import liang_barsky_screen  # noqa: E402

app = FastAPI(title="technical-vectorizer")
logger = logging.getLogger("technical_vectorizer")

_MODEL = None
_DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
_CHECKPOINT = pathlib.Path(os.environ.get("TECHNICAL_VECTORIZER_CHECKPOINT", "/models/model_lines.weights"))
_SPEC = str(
    pathlib.Path(__file__).parent
    / "vendor/vectorization/models/specs/resnet18_blocks3_bn_256__c2h__trans_heads4_feat256_blocks4_ffmaps512__h2o__out512.json"
)
_MODEL_OUTPUT_COUNT = 10
_PATCH_SIZE = 64
_MIN_CONFIDENCE = 0.3
_MIN_WIDTH = 0.5


class SegmentOut(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float
    width: float


class VectorizeResponse(BaseModel):
    segments: List[SegmentOut]
    checkpoint_loaded: bool


def _serialize_state_dict(checkpoint: dict) -> dict:
    """The vendored model's Transformer decoder was renamed after these
    checkpoints were trained (hidden.transformer -> hidden.decoder.transformer)
    — same key migration the upstream notebooks apply before loading."""
    state = checkpoint["model_state_dict"]
    keys = [k for k in state if "hidden.transformer" in k]
    for k in keys:
        new_key = "hidden.decoder.transformer" + k[len("hidden.transformer"):]
        state[new_key] = state[k]
        del state[k]
    return checkpoint


def _load_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    model = load_model(_SPEC).to(_DEVICE)
    if _CHECKPOINT.exists():
        checkpoint = _serialize_state_dict(torch.load(_CHECKPOINT, map_location=_DEVICE))
        model.load_state_dict(checkpoint["model_state_dict"])
        app.state.checkpoint_loaded = True
    else:
        app.state.checkpoint_loaded = False
        logger.warning("technical_vectorizer_checkpoint_missing", extra={"path": str(_CHECKPOINT)})
    model.eval()
    _MODEL = model
    return model


@app.on_event("startup")
def _startup() -> None:
    _load_model()


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "device": str(_DEVICE),
        "checkpoint": str(_CHECKPOINT),
        "checkpoint_loaded": getattr(app.state, "checkpoint_loaded", False),
    }


def _preprocess_patches(patches_rgb: np.ndarray) -> torch.Tensor:
    patch_height, patch_width = patches_rgb.shape[1:3]
    image = torch.as_tensor(patches_rgb).type(torch.float32).reshape(-1, patch_height, patch_width) / 255
    image = 1 - image
    mask = (image > 0).type(torch.float32)
    xs = np.arange(1, patch_width + 1, dtype=np.float32)[None].repeat(patch_height, 0) / patch_width
    ys = np.arange(1, patch_height + 1, dtype=np.float32)[..., None].repeat(patch_width, 1) / patch_height
    xs = torch.from_numpy(xs)[None]
    ys = torch.from_numpy(ys)[None]
    return torch.stack([image, xs * mask, ys * mask], dim=1)


def _split_to_patches(gray: np.ndarray, patch_size: int):
    rgb = gray[..., None].astype(np.float64)
    rgb_t = np.full((rgb.shape[0] + 33, rgb.shape[1] + 33, 1), 255.0)
    rgb_t[: rgb.shape[0], : rgb.shape[1], :] = rgb
    height, width, channels = rgb_t.shape
    patches = patchify(rgb_t, patch_size=(patch_size, patch_size, channels), step=patch_size)
    patches = patches.reshape((-1, patch_size, patch_size, channels))
    height_offsets = np.arange(0, height - patch_size, step=patch_size)
    width_offsets = np.arange(0, width - patch_size, step=patch_size)
    offsets = np.array(list(product(height_offsets, width_offsets)))
    return patches, offsets


def _clip_to_box(y_pred, box_size=(64, 64)):
    width, height = box_size
    bbox = (0, 0, width, height)
    point1, point2 = y_pred[:2], y_pred[2:4]
    try:
        clipped1, clipped2, is_drawn = liang_barsky_screen(point1, point2, bbox)
    except Exception:  # noqa: BLE001 — degenerate line, drop it
        return np.asarray([np.nan] * len(y_pred))
    if clipped1 and clipped2:
        return np.asarray([clipped1, clipped2, y_pred[4:]]).ravel()
    return np.asarray([np.nan] * len(y_pred))


@app.post("/vectorize", response_model=VectorizeResponse)
async def vectorize(file: UploadFile = File(...)) -> VectorizeResponse:
    content = await file.read()
    try:
        img = Image.open(io.BytesIO(content)).convert("L")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"invalid image: {exc}") from exc

    w0, h0 = img.size
    arr = np.asarray(img)
    pad_h = (32 - arr.shape[0] % 32) % 32
    pad_w = (32 - arr.shape[1] % 32) % 32
    padded = np.full((arr.shape[0] + pad_h, arr.shape[1] + pad_w), 255, dtype=np.uint8)
    padded[: arr.shape[0], : arr.shape[1]] = arr

    model = _load_model()
    if not getattr(app.state, "checkpoint_loaded", False):
        raise HTTPException(503, "model checkpoint not loaded")

    try:
        patches_rgb, offsets = _split_to_patches(padded, _PATCH_SIZE)
        patch_images = _preprocess_patches(patches_rgb)
        outputs = []
        for start in range(0, patch_images.shape[0], 400):
            end = min(start + 400, patch_images.shape[0])
            with torch.no_grad():
                out = model(patch_images[start:end].to(_DEVICE).float(), _MODEL_OUTPUT_COUNT)
                outputs.append(out.detach().cpu().numpy())
        patches_vector = np.concatenate(outputs, axis=0) * _PATCH_SIZE

        clipped = np.array([_clip_to_box(row) for row in patches_vector.reshape(-1, 6)])
        clipped = clipped.reshape(-1, _MODEL_OUTPUT_COUNT, 6)
        primitives = assemble_vector_patches_lines(clipped, np.array(offsets)).reshape(-1, 6)
        primitives = primitives[~np.isnan(primitives).any(axis=1)]
        primitives = primitives[primitives[:, 4] > _MIN_CONFIDENCE]
        primitives = primitives[primitives[:, 5] > _MIN_WIDTH]
    except Exception as exc:  # noqa: BLE001 — any model failure must be a clear, loggable 500
        logger.exception("technical_vectorizer_inference_failed")
        raise HTTPException(500, f"model inference failed: {exc}") from exc

    segments = [
        SegmentOut(x1=float(p[0]), y1=float(p[1]), x2=float(p[2]), y2=float(p[3]),
                   confidence=float(min(1.0, max(0.0, p[4]))), width=float(p[5]))
        for p in primitives
    ]
    return VectorizeResponse(segments=segments, checkpoint_loaded=True)
