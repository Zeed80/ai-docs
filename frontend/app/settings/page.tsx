"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders, mutFetch } from "@/lib/auth";
import { MailboxSection } from "@/components/email/mailbox-settings";
import { EmailTemplatesSection } from "@/components/email/email-templates";

const API = getApiBaseUrl();

// ── Types ─────────────────────────────────────────────────────────────────────

interface OllamaModel {
  name: string;
  size: number;
  parameter_size: string;
  family: string;
  modified_at: string;
}

interface AiConfig {
  model_agent: string;
  model_ocr: string;
  model_ocr_provider: string;
  model_reasoning: string;
  model_reasoning_provider: string;
  model_vlm: string;
  model_vlm_provider: string;
  embedding_model: string;
  reranker_model: string | null;
  verify_model_1: string;
  verify_model_1_provider: string;
  turboquant_enabled: boolean;
  turboquant_kv_cache_dtype: string;
  turboquant_max_model_len: number;
}

interface GgufModel {
  name: string;
  path: string;
  size_bytes: number;
  size_human: string;
  active: boolean;
}

interface RegistryModel {
  name: string;
  provider: string;
  provider_model: string;
  modalities: string[];
  embedding_dimension: number | null;
  distance_metric: string;
  normalize_embeddings: boolean;
  max_input_tokens: number | null;
  supports_batching: boolean;
  capability_source: string;
}

interface EmbeddingProfile {
  model_key: string;
  provider_model: string;
  collection_name: string;
  dimension: number;
  distance_metric: string;
  normalize: boolean;
}

interface EmbeddingStats {
  active_model: string;
  active_collection: string;
  dimension: number;
  counts_by_status: Record<string, number>;
  total: number;
}

interface NtdControlConfig {
  mode: "manual" | "auto";
  updated_by: string | null;
  updated_at: string | null;
}

interface AiConfigStatus {
  ok: boolean;
  ollama_available: boolean;
  installed_models: string[];
  warnings: string[];
}

interface AgentConfig {
  enabled: boolean;
  agent_name: string;
  model: string;
  department_enabled: boolean;
  orchestrator_model: string | null;
  orchestrator_provider: string | null;
  orchestrator_disable_thinking: boolean;
  worker_model: string | null;
  worker_provider: string | null;
  worker_disable_thinking: boolean;
  auditor_model: string | null;
  auditor_provider: string | null;
  auditor_disable_thinking: boolean;
  builder_model: string | null;
  builder_provider: string | null;
  builder_disable_thinking: boolean;
  fast_model: string | null;
  fast_provider: string | null;
  fast_disable_thinking: boolean;
  provider: string;
  fallback_providers: string[];
  prompt_cache_enabled: boolean;
  disable_thinking: boolean;
  ollama_url: string;
  vllm_url: string;
  lmstudio_url: string;
  openai_compatible_url: string;
  backend_url: string;
  temperature: number;
  max_steps: number;
  llm_timeout_seconds: number;
  backend_timeout_seconds: number;
  approval_timeout_seconds: number;
  max_worker_steps: number;
  max_audit_retries: number;
  memory_enabled: boolean;
  audit_enabled: boolean;
  allow_capability_builder: boolean;
  capability_builder_requires_approval: boolean;
  autonomy_mode: string;
  permission_mode: string;
  safe_auto_apply_enabled: boolean;
  max_history_messages: number;
  exposed_skills: string[];
  approval_gates: string[];
  system_prompt: string | null;
  context_compression_enabled: boolean;
  context_compression_threshold: number;
  compression_model: string | null;
  mcp_servers: Array<{
    name: string;
    transport: "stdio" | "http";
    command?: string;
    args?: string[];
    url?: string;
  }>;
}

interface AgentSkill {
  name: string;
  description: string;
  method: string;
  path: string;
  enabled: boolean;
  approval_required: boolean;
  gate_actions?: string[];
}

interface AgentControlPlaneStatus {
  ok: boolean;
  autonomy_mode: string;
  permission_mode: string;
  safe_auto_apply_enabled: boolean;
  protected_settings: string[];
  skills_total: number;
  approval_gates_total: number;
  plugins_total: number;
  plugins_enabled: number;
  tasks_open: number;
  crons_enabled: number;
  memory_facts_total: number;
  mcp_servers_total: number;
  capability_proposals_open: number;
}

interface AgentRuntimeStatus {
  ok: boolean;
  models: {
    provider: string;
    orchestrator_model: string | null;
    worker_model: string | null;
    auditor_model: string | null;
    builder_model: string | null;
    fast_model: string | null;
    compression_model: string | null;
    fallback_providers: string[];
  };
  counters: {
    llm_calls_24h: number;
    tool_calls_24h: number;
    errors_24h: number;
    avg_llm_duration_ms_24h: number | null;
    last_error: string | null;
    last_error_at: string | null;
  };
  memory: {
    enabled: boolean;
    episodic_facts_total: number;
    pinned_facts_total: number;
    memory_facts_total: number;
    graph_nodes_total: number;
    graph_edges_total: number;
    chunks_total: number;
    evidence_total: number;
    embeddings_total: number;
    embeddings_by_status: Record<string, number>;
    active_embedding_model: string | null;
    active_embedding_collection: string | null;
    qdrant_points: number | null;
    last_episodic_at: string | null;
  };
  recent_actions: Array<{
    id: string;
    session_id: string;
    action_type: string;
    tool_name: string | null;
    model_name: string | null;
    duration_ms: number | null;
    error: string | null;
    created_at: string;
  }>;
}

interface AgentConfigProposal {
  id: string;
  setting_path: string;
  proposed_value: unknown;
  current_value: unknown;
  reason: string;
  risk_level: string;
  protected: boolean;
  status: string;
  requested_by: string;
  decided_by: string | null;
  decided_at: string | null;
  decision_comment: string | null;
  created_at: string;
}

interface CapabilityProposal {
  id: string;
  title: string;
  missing_capability: string;
  reason: string;
  suggested_artifact: string;
  status: string;
  risk_level: string;
  sandbox_status: string;
  test_status: string;
  audit_status: string;
  draft: Record<string, unknown>;
  rollback_plan: string[] | null;
  requested_by: string;
  decided_by?: string | null;
  decision_comment?: string | null;
  created_at: string;
}

interface AgentTask {
  id: string;
  objective: string;
  description: string | null;
  role: string;
  status: string;
  output: string | null;
  created_at: string;
}

interface AgentTeam {
  id: string;
  name: string;
  purpose: string | null;
  status: string;
  created_at: string;
}

interface AgentCron {
  id: string;
  schedule: string;
  prompt: string;
  description: string | null;
  enabled: boolean;
  last_run_at: string | null;
  run_count: number;
  created_at: string;
}

interface AgentPlugin {
  id: string;
  plugin_key: string;
  name: string;
  version: string;
  description: string | null;
  enabled: boolean;
  risk_level: string;
  created_at: string;
}

// ── Constants ─────────────────────────────────────────────────────────────────

const PROVIDERS = [
  { value: "ollama", label: "Ollama (локально)" },
  { value: "llamacpp", label: "llama.cpp GGUF (локально)" },
  { value: "vllm", label: "vLLM (локально)" },
  { value: "lmstudio", label: "LM Studio (локально)" },
  { value: "openai_compatible", label: "OpenAI-compatible" },
  { value: "openrouter", label: "OpenRouter (200+ моделей)" },
  { value: "openai", label: "OpenAI" },
  { value: "anthropic", label: "Anthropic (Claude)" },
  { value: "deepseek", label: "DeepSeek" },
  { value: "gemini", label: "Google Gemini" },
  { value: "mistral", label: "Mistral AI" },
  { value: "groq", label: "Groq" },
  { value: "together", label: "Together AI" },
  { value: "fireworks", label: "Fireworks AI" },
  { value: "xai", label: "xAI" },
  { value: "cohere", label: "Cohere" },
  { value: "perplexity", label: "Perplexity" },
  { value: "minimax", label: "MiniMax" },
  { value: "kimi", label: "Kimi / Moonshot" },
  { value: "qwen", label: "Qwen / DashScope" },
] as const;

const PROVIDER_ENV: Record<string, string> = {
  vllm: "VLLM_API_KEY",
  lmstudio: "LMSTUDIO_API_KEY",
  openai_compatible: "OPENAI_COMPATIBLE_API_KEY",
  openrouter: "OPENROUTER_API_KEY",
  openai: "OPENAI_API_KEY",
  anthropic: "ANTHROPIC_API_KEY",
  deepseek: "DEEPSEEK_API_KEY",
  gemini: "GEMINI_API_KEY",
  mistral: "MISTRAL_API_KEY",
  groq: "GROQ_API_KEY",
  together: "TOGETHER_API_KEY",
  fireworks: "FIREWORKS_API_KEY",
  xai: "XAI_API_KEY",
  cohere: "COHERE_API_KEY",
  perplexity: "PERPLEXITY_API_KEY",
  minimax: "MINIMAX_API_KEY",
  kimi: "MOONSHOT_API_KEY",
  qwen: "DASHSCOPE_API_KEY",
};

const PROVIDER_MODEL_PLACEHOLDER: Record<string, string> = {
  ollama: "qwen3.5:9b",
  llamacpp: "/llamacpp-models/model.gguf",
  vllm: "Qwen/Qwen3-32B-AWQ",
  lmstudio: "local-model",
  openai_compatible: "model-name",
  openrouter: "deepseek/deepseek-r1  или  qwen/qwen3-235b-a22b",
  openai: "gpt-4.1  или  gpt-4.1-mini",
  anthropic: "claude-sonnet-4-6  или  claude-haiku-4-5",
  deepseek: "deepseek-chat  или  deepseek-reasoner",
  gemini: "gemini-2.5-pro  или  gemini-2.5-flash",
  mistral: "mistral-large-latest",
  groq: "llama-3.3-70b-versatile",
  together: "meta-llama/Llama-3.3-70B-Instruct-Turbo",
  fireworks: "accounts/fireworks/models/llama-v3p1-70b-instruct",
  xai: "grok-3  или  grok-3-mini",
  cohere: "command-r-plus",
  perplexity: "sonar-pro",
  minimax: "MiniMax-M1",
  kimi: "moonshot-v1-128k",
  qwen: "qwen-plus  или  qwen-max",
};

const PROVIDER_COMMON_MODELS: Record<string, string[]> = {
  vllm: [
    "Qwen/Qwen3-32B-AWQ",
    "Qwen/Qwen3-235B-A22B",
    "deepseek-ai/DeepSeek-R1",
  ],
  lmstudio: ["local-model", "qwen3-30b-a3b", "llama-3.3-70b-instruct"],
  openrouter: [
    "deepseek/deepseek-r1",
    "qwen/qwen3-235b-a22b",
    "anthropic/claude-sonnet-4.5",
  ],
  openai: ["gpt-4.1", "gpt-4.1-mini", "o3", "o4-mini"],
  anthropic: ["claude-sonnet-4-6", "claude-haiku-4-5", "claude-opus-4-1"],
  deepseek: ["deepseek-chat", "deepseek-reasoner"],
  gemini: ["gemini-2.5-pro", "gemini-2.5-flash"],
  mistral: ["mistral-large-latest", "ministral-8b-latest"],
  groq: ["llama-3.3-70b-versatile", "openai/gpt-oss-120b"],
  together: [
    "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "Qwen/Qwen3-235B-A22B-fp8-tput",
  ],
  fireworks: [
    "accounts/fireworks/models/llama-v3p1-70b-instruct",
    "accounts/fireworks/models/qwen3-235b-a22b",
  ],
  xai: ["grok-3", "grok-3-mini", "grok-4"],
  cohere: ["command-r-plus", "command-r"],
  perplexity: ["sonar-pro", "sonar-reasoning-pro"],
  minimax: ["MiniMax-M1", "MiniMax-Text-01"],
  kimi: ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
  qwen: ["qwen-plus", "qwen-max", "qwen-turbo"],
};

const LOCAL_PROVIDER_URL_LABEL: Record<string, keyof AgentConfig> = {
  ollama: "ollama_url",
  vllm: "vllm_url",
  lmstudio: "lmstudio_url",
  openai_compatible: "openai_compatible_url",
};

type TabId = "agent" | "memory" | "data" | "system" | "email";

const TABS: { id: TabId; label: string }[] = [
  { id: "agent", label: "Агент" },
  { id: "memory", label: "Память" },
  { id: "data", label: "Данные" },
  { id: "system", label: "Система" },
  { id: "email", label: "Почта" },
];

// ── UI helpers ────────────────────────────────────────────────────────────────

const inputCls =
  "w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50";
const selectCls =
  "w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500";
const btnPrimary =
  "px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors";
const btnSecondary =
  "px-4 py-2 bg-slate-700 text-slate-100 text-sm rounded-lg hover:bg-slate-600 disabled:opacity-50 transition-colors";

function fmtBytes(b: number) {
  if (b >= 1e9) return (b / 1e9).toFixed(1) + " GB";
  if (b >= 1e6) return (b / 1e6).toFixed(0) + " MB";
  return b + " B";
}

function unwrapProposalValue(value: unknown): unknown {
  if (
    value &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    Object.keys(value).length === 1 &&
    "value" in value
  ) {
    return (value as { value: unknown }).value;
  }
  return value;
}

function formatProposalValue(value: unknown): string {
  const unwrapped = unwrapProposalValue(value);
  if (unwrapped === null || unwrapped === undefined) return "null";
  if (typeof unwrapped === "string") return unwrapped;
  return JSON.stringify(unwrapped, null, 2);
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label className="block text-sm font-medium text-slate-300 mb-1">
        {label}
      </label>
      {children}
      {hint && <p className="mt-1 text-xs text-slate-400">{hint}</p>}
    </div>
  );
}

function SectionCard({
  title,
  subtitle,
  action,
  children,
}: {
  title: React.ReactNode;
  subtitle?: string;
  action?: React.ReactNode;
  children?: React.ReactNode;
}) {
  return (
    <section className="bg-slate-800 border border-slate-700 rounded-lg p-6">
      <div className="flex items-start justify-between gap-4 mb-4">
        <div>
          <h2 className="text-lg font-semibold">{title}</h2>
          {subtitle && (
            <p className="mt-1 text-sm text-slate-400">{subtitle}</p>
          )}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

function SaveRow({
  saving,
  saved,
  onSave,
  onReset,
  saveLabel = "Сохранить",
}: {
  saving: boolean;
  saved: boolean;
  onSave: () => void;
  onReset?: () => void;
  saveLabel?: string;
}) {
  return (
    <div className="flex items-center gap-3 pt-2">
      <button onClick={onSave} disabled={saving} className={btnPrimary}>
        {saving ? "Сохранение..." : saved ? "Сохранено ✓" : saveLabel}
      </button>
      {onReset && (
        <button onClick={onReset} disabled={saving} className={btnSecondary}>
          Сбросить
        </button>
      )}
    </div>
  );
}

function modelOptionsForProvider(
  provider: string,
  models: OllamaModel[],
  registryModels: RegistryModel[],
) {
  const base =
    provider === "ollama"
      ? models.map((m) => m.name)
      : registryModels
          .filter((model) => model.provider === provider)
          .map((model) => model.name);
  return Array.from(
    new Set(
      [
        ...base,
        ...(PROVIDER_COMMON_MODELS[provider] ?? []),
        PROVIDER_MODEL_PLACEHOLDER[provider],
      ].filter(Boolean),
    ),
  );
}

function DefaultModelField({
  provider,
  model,
  ollamaModels,
  ggufModels,
  registryModels,
  onChange,
}: {
  provider: string;
  model: string;
  ollamaModels: OllamaModel[];
  ggufModels: GgufModel[];
  registryModels: RegistryModel[];
  onChange: (model: string) => void;
}) {
  const isOllama = provider === "ollama";
  const isLlamacpp = provider === "llamacpp";

  const options: { value: string; label: string }[] = isOllama
    ? ollamaModels.map((m) => ({
        value: m.name,
        label: m.parameter_size ? `${m.name} — ${m.parameter_size}` : m.name,
      }))
    : isLlamacpp
      ? ggufModels.map((m) => ({
          value: m.path,
          label: `${m.name} (${m.size_human})`,
        }))
      : modelOptionsForProvider(provider, ollamaModels, registryModels).map(
          (o) => ({ value: o, label: o }),
        );

  const useSelect = isOllama || isLlamacpp || options.length > 0;
  const hint = isLlamacpp
    ? "GGUF-модель из локального хранилища"
    : isOllama
      ? "Fallback-модель для диалога и инструментов"
      : `Пример: ${PROVIDER_MODEL_PLACEHOLDER[provider] ?? "model-name"}`;

  return (
    <Field label="Модель по умолчанию" hint={hint}>
      {useSelect ? (
        <select
          value={model}
          onChange={(e) => onChange(e.target.value)}
          className={selectCls}
        >
          {isLlamacpp && ggufModels.length === 0 && (
            <option value="" disabled>
              — нет GGUF-моделей —
            </option>
          )}
          {!options.find((o) => o.value === model) && model && (
            <option value={model}>{model}</option>
          )}
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      ) : (
        <>
          <input
            className={inputCls}
            list="default-model-datalist"
            value={model}
            onChange={(e) => onChange(e.target.value)}
            placeholder={PROVIDER_MODEL_PLACEHOLDER[provider] ?? "model-name"}
          />
          <datalist id="default-model-datalist">
            {options.map((o) => (
              <option key={o.value} value={o.value} />
            ))}
          </datalist>
        </>
      )}
      {isLlamacpp && ggufModels.length === 0 && (
        <p className="mt-1 text-xs text-amber-400">
          Нет локальных GGUF-моделей.{" "}
          <a href="/settings/models" className="underline hover:text-amber-300">
            Загрузите модель →
          </a>
        </p>
      )}
    </Field>
  );
}

function AgentModelProviderSelector({
  label,
  provider,
  model,
  disableThinking,
  fallbackProvider,
  fallbackModel,
  models,
  registryModels,
  onChange,
}: {
  label: string;
  provider: string | null;
  model: string | null;
  disableThinking: boolean;
  fallbackProvider: string;
  fallbackModel: string;
  models: OllamaModel[];
  registryModels: RegistryModel[];
  onChange: (next: {
    provider: string | null;
    model: string | null;
    disableThinking: boolean;
  }) => void;
}) {
  const effectiveProvider = provider || fallbackProvider;
  const options = modelOptionsForProvider(
    effectiveProvider,
    models,
    registryModels,
  );

  return (
    <div className="rounded-md border border-slate-700 bg-slate-900/40 p-3">
      <div className="mb-3 text-sm font-medium text-slate-200">{label}</div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="Провайдер" hint="Пусто = как у основного агента">
          <select
            value={provider ?? ""}
            onChange={(e) =>
              onChange({
                provider: e.target.value || null,
                model,
                disableThinking,
              })
            }
            className={selectCls}
          >
            <option value="">
              Как у основного агента ({fallbackProvider})
            </option>
            {PROVIDERS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
        </Field>
        <Field
          label="Модель"
          hint="Пусто = модель основного агента или роль по умолчанию"
        >
          <select
            className={selectCls}
            value={model ?? ""}
            onChange={(e) =>
              onChange({
                provider,
                model: e.target.value || null,
                disableThinking,
              })
            }
          >
            <option value="">Как у основного агента ({fallbackModel})</option>
            {model && !options.includes(model) && (
              <option value={model}>{model} (текущее значение)</option>
            )}
            {options.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <label className="mt-3 flex items-start gap-3 rounded-md border border-slate-700 bg-slate-950/40 p-3">
        <input
          type="checkbox"
          className="mt-0.5"
          checked={disableThinking}
          onChange={(e) =>
            onChange({
              provider,
              model,
              disableThinking: e.target.checked,
            })
          }
        />
        <span className="text-sm text-slate-200">
          Отключить размышления для этой роли
        </span>
      </label>
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

interface ApprovalPolicy {
  enabled: boolean;
  trust_threshold: number;
  max_amount: number | null;
}

export default function SettingsPage() {
  const [activeTab, setActiveTab] = useState<TabId>("agent");

  // Approval policy
  const [approvalPolicy, setApprovalPolicy] = useState<ApprovalPolicy>({
    enabled: false,
    trust_threshold: 0.85,
    max_amount: null,
  });
  const [policyLoading, setPolicyLoading] = useState(false);
  const [policySaved, setPolicySaved] = useState(false);

  useEffect(() => {
    mutFetch(`${API}/api/approvals/policy`)
      .then((r) => r.json())
      .then((d) => setApprovalPolicy(d))
      .catch(() => {});
  }, []);

  async function saveApprovalPolicy() {
    setPolicyLoading(true);
    setPolicySaved(false);
    try {
      const res = await mutFetch(`${API}/api/approvals/policy`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(approvalPolicy),
      });
      if (res.ok) {
        setApprovalPolicy(await res.json());
        setPolicySaved(true);
        setTimeout(() => setPolicySaved(false), 2000);
      }
    } finally {
      setPolicyLoading(false);
    }
  }

  // AI Config (models tab)
  const [models, setModels] = useState<OllamaModel[]>([]);
  const [ollamaAvailable, setOllamaAvailable] = useState<boolean | null>(null);
  const [ollamaError, setOllamaError] = useState<string | null>(null);
  const [ollamaGpuInfo, setOllamaGpuInfo] = useState<{
    has_gpu: boolean;
    running_models: { name: string; size_vram: number }[];
  } | null>(null);
  const [config, setConfig] = useState<AiConfig | null>(null);
  const [registryModels, setRegistryModels] = useState<RegistryModel[]>([]);
  const [configStatus, setConfigStatus] = useState<AiConfigStatus | null>(null);
  const [ggufModels, setGgufModels] = useState<GgufModel[]>([]);
  const [loadingModels, setLoadingModels] = useState(true);
  const [configSaving, setConfigSaving] = useState(false);
  const [configSaved, setConfigSaved] = useState(false);

  // Model pull
  const [pullName, setPullName] = useState("");
  const [pulling, setPulling] = useState(false);
  const [pullLog, setPullLog] = useState<string[]>([]);
  const [deletingModel, setDeletingModel] = useState<string | null>(null);
  const pullLogRef = useRef<HTMLDivElement>(null);

  // Memory / embeddings
  const [embeddingProfile, setEmbeddingProfile] =
    useState<EmbeddingProfile | null>(null);
  const [embeddingStats, setEmbeddingStats] = useState<EmbeddingStats | null>(
    null,
  );
  const [rebuildingEmbeddings, setRebuildingEmbeddings] = useState(false);
  const [indexingEmbeddings, setIndexingEmbeddings] = useState(false);
  const [rebuildMessage, setRebuildMessage] = useState<string | null>(null);

  // NTD
  const [ntdConfig, setNtdConfig] = useState<NtdControlConfig | null>(null);
  const [ntdSaving, setNtdSaving] = useState(false);
  const [ntdSaved, setNtdSaved] = useState(false);

  // Agent
  const [agentConfig, setAgentConfig] = useState<AgentConfig | null>(null);
  const [agentConfigBaseline, setAgentConfigBaseline] =
    useState<AgentConfig | null>(null);
  const [agentSkills, setAgentSkills] = useState<AgentSkill[]>([]);
  const [agentSkillsMode, setAgentSkillsMode] = useState<
    "capabilities" | "registry"
  >("registry");
  const [agentControlPlane, setAgentControlPlane] =
    useState<AgentControlPlaneStatus | null>(null);
  const [agentRuntime, setAgentRuntime] = useState<AgentRuntimeStatus | null>(
    null,
  );
  const [agentConfigProposals, setAgentConfigProposals] = useState<
    AgentConfigProposal[]
  >([]);
  const [capabilityProposals, setCapabilityProposals] = useState<
    CapabilityProposal[]
  >([]);
  const [agentTasks, setAgentTasks] = useState<AgentTask[]>([]);
  const [agentTeams, setAgentTeams] = useState<AgentTeam[]>([]);
  const [agentCrons, setAgentCrons] = useState<AgentCron[]>([]);
  const [agentPlugins, setAgentPlugins] = useState<AgentPlugin[]>([]);
  const [agentSkillFilter, setAgentSkillFilter] = useState("");
  const selectAllSkillsRef = useRef<HTMLInputElement | null>(null);
  const agentSkillToggleRefs = useRef<Array<HTMLInputElement | null>>([]);
  const loadedTabsRef = useRef<Set<TabId>>(new Set());
  const [agentSaving, setAgentSaving] = useState(false);
  const [agentSaved, setAgentSaved] = useState(false);
  const [agentError, setAgentError] = useState<string | null>(null);

  // Dev purge
  const [purgeConfirm, setPurgeConfirm] = useState("");
  const [purgeBusy, setPurgeBusy] = useState(false);
  const [purgeMessage, setPurgeMessage] = useState<string | null>(null);

  // Telegram
  const [tgStatus, setTgStatus] = useState<{
    configured: boolean;
    bot_running: boolean;
    notifications_enabled: boolean;
    has_default_chat: boolean;
    allowed_users_count: number;
    token_masked: string;
    chat_id_masked: string;
    allowed_users_masked: string;
    last_error: string;
  } | null>(null);
  const [tgRestarting, setTgRestarting] = useState(false);
  const [tgTesting, setTgTesting] = useState(false);
  const [tgTestResult, setTgTestResult] = useState<string | null>(null);
  const [tgEditing, setTgEditing] = useState(false);
  const [tgDraft, setTgDraft] = useState({
    bot_token: "",
    notifications_chat_id: "",
    allowed_users: "",
    notifications_enabled: false,
  });
  const [tgSaving, setTgSaving] = useState(false);
  const [tgSaved, setTgSaved] = useState(false);

  // ── Data loaders ─────────────────────────────────────────────────────────

  async function loadModels() {
    setLoadingModels(true);
    try {
      const r = await fetch(`${API}/api/ai/models`);
      const d = await r.json();
      setModels(d.models ?? []);
      setOllamaAvailable(d.ollama_available ?? false);
      setOllamaError(d.ollama_error ?? null);
      setOllamaGpuInfo(d.gpu_info ?? null);
    } catch {
      setModels([]);
      setOllamaAvailable(false);
      setOllamaError("Не удалось подключиться к backend");
    } finally {
      setLoadingModels(false);
    }
  }

  async function loadConfig() {
    try {
      const r = await fetch(`${API}/api/ai/config`);
      setConfig(await r.json());
      await loadConfigStatus();
    } catch {}
  }

  async function loadConfigStatus() {
    try {
      const r = await fetch(`${API}/api/ai/config/status`);
      setConfigStatus(await r.json());
    } catch {
      setConfigStatus(null);
    }
  }

  async function loadGgufModels() {
    try {
      const r = await fetch(`${API}/api/local-models/llamacpp/models`);
      const d = await r.json();
      const list = Array.isArray(d) ? d : d?.models;
      setGgufModels(Array.isArray(list) ? list : []);
    } catch {
      setGgufModels([]);
    }
  }

  async function loadCapabilities() {
    try {
      const r = await fetch(`${API}/api/ai/models/capabilities`);
      const d = await r.json();
      setRegistryModels(d.models ?? []);
    } catch {
      setRegistryModels([]);
    }
  }

  async function loadEmbeddingProfile() {
    try {
      const r = await fetch(`${API}/api/ai/embedding-profile`);
      setEmbeddingProfile(await r.json());
    } catch {
      setEmbeddingProfile(null);
    }
  }

  async function loadEmbeddingStats() {
    try {
      const r = await fetch(`${API}/api/memory/embeddings/stats`);
      setEmbeddingStats(await r.json());
    } catch {
      setEmbeddingStats(null);
    }
  }

  async function loadNtdConfig() {
    try {
      const r = await fetch(`${API}/api/settings/ntd-control`);
      setNtdConfig(await r.json());
    } catch {
      setNtdConfig(null);
    }
  }

  async function loadAgentConfig() {
    try {
      const r = await fetch(`${API}/api/ai/agent-config`);
      const data = await r.json();
      setAgentConfig(data);
      setAgentConfigBaseline(data);
    } catch {
      setAgentConfig(null);
      setAgentConfigBaseline(null);
    }
  }

  async function loadAgentSkills() {
    try {
      const r = await fetch(`${API}/api/ai/agent-skills`);
      const d = await r.json();
      setAgentSkills(d.skills ?? []);
      setAgentSkillsMode(
        d.mode === "capabilities" ? "capabilities" : "registry",
      );
    } catch {
      setAgentSkills([]);
    }
  }

  async function loadAgentControlPlane() {
    try {
      const r = await fetch(`${API}/api/agent/control-plane/status`);
      setAgentControlPlane(await r.json());
    } catch {
      setAgentControlPlane(null);
    }
  }

  async function loadAgentRuntime() {
    try {
      const r = await fetch(`${API}/api/agent/runtime/status`);
      setAgentRuntime(await r.json());
    } catch {
      setAgentRuntime(null);
    }
  }

  async function loadAgentConfigProposals() {
    try {
      const r = await fetch(`${API}/api/agent/config/proposals?status=pending`);
      setAgentConfigProposals(await r.json());
    } catch {
      setAgentConfigProposals([]);
    }
  }

  async function loadCapabilityProposals() {
    try {
      const r = await fetch(`${API}/api/agent/capabilities`);
      setCapabilityProposals(await r.json());
    } catch {
      setCapabilityProposals([]);
    }
  }

  async function loadAgentWorkRegistry() {
    try {
      const [tasksR, teamsR, cronsR, pluginsR] = await Promise.all([
        fetch(`${API}/api/agent/tasks`),
        fetch(`${API}/api/agent/teams`),
        fetch(`${API}/api/agent/cron`),
        mutFetch(`${API}/api/agent/plugins`),
      ]);
      setAgentTasks(tasksR.ok ? await tasksR.json() : []);
      setAgentTeams(teamsR.ok ? await teamsR.json() : []);
      setAgentCrons(cronsR.ok ? await cronsR.json() : []);
      setAgentPlugins(pluginsR.ok ? await pluginsR.json() : []);
    } catch {
      // keep previous state on network error
    }
  }

  async function togglePlugin(pluginKey: string, enable: boolean) {
    const action = enable ? "enable" : "disable";
    await mutFetch(`${API}/api/agent/plugins/${pluginKey}/${action}`, {
      method: "POST",
    });
    await loadAgentWorkRegistry();
  }

  async function toggleCron(cronId: string, enable: boolean) {
    // Cron enable/disable via PATCH (not yet in API — use as placeholder)
    await mutFetch(`${API}/api/agent/cron/${cronId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: enable }),
    });
    await loadAgentWorkRegistry();
  }

  async function loadTgStatus() {
    try {
      const r = await fetch(`${API}/api/telegram/status`);
      setTgStatus(await r.json());
    } catch {
      setTgStatus(null);
    }
  }

  useEffect(() => {
    function loadTab(tab: TabId) {
      if (loadedTabsRef.current.has(tab)) return;
      loadedTabsRef.current.add(tab);
      if (tab === "agent") {
        loadAgentConfig();
        loadAgentSkills();
        loadAgentControlPlane();
        loadAgentRuntime();
        loadAgentConfigProposals();
        loadCapabilityProposals();
        loadAgentWorkRegistry();
        loadModels();
        loadCapabilities();
        loadGgufModels();
      } else if (tab === "memory") {
        loadEmbeddingProfile();
        loadEmbeddingStats();
      } else if (tab === "data") {
        loadNtdConfig();
        loadEmbeddingStats();
      } else if (tab === "email") {
        loadTgStatus();
      }
    }
    loadTab(activeTab);
  }, [activeTab]);

  useEffect(() => {
    pullLogRef.current?.scrollTo(0, pullLogRef.current.scrollHeight);
  }, [pullLog]);

  // ── Handlers ──────────────────────────────────────────────────────────────


  async function handleRebuildEmbeddings() {
    setRebuildingEmbeddings(true);
    setRebuildMessage(null);
    try {
      const r = await mutFetch(`${API}/api/memory/embeddings/rebuild-active`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content_types: ["document_chunk", "evidence_span"],
          limit: 1000,
          mark_stale_existing: true,
        }),
      });
      const data = await r.json();
      setRebuildMessage(
        `Создано записей: ${data.created}; stale: ${data.stale_marked}`,
      );
      await loadEmbeddingStats();
    } catch {
      setRebuildMessage("Не удалось подготовить переиндексацию");
    } finally {
      setRebuildingEmbeddings(false);
    }
  }

  async function handleIndexEmbeddings() {
    setIndexingEmbeddings(true);
    setRebuildMessage(null);
    try {
      const r = await mutFetch(`${API}/api/memory/embeddings/index-active`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ statuses: ["queued", "stale"], limit: 100 }),
      });
      const data = await r.json();
      setRebuildMessage(
        `Qdrant: indexed ${data.indexed}; failed ${data.failed}`,
      );
      await loadEmbeddingStats();
    } catch {
      setRebuildMessage("Не удалось индексировать embeddings в Qdrant");
    } finally {
      setIndexingEmbeddings(false);
    }
  }

  async function handleSaveNtdConfig(mode: NtdControlConfig["mode"]) {
    setNtdSaving(true);
    try {
      const r = await mutFetch(`${API}/api/settings/ntd-control`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode, updated_by: "user" }),
      });
      if (!r.ok) throw new Error(await r.text());
      setNtdConfig(await r.json());
      setNtdSaved(true);
      setTimeout(() => setNtdSaved(false), 2000);
    } catch {}
    setNtdSaving(false);
  }

  async function handleSaveAgentConfig() {
    if (!agentConfig) return;
    setAgentSaving(true);
    try {
      const protectedSettings = new Set(
        agentControlPlane?.protected_settings ?? [],
      );
      const baseline = agentConfigBaseline;
      const entries = Object.entries(agentConfig) as Array<
        [keyof AgentConfig, AgentConfig[keyof AgentConfig]]
      >;
      const safePatch: Partial<AgentConfig> = {};
      const protectedChanges: Array<
        [keyof AgentConfig, AgentConfig[keyof AgentConfig]]
      > = [];

      for (const [key, value] of entries) {
        const previous = baseline?.[key];
        if (JSON.stringify(previous) === JSON.stringify(value)) continue;
        if (protectedSettings.has(String(key))) {
          protectedChanges.push([key, value]);
        } else {
          safePatch[key] = value as never;
        }
      }

      let nextConfig = agentConfig;
      if (Object.keys(safePatch).length > 0) {
        const r = await mutFetch(`${API}/api/ai/agent-config`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(safePatch),
        });
        if (!r.ok) throw new Error(await r.text());
        nextConfig = await r.json();
      }

      for (const [key, value] of protectedChanges) {
        await mutFetch(`${API}/api/agent/config/proposals`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            setting_path: key,
            proposed_value: value,
            reason:
              "Изменение защищенной настройки из GUI. Требуется подтверждение, чтобы не ухудшить личность агента, память, аудит или контур безопасности.",
            risk_level:
              key === "system_prompt" ||
              key === "agent_name" ||
              key === "approval_gates"
                ? "critical"
                : "high",
            requested_by: "user",
          }),
        });
      }

      setAgentConfig(nextConfig);
      setAgentConfigBaseline(nextConfig);
      await loadAgentSkills();
      await loadAgentControlPlane();
      await loadAgentRuntime();
      await loadAgentConfigProposals();
      await loadCapabilityProposals();
      setAgentSaved(true);
      setTimeout(() => setAgentSaved(false), 2000);
    } catch (error) {
      setAgentError(
        error instanceof Error
          ? error.message
          : "Не удалось сохранить настройки агента",
      );
    }
    setAgentSaving(false);
  }

  async function handleResetAgentConfig() {
    setAgentSaving(true);
    setAgentError(null);
    try {
      const r = await mutFetch(`${API}/api/ai/agent-config/reset`, {
        method: "POST",
      });
      if (!r.ok) throw new Error(await r.text());
      const data = await r.json();
      setAgentConfig(data);
      setAgentConfigBaseline(data);
      await loadAgentSkills();
      await loadAgentControlPlane();
      await loadAgentRuntime();
      await loadAgentConfigProposals();
      await loadCapabilityProposals();
      setAgentSaved(true);
      setTimeout(() => setAgentSaved(false), 2000);
    } catch {}
    setAgentSaving(false);
  }

  async function handleApplyStableLocalPreset() {
    setAgentSaving(true);
    setAgentError(null);
    try {
      const response = await mutFetch(
        `${API}/api/ai/agent-config/presets/stable-local`,
        { method: "POST" },
      );
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(payload?.detail || `HTTP ${response.status}`);
      }
      const data = await response.json();
      setAgentConfig(data);
      setAgentConfigBaseline(data);
      await loadConfig();
      await loadAgentSkills();
      await loadAgentControlPlane();
      await loadAgentRuntime();
      await loadAgentConfigProposals();
      setAgentSaved(true);
      setTimeout(() => setAgentSaved(false), 2000);
    } catch (error) {
      setAgentError(
        error instanceof Error
          ? error.message
          : "Не удалось применить стабильный локальный режим",
      );
    }
    setAgentSaving(false);
  }

  async function decideAgentConfigProposal(
    proposalId: string,
    approved: boolean,
  ) {
    setAgentSaving(true);
    try {
      await mutFetch(`${API}/api/agent/config/proposals/${proposalId}/decide`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approved, decided_by: "user" }),
      });
      await loadAgentConfig();
      await loadAgentSkills();
      await loadAgentControlPlane();
      await loadAgentRuntime();
      await loadAgentConfigProposals();
    } catch {}
    setAgentSaving(false);
  }

  async function decideCapabilityProposal(
    proposalId: string,
    approved: boolean,
  ) {
    setAgentSaving(true);
    setAgentError(null);
    try {
      const response = await mutFetch(
        `${API}/api/agent/capabilities/${proposalId}/decide`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ approved, decided_by: "user" }),
        },
      );
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(payload?.detail || `HTTP ${response.status}`);
      }
      await loadAgentControlPlane();
      await loadAgentRuntime();
      await loadCapabilityProposals();
      setAgentSaved(true);
      setTimeout(() => setAgentSaved(false), 2000);
    } catch (error) {
      setAgentError(
        error instanceof Error ? error.message : "Не удалось применить решение",
      );
    }
    setAgentSaving(false);
  }

  async function sandboxApplyCapabilityProposal(proposalId: string) {
    setAgentSaving(true);
    setAgentError(null);
    try {
      const response = await mutFetch(
        `${API}/api/agent/capabilities/${proposalId}/sandbox-apply`,
        {
          method: "POST",
        },
      );
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(
          typeof payload?.detail === "string"
            ? payload.detail
            : payload?.detail?.message || `HTTP ${response.status}`,
        );
      }
      await loadAgentControlPlane();
      await loadAgentRuntime();
      await loadCapabilityProposals();
      setAgentSaved(true);
      setTimeout(() => setAgentSaved(false), 2000);
    } catch (error) {
      setAgentError(
        error instanceof Error ? error.message : "Sandbox не выполнен",
      );
    }
    setAgentSaving(false);
  }

  async function promoteCapabilityProposal(proposalId: string) {
    setAgentSaving(true);
    setAgentError(null);
    try {
      const response = await mutFetch(
        `${API}/api/agent/capabilities/${proposalId}/promote`,
        { method: "POST" },
      );
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(payload?.detail || `HTTP ${response.status}`);
      }
      await loadAgentControlPlane();
      await loadAgentRuntime();
      await loadCapabilityProposals();
      setAgentSaved(true);
      setTimeout(() => setAgentSaved(false), 2000);
    } catch (error) {
      setAgentError(
        error instanceof Error
          ? error.message
          : "Не удалось продвинуть capability",
      );
    }
    setAgentSaving(false);
  }

  function updateAgentSkill(name: string, enabled: boolean) {
    if (!agentConfig) return;
    const exposed = new Set(agentConfig.exposed_skills);
    if (enabled) exposed.add(name);
    else exposed.delete(name);
    setAgentConfig({
      ...agentConfig,
      exposed_skills: Array.from(exposed).sort(),
    });
  }

  function updateAgentApprovalGate(name: string, enabled: boolean) {
    if (!agentConfig) return;
    const exposed = new Set(agentConfig.exposed_skills);
    const gates = new Set(agentConfig.approval_gates);
    if (enabled) {
      exposed.add(name);
      gates.add(name);
    } else gates.delete(name);
    setAgentConfig({
      ...agentConfig,
      exposed_skills: Array.from(exposed).sort(),
      approval_gates: Array.from(gates).sort(),
    });
  }

  async function handleDevelopmentPurge() {
    if (purgeConfirm !== "DELETE ALL DOCUMENT DATA") return;
    if (!confirm("Полностью удалить все документы и связанные записи БД?"))
      return;
    setPurgeBusy(true);
    setPurgeMessage(null);
    try {
      const r = await mutFetch(`${API}/api/documents/dev/purge-all`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: purgeConfirm, delete_files: true }),
      });
      if (!r.ok) throw new Error(await r.text());
      const data = await r.json();
      setPurgeMessage(
        `Очистка выполнена: найдено ${data.documents_seen}, удалено ${data.deleted}`,
      );
      setPurgeConfirm("");
      await loadEmbeddingStats();
    } catch {
      setPurgeMessage("Не удалось выполнить полную очистку");
    } finally {
      setPurgeBusy(false);
    }
  }

  async function handleTelegramRestart() {
    setTgRestarting(true);
    setTgTestResult(null);
    try {
      const r = await mutFetch(`${API}/api/telegram/restart`, {
        method: "POST",
      });
      const d = await r.json();
      setTgTestResult(
        d.bot_running
          ? "✅ Бот запущен"
          : `❌ ${d.last_error || "Не удалось запустить"}`,
      );
      await loadTgStatus();
    } catch (e) {
      setTgTestResult(`❌ Ошибка: ${e}`);
    } finally {
      setTgRestarting(false);
    }
  }

  async function handleTelegramTest() {
    setTgTesting(true);
    setTgTestResult(null);
    try {
      const r = await mutFetch(`${API}/api/telegram/test`, { method: "POST" });
      const d = await r.json();
      setTgTestResult(
        d.ok ? "✅ Тестовое сообщение отправлено" : `❌ ${d.detail}`,
      );
    } catch (e) {
      setTgTestResult(`❌ Ошибка: ${e}`);
    } finally {
      setTgTesting(false);
      await loadTgStatus();
    }
  }

  async function handleTelegramSave() {
    setTgSaving(true);
    setTgTestResult(null);
    try {
      const payload: Record<string, string | boolean> = {
        notifications_enabled: tgDraft.notifications_enabled,
      };
      // Only send non-empty fields (empty string = clear)
      if (tgDraft.bot_token !== "") payload.bot_token = tgDraft.bot_token;
      if (tgDraft.notifications_chat_id !== "")
        payload.notifications_chat_id = tgDraft.notifications_chat_id;
      if (tgDraft.allowed_users !== "")
        payload.allowed_users = tgDraft.allowed_users;
      const r = await mutFetch(`${API}/api/telegram/config`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!r.ok) throw new Error(await r.text());
      await loadTgStatus();
      setTgDraft({
        bot_token: "",
        notifications_chat_id: "",
        allowed_users: "",
        notifications_enabled: false,
      });
      setTgEditing(false);
      setTgSaved(true);
      setTimeout(() => setTgSaved(false), 3000);
    } catch (e) {
      setTgTestResult(`❌ Ошибка сохранения: ${e}`);
    } finally {
      setTgSaving(false);
    }
  }

  const filteredAgentSkills = agentSkills.filter((skill) => {
    const q = agentSkillFilter.trim().toLowerCase();
    if (!q) return true;
    return (
      skill.name.toLowerCase().includes(q) ||
      skill.description.toLowerCase().includes(q) ||
      skill.path.toLowerCase().includes(q)
    );
  });

  const selectedSkillsCount = agentConfig
    ? agentSkills.filter((skill) =>
        agentConfig.exposed_skills.includes(skill.name),
      ).length
    : 0;
  const allSkillsSelected =
    !!agentConfig &&
    agentSkills.length > 0 &&
    selectedSkillsCount === agentSkills.length;
  const someSkillsSelected =
    !!agentConfig && selectedSkillsCount > 0 && !allSkillsSelected;

  useEffect(() => {
    if (selectAllSkillsRef.current) {
      selectAllSkillsRef.current.indeterminate = someSkillsSelected;
    }
  }, [someSkillsSelected]);

  useEffect(() => {
    agentSkillToggleRefs.current = agentSkillToggleRefs.current.slice(
      0,
      filteredAgentSkills.length,
    );
  }, [filteredAgentSkills.length]);

  function toggleAllSkills(enabled: boolean) {
    if (!agentConfig) return;
    const nextExposed = enabled
      ? agentSkills.map((skill) => skill.name).sort()
      : [];
    const nextGates = agentConfig.approval_gates.filter((gate) =>
      nextExposed.includes(gate),
    );
    setAgentConfig({
      ...agentConfig,
      exposed_skills: nextExposed,
      approval_gates: nextGates,
    });
  }

  function handleSkillArrowNavigation(
    event: React.KeyboardEvent<HTMLInputElement>,
    index: number,
  ) {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") {
      return;
    }
    event.preventDefault();
    const delta = event.key === "ArrowDown" ? 1 : -1;
    const nextIndex = index + delta;
    const target = agentSkillToggleRefs.current[nextIndex];
    if (target) {
      target.focus();
      target.scrollIntoView({ block: "nearest" });
    }
  }

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <div className="p-6 max-w-3xl mx-auto">
      <h1 className="text-2xl font-bold mb-5">Настройки</h1>

      {/* Tab bar */}
      <div className="flex gap-1 border-b border-slate-700 mb-6">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`px-4 py-2 text-sm rounded-t-md transition-colors ${
              activeTab === tab.id
                ? "bg-slate-700 text-slate-100 border border-b-0 border-slate-600"
                : "text-slate-400 hover:text-slate-200 hover:bg-slate-800/50"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* ── TAB: Агент ───────────────────────────────────────────────────── */}
      {activeTab === "agent" && (
        <div className="space-y-6">
          {agentConfig ? (
            <>
              {/* General */}
              <SectionCard
                title="Агент «Света»"
                subtitle="Основной AI-сотрудник — обрабатывает документы, отвечает на вопросы, вызывает инструменты."
              >
                <div className="space-y-4">
                  <label className="flex items-center gap-2 text-sm text-slate-200">
                    <input
                      type="checkbox"
                      checked={agentConfig.enabled}
                      onChange={(e) =>
                        setAgentConfig({
                          ...agentConfig,
                          enabled: e.target.checked,
                        })
                      }
                    />
                    Включить встроенного агента
                  </label>
                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    <Field
                      label="Имя сотрудника"
                      hint="Используется в системном промпте"
                    >
                      <input
                        className={inputCls}
                        value={agentConfig.agent_name}
                        onChange={(e) =>
                          setAgentConfig({
                            ...agentConfig,
                            agent_name: e.target.value,
                          })
                        }
                      />
                    </Field>
                    <Field
                      label="Backend URL"
                      hint="URL FastAPI-бэкенда для вызова инструментов"
                    >
                      <input
                        className={inputCls}
                        value={agentConfig.backend_url}
                        onChange={(e) =>
                          setAgentConfig({
                            ...agentConfig,
                            backend_url: e.target.value,
                          })
                        }
                      />
                    </Field>
                  </div>
                </div>
              </SectionCard>

              <SectionCard
                title="Отдел ИИ"
                subtitle="Оркестратор управляет задачей: назначает исполнителя, контролирует инструменты, Рабочий стол и аудит результата."
              >
                <div className="space-y-4">
                  <div className="grid grid-cols-1 gap-3 lg:grid-cols-4">
                    <Field
                      label="Режим автономии"
                      hint="Max autonomy применяет безопасные изменения в sandbox, защищённые — через подтверждение"
                    >
                      <select
                        className={selectCls}
                        value={agentConfig.autonomy_mode}
                        onChange={(e) =>
                          setAgentConfig({
                            ...agentConfig,
                            autonomy_mode: e.target.value,
                          })
                        }
                      >
                        <option value="draft_approval">Draft + approval</option>
                        <option value="auto_safe_changes">
                          Auto safe changes
                        </option>
                        <option value="max_autonomy">Max autonomy</option>
                      </select>
                    </Field>
                    <Field
                      label="Режим прав"
                      hint="Определяет, какие tools агент может запускать без блокировки"
                    >
                      <select
                        className={selectCls}
                        value={agentConfig.permission_mode}
                        onChange={(e) =>
                          setAgentConfig({
                            ...agentConfig,
                            permission_mode: e.target.value,
                          })
                        }
                      >
                        <option value="read_only">Read-only</option>
                        <option value="workspace_write">Workspace write</option>
                        <option value="danger_full_access">
                          Danger full access
                        </option>
                      </select>
                    </Field>
                    <label className="flex items-start gap-3 rounded-md bg-slate-900/50 border border-slate-700 p-3">
                      <input
                        type="checkbox"
                        className="mt-0.5"
                        checked={agentConfig.safe_auto_apply_enabled}
                        onChange={(e) =>
                          setAgentConfig({
                            ...agentConfig,
                            safe_auto_apply_enabled: e.target.checked,
                          })
                        }
                      />
                      <span className="text-sm text-slate-200">
                        Auto-apply безопасных изменений
                      </span>
                    </label>
                    <div className="rounded-md border border-slate-700 bg-slate-900/50 p-3 text-xs text-slate-300">
                      <div className="font-medium text-slate-100">
                        Control Plane
                      </div>
                      {agentControlPlane ? (
                        <div className="mt-2 space-y-1 text-slate-400">
                          <div>Tasks: {agentControlPlane.tasks_open}</div>
                          <div>
                            Plugins: {agentControlPlane.plugins_enabled}/
                            {agentControlPlane.plugins_total}
                          </div>
                          <div>
                            Memory facts: {agentControlPlane.memory_facts_total}
                          </div>
                          <div>
                            Capabilities:{" "}
                            {agentControlPlane.capability_proposals_open}
                          </div>
                        </div>
                      ) : (
                        <div className="mt-2 text-slate-500">
                          Статус недоступен
                        </div>
                      )}
                    </div>
                  </div>
                  {agentRuntime && (
                    <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
                      <div className="rounded-md border border-slate-700 bg-slate-900/50 p-3 text-xs">
                        <div className="font-medium text-slate-100">
                          Agent runtime
                        </div>
                        <div className="mt-2 grid grid-cols-2 gap-2 text-slate-400">
                          <div>
                            LLM 24h: {agentRuntime.counters.llm_calls_24h}
                          </div>
                          <div>
                            Tools 24h: {agentRuntime.counters.tool_calls_24h}
                          </div>
                          <div>
                            Errors 24h: {agentRuntime.counters.errors_24h}
                          </div>
                          <div>
                            Avg:{" "}
                            {agentRuntime.counters.avg_llm_duration_ms_24h ??
                              "—"}{" "}
                            ms
                          </div>
                        </div>
                        {agentRuntime.counters.last_error && (
                          <div className="mt-2 line-clamp-2 rounded bg-red-950/40 p-2 text-red-200">
                            {agentRuntime.counters.last_error}
                          </div>
                        )}
                      </div>
                      <div className="rounded-md border border-slate-700 bg-slate-900/50 p-3 text-xs">
                        <div className="font-medium text-slate-100">
                          Model routing
                        </div>
                        <div className="mt-2 space-y-1 text-slate-400">
                          <div>
                            Orchestrator:{" "}
                            {agentRuntime.models.orchestrator_model ?? "—"}
                          </div>
                          <div>
                            Workers: {agentRuntime.models.worker_model ?? "—"}
                          </div>
                          <div>
                            Builder: {agentRuntime.models.builder_model ?? "—"}
                          </div>
                          <div>
                            Fallbacks:{" "}
                            {agentRuntime.models.fallback_providers.length}
                          </div>
                        </div>
                      </div>
                      <div className="rounded-md border border-slate-700 bg-slate-900/50 p-3 text-xs">
                        <div className="font-medium text-slate-100">
                          Memory stack
                        </div>
                        <div className="mt-2 grid grid-cols-2 gap-2 text-slate-400">
                          <div>
                            Episodic: {agentRuntime.memory.episodic_facts_total}
                          </div>
                          <div>
                            Pinned: {agentRuntime.memory.pinned_facts_total}
                          </div>
                          <div>
                            Graph: {agentRuntime.memory.graph_nodes_total}
                          </div>
                          <div>Chunks: {agentRuntime.memory.chunks_total}</div>
                          <div>
                            Embeddings: {agentRuntime.memory.embeddings_total}
                          </div>
                          <div>
                            Qdrant: {agentRuntime.memory.qdrant_points ?? "—"}
                          </div>
                        </div>
                        <div className="mt-2 truncate text-slate-500">
                          {agentRuntime.memory.active_embedding_model ??
                            "embedding model —"}
                        </div>
                      </div>
                    </div>
                  )}
                  {/* ── Work Registry: Tasks / Teams / Cron / Plugins ── */}
                  <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                    {/* Tasks */}
                    <div className="rounded-md border border-slate-700 bg-slate-900/50 p-3 text-xs">
                      {(() => {
                        const activeTasks = agentTasks.filter(
                          (t) =>
                            ![
                              "completed",
                              "failed",
                              "stopped",
                              "done",
                            ].includes(t.status ?? ""),
                        );
                        return (
                          <>
                            <div className="flex items-center justify-between">
                              <span className="font-medium text-slate-100">
                                Задачи агента
                              </span>
                              <span className="rounded bg-slate-800 px-1.5 py-0.5 text-slate-400">
                                {activeTasks.length}
                              </span>
                            </div>
                            {activeTasks.length === 0 ? (
                              <div className="mt-2 text-slate-500">
                                Нет активных задач
                              </div>
                            ) : (
                              <ul className="mt-2 space-y-1">
                                {activeTasks.slice(0, 5).map((t) => (
                                  <li
                                    key={t.id}
                                    className="flex items-center gap-2 text-slate-300"
                                  >
                                    <span
                                      className={`h-1.5 w-1.5 rounded-full flex-shrink-0 ${
                                        t.status === "completed"
                                          ? "bg-green-500"
                                          : t.status === "failed"
                                            ? "bg-red-500"
                                            : "bg-yellow-500"
                                      }`}
                                    />
                                    <span className="truncate">
                                      {t.objective}
                                    </span>
                                    <span className="ml-auto flex-shrink-0 text-slate-500">
                                      {t.role}
                                    </span>
                                  </li>
                                ))}
                                {activeTasks.length > 5 && (
                                  <li className="text-slate-500">
                                    …ещё {activeTasks.length - 5}
                                  </li>
                                )}
                              </ul>
                            )}
                          </>
                        );
                      })()}
                    </div>
                    {/* Plugins */}
                    <div className="rounded-md border border-slate-700 bg-slate-900/50 p-3 text-xs">
                      <div className="flex items-center justify-between">
                        <span className="font-medium text-slate-100">
                          Плагины
                        </span>
                        <span className="rounded bg-slate-800 px-1.5 py-0.5 text-slate-400">
                          {agentPlugins.filter((p) => p.enabled).length}/
                          {agentPlugins.length}
                        </span>
                      </div>
                      {agentPlugins.length === 0 ? (
                        <div className="mt-2 text-slate-500">
                          Плагины не установлены
                        </div>
                      ) : (
                        <ul className="mt-2 space-y-1.5">
                          {agentPlugins.map((p) => (
                            <li key={p.id} className="flex items-center gap-2">
                              <span
                                className={`h-1.5 w-1.5 rounded-full flex-shrink-0 ${p.enabled ? "bg-green-500" : "bg-slate-600"}`}
                              />
                              <span className="truncate text-slate-300">
                                {p.name}
                              </span>
                              <span className="ml-auto flex-shrink-0 text-slate-500">
                                v{p.version}
                              </span>
                              <button
                                className={`rounded px-1.5 py-0.5 text-[10px] ${p.enabled ? "bg-red-900/40 text-red-300 hover:bg-red-900/60" : "bg-green-900/40 text-green-300 hover:bg-green-900/60"}`}
                                onClick={() =>
                                  togglePlugin(p.plugin_key, !p.enabled)
                                }
                              >
                                {p.enabled ? "Откл" : "Вкл"}
                              </button>
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                    {/* Cron */}
                    <div className="rounded-md border border-slate-700 bg-slate-900/50 p-3 text-xs">
                      <div className="flex items-center justify-between">
                        <span className="font-medium text-slate-100">
                          Расписание (Cron)
                        </span>
                        <span className="rounded bg-slate-800 px-1.5 py-0.5 text-slate-400">
                          {agentCrons.filter((c) => c.enabled).length}/
                          {agentCrons.length} активных
                        </span>
                      </div>
                      {agentCrons.length === 0 ? (
                        <div className="mt-2 text-slate-500">
                          Задания не настроены
                        </div>
                      ) : (
                        <ul className="mt-2 space-y-1.5">
                          {agentCrons.map((c) => (
                            <li key={c.id} className="flex items-center gap-2">
                              <span
                                className={`h-1.5 w-1.5 rounded-full flex-shrink-0 ${c.enabled ? "bg-green-500" : "bg-slate-600"}`}
                              />
                              <span className="truncate text-slate-300">
                                {c.description || c.prompt.slice(0, 40)}
                              </span>
                              <span className="ml-auto flex-shrink-0 font-mono text-slate-500">
                                {c.schedule}
                              </span>
                              <button
                                className={`rounded px-1.5 py-0.5 text-[10px] ${c.enabled ? "bg-red-900/40 text-red-300 hover:bg-red-900/60" : "bg-green-900/40 text-green-300 hover:bg-green-900/60"}`}
                                onClick={() => toggleCron(c.id, !c.enabled)}
                              >
                                {c.enabled ? "Пауза" : "Запуск"}
                              </button>
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                    {/* Teams */}
                    <div className="rounded-md border border-slate-700 bg-slate-900/50 p-3 text-xs">
                      <div className="flex items-center justify-between">
                        <span className="font-medium text-slate-100">
                          Команды агентов
                        </span>
                        <span className="rounded bg-slate-800 px-1.5 py-0.5 text-slate-400">
                          {agentTeams.length}
                        </span>
                      </div>
                      {agentTeams.length === 0 ? (
                        <div className="mt-2 text-slate-500">
                          Команды не созданы
                        </div>
                      ) : (
                        <ul className="mt-2 space-y-1">
                          {agentTeams.map((t) => (
                            <li
                              key={t.id}
                              className="flex items-center gap-2 text-slate-300"
                            >
                              <span className="truncate">{t.name}</span>
                              {t.purpose && (
                                <span className="ml-auto flex-shrink-0 truncate text-slate-500 max-w-[120px]">
                                  {t.purpose}
                                </span>
                              )}
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  </div>

                  {agentConfigProposals.length > 0 && (
                    <div className="rounded-md border border-amber-800/50 bg-amber-950/20 p-3">
                      <div className="text-sm font-medium text-amber-200">
                        Ожидают подтверждения защищенные настройки
                      </div>
                      <div className="mt-2 space-y-2">
                        {agentConfigProposals.slice(0, 5).map((proposal) => (
                          <div
                            key={proposal.id}
                            className="rounded border border-amber-900/60 bg-slate-950/40 p-3 text-xs"
                          >
                            <div className="flex flex-wrap items-start justify-between gap-3">
                              <div>
                                <div className="flex flex-wrap items-center gap-2">
                                  <span className="font-mono text-amber-100">
                                    {proposal.setting_path}
                                  </span>
                                  <span className="rounded bg-amber-900/50 px-1.5 py-0.5 text-amber-200">
                                    {proposal.risk_level}
                                  </span>
                                  <span className="text-slate-500">
                                    {proposal.requested_by}
                                  </span>
                                </div>
                                <div className="mt-1 line-clamp-2 text-slate-400">
                                  {proposal.reason}
                                </div>
                              </div>
                              <div className="flex gap-2">
                                <button
                                  type="button"
                                  disabled={agentSaving}
                                  onClick={() =>
                                    decideAgentConfigProposal(proposal.id, true)
                                  }
                                  className="rounded bg-emerald-700 px-2 py-1 text-xs text-white hover:bg-emerald-600 disabled:opacity-50"
                                >
                                  Разрешить
                                </button>
                                <button
                                  type="button"
                                  disabled={agentSaving}
                                  onClick={() =>
                                    decideAgentConfigProposal(
                                      proposal.id,
                                      false,
                                    )
                                  }
                                  className="rounded bg-slate-700 px-2 py-1 text-xs text-slate-100 hover:bg-slate-600 disabled:opacity-50"
                                >
                                  Отклонить
                                </button>
                              </div>
                            </div>
                            <div className="mt-3 grid grid-cols-1 gap-2 md:grid-cols-2">
                              <pre className="max-h-28 overflow-auto rounded bg-slate-950/70 p-2 text-slate-500">
                                {formatProposalValue(proposal.current_value)}
                              </pre>
                              <pre className="max-h-28 overflow-auto rounded bg-slate-950/70 p-2 text-amber-100">
                                {formatProposalValue(proposal.proposed_value)}
                              </pre>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                  {(() => {
                    const DONE_STATUSES = [
                      "promoted",
                      "rejected",
                      "rolled_back",
                    ];
                    const openProposals = capabilityProposals.filter(
                      (p) => !DONE_STATUSES.includes(p.status),
                    );
                    const doneProposals = capabilityProposals.filter((p) =>
                      DONE_STATUSES.includes(p.status),
                    );
                    const statusColor: Record<string, string> = {
                      draft: "text-slate-400 bg-slate-800",
                      sandbox_ready: "text-blue-300 bg-blue-900/50",
                      approved: "text-emerald-300 bg-emerald-950",
                      promoted: "text-violet-300 bg-violet-950",
                      rejected: "text-red-400 bg-red-950",
                    };
                    return (
                      <>
                        {openProposals.length > 0 && (
                          <div className="rounded-md border border-blue-800/50 bg-blue-950/20 p-3">
                            <div className="flex items-center justify-between">
                              <span className="text-sm font-medium text-blue-200">
                                Capability proposals
                              </span>
                              <span className="rounded bg-blue-900/50 px-1.5 py-0.5 text-xs text-blue-300">
                                {openProposals.length} ожидают
                              </span>
                            </div>
                            <div className="mt-2 space-y-2">
                              {openProposals.slice(0, 5).map((proposal) => (
                                <div
                                  key={proposal.id}
                                  className="rounded border border-blue-900/60 bg-slate-950/40 p-3 text-xs"
                                >
                                  <div className="flex flex-wrap items-start justify-between gap-2">
                                    <div className="min-w-0 flex-1">
                                      <div className="flex flex-wrap items-center gap-1.5">
                                        <span className="font-medium text-blue-100">
                                          {proposal.title}
                                        </span>
                                        <span
                                          className={`rounded px-1.5 py-0.5 text-[10px] ${statusColor[proposal.status] ?? "text-slate-400 bg-slate-800"}`}
                                        >
                                          {proposal.status}
                                        </span>
                                        <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-300">
                                          {proposal.risk_level}
                                        </span>
                                        {proposal.decided_by ===
                                          "auto-policy" && (
                                          <span className="rounded bg-emerald-950 px-1.5 py-0.5 text-[10px] text-emerald-300">
                                            auto
                                          </span>
                                        )}
                                      </div>
                                      <div className="mt-1 line-clamp-2 text-slate-400">
                                        {proposal.missing_capability}
                                      </div>
                                      {proposal.sandbox_status && (
                                        <div className="mt-1 flex gap-2 text-slate-500">
                                          <span>
                                            Sandbox: {proposal.sandbox_status}
                                          </span>
                                          {proposal.test_status && (
                                            <span>
                                              Tests: {proposal.test_status}
                                            </span>
                                          )}
                                        </div>
                                      )}
                                    </div>
                                    <div className="flex flex-wrap gap-1.5">
                                      {!["sandbox_ready", "approved"].includes(
                                        proposal.status,
                                      ) && (
                                        <button
                                          type="button"
                                          disabled={agentSaving}
                                          onClick={() =>
                                            sandboxApplyCapabilityProposal(
                                              proposal.id,
                                            )
                                          }
                                          className="rounded bg-blue-700 px-2 py-1 text-xs text-white hover:bg-blue-600 disabled:opacity-50"
                                        >
                                          Sandbox
                                        </button>
                                      )}
                                      {proposal.status !== "approved" && (
                                        <button
                                          type="button"
                                          disabled={agentSaving}
                                          onClick={() =>
                                            decideCapabilityProposal(
                                              proposal.id,
                                              true,
                                            )
                                          }
                                          className="rounded bg-emerald-700 px-2 py-1 text-xs text-white hover:bg-emerald-600 disabled:opacity-50"
                                        >
                                          Разрешить
                                        </button>
                                      )}
                                      <button
                                        type="button"
                                        disabled={agentSaving}
                                        onClick={() =>
                                          decideCapabilityProposal(
                                            proposal.id,
                                            false,
                                          )
                                        }
                                        className="rounded bg-slate-700 px-2 py-1 text-xs text-slate-100 hover:bg-slate-600 disabled:opacity-50"
                                      >
                                        Отклонить
                                      </button>
                                    </div>
                                  </div>
                                </div>
                              ))}
                            </div>
                          </div>
                        )}
                        {doneProposals.length > 0 && (
                          <div className="rounded-md border border-slate-700/50 bg-slate-900/30 p-3 text-xs text-slate-500">
                            <span className="font-medium">История:</span>{" "}
                            {doneProposals
                              .slice(0, 3)
                              .map((p) => (
                                <span
                                  key={p.id}
                                  className="ml-1 inline-flex items-center gap-1"
                                >
                                  <span
                                    className={`rounded px-1 py-0.5 text-[10px] ${statusColor[p.status] ?? ""}`}
                                  >
                                    {p.status}
                                  </span>
                                  {p.title}
                                </span>
                              ))
                              .reduce<React.ReactNode[]>(
                                (acc, el, i) =>
                                  i === 0 ? [el] : [...acc, " · ", el],
                                [],
                              )}
                          </div>
                        )}
                      </>
                    );
                  })()}
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <label className="flex items-start gap-3 rounded-md bg-slate-900/50 border border-slate-700 p-3">
                      <input
                        type="checkbox"
                        className="mt-0.5"
                        checked={agentConfig.department_enabled}
                        onChange={(e) =>
                          setAgentConfig({
                            ...agentConfig,
                            department_enabled: e.target.checked,
                          })
                        }
                      />
                      <span className="text-sm text-slate-200">
                        Включить оркестратор отдела
                      </span>
                    </label>
                    <label className="flex items-start gap-3 rounded-md bg-slate-900/50 border border-slate-700 p-3">
                      <input
                        type="checkbox"
                        className="mt-0.5"
                        checked={agentConfig.audit_enabled}
                        onChange={(e) =>
                          setAgentConfig({
                            ...agentConfig,
                            audit_enabled: e.target.checked,
                          })
                        }
                      />
                      <span className="text-sm text-slate-200">
                        Проверять результат аудитором
                      </span>
                    </label>
                    <label className="flex items-start gap-3 rounded-md bg-slate-900/50 border border-slate-700 p-3">
                      <input
                        type="checkbox"
                        className="mt-0.5"
                        checked={agentConfig.allow_capability_builder}
                        onChange={(e) =>
                          setAgentConfig({
                            ...agentConfig,
                            allow_capability_builder: e.target.checked,
                          })
                        }
                      />
                      <span className="text-sm text-slate-200">
                        Разрешить выявлять недостающие tools/skills
                      </span>
                    </label>
                    <label className="flex items-start gap-3 rounded-md bg-slate-900/50 border border-slate-700 p-3">
                      <input
                        type="checkbox"
                        className="mt-0.5"
                        checked={
                          agentConfig.capability_builder_requires_approval
                        }
                        onChange={(e) =>
                          setAgentConfig({
                            ...agentConfig,
                            capability_builder_requires_approval:
                              e.target.checked,
                          })
                        }
                      />
                      <span className="text-sm text-slate-200">
                        Требовать подтверждение перед builder-режимом
                      </span>
                    </label>
                  </div>

                  <div className="rounded-md border border-emerald-800/50 bg-emerald-950/20 p-3">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <div className="text-sm font-medium text-emerald-200">
                          Стабильный локальный режим
                        </div>
                        <div className="mt-1 text-xs text-emerald-100/70">
                          3 модели: малая для оркестратора, основная для
                          исполнителей и аудитора, большая только для builder и
                          сложных задач.
                        </div>
                      </div>
                      <button
                        type="button"
                        disabled={agentSaving}
                        onClick={handleApplyStableLocalPreset}
                        className="rounded bg-emerald-700 px-3 py-2 text-sm font-medium text-white hover:bg-emerald-600 disabled:opacity-50"
                      >
                        Применить режим
                      </button>
                    </div>
                  </div>

                  {/* Ollama diagnostics */}
                  {ollamaAvailable === false && ollamaError && (
                    <div className="rounded-md border border-red-800/60 bg-red-950/20 p-3 text-xs text-red-300">
                      <span className="font-medium">Ollama недоступен</span>
                      {" — "}
                      {ollamaError}. Списки локальных моделей пусты.
                    </div>
                  )}
                  {ollamaAvailable && ollamaGpuInfo !== null && (
                    <div
                      className={`rounded-md border p-3 text-xs ${ollamaGpuInfo.has_gpu ? "border-emerald-800/50 bg-emerald-950/20 text-emerald-300" : "border-amber-800/50 bg-amber-950/20 text-amber-300"}`}
                    >
                      {ollamaGpuInfo.has_gpu ? (
                        <>
                          GPU активен. Запущено моделей:{" "}
                          {ollamaGpuInfo.running_models.length}
                          {ollamaGpuInfo.running_models.length > 0 && (
                            <span className="ml-2 text-emerald-200">
                              {ollamaGpuInfo.running_models
                                .map((m) => m.name)
                                .join(", ")}
                            </span>
                          )}
                        </>
                      ) : (
                        "GPU не обнаружен в Ollama — модели работают на CPU. Проверьте NVIDIA-драйверы и nvidia-container-toolkit на хосте."
                      )}
                    </div>
                  )}
                  {ollamaAvailable &&
                    ollamaGpuInfo === null &&
                    models.length === 0 && (
                      <div className="rounded-md border border-amber-800/50 bg-amber-950/20 p-3 text-xs text-amber-300">
                        Ollama доступен, но модели не загружены. Перейдите на
                        вкладку «Модели» чтобы скачать модель.
                      </div>
                    )}

                  {/* Default provider & model — used as fallback when per-role is not configured */}
                  <div className="rounded-md border border-slate-700 bg-slate-950/30 p-4">
                    <div className="mb-3 text-sm font-medium text-slate-300">
                      Провайдер по умолчанию
                      <span className="ml-2 text-xs font-normal text-slate-500">
                        — используется когда для роли не задан отдельный
                        провайдер/модель
                      </span>
                    </div>
                    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                      <Field
                        label="Провайдер"
                        hint="Ollama (локально) рекомендуется"
                      >
                        <select
                          value={agentConfig.provider}
                          onChange={(e) =>
                            setAgentConfig({
                              ...agentConfig,
                              provider: e.target.value,
                            })
                          }
                          className={selectCls}
                        >
                          {PROVIDERS.map((p) => (
                            <option key={p.value} value={p.value}>
                              {p.label}
                            </option>
                          ))}
                        </select>
                      </Field>

                      <DefaultModelField
                        provider={agentConfig.provider}
                        model={agentConfig.model}
                        ollamaModels={models}
                        ggufModels={ggufModels}
                        registryModels={registryModels}
                        onChange={(model) =>
                          setAgentConfig({ ...agentConfig, model })
                        }
                      />
                    </div>

                    {/* Local endpoint URL */}
                    {LOCAL_PROVIDER_URL_LABEL[agentConfig.provider] && (
                      <div className="mt-3">
                        <Field
                          label={`${PROVIDERS.find((p) => p.value === agentConfig.provider)?.label ?? "Provider"} URL`}
                          hint="Адрес локального endpoint"
                        >
                          <input
                            className={inputCls}
                            value={String(
                              agentConfig[
                                LOCAL_PROVIDER_URL_LABEL[agentConfig.provider]
                              ] ?? "",
                            )}
                            onChange={(e) =>
                              setAgentConfig({
                                ...agentConfig,
                                [LOCAL_PROVIDER_URL_LABEL[
                                  agentConfig.provider
                                ]]: e.target.value,
                              })
                            }
                          />
                        </Field>
                      </div>
                    )}

                    {/* llamacpp info */}
                    {agentConfig.provider === "llamacpp" && (
                      <div className="mt-3 flex items-start gap-2 rounded-md bg-blue-950/30 border border-blue-800/40 px-3 py-2 text-xs text-blue-300">
                        <span>⚡</span>
                        <span>
                          llama.cpp сервер: настройки и загрузка моделей в{" "}
                          <a href="/settings/models" className="underline">
                            Настройки / llama.cpp
                          </a>
                        </span>
                      </div>
                    )}

                    {/* Cloud API key hint */}
                    {agentConfig.provider !== "ollama" &&
                      agentConfig.provider !== "llamacpp" &&
                      !LOCAL_PROVIDER_URL_LABEL[agentConfig.provider] && (
                        <div className="mt-3 flex items-start gap-2 rounded-md bg-amber-950/30 border border-amber-800/40 px-3 py-2 text-xs text-amber-300">
                          <span>🔑</span>
                          <span>
                            Установите{" "}
                            <code className="font-mono bg-amber-900/40 px-1 rounded">
                              {PROVIDER_ENV[agentConfig.provider] ?? "API_KEY"}
                            </code>{" "}
                            в{" "}
                            <code className="font-mono bg-amber-900/40 px-1 rounded">
                              .env
                            </code>
                          </span>
                        </div>
                      )}

                    {/* Anthropic prompt caching */}
                    {agentConfig.provider === "anthropic" && (
                      <label className="mt-3 flex items-start gap-3 rounded-md border border-slate-700 bg-slate-900/50 p-3 cursor-pointer">
                        <input
                          type="checkbox"
                          className="mt-0.5"
                          checked={agentConfig.prompt_cache_enabled}
                          onChange={(e) =>
                            setAgentConfig({
                              ...agentConfig,
                              prompt_cache_enabled: e.target.checked,
                            })
                          }
                        />
                        <div>
                          <span className="text-sm text-slate-200">
                            Prompt Caching (beta)
                          </span>
                          <p className="mt-0.5 text-xs text-slate-400">
                            Ускоряет повторные запросы, снижает стоимость при
                            длинных сессиях.
                          </p>
                        </div>
                      </label>
                    )}
                  </div>

                  <div className="grid grid-cols-1 gap-4">
                    {(
                      [
                        [
                          "orchestrator",
                          "Оркестратор",
                          "orchestrator_provider",
                          "orchestrator_model",
                          "orchestrator_disable_thinking",
                          agentConfig.provider,
                          agentConfig.model,
                        ],
                        [
                          "worker",
                          "Исполнители",
                          "worker_provider",
                          "worker_model",
                          "worker_disable_thinking",
                          agentConfig.provider,
                          agentConfig.model,
                        ],
                        [
                          "auditor",
                          "Аудитор",
                          "auditor_provider",
                          "auditor_model",
                          "auditor_disable_thinking",
                          agentConfig.provider,
                          agentConfig.model,
                        ],
                        [
                          "builder",
                          "Большая builder-модель",
                          "builder_provider",
                          "builder_model",
                          "builder_disable_thinking",
                          agentConfig.orchestrator_provider ||
                            agentConfig.provider,
                          agentConfig.orchestrator_model || agentConfig.model,
                        ],
                        [
                          "fast",
                          "Быстрая модель",
                          "fast_provider",
                          "fast_model",
                          "fast_disable_thinking",
                          agentConfig.provider,
                          agentConfig.model,
                        ],
                      ] as const
                    ).map(
                      ([
                        ,
                        label,
                        providerKey,
                        modelKey,
                        thinkingKey,
                        fallbackProvider,
                        fallbackModel,
                      ]) => (
                        <AgentModelProviderSelector
                          key={providerKey}
                          label={label}
                          provider={agentConfig[providerKey]}
                          model={agentConfig[modelKey]}
                          disableThinking={agentConfig[thinkingKey]}
                          fallbackProvider={fallbackProvider}
                          fallbackModel={fallbackModel}
                          models={models}
                          registryModels={registryModels}
                          onChange={(next) =>
                            setAgentConfig({
                              ...agentConfig,
                              [providerKey]: next.provider,
                              [modelKey]: next.model,
                              [thinkingKey]: next.disableThinking,
                            })
                          }
                        />
                      ),
                    )}
                  </div>

                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    <Field
                      label="Max worker steps"
                      hint="Сколько шагов может сделать назначенный исполнитель"
                    >
                      <input
                        className={`${inputCls} max-w-xs`}
                        type="number"
                        min={1}
                        max={60}
                        value={agentConfig.max_worker_steps}
                        onChange={(e) =>
                          setAgentConfig({
                            ...agentConfig,
                            max_worker_steps: Number(e.target.value),
                          })
                        }
                      />
                    </Field>
                    <Field
                      label="Audit retries"
                      hint="Сколько раз оркестратор может пытаться исправить провал аудита"
                    >
                      <input
                        className={`${inputCls} max-w-xs`}
                        type="number"
                        min={0}
                        max={5}
                        value={agentConfig.max_audit_retries}
                        onChange={(e) =>
                          setAgentConfig({
                            ...agentConfig,
                            max_audit_retries: Number(e.target.value),
                          })
                        }
                      />
                    </Field>
                  </div>
                </div>
              </SectionCard>

              {/* Generation params */}
              <SectionCard title="Параметры генерации">
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  {(
                    [
                      {
                        key: "temperature",
                        label: "Temperature",
                        min: 0,
                        max: 2,
                        step: 0.05,
                      },
                      {
                        key: "max_steps",
                        label: "Max steps",
                        min: 1,
                        max: 30,
                        step: 1,
                      },
                      {
                        key: "llm_timeout_seconds",
                        label: "LLM timeout (с)",
                        min: 10,
                        max: 1800,
                        step: 1,
                      },
                      {
                        key: "approval_timeout_seconds",
                        label: "Approval timeout (с)",
                        min: 10,
                        max: 1800,
                        step: 1,
                      },
                    ] as const
                  ).map(({ key, label, min, max, step }) => (
                    <label key={key} className="text-xs text-slate-400">
                      {label}
                      <input
                        className={`mt-1 ${inputCls}`}
                        type="number"
                        min={min}
                        max={max}
                        step={step}
                        value={
                          (agentConfig as unknown as Record<string, unknown>)[
                            key
                          ] as number
                        }
                        onChange={(e) =>
                          setAgentConfig({
                            ...agentConfig,
                            [key]: Number(e.target.value),
                          })
                        }
                      />
                    </label>
                  ))}
                </div>
              </SectionCard>

              {/* Memory */}
              <SectionCard
                title="Память"
                subtitle="Долговременный контекст и поиск по истории"
              >
                <div className="space-y-4">
                  <label className="flex items-center gap-2 text-sm text-slate-200">
                    <input
                      type="checkbox"
                      checked={agentConfig.memory_enabled}
                      onChange={(e) =>
                        setAgentConfig({
                          ...agentConfig,
                          memory_enabled: e.target.checked,
                        })
                      }
                    />
                    Подключать память к каждому запросу
                  </label>
                  <div className="rounded-md border border-slate-800 bg-slate-900/40 p-3">
                    <p className="text-sm font-medium text-slate-100">
                      Автоматическая гибридная память
                    </p>
                    <p className="mt-1 text-xs leading-relaxed text-slate-400">
                      Агент сам использует SQL, Qdrant, rerank, граф связей и
                      историю чата. Ограничения выборки управляются сервером и
                      не требуют ручной настройки.
                    </p>
                  </div>
                  <Field
                    label="Макс. сообщений в истории"
                    hint="Старые сообщения обрезаются сверху"
                  >
                    <input
                      className={`${inputCls} max-w-xs`}
                      type="number"
                      min={4}
                      max={200}
                      value={agentConfig.max_history_messages}
                      onChange={(e) =>
                        setAgentConfig({
                          ...agentConfig,
                          max_history_messages: Number(e.target.value),
                        })
                      }
                    />
                  </Field>
                </div>
              </SectionCard>

              {/* Context Compression */}
              <SectionCard
                title="Сжатие контекста"
                subtitle="Когда история разговора приближается к лимиту модели, вспомогательная LLM автоматически сжимает средние сообщения."
              >
                <div className="space-y-4">
                  <label className="flex items-center gap-3 cursor-pointer">
                    <input
                      type="checkbox"
                      className="h-4 w-4 rounded border-slate-600 bg-slate-900"
                      checked={agentConfig.context_compression_enabled}
                      onChange={(e) =>
                        setAgentConfig({
                          ...agentConfig,
                          context_compression_enabled: e.target.checked,
                        })
                      }
                    />
                    <span className="text-sm text-slate-300">
                      Включить автоматическое сжатие
                    </span>
                  </label>
                  {agentConfig.context_compression_enabled && (
                    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                      <Field
                        label="Порог сжатия"
                        hint="Доля контекстного окна (0.50–0.98), при достижении которой запускается сжатие"
                      >
                        <input
                          className={`${inputCls} max-w-xs`}
                          type="number"
                          min={0.5}
                          max={0.98}
                          step={0.05}
                          value={agentConfig.context_compression_threshold}
                          onChange={(e) =>
                            setAgentConfig({
                              ...agentConfig,
                              context_compression_threshold: Number(
                                e.target.value,
                              ),
                            })
                          }
                        />
                      </Field>
                      <Field
                        label="Модель для сжатия"
                        hint="Пусто = использовать основную модель агента"
                      >
                        <input
                          className={inputCls}
                          placeholder="gemma4:e4b  или  claude-haiku-4-5"
                          value={agentConfig.compression_model ?? ""}
                          onChange={(e) =>
                            setAgentConfig({
                              ...agentConfig,
                              compression_model: e.target.value.trim() || null,
                            })
                          }
                        />
                      </Field>
                    </div>
                  )}
                </div>
              </SectionCard>

              {/* MCP Servers */}
              <SectionCard
                title="MCP Серверы"
                subtitle="Model Context Protocol — подключение внешних инструментов (filesystem, postgres и др.)."
              >
                <div className="space-y-3">
                  {(agentConfig.mcp_servers ?? []).length === 0 && (
                    <p className="text-sm text-slate-400">
                      Серверов не добавлено. MCP-инструменты автоматически
                      появятся в списке Skills после добавления сервера.
                    </p>
                  )}
                  {(agentConfig.mcp_servers ?? []).map((srv, idx) => (
                    <div
                      key={idx}
                      className="flex items-start gap-3 rounded-md border border-slate-700 bg-slate-900/50 p-3"
                    >
                      <div className="flex-1 min-w-0 space-y-1">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-medium text-slate-200">
                            {srv.name}
                          </span>
                          <span className="rounded px-1.5 py-0.5 text-xs bg-slate-700 text-slate-400">
                            {srv.transport}
                          </span>
                        </div>
                        <p className="text-xs text-slate-400 font-mono truncate">
                          {srv.transport === "stdio"
                            ? [srv.command, ...(srv.args ?? [])].join(" ")
                            : srv.url}
                        </p>
                      </div>
                      <button
                        className="text-xs text-red-400 hover:text-red-300 shrink-0"
                        onClick={() =>
                          setAgentConfig({
                            ...agentConfig,
                            mcp_servers: agentConfig.mcp_servers.filter(
                              (_, i) => i !== idx,
                            ),
                          })
                        }
                      >
                        Удалить
                      </button>
                    </div>
                  ))}
                  <button
                    className={btnSecondary}
                    onClick={() => {
                      const name = prompt("Имя сервера (например: filesystem)");
                      if (!name) return;
                      const transport = prompt(
                        "Транспорт: stdio или http",
                        "stdio",
                      ) as "stdio" | "http";
                      if (!transport) return;
                      let entry: (typeof agentConfig.mcp_servers)[0];
                      if (transport === "stdio") {
                        const cmd = prompt("Команда (например: npx)", "npx");
                        const argsStr = prompt(
                          "Аргументы через пробел",
                          "-y @modelcontextprotocol/server-filesystem /data",
                        );
                        entry = {
                          name,
                          transport: "stdio",
                          command: cmd ?? "npx",
                          args: argsStr ? argsStr.split(" ") : [],
                        };
                      } else {
                        const url = prompt(
                          "URL сервера",
                          "http://localhost:5173",
                        );
                        entry = { name, transport: "http", url: url ?? "" };
                      }
                      setAgentConfig({
                        ...agentConfig,
                        mcp_servers: [
                          ...(agentConfig.mcp_servers ?? []),
                          entry,
                        ],
                      });
                    }}
                  >
                    + Добавить сервер
                  </button>
                </div>
              </SectionCard>

              {/* Skills table */}
              <SectionCard
                title={
                  <span className="flex items-center gap-2">
                    Инструменты (Skills)
                    <span
                      className={`rounded px-1.5 py-0.5 text-xs font-medium ${
                        agentSkillsMode === "capabilities"
                          ? "bg-blue-900/60 text-blue-300"
                          : "bg-slate-700 text-slate-400"
                      }`}
                    >
                      {agentSkillsMode === "capabilities"
                        ? "capabilities"
                        : "registry"}
                    </span>
                  </span>
                }
              >
                <div className="space-y-3">
                  {agentSkillsMode === "capabilities" && (
                    <p className="text-xs text-slate-400">
                      Агент работает в режиме{" "}
                      <span className="text-blue-300">capabilities</span> —
                      {agentSkills.length} широких инструментов вместо сотен
                      endpoint-tools. Управление отдельными skills недоступно.
                    </p>
                  )}
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                    <div className="flex flex-wrap items-center gap-3">
                      <p className="text-xs text-slate-400">
                        {agentSkillsMode === "capabilities" ? (
                          <>
                            {agentSkills.length} capabilities ·{" "}
                            {
                              agentSkills.filter((s) => s.approval_required)
                                .length
                            }{" "}
                            с [GATE]
                          </>
                        ) : (
                          <>
                            Включено {selectedSkillsCount} из{" "}
                            {agentSkills.length} · подтверждений{" "}
                            {agentConfig.approval_gates.length}
                          </>
                        )}
                      </p>
                      {agentSkillsMode === "registry" && (
                        <label className="inline-flex items-center gap-2 text-xs text-slate-300">
                          <input
                            ref={selectAllSkillsRef}
                            type="checkbox"
                            checked={allSkillsSelected}
                            onChange={(e) => toggleAllSkills(e.target.checked)}
                          />
                          Выбрать все скиллы
                        </label>
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      <input
                        className="w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-1.5 text-xs text-slate-200 sm:w-48 focus:outline-none focus:ring-2 focus:ring-blue-500"
                        value={agentSkillFilter}
                        onChange={(e) => setAgentSkillFilter(e.target.value)}
                        placeholder="Поиск…"
                      />
                      <a
                        href="/settings/skills"
                        className="shrink-0 text-xs text-blue-400 hover:text-blue-300 hover:underline whitespace-nowrap"
                      >
                        Маркетплейс →
                      </a>
                    </div>
                  </div>
                  <div className="max-h-72 overflow-auto rounded-md border border-slate-700">
                    <table className="w-full text-left text-xs">
                      <thead className="sticky top-0 bg-slate-900 text-slate-400">
                        <tr>
                          {agentSkillsMode === "registry" && (
                            <>
                              <th className="w-20 px-3 py-2 font-medium">
                                Агент
                              </th>
                              <th className="w-24 px-3 py-2 font-medium">
                                Подтв.
                              </th>
                            </>
                          )}
                          {agentSkillsMode === "capabilities" && (
                            <th className="w-20 px-3 py-2 font-medium">
                              [GATE]
                            </th>
                          )}
                          <th className="px-3 py-2 font-medium">
                            {agentSkillsMode === "capabilities"
                              ? "Capability"
                              : "Tool"}
                          </th>
                          <th className="hidden px-3 py-2 font-medium md:table-cell">
                            Endpoint
                          </th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-800">
                        {filteredAgentSkills.map((skill, index) => {
                          const isExposed =
                            agentSkillsMode === "capabilities"
                              ? skill.enabled
                              : agentConfig.exposed_skills.includes(skill.name);
                          const needsApproval =
                            agentSkillsMode === "capabilities"
                              ? skill.approval_required
                              : agentConfig.approval_gates.includes(skill.name);
                          return (
                            <tr
                              key={`${skill.name}-${index}`}
                              className="bg-slate-900/40 hover:bg-slate-900/70"
                            >
                              {agentSkillsMode === "registry" && (
                                <>
                                  <td className="px-3 py-2">
                                    <input
                                      type="checkbox"
                                      checked={isExposed}
                                      ref={(el) => {
                                        agentSkillToggleRefs.current[index] =
                                          el;
                                      }}
                                      onKeyDown={(event) =>
                                        handleSkillArrowNavigation(event, index)
                                      }
                                      onChange={(e) =>
                                        updateAgentSkill(
                                          skill.name,
                                          e.target.checked,
                                        )
                                      }
                                    />
                                  </td>
                                  <td className="px-3 py-2">
                                    <input
                                      type="checkbox"
                                      checked={needsApproval}
                                      onChange={(e) =>
                                        updateAgentApprovalGate(
                                          skill.name,
                                          e.target.checked,
                                        )
                                      }
                                    />
                                  </td>
                                </>
                              )}
                              {agentSkillsMode === "capabilities" && (
                                <td className="px-3 py-2">
                                  {needsApproval ? (
                                    <span className="rounded bg-amber-900/50 px-1.5 py-0.5 text-amber-300">
                                      GATE
                                    </span>
                                  ) : (
                                    <span className="text-slate-600">—</span>
                                  )}
                                </td>
                              )}
                              <td className="px-3 py-2">
                                <div className="font-mono text-slate-200">
                                  {skill.name}
                                </div>
                                <div className="mt-0.5 text-slate-500">
                                  {skill.description || "—"}
                                </div>
                              </td>
                              <td className="hidden px-3 py-2 font-mono text-slate-500 md:table-cell">
                                {skill.method} {skill.path}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                </div>
              </SectionCard>

              {/* System prompt */}
              <SectionCard
                title="Системный промпт"
                subtitle="Переопределяет базовый промпт из aiagent/prompts/base.md"
              >
                <Field
                  label=""
                  hint="Оставьте пустым чтобы использовать базовый промпт"
                >
                  <textarea
                    className={`${inputCls} h-28 font-mono text-xs`}
                    value={agentConfig.system_prompt ?? ""}
                    onChange={(e) =>
                      setAgentConfig({
                        ...agentConfig,
                        system_prompt: e.target.value || null,
                      })
                    }
                    placeholder="Пусто: используется базовый промпт aiagent/prompts/base.md"
                  />
                </Field>
              </SectionCard>

              {agentError && (
                <div className="rounded-md border border-red-800/60 bg-red-950/30 px-3 py-2 text-sm text-red-200">
                  {agentError}
                </div>
              )}

              <SaveRow
                saving={agentSaving}
                saved={agentSaved}
                onSave={handleSaveAgentConfig}
                onReset={handleResetAgentConfig}
                saveLabel="Сохранить настройки агента"
              />
            </>
          ) : (
            <div className="text-sm text-slate-400 py-12 text-center">
              Загрузка конфигурации агента…
            </div>
          )}
        </div>
      )}

      {/* ── Trust Score Auto-approval ──────────────────────────────────── */}
      {activeTab === "agent" && (
        <SectionCard title="Автоутверждение по Trust Score">
          <p className="text-xs text-slate-400 mb-4">
            Счета от поставщиков с высоким Trust Score утверждаются
            автоматически без участия человека.
          </p>
          <div className="space-y-4">
            <label className="flex items-center gap-3 cursor-pointer">
              <input
                type="checkbox"
                checked={approvalPolicy.enabled}
                onChange={(e) =>
                  setApprovalPolicy((p) => ({
                    ...p,
                    enabled: e.target.checked,
                  }))
                }
                className="w-4 h-4 accent-blue-500"
              />
              <span className="text-sm text-slate-200">
                Включить автоутверждение
              </span>
            </label>
            <div className="flex items-center gap-4">
              <label className="text-xs text-slate-400 w-40 shrink-0">
                Минимальный Trust Score
              </label>
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={approvalPolicy.trust_threshold}
                onChange={(e) =>
                  setApprovalPolicy((p) => ({
                    ...p,
                    trust_threshold: Number(e.target.value),
                  }))
                }
                disabled={!approvalPolicy.enabled}
                className="flex-1 accent-blue-500 disabled:opacity-40"
              />
              <span className="text-sm font-mono text-slate-200 w-10 text-right">
                {(approvalPolicy.trust_threshold * 100).toFixed(0)}%
              </span>
            </div>
            <div className="flex items-center gap-4">
              <label className="text-xs text-slate-400 w-40 shrink-0">
                Макс. сумма счёта (₽)
              </label>
              <input
                type="number"
                value={approvalPolicy.max_amount ?? ""}
                onChange={(e) =>
                  setApprovalPolicy((p) => ({
                    ...p,
                    max_amount: e.target.value ? Number(e.target.value) : null,
                  }))
                }
                disabled={!approvalPolicy.enabled}
                placeholder="Без ограничения"
                className="w-40 px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-blue-400 disabled:opacity-40"
              />
            </div>
            <div className="flex items-center gap-3">
              <button
                onClick={saveApprovalPolicy}
                disabled={policyLoading}
                className="px-4 py-1.5 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
              >
                {policyLoading ? "Сохраняю…" : "Сохранить"}
              </button>
              {policySaved && (
                <span className="text-xs text-green-400">Сохранено ✓</span>
              )}
            </div>
          </div>
        </SectionCard>
      )}

      {/* ── TAB: Память ──────────────────────────────────────────────────── */}
      {activeTab === "memory" && (
        <div className="space-y-6">
          <SectionCard
            title="Векторная память"
            subtitle="Активный embedding profile определяет Qdrant-коллекцию, размерность и модель."
            action={
              <div className="flex gap-2">
                <button
                  onClick={handleRebuildEmbeddings}
                  disabled={rebuildingEmbeddings}
                  className={btnSecondary}
                >
                  {rebuildingEmbeddings ? "Готовлю…" : "Подготовить records"}
                </button>
                <button
                  onClick={handleIndexEmbeddings}
                  disabled={indexingEmbeddings}
                  className={btnPrimary}
                >
                  {indexingEmbeddings
                    ? "Индексирую…"
                    : "Индексировать в Qdrant"}
                </button>
              </div>
            }
          >
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              {[
                {
                  label: "Модель",
                  value:
                    embeddingStats?.active_model ??
                    embeddingProfile?.model_key ??
                    "—",
                },
                {
                  label: "Коллекция",
                  value:
                    embeddingStats?.active_collection ??
                    embeddingProfile?.collection_name ??
                    "—",
                },
                {
                  label: "Записей",
                  value: embeddingStats ? String(embeddingStats.total) : "—",
                },
              ].map(({ label, value }) => (
                <div key={label} className="rounded-md bg-slate-900/50 p-3">
                  <p className="text-xs text-slate-500">{label}</p>
                  <p className="mt-1 text-sm font-mono text-slate-200 break-all">
                    {value}
                  </p>
                </div>
              ))}
            </div>
            {embeddingStats && (
              <p className="mt-3 text-xs text-slate-400">
                Статусы:{" "}
                {Object.entries(embeddingStats.counts_by_status)
                  .map(([s, c]) => `${s}: ${c}`)
                  .join(" · ") || "нет записей"}
              </p>
            )}
            {rebuildMessage && (
              <p className="mt-3 text-xs text-slate-300">{rebuildMessage}</p>
            )}
          </SectionCard>
        </div>
      )}

      {/* ── TAB: Данные ──────────────────────────────────────────────────── */}
      {activeTab === "data" && (
        <div className="space-y-6">
          {/* NTD control */}
          <SectionCard
            title="Нормоконтроль НТД"
            subtitle="Проверка документов по базе НТД — вручную или автоматически после обработки."
            action={
              ntdSaved ? (
                <span className="text-xs text-emerald-400 pt-1">Сохранено</span>
              ) : undefined
            }
          >
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              {[
                {
                  mode: "manual" as const,
                  title: "Вручную",
                  desc: 'Кнопка "Проверить на соответствие НТД" в review-документе.',
                },
                {
                  mode: "auto" as const,
                  title: "Автоматически",
                  desc: "Нормоконтроль запускается после extraction, без применения исправлений.",
                },
              ].map(({ mode, title, desc }) => (
                <button
                  key={mode}
                  onClick={() => handleSaveNtdConfig(mode)}
                  disabled={ntdSaving}
                  className={`text-left rounded-md border p-4 transition ${
                    ntdConfig?.mode === mode
                      ? "border-blue-500 bg-blue-950/30"
                      : "border-slate-700 bg-slate-900/40 hover:border-slate-500"
                  } disabled:opacity-50`}
                >
                  <span className="block text-sm font-semibold text-slate-100">
                    {title}
                  </span>
                  <span className="mt-1 block text-xs text-slate-400">
                    {desc}
                  </span>
                </button>
              ))}
            </div>
            <p className="mt-3 text-xs text-slate-500">
              Текущий режим:{" "}
              {ntdConfig?.mode === "auto" ? "автоматический" : "ручной"}
              {ntdConfig?.updated_at
                ? ` · обновлено ${new Date(ntdConfig.updated_at).toLocaleString("ru-RU")}`
                : ""}
            </p>
          </SectionCard>

          {/* Links to sub-pages */}
          <SectionCard
            title="Разделы данных"
            subtitle="Упр��вление нормативной базой и правилами нормализации"
          >
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              {[
                {
                  href: "/settings/ntd",
                  title: "НТД",
                  desc: "Нормативные документы и требования",
                },
                {
                  href: "/settings/norm-cards",
                  title: "Нормкарточки",
                  desc: "Нормы расхода и каталог ОКПД2",
                },
                {
                  href: "/settings/normalization",
                  title: "Нормализация",
                  desc: "Правила автоматической нормализации",
                },
              ].map(({ href, title, desc }) => (
                <Link
                  key={href}
                  href={href}
                  className="block rounded-md border border-slate-700 bg-slate-900/40 p-4 hover:bg-slate-700/50 hover:border-slate-500 transition-colors"
                >
                  <span className="block text-sm font-semibold text-slate-100">
                    {title}
                  </span>
                  <span className="mt-1 block text-xs text-slate-400">
                    {desc}
                  </span>
                </Link>
              ))}
            </div>
          </SectionCard>

          {/* Dev purge */}
          <section className="rounded-lg border border-red-900 bg-red-950/20 p-6">
            <h2 className="text-lg font-semibold text-red-100">
              Полная очистка документов
            </h2>
            <p className="mt-1 text-sm text-red-200/80">
              Dev-команда удаляет все документы, файлы и связанные записи:
              извлечения, память, граф, НТД-проверки, счета, техпроцессы, BOM и
              складские приёмки.
            </p>
            <div className="mt-4 flex flex-col gap-3 sm:flex-row">
              <input
                value={purgeConfirm}
                onChange={(e) => setPurgeConfirm(e.target.value)}
                placeholder='Введите "DELETE ALL DOCUMENT DATA"'
                className="flex-1 rounded-md border border-red-900 bg-slate-950 px-3 py-2 text-sm text-slate-100"
              />
              <button
                onClick={handleDevelopmentPurge}
                disabled={
                  purgeBusy || purgeConfirm !== "DELETE ALL DOCUMENT DATA"
                }
                className="rounded-md bg-red-700 px-4 py-2 text-sm text-white hover:bg-red-600 disabled:opacity-50"
              >
                {purgeBusy ? "Очищаю..." : "Очистить всё"}
              </button>
            </div>
            {purgeMessage && (
              <p className="mt-3 text-xs text-red-100">{purgeMessage}</p>
            )}
          </section>
        </div>
      )}

      {/* ── TAB: Система ─────────────────────────────────────────────────── */}
      {activeTab === "system" && (
        <div className="space-y-6">
          {/* Telegram */}
          <SectionCard
            title="Telegram"
            subtitle="Бот для уведомлений и управления агентом из Telegram. Токен и ID хранятся зашифрованными в Redis."
            action={
              <button
                className={btnSecondary}
                onClick={() => {
                  setTgEditing((v) => !v);
                  setTgTestResult(null);
                  if (!tgEditing) {
                    setTgDraft({
                      bot_token: "",
                      notifications_chat_id: "",
                      allowed_users: tgStatus?.allowed_users_masked ?? "",
                      notifications_enabled:
                        tgStatus?.notifications_enabled ?? false,
                    });
                  }
                }}
              >
                {tgEditing ? "Отмена" : "Изменить"}
              </button>
            }
          >
            <div className="space-y-4">
              {/* Status badges */}
              <div className="flex flex-wrap items-center gap-2">
                {/* Configured */}
                <span
                  className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium border ${
                    tgStatus?.configured
                      ? "bg-green-900/50 text-green-300 border-green-700"
                      : "bg-slate-700 text-slate-400 border-slate-600"
                  }`}
                >
                  <span
                    className={`w-1.5 h-1.5 rounded-full ${tgStatus?.configured ? "bg-green-400" : "bg-slate-500"}`}
                  />
                  {tgStatus?.configured ? "Настроен" : "Не настроен"}
                </span>
                {/* Polling status */}
                {tgStatus?.configured && (
                  <span
                    className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium border ${
                      tgStatus.bot_running
                        ? "bg-emerald-900/50 text-emerald-300 border-emerald-700"
                        : "bg-yellow-900/50 text-yellow-300 border-yellow-700"
                    }`}
                  >
                    <span
                      className={`w-1.5 h-1.5 rounded-full ${tgStatus.bot_running ? "bg-emerald-400 animate-pulse" : "bg-yellow-400"}`}
                    />
                    {tgStatus.bot_running ? "Polling запущен" : "Не запущен"}
                  </span>
                )}
                {/* Notifications */}
                {tgStatus?.configured && (
                  <span
                    className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium border ${
                      tgStatus.notifications_enabled
                        ? "bg-blue-900/50 text-blue-300 border-blue-700"
                        : "bg-slate-700 text-slate-400 border-slate-600"
                    }`}
                  >
                    {tgStatus.notifications_enabled
                      ? "Уведомления вкл."
                      : "Уведомления выкл."}
                  </span>
                )}
                {tgSaved && (
                  <span className="text-xs text-green-400">✓ Сохранено</span>
                )}
              </div>
              {/* Error banner */}
              {tgStatus?.last_error && !tgStatus.bot_running && (
                <div className="rounded-md bg-red-900/30 border border-red-700 px-3 py-2 text-sm text-red-300">
                  {tgStatus.last_error}
                </div>
              )}

              {/* Current values (masked) */}
              {tgStatus?.configured && !tgEditing && (
                <div className="rounded-md border border-slate-700 bg-slate-900/50 p-3 space-y-1.5 text-sm">
                  <div className="flex gap-3">
                    <span className="text-slate-500 w-36 shrink-0">
                      Токен бота
                    </span>
                    <span className="font-mono text-slate-300">
                      {tgStatus.token_masked || "—"}
                    </span>
                  </div>
                  <div className="flex gap-3">
                    <span className="text-slate-500 w-36 shrink-0">
                      Chat ID
                    </span>
                    <span className="font-mono text-slate-300">
                      {tgStatus.chat_id_masked || "—"}
                    </span>
                  </div>
                  {tgStatus.allowed_users_count > 0 && (
                    <div className="flex gap-3">
                      <span className="text-slate-500 w-36 shrink-0">
                        Разрешённые ID
                      </span>
                      <span className="font-mono text-slate-300 truncate">
                        {tgStatus.allowed_users_masked}
                      </span>
                    </div>
                  )}
                </div>
              )}

              {/* Edit form */}
              {tgEditing && (
                <div className="space-y-3 rounded-md border border-slate-600 bg-slate-900/30 p-4">
                  <Field
                    label="Токен бота"
                    hint="Получить у @BotFather в Telegram. Оставьте пустым, чтобы не менять."
                  >
                    <input
                      className={inputCls}
                      type="password"
                      autoComplete="off"
                      placeholder="1234567890:ABCdef..."
                      value={tgDraft.bot_token}
                      onChange={(e) =>
                        setTgDraft({ ...tgDraft, bot_token: e.target.value })
                      }
                    />
                  </Field>
                  <Field
                    label="Chat ID для уведомлений"
                    hint="Ваш личный Telegram user ID (узнать у @userinfobot — именно свой ID, не ID бота). Для группы/канала — ID чата."
                  >
                    <input
                      className={inputCls}
                      placeholder="-1001234567890"
                      value={tgDraft.notifications_chat_id}
                      onChange={(e) =>
                        setTgDraft({
                          ...tgDraft,
                          notifications_chat_id: e.target.value,
                        })
                      }
                    />
                  </Field>
                  <Field
                    label="Разрешённые пользователи"
                    hint="Telegram user ID через запятую. Пусто — принимать сообщения от всех."
                  >
                    <input
                      className={inputCls}
                      placeholder="123456789, 987654321"
                      value={tgDraft.allowed_users}
                      onChange={(e) =>
                        setTgDraft({
                          ...tgDraft,
                          allowed_users: e.target.value,
                        })
                      }
                    />
                  </Field>
                  <label className="flex items-center gap-3 cursor-pointer">
                    <input
                      type="checkbox"
                      className="h-4 w-4 rounded border-slate-600 bg-slate-900"
                      checked={tgDraft.notifications_enabled}
                      onChange={(e) =>
                        setTgDraft({
                          ...tgDraft,
                          notifications_enabled: e.target.checked,
                        })
                      }
                    />
                    <span className="text-sm text-slate-300">
                      Включить push-уведомления
                    </span>
                  </label>
                  <div className="flex gap-3 pt-1">
                    <button
                      className={btnPrimary}
                      disabled={tgSaving}
                      onClick={handleTelegramSave}
                    >
                      {tgSaving ? "Сохранение…" : "Сохранить"}
                    </button>
                    <button
                      className={btnSecondary}
                      onClick={() => setTgEditing(false)}
                    >
                      Отмена
                    </button>
                  </div>
                </div>
              )}

              {/* Bot control + test */}
              <div className="flex flex-wrap items-center gap-3">
                {tgStatus?.configured && !tgStatus.bot_running && (
                  <button
                    onClick={handleTelegramRestart}
                    disabled={tgRestarting}
                    className={btnPrimary}
                  >
                    {tgRestarting ? "Запуск…" : "▶ Запустить бота"}
                  </button>
                )}
                {tgStatus?.bot_running && (
                  <button
                    onClick={async () => {
                      await mutFetch(`${API}/api/telegram/stop`, {
                        method: "POST",
                      });
                      await loadTgStatus();
                    }}
                    className={btnSecondary}
                  >
                    ■ Остановить
                  </button>
                )}
                <button
                  onClick={handleTelegramTest}
                  disabled={
                    tgTesting ||
                    !tgStatus?.configured ||
                    !tgStatus?.has_default_chat
                  }
                  className={btnSecondary}
                  title={
                    !tgStatus?.has_default_chat
                      ? "Сначала укажите Chat ID для уведомлений"
                      : ""
                  }
                >
                  {tgTesting ? "Отправка…" : "Отправить тест"}
                </button>
                {tgTestResult && (
                  <span className="text-sm text-slate-300">{tgTestResult}</span>
                )}
              </div>
            </div>
          </SectionCard>

          <SectionCard title="О системе">
            <div className="space-y-1 text-sm">
              <p className="text-slate-300 font-medium">
                AI Manufacturing Workspace v0.1.0
              </p>
              <p className="text-slate-400">
                AI-ассистент: Света · Backend: FastAPI · AI: Ollama / OpenRouter
                / Anthropic
              </p>
              <p className="text-slate-500 text-xs mt-2">
                Настройки сохраняются в Redis (shared) и локальном файле
                (fallback).
              </p>
            </div>
          </SectionCard>
        </div>
      )}

      {/* ── TAB: Почта ──────────────────────────────────────────────────────── */}
      {activeTab === "email" && (
        <div className="space-y-6">
          <MailboxSection />
          <EmailTemplatesSection />
        </div>
      )}
    </div>
  );
}
