# ADR 003: Dual AI — local Ollama vs cloud API

**Status**: Accepted  
**Date**: 2026-05

## Context

The project processes two categories of AI tasks:

1. **Confidential document processing** (OCR, invoice extraction, classification): involves sensitive financial and corporate data that must not leave the premises.
2. **Reasoning and NL generation** (email drafts, anomaly explanations, agent planning): does not directly handle raw document content; can use external APIs.

Running a single powerful model for both would either:
- Require a large on-prem GPU (expensive), or
- Send confidential document data to external APIs (unacceptable)

## Decision

**Dual AI routing** based on task type:

| Task type | Model | Where |
|-----------|-------|-------|
| OCR, classification, invoice extraction | `gemma4:e4b` (4B params) | Ollama (local) |
| Reasoning, planning, email drafts, NL | `gemma4:26b` or Claude API | Configurable per-task |

Configuration in `.env`:
- `OLLAMA_BASE_URL=http://host-gateway:11434` — local Ollama
- `CLAUDE_API_KEY=...` — optional Claude API fallback
- `REASONING_PROVIDER=ollama|claude` — choose per deployment

The `AIRouter` in `backend/app/ai/router.py` dispatches based on `task_type` metadata.

## Consequences

**Positive:**
- Confidential documents never leave the server
- Reasoning quality can be upgraded without changing document processing
- Works offline (Ollama for all tasks when no internet)

**Negative:**
- Two models to maintain and update
- Different latency profiles: 4B model is fast (~2s), 26B is slower (~15s)
- Context window differs between models (affects long documents)

## Alternatives considered

- **Single model (small)**: insufficient reasoning quality for email drafts and planning
- **Single model (large, cloud)**: data security violation for document processing
- **Single model (large, local)**: requires 24+ GB VRAM; operationally complex
