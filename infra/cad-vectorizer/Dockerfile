# Neural CAD vectorizer: PyTorch dual-decoder seq2seq (Drawing2CAD-class,
# trained from scratch on our own synthetic + real-DWG-holdout dataset — no
# published weights exist for this custom command vocabulary). Serves
# GET /health, POST /vectorize over an isolated FastAPI app; the checkpoint
# is mounted read-only, never baked into the image (keeps rebuilds cheap and
# checkpoints swappable without a rebuild).
#
# Volumes expected at runtime:
#   /models              trained checkpoints (read-only; CAD_VECTORIZER_CHECKPOINT picks one)
#   /repo/backend         read-only mount of the app's backend/ (for cad_ir.sequence.COMMANDS only —
#                         see serve.py's fake-parent-package import, no heavy backend deps needed)

FROM nvidia/cuda:12.6.3-runtime-ubuntu24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt \
    --extra-index-url https://download.pytorch.org/whl/cu126

COPY model.py dataset.py primitive_dataset.py primitive_model.py \
    sheet_layout_dataset.py sheet_layout_model.py \
    evidence_dataset.py evidence_model.py \
    directional_dataset.py directional_model.py directional_decode.py \
    edge_verifier.py serve.py .

RUN groupadd --gid 10001 cadvec \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin cadvec \
    && mkdir -p /models \
    && chown -R cadvec:cadvec /app /models
USER 10001:10001

ENV CAD_VECTORIZER_CHECKPOINT=/models/best.pt
ENV CAD_PRIMITIVE_CHECKPOINT=/models/primitive-best.pt
ENV CAD_SHEET_LAYOUT_CHECKPOINT=/models/sheet-layout-best.pt
ENV CAD_EVIDENCE_CHECKPOINT=/models/evidence-best.pt
ENV CAD_DIRECTIONAL_CHECKPOINT=/models/directional-best.pt
ENV CAD_EDGE_VERIFIER_CHECKPOINT=/models/edge-verifier-best.pt
ENV REPO_BACKEND=/repo/backend
ENV PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

EXPOSE 8090
CMD ["python3", "-m", "uvicorn", "serve:app", "--host", "0.0.0.0", "--port", "8090"]
