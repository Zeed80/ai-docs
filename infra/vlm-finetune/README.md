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

## First training run (2026-07-21) — honest result

- **Data:** 2784/387/337 SFT pairs (web-dxf + profile-corpus-large + web-step).
- **Train:** LoRA rank-32 on Qwen3-VL-2B, 3 epochs (~2.9 h). train_loss
  0.86 -> 0.088, **eval_loss 0.052** (in-distribution synthetic val — strong,
  no overfit).
- **Inference:** clean primitives, NO fragmentation (the core "куча отрезков"
  complaint is gone). On the real shaft: 28 lines + 2 circles (vs 9 lines
  zero-shot) — more structure, but incomplete/imprecise out-of-distribution.
- **Entity gate (6 real-QCAD holdout, the honest test):** raw VLM
  **F1 = 0.000** vs CV baseline 0.186 — positions too coarse for the exact
  0.0025 tolerance, and one sheet emitted nothing. **Does NOT beat CV yet.**

### Why, and what's next
The paradigm produces clean structure but not pixel-exact geometry (the known
VLM-localization wall). To make it useful:
1. **Hybrid:** VLM proposes clean primitives -> classical CV snaps each to the
   source ink for pixel precision (the intended production path; raw VLM alone
   fails the exact gate).
2. **Data:** 2784 synthetic-heavy samples is proof-of-concept scale; grow the
   corpus and make it more like real scans (domain gap on the QCAD holdout).
3. Promote only if the hybrid beats CV on `make cad-candidate-gate`.

Reproduce: `infer.py` (visual), `dump_holdout_dsl.py` + `score_holdout.py`
(entity F1). Adapter: `cad-dataset-out/vlm-sft/out/qwen3vl-cad-lora/`.

## Second run (2026-07-21) — ADSK base + 2x data — still doesn't beat CV

- **Base:** ADSKAILab/Zero-To-CAD-Qwen3-VL-2B (CAD-pretrained). **Data:** 5969/
  808/731 (added profile-corpus-xl). **eval_loss 0.038** (better than 0.052) —
  synthetic fit improved.
- **Entity gate (real-QCAD holdout):** raw VLM **F1 = 0.000** again. Best
  primitives are ~20 px off (the bolt circle: VLM (509,489,r127) vs GT
  (512,512,r146)) — far beyond the exact 2.56 px tolerance; one sheet emits
  nothing; polygon structure partly wrong.
- **Hybrid (VLM proposes -> CV supplies precision):** F1 **0.011** (recovers 1
  entity) — still far below CV's 0.186. The VLM structure is too sparse/coarse
  to select CV's primitives without losing most of CV's recall.

### Honest conclusion
Two thorough iterations (instruct base then CAD-pretrained base, 2x-4x data)
both hit entity F1 ~0 on real drawings. The synthetic fit keeps improving
(eval_loss 0.052 -> 0.038) but does NOT transfer to real-drawing pixel accuracy
— a synthetic-to-real DOMAIN GAP, not a recipe problem. More synthetic data
won't cross it. **CV stays production (F1 0.186).** The generative branch is a
validated pipeline + paradigm (clean, fragmentation-free output) but needs REAL
labeled data — the B5 active-learning flywheel (accepted human edits from
production) — to progress. Not shipped.
