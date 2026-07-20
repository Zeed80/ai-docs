# Containerized LoRA fine-tune for the generative 2D vectorizer (Qwen3-VL).
# Isolated like infra-lora-trainer: no repo secrets, GPU only. The official
# PyTorch image ships Python 3.11 + torch 2.5.1 + cuda 12.4 (LLaMA-Factory needs
# Python >=3.11, which the plain cuda/ubuntu22.04 base lacked).
FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-devel

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# LLaMA-Factory pulls its compatible stack; then UPGRADE transformers to git —
# Qwen3-VL needs >=4.57, not yet on PyPI at build time. deepspeed/av dropped:
# single-GPU LoRA needs neither.
RUN pip install --no-cache-dir \
    "llamafactory @ git+https://github.com/hiyouga/LLaMA-Factory.git" \
    accelerate peft trl qwen-vl-utils
RUN pip install --no-cache-dir -U "git+https://github.com/huggingface/transformers.git"

WORKDIR /workspace
# Config + dataset registration are mounted at /data; SFT jsonl + images too.
ENTRYPOINT ["llamafactory-cli", "train", "/data/qwen3vl_lora_sft.yaml"]
