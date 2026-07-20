# Generative 2D vectorizer — Qwen3-VL LoRA fine-tune (stage 2)

Teaches Qwen3-VL to emit our CAD primitive DSL directly from a drawing image
(the Zero-To-CAD paradigm applied to 2D DXF). This replaces pixel tracing —
which fragments into thousands of short segments — with a model that outputs
clean primitives. The honest entity gate (`make cad-candidate-gate`) still
decides whether the result beats classical CV before anything ships.

## Pipeline

1. **Data (stage 1, done):** `make cad-vlm-sft` reshapes the `(image, CadIR)`
   corpus into `cad-dataset-out/vlm-sft/{train,val,holdout}.jsonl` — image →
   isotropic-0..1000 primitive DSL. Currently 866 / 128 / 114.
2. **Train (this dir):** LoRA-fine-tune `Qwen/Qwen3-VL-2B-Instruct` on the SFT
   set (`qwen3vl_lora_sft.yaml`).
3. **Inference hybrid (next):** the model proposes clean primitives; classical
   CV snaps each to the source ink for pixel precision (kills both
   fragmentation and the VLM's coarse localization).

## GPU constraint (read first)

The RTX 3090 (24 GB) is held by the production `qwen3-vl:32b` in ollama
(~18 GB). A LoRA fine-tune needs ~8-12 GB, so training and production VLM
inference cannot share the card. **Free the GPU for the training window:**

```bash
# 1. Stop the production VLM (digitize text falls back to tesseract meanwhile)
sudo systemctl stop ollama            # or: pkill -f 'ollama'
nvidia-smi                            # confirm the card is (mostly) free

# 2. Build the trainer image once
cd infra && docker compose -f docker-compose.yml -f docker-compose.prod.yml \
    --profile vlm-finetune build vlm-finetune

# 3. Run training (writes the LoRA adapter to cad-dataset-out/vlm-sft/out/)
make cad-vlm-train                    # ~a few hours for 3 epochs on 866 samples

# 4. Restart the production VLM
sudo systemctl start ollama
```

## After training

- The LoRA adapter lands in `cad-dataset-out/vlm-sft/out/qwen3vl-cad-lora/`.
- Merge + export to GGUF to serve via ollama, or serve the adapter via vLLM,
  then wire a `GenerativeVectorizer` recognizer that calls it and feeds the
  parsed DSL through the same `_consolidated` + coverage verifier + gate as the
  other backends. Promote only if it beats CV on the entity gate.

## Notes

- `template: qwen3_vl` — if the installed LLaMA-Factory predates Qwen3-VL,
  pin a newer commit or use `qwen2_vl` with the matching base model.
- 866 samples is a proof-of-concept size; regenerate the profile corpus larger
  (`make cad-corpus-generate` with a higher `--count`) before a serious run.
- Consider starting from `ADSKAILab/Zero-To-CAD-Qwen3-VL-2B` (already
  CAD-pretrained) instead of the base instruct model — set `model_name_or_path`.
