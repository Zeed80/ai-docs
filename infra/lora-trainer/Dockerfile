# LoRA trainer image: ostris/ai-toolkit pinned to a known-good commit, run on
# demand by the celery worker (docker SDK) with GPU passthrough. Heavy
# (~15GB with CUDA torch) — built rarely, only when ai-toolkit is updated.
#
# Volumes expected at runtime (bound by app/tasks/lora_training.py):
#   /lora-data                  datasets + run configs + outputs (shared with worker)
#   /root/.cache/huggingface    base model cache (Qwen-Image-Edit-2511, ~54GB)

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git build-essential libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/ai-toolkit

# Pin for reproducibility; bump deliberately.
ARG AI_TOOLKIT_REF=main
RUN git clone --depth 1 --branch ${AI_TOOLKIT_REF} https://github.com/ostris/ai-toolkit.git . \
    && pip install --no-cache-dir -r requirements.txt \
    # torchaudio is imported by config_modules but missing from requirements
    # (confirmed on the host install, 2026-07-02).
    && pip install --no-cache-dir torchaudio

ENV PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
ENTRYPOINT []
CMD ["python", "run.py", "--help"]
