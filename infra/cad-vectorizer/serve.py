#!/usr/bin/env python3
"""FastAPI inference server for the neural CAD vectorizer.

POST /vectorize takes a binarized ink PNG (same contract as
``cad_recognize.cv.CvRecognizer.recognize`` — uint8, 255=ink) and returns a
list of CAD IR entity dicts, greedy-decoded and rescaled back to the input's
own resolution. It does NOT decide truth: every entity comes back with
``origin="neural"``/``assurance="inferred"`` — the caller's verifier
(``cad_recognize/verify.py``) scores it against the source raster exactly
like the CV backend, and arbitration/promotion to higher assurance happens
there, never inside this service.

Runs in its own container (GPU, isolated deps) and is looked up like any
other provider node — see ``cad_recognize/neural.py`` for the client side.
"""

from __future__ import annotations

import io
import os
import pathlib
import sys

import numpy as np
import torch
from fastapi import FastAPI, File, HTTPException, UploadFile
from PIL import Image
from pydantic import BaseModel

from model import IMG_SIZE, CadVectorizerModel

app = FastAPI(title="cad-vectorizer")

_MODEL: CadVectorizerModel | None = None
_DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
_CHECKPOINT = pathlib.Path(os.environ.get("CAD_VECTORIZER_CHECKPOINT", "/models/best.pt"))


class EntityOut(BaseModel):
    type: str
    line_class: str
    width_class: str
    confidence: float
    origin: str = "neural"
    assurance: str = "inferred"
    p1: dict | None = None
    p2: dict | None = None
    center: dict | None = None
    radius: float | None = None
    start_angle: float | None = None
    end_angle: float | None = None
    points: list[dict] | None = None
    closed: bool | None = None


class VectorizeResponse(BaseModel):
    entities: list[EntityOut]
    model_step: int | None = None


def _load_model() -> CadVectorizerModel:
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    model = CadVectorizerModel().to(_DEVICE)
    if _CHECKPOINT.exists():
        state = torch.load(_CHECKPOINT, map_location=_DEVICE)
        model.load_state_dict(state["model"])
        app.state.model_step = state.get("step")
    else:
        app.state.model_step = None
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
        "checkpoint_loaded": _CHECKPOINT.exists(),
    }


_LC_NAMES = ("contour", "axis", "dim", "hatch", "hidden", "thin")
_WC_NAMES = ("main", "thin")


def _rows_to_entities(rows, scale_x: float, scale_y: float) -> list[EntityOut]:
    from app.ai.cad_ir.sequence import COMMANDS  # lazy, see neural.py for the same trick

    out: list[EntityOut] = []
    open_points: list[dict] | None = None
    open_kind: str | None = None
    open_closed = False

    def flush():
        nonlocal open_points, open_kind
        if open_points is None:
            return
        if open_kind == "PLN" and len(open_points) >= 2:
            out.append(EntityOut(
                type="polyline", line_class="contour", width_class="main",
                confidence=0.55, points=open_points, closed=open_closed,
            ))
        elif open_kind == "HAT" and len(open_points) >= 3:
            out.append(EntityOut(
                type="hatch", line_class="hatch", width_class="thin",
                confidence=0.5, points=open_points,
            ))
        open_points = None

    for cmd_idx, params, lc_idx, wc_idx in rows:
        if not 0 <= cmd_idx < len(COMMANDS):
            continue
        cmd = COMMANDS[cmd_idx]
        lc = _LC_NAMES[lc_idx] if 0 <= lc_idx < len(_LC_NAMES) else "contour"
        wc = _WC_NAMES[wc_idx] if 0 <= wc_idx < len(_WC_NAMES) else "main"
        if cmd == "EOS":
            break
        if cmd == "PT":
            if open_points is not None:
                open_points.append({"x": params[0] * scale_x, "y": params[1] * scale_y})
            continue
        flush()
        if cmd == "SEG":
            out.append(EntityOut(
                type="segment", line_class=lc, width_class=wc, confidence=0.6,
                p1={"x": params[0] * scale_x, "y": params[1] * scale_y},
                p2={"x": params[2] * scale_x, "y": params[3] * scale_y},
            ))
        elif cmd == "CIR":
            out.append(EntityOut(
                type="circle", line_class=lc, width_class=wc, confidence=0.6,
                center={"x": params[0] * scale_x, "y": params[1] * scale_y},
                radius=max(params[2] * (scale_x + scale_y) / 2, 1e-3),
            ))
        elif cmd == "ARC":
            out.append(EntityOut(
                type="arc", line_class=lc, width_class=wc, confidence=0.55,
                center={"x": params[0] * scale_x, "y": params[1] * scale_y},
                radius=max(params[2] * (scale_x + scale_y) / 2, 1e-3),
                start_angle=params[3], end_angle=params[4],
            ))
        elif cmd in ("PLN", "HAT"):
            open_points, open_kind, open_closed = [], cmd, params[0] >= 0.5
    flush()
    return out


@app.post("/vectorize", response_model=VectorizeResponse)
async def vectorize(file: UploadFile = File(...)) -> VectorizeResponse:
    content = await file.read()
    try:
        img = Image.open(io.BytesIO(content)).convert("L")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"invalid image: {exc}") from exc

    w0, h0 = img.size
    resized = img.resize((IMG_SIZE, IMG_SIZE), Image.LANCZOS)
    arr = 1.0 - (np.asarray(resized, dtype=np.float32) / 255.0)
    tensor = torch.from_numpy(arr).unsqueeze(0).unsqueeze(0).to(_DEVICE)

    model = _load_model()
    rows = model.generate(tensor, device=_DEVICE)
    entities = _rows_to_entities(rows, w0 / IMG_SIZE, h0 / IMG_SIZE)
    return VectorizeResponse(entities=entities, model_step=getattr(app.state, "model_step", None))


def _bootstrap_backend_path() -> None:
    """Same fake-parent-package trick as train.py — needed only for the
    ``COMMANDS`` tuple import above, nothing heavier."""
    import types

    repo_backend = pathlib.Path(os.environ.get("REPO_BACKEND", "/repo/backend"))
    sys.path.insert(0, str(repo_backend))
    if "app" not in sys.modules:
        pkg = types.ModuleType("app")
        pkg.__path__ = [str(repo_backend / "app")]
        sys.modules["app"] = pkg
    if "app.ai" not in sys.modules:
        pkg = types.ModuleType("app.ai")
        pkg.__path__ = [str(repo_backend / "app" / "ai")]
        sys.modules["app.ai"] = pkg


_bootstrap_backend_path()
