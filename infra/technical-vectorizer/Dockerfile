# Technical-drawing line vectorizer: wraps the vendored, openly licensed
# Deep Vectorization of Technical Drawings model (Egiazarian et al., ECCV
# 2020, MPL-2.0 — see vendor/LICENSE) with a small FastAPI service. Old
# torch/CUDA pins (1.7.1 / 11.0) match the published checkpoints exactly —
# do not "modernize" without re-verifying the checkpoint still loads
# (state_dict keys are architecture-version-sensitive).
#
# Volumes expected at runtime:
#   /models   pretrained line-model checkpoint (read-only; TECHNICAL_VECTORIZER_CHECKPOINT picks the file)

FROM pytorch/pytorch:1.7.1-cuda11.0-cudnn8-devel

# The image's baked-in NVIDIA CUDA apt repo entry references a rotated GPG
# key with no matching keyring in this image — harmless to drop since we
# only need apt for a handful of native libs (cairo/rtree), not the CUDA
# toolkit itself (already present via the base image's conda env).
RUN rm -f /etc/apt/sources.list.d/cuda*.list /etc/apt/sources.list.d/nvidia-ml.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        libcairo2-dev pkg-config libspatialindex-dev libgl1-mesa-glx \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vendor ./vendor
COPY serve.py .

RUN groupadd --gid 10001 techvec \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin techvec \
    && mkdir -p /models \
    && chown -R techvec:techvec /app /models
USER 10001:10001

ENV TECHNICAL_VECTORIZER_CHECKPOINT=/models/model_lines.weights

EXPOSE 8091
CMD ["python", "-m", "uvicorn", "serve:app", "--host", "0.0.0.0", "--port", "8091"]
