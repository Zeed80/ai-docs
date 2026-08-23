# Multimodal embedding sidecar: Qwen3-VL-Embedding puts pictures and text into
# ONE vector space, which is the whole point — a person can search the supplier
# catalogs by a photo of a tool, and the same index still answers a text query.
#
# It lives outside the backend image because it needs its own torch/CUDA stack
# (the same reason infra/technical-vectorizer does), and outside Ollama because
# Ollama's embedding API silently ignores `images` — measured on this stand.
#
# Volumes expected at runtime:
#   /models   HuggingFace cache for the weights (~4.5 GB, downloaded once)
FROM pytorch/pytorch:2.8.0-cuda12.9-cudnn9-runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && apt-get clean \
    && find /var/lib/apt/lists -type f -delete

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY serve.py .

RUN groupadd --gid 10001 vlembed \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin vlembed \
    && mkdir -p /models \
    && chown -R vlembed:vlembed /app /models
USER 10001:10001

ENV VL_EMBEDDING_MODEL=Qwen/Qwen3-VL-Embedding-2B \
    HF_HOME=/models \
    VL_EMBEDDING_IDLE_UNLOAD_SECONDS=600

EXPOSE 8093
CMD ["python", "-m", "uvicorn", "serve:app", "--host", "0.0.0.0", "--port", "8093"]
