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

/**
 * Цвет полосы провайдера в графике VRAM.
 * Таблица была продублирована в двух местах экрана моделей — в общей полосе и
 * в GPU-вкладке, — и при добавлении провайдера её правили в одном из них.
 */
export const PROVIDER_BAR_COLOR: Record<string, string> = {
  ollama: "bg-blue-500",
  llamacpp: "bg-emerald-500",
  vllm: "bg-purple-500",
};

export const providerBarColor = (kind: string): string =>
  PROVIDER_BAR_COLOR[kind] ?? "bg-slate-500";

/**
 * Значок группы слотов. Ключи совпадают с `group` из ответа /slots — если на
 * бэкенде появится новая группа, она просто останется без значка, а не
 * исчезнет с экрана (раньше список групп был захардкожен, и слот новой группы
 * не отрисовывался вовсе).
 */
export const GROUP_ICON: Record<string, string> = {
  Документы: "📄",
  Агент: "🤖",
  Поиск: "🔎",
  Оцифровка: "📐",
};

export const groupIcon = (group: string): string => GROUP_ICON[group] ?? "•";

/**
 * Провайдеры, у которых рассуждение можно принудительно выключить. Копия
 * `_THINKING_DISABLE_SUPPORTED_PROVIDERS` из providers_api.py — держим для
 * подсказки в интерфейсе; настоящую проверку делает бэкенд, он же присылает
 * `thinking_disable_supported` в ответе слота.
 */
export const THINKING_DISABLE_SUPPORTED_PROVIDERS = [
  "ollama",
  "llamacpp",
  "vllm",
  "openrouter",
  "ollama_cloud",
  "openai",
  "groq",
  "xai",
  "dashscope",
  "qwen",
  "cerebras",
] as const;

export const providerCanDisableThinking = (kind: string): boolean =>
  (THINKING_DISABLE_SUPPORTED_PROVIDERS as readonly string[]).includes(kind);

/**
 * Названия профилей параметров инференса.
 * Используются и на вкладке параметров, и в телеметрии — где ими же
 * подписываются задачи.
 */
export const PROFILE_LABELS: Record<string, string> = {
  anti_hallucination: "Без галлюцинаций",
  structured_reasoning: "Структ. рассуждение",
  balanced: "Баланс",
  creative: "Творческий",
};
