/**
 * Подписи для раздела «Модели» — единственная копия.
 *
 * Раньше провайдеры подписывались двумя словарями в одном файле
 * (PROVIDER_LABELS на три локальных и PROVIDER_DISPLAY на двадцать семь), и в
 * разных местах экрана использовались разные, из-за чего один и тот же
 * провайдер назывался по-разному.
 */

import type { Modality, ThinkingLevel } from "./types";

const PROVIDER_DISPLAY: Record<string, string> = {
  ollama: "Ollama",
  llamacpp: "llama.cpp",
  vllm: "vLLM",
  openai_compatible: "OpenAI-совм.",
  lmstudio: "LM Studio",
  comfyui: "ComfyUI",
  anthropic: "Anthropic",
  openrouter: "OpenRouter",
  deepseek: "DeepSeek",
  gemini: "Gemini",
  openai: "OpenAI",
  ollama_cloud: "Ollama Cloud",
  moonshot: "Kimi (Moonshot)",
  minimax: "MiniMax",
  dashscope: "Qwen (DashScope)",
  mistral: "Mistral",
  groq: "Groq",
  together: "Together",
  fireworks: "Fireworks",
  xai: "xAI (Grok)",
  cohere: "Cohere",
  perplexity: "Perplexity",
  deepinfra: "DeepInfra",
  cerebras: "Cerebras",
  sambanova: "SambaNova",
  nebius: "Nebius",
  novita: "Novita",
  hyperbolic: "Hyperbolic",
};

export const providerLabel = (kind: string): string =>
  PROVIDER_DISPLAY[kind] ?? kind;

export const MODALITY_LABEL: Record<Modality | string, string> = {
  text: "текст",
  vision: "изображения",
  audio: "звук",
  embedding: "векторизация",
  rerank: "переранжирование",
  tool_calling: "вызов инструментов",
};

export const modalityLabel = (m: string): string => MODALITY_LABEL[m] ?? m;

export const THINKING_LEVEL_LABEL: Record<ThinkingLevel | string, string> = {
  low: "низкое",
  medium: "среднее",
  high: "высокое",
};

export const AVAILABILITY_LABEL: Record<string, string> = {
  available: "есть на узле",
  missing: "модели нет ни на одном узле",
  unknown: "узел не ответил",
};

/**
 * Локальные kind. Список повторяет `_LOCAL_PROVIDER_KINDS` из
 * backend/app/ai/task_routing.py — это фронтовая копия серверного знания, и
 * она уже расходилась с ним. Использовать только для подсказок в интерфейсе;
 * решение о допустимости модели принимает бэкенд.
 */
export const LOCAL_PROVIDER_KINDS = [
  "ollama",
  "llamacpp",
  "vllm",
  "openai_compatible",
  "lmstudio",
] as const;

export const isLocalProvider = (kind: string): boolean =>
  (LOCAL_PROVIDER_KINDS as readonly string[]).includes(kind);
