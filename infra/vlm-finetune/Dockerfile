# Containerized LoRA fine-tune for the generative 2D vectorizer (Qwen3-VL).
# Isolated like infra-lora-trainer: no repo secrets, GPU only.
FROM nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-dev git && rm -rf /var/lib/apt/lists/*

# torch first (cu124). Qwen3-VL needs a bleeding-edge transformers (>=4.57,
# not yet on PyPI at build time) — install it and LLaMA-Factory from source.
RUN pip3 install --no-cache-dir torch==2.5.1 torchvision --index-url https://download.pytorch.org/whl/cu124
RUN pip3 install --no-cache-dir \
    "git+https://github.com/huggingface/transformers.git" \
    "llamafactory[metrics] @ git+https://github.com/hiyouga/LLaMA-Factory.git" \
    accelerate peft trl deepspeed qwen-vl-utils av

WORKDIR /workspace
# Config + dataset registration are mounted at /data; SFT jsonl + images too.
ENTRYPOINT ["llamafactory-cli", "train", "/data/qwen3vl_lora_sft.yaml"]
