"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { getApiBaseUrl } from "@/lib/api-base";
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
  model_reasoning: string;
  embedding_model: string;
  reranker_model: string | null;
  verify_model_1: string;
  turboquant_enabled: boolean;
  turboquant_kv_cache_dtype: string;
  turboquant_max_model_len: number;
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
  provider: string;
  fallback_providers: string[];
  prompt_cache_enabled: boolean;
  ollama_url: string;
  backend_url: string;
  temperature: number;
  max_steps: number;
  llm_timeout_seconds: number;
  backend_timeout_seconds: number;
  approval_timeout_seconds: number;
  memory_enabled: boolean;
  memory_mode: "sql" | "sql_vector" | "sql_vector_rerank" | "graph" | "hybrid";
  memory_top_k: number;
  memory_max_chars: number;
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
}

// ── Constants ─────────────────────────────────────────────────────────────────

const PROVIDERS = [
  { value: "ollama", label: "Ollama (локально)" },
  { value: "openrouter", label: "OpenRouter (200+ моделей)" },
  { value: "anthropic", label: "Anthropic (Claude)" },
  { value: "deepseek", label: "DeepSeek" },
] as const;

const PROVIDER_ENV: Record<string, string> = {
  openrouter: "OPENROUTER_API_KEY",
  anthropic: "ANTHROPIC_API_KEY",
  deepseek: "DEEPSEEK_API_KEY",
};

const PROVIDER_MODEL_PLACEHOLDER: Record<string, string> = {
  openrouter: "deepseek/deepseek-r1  или  qwen/qwen3-235b-a22b",
  anthropic: "claude-sonnet-4-6  или  claude-haiku-4-5",
  deepseek: "deepseek-chat  или  deepseek-reasoner",
};

type TabId = "agent" | "models" | "memory" | "data" | "system" | "email";

const TABS: { id: TabId; label: string }[] = [
  { id: "agent", label: "Агент" },
  { id: "models", label: "Модели" },
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
  title: string;
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

function ModelSelector({
  label,
  description,
  value,
  models,
  onChange,
}: {
  label: string;
  description: string;
  value: string;
  models: OllamaModel[];
  onChange: (v: string) => void;
}) {
  return (
    <Field label={label} hint={description}>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={selectCls}
      >
        {!models.find((m) => m.name === value) && (
          <option value={value}>{value} (не установлена)</option>
        )}
        {models.map((m) => (
          <option key={m.name} value={m.name}>
            {m.name}
            {m.parameter_size ? ` — ${m.parameter_size}` : ""}
          </option>
        ))}
      </select>
    </Field>
  );
}

function FallbackProvidersInput({
  value,
  onChange,
  currentProvider,
}: {
  value: string[];
  onChange: (v: string[]) => void;
  currentProvider: string;
}) {
  const [draft, setDraft] = useState("");
  const available = PROVIDERS.map((p) => p.value).filter(
    (p) => p !== currentProvider && !value.includes(p),
  );

  function add() {
    if (draft && !value.includes(draft)) {
      onChange([...value, draft]);
      setDraft("");
    }
  }

  return (
    <div>
      <div className="flex flex-wrap gap-1 mb-2 min-h-6">
        {value.map((p, i) => (
          <span
            key={p}
            className="flex items-center gap-1 px-2 py-0.5 bg-slate-700 border border-slate-600 rounded text-xs text-slate-200"
          >
            {PROVIDERS.find((x) => x.value === p)?.label ?? p}
            <button
              onClick={() => onChange(value.filter((_, j) => j !== i))}
              className="text-slate-400 hover:text-red-400 ml-0.5 leading-none"
            >
              ×
            </button>
          </span>
        ))}
        {value.length === 0 && (
          <span className="text-xs text-slate-600 italic">нет резервных</span>
        )}
      </div>
      <div className="flex gap-2">
        <select
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          className="flex-1 rounded-md border border-slate-600 bg-slate-900 px-2 py-1.5 text-xs text-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          <option value="">Добавить резервный провайдер…</option>
          {available.map((p) => (
            <option key={p} value={p}>
              {PROVIDERS.find((x) => x.value === p)?.label ?? p}
            </option>
          ))}
        </select>
        <button
          onClick={add}
          disabled={!draft}
          className="px-3 py-1.5 bg-slate-700 text-sm text-slate-200 rounded-md hover:bg-slate-600 disabled:opacity-40"
        >
          +
        </button>
      </div>
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function SettingsPage() {
  const [activeTab, setActiveTab] = useState<TabId>("agent");

  // AI Config (models tab)
  const [models, setModels] = useState<OllamaModel[]>([]);
  const [config, setConfig] = useState<AiConfig | null>(null);
  const [registryModels, setRegistryModels] = useState<RegistryModel[]>([]);
  const [configStatus, setConfigStatus] = useState<AiConfigStatus | null>(null);
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
  const [agentSkills, setAgentSkills] = useState<AgentSkill[]>([]);
  const [agentSkillFilter, setAgentSkillFilter] = useState("");
  const [agentSaving, setAgentSaving] = useState(false);
  const [agentSaved, setAgentSaved] = useState(false);

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
    } catch {
      setModels([]);
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
      setAgentConfig(await r.json());
    } catch {
      setAgentConfig(null);
    }
  }

  async function loadAgentSkills() {
    try {
      const r = await fetch(`${API}/api/ai/agent-skills`);
      const d = await r.json();
      setAgentSkills(d.skills ?? []);
    } catch {
      setAgentSkills([]);
    }
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
    loadModels();
    loadConfig();
    loadCapabilities();
    loadEmbeddingProfile();
    loadEmbeddingStats();
    loadNtdConfig();
    loadAgentConfig();
    loadAgentSkills();
    loadTgStatus();
  }, []);

  useEffect(() => {
    pullLogRef.current?.scrollTo(0, pullLogRef.current.scrollHeight);
  }, [pullLog]);

  // ── Handlers ──────────────────────────────────────────────────────────────

  async function handlePull() {
    if (!pullName.trim()) return;
    setPulling(true);
    setPullLog([`Загрузка ${pullName}...`]);
    try {
      const resp = await fetch(`${API}/api/ai/models/pull`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: pullName.trim() }),
      });
      const reader = resp.body?.getReader();
      if (!reader) throw new Error("No response body");
      const dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split("\n");
        buf = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const obj = JSON.parse(line);
            const status = obj.status ?? "";
            const detail =
              obj.completed && obj.total
                ? ` ${Math.round((obj.completed / obj.total) * 100)}%`
                : "";
            setPullLog((prev) => {
              const last = prev[prev.length - 1] ?? "";
              const msg = status + detail;
              if (last.startsWith(status.split(" ")[0]))
                return [...prev.slice(0, -1), msg];
              return [...prev, msg];
            });
            if (obj.status === "error") break;
          } catch {}
        }
      }
      setPullLog((prev) => [...prev, "Готово!"]);
      setPullName("");
      await loadModels();
    } catch (e) {
      setPullLog((prev) => [...prev, `Ошибка: ${e}`]);
    } finally {
      setPulling(false);
    }
  }

  async function handleDelete(name: string) {
    if (!confirm(`Удалить модель ${name}?`)) return;
    setDeletingModel(name);
    try {
      await fetch(`${API}/api/ai/models/${encodeURIComponent(name)}`, {
        method: "DELETE",
      });
      await loadModels();
    } catch {}
    setDeletingModel(null);
  }

  async function handleSaveConfig() {
    if (!config) return;
    setConfigSaving(true);
    try {
      const r = await fetch(`${API}/api/ai/config`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(config),
      });
      setConfig(await r.json());
      await loadConfigStatus();
      await loadEmbeddingProfile();
      await loadEmbeddingStats();
      setConfigSaved(true);
      setTimeout(() => setConfigSaved(false), 2000);
    } catch {}
    setConfigSaving(false);
  }

  async function handleRebuildEmbeddings() {
    setRebuildingEmbeddings(true);
    setRebuildMessage(null);
    try {
      const r = await fetch(`${API}/api/memory/embeddings/rebuild-active`, {
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
      const r = await fetch(`${API}/api/memory/embeddings/index-active`, {
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
      const r = await fetch(`${API}/api/settings/ntd-control`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode, updated_by: "user" }),
      });
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
      const r = await fetch(`${API}/api/ai/agent-config`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(agentConfig),
      });
      setAgentConfig(await r.json());
      await loadAgentSkills();
      setAgentSaved(true);
      setTimeout(() => setAgentSaved(false), 2000);
    } catch {}
    setAgentSaving(false);
  }

  async function handleResetAgentConfig() {
    setAgentSaving(true);
    try {
      const r = await fetch(`${API}/api/ai/agent-config/reset`, {
        method: "POST",
      });
      setAgentConfig(await r.json());
      await loadAgentSkills();
      setAgentSaved(true);
      setTimeout(() => setAgentSaved(false), 2000);
    } catch {}
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
      const r = await fetch(`${API}/api/documents/dev/purge-all`, {
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
      const r = await fetch(`${API}/api/telegram/restart`, { method: "POST" });
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
      const r = await fetch(`${API}/api/telegram/test`, { method: "POST" });
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
      const r = await fetch(`${API}/api/telegram/config`, {
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

              {/* LLM Provider */}
              <SectionCard
                title="Провайдер LLM"
                subtitle="Выбор модели и провайдера. При недоступности основного используются резервные."
              >
                <div className="space-y-5">
                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    <Field
                      label="Провайдер"
                      hint="Откуда берётся LLM для агента"
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

                    {agentConfig.provider === "ollama" ? (
                      <ModelSelector
                        label="Модель агента"
                        description="Для диалога, планирования и вызова инструментов"
                        value={agentConfig.model}
                        models={models}
                        onChange={(v) =>
                          setAgentConfig({ ...agentConfig, model: v })
                        }
                      />
                    ) : (
                      <Field
                        label="Модель агента"
                        hint={`Пример: ${PROVIDER_MODEL_PLACEHOLDER[agentConfig.provider] ?? "model-name"}`}
                      >
                        <input
                          className={inputCls}
                          value={agentConfig.model}
                          onChange={(e) =>
                            setAgentConfig({
                              ...agentConfig,
                              model: e.target.value,
                            })
                          }
                          placeholder={
                            PROVIDER_MODEL_PLACEHOLDER[agentConfig.provider] ??
                            "model-name"
                          }
                        />
                      </Field>
                    )}
                  </div>

                  {/* API key hint */}
                  {agentConfig.provider !== "ollama" && (
                    <div className="flex items-start gap-2 rounded-md bg-amber-950/30 border border-amber-800/40 px-3 py-2 text-xs text-amber-300">
                      <span>🔑</span>
                      <span>
                        Установите переменную окружения{" "}
                        <code className="font-mono bg-amber-900/40 px-1 rounded">
                          {PROVIDER_ENV[agentConfig.provider] ?? "API_KEY"}
                        </code>{" "}
                        в{" "}
                        <code className="font-mono bg-amber-900/40 px-1 rounded">
                          .env
                        </code>{" "}
                        или Docker Compose.
                      </span>
                    </div>
                  )}

                  {/* Ollama URL (only for ollama) */}
                  {agentConfig.provider === "ollama" && (
                    <Field
                      label="Ollama URL"
                      hint="Адрес локального Ollama-сервера"
                    >
                      <input
                        className={inputCls}
                        value={agentConfig.ollama_url}
                        onChange={(e) =>
                          setAgentConfig({
                            ...agentConfig,
                            ollama_url: e.target.value,
                          })
                        }
                      />
                    </Field>
                  )}

                  {/* Fallback providers */}
                  <Field
                    label="Резервные провайдеры"
                    hint="При ошибке основного провайдера агент попробует следующий из списка"
                  >
                    <FallbackProvidersInput
                      value={agentConfig.fallback_providers}
                      onChange={(v) =>
                        setAgentConfig({
                          ...agentConfig,
                          fallback_providers: v,
                        })
                      }
                      currentProvider={agentConfig.provider}
                    />
                  </Field>

                  {/* Prompt caching (Anthropic only) */}
                  {agentConfig.provider === "anthropic" && (
                    <label className="flex items-start gap-3 rounded-md bg-slate-900/50 border border-slate-700 p-3 cursor-pointer hover:bg-slate-900/80 transition-colors">
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
                          Ускоряет повторные запросы с общим системным промптом.
                          Снижает стоимость и latency при длинных сессиях.
                          Требует поддержки провайдером.
                        </p>
                      </div>
                    </label>
                  )}
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
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                    <Field label="Режим поиска">
                      <select
                        value={agentConfig.memory_mode}
                        onChange={(e) =>
                          setAgentConfig({
                            ...agentConfig,
                            memory_mode: e.target
                              .value as AgentConfig["memory_mode"],
                          })
                        }
                        className={selectCls}
                      >
                        <option value="sql">SQL (FTS)</option>
                        <option value="sql_vector">SQL + vector</option>
                        <option value="sql_vector_rerank">
                          SQL + vector + rerank
                        </option>
                        <option value="hybrid">Hybrid</option>
                        <option value="graph">Graph</option>
                      </select>
                    </Field>
                    <Field label="Top-K результатов">
                      <input
                        className={inputCls}
                        type="number"
                        min={1}
                        max={30}
                        value={agentConfig.memory_top_k}
                        onChange={(e) =>
                          setAgentConfig({
                            ...agentConfig,
                            memory_top_k: Number(e.target.value),
                          })
                        }
                      />
                    </Field>
                    <Field label="Макс. символов контекста">
                      <input
                        className={inputCls}
                        type="number"
                        min={1000}
                        max={30000}
                        step={500}
                        value={agentConfig.memory_max_chars}
                        onChange={(e) =>
                          setAgentConfig({
                            ...agentConfig,
                            memory_max_chars: Number(e.target.value),
                          })
                        }
                      />
                    </Field>
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
              <SectionCard title="Инструменты (Skills)">
                <div className="space-y-3">
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                    <p className="text-xs text-slate-400">
                      Включено {agentConfig.exposed_skills.length} из{" "}
                      {agentSkills.length} · подтверждений{" "}
                      {agentConfig.approval_gates.length}
                    </p>
                    <input
                      className="w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-1.5 text-xs text-slate-200 sm:w-64 focus:outline-none focus:ring-2 focus:ring-blue-500"
                      value={agentSkillFilter}
                      onChange={(e) => setAgentSkillFilter(e.target.value)}
                      placeholder="Поиск tools…"
                    />
                  </div>
                  <div className="max-h-72 overflow-auto rounded-md border border-slate-700">
                    <table className="w-full text-left text-xs">
                      <thead className="sticky top-0 bg-slate-900 text-slate-400">
                        <tr>
                          <th className="w-20 px-3 py-2 font-medium">Агент</th>
                          <th className="w-24 px-3 py-2 font-medium">Подтв.</th>
                          <th className="px-3 py-2 font-medium">Tool</th>
                          <th className="hidden px-3 py-2 font-medium md:table-cell">
                            Endpoint
                          </th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-800">
                        {filteredAgentSkills.map((skill) => {
                          const isExposed = agentConfig.exposed_skills.includes(
                            skill.name,
                          );
                          const needsApproval =
                            agentConfig.approval_gates.includes(skill.name);
                          return (
                            <tr
                              key={skill.name}
                              className="bg-slate-900/40 hover:bg-slate-900/70"
                            >
                              <td className="px-3 py-2">
                                <input
                                  type="checkbox"
                                  checked={isExposed}
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
                subtitle="Переопределяет базовый промпт из openclaw/prompts/base.md"
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
                    placeholder="Пусто: используется базовый промпт openclaw/prompts/base.md"
                  />
                </Field>
              </SectionCard>

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

      {/* ── TAB: Модели ──────────────────────────────────────────────────── */}
      {activeTab === "models" && (
        <div className="space-y-6">
          {/* Status + model config */}
          <SectionCard
            title="Выбор моделей"
            subtitle="Модели для OCR, reasoning и верификации. Используются Celery-задачами и маршрутизатором AI."
            action={
              <button
                onClick={() => {
                  loadModels();
                  loadConfigStatus();
                }}
                className="rounded-md bg-slate-700 px-3 py-2 text-xs text-slate-100 hover:bg-slate-600"
              >
                Проверить
              </button>
            }
          >
            {configStatus && (
              <div
                className={`mb-4 rounded-md border p-3 text-sm ${
                  configStatus.ok
                    ? "border-emerald-800 bg-emerald-950/30 text-emerald-200"
                    : "border-amber-800 bg-amber-950/30 text-amber-200"
                }`}
              >
                <div>
                  Ollama:{" "}
                  {configStatus.ollama_available ? "доступна" : "недоступна"} ·
                  моделей: {configStatus.installed_models.length}
                </div>
                {configStatus.warnings.length > 0 && (
                  <div className="mt-2 space-y-1 text-xs">
                    {configStatus.warnings.map((w) => (
                      <div key={w}>{w}</div>
                    ))}
                  </div>
                )}
              </div>
            )}
            {config && (
              <div className="space-y-4">
                <ModelSelector
                  label="Модель OCR / извлечения"
                  description="Распознавание и извлечение данных из документов. Работает только локально."
                  value={config.model_ocr}
                  models={models}
                  onChange={(v) => setConfig({ ...config, model_ocr: v })}
                />
                <ModelSelector
                  label="Модель reasoning"
                  description="Сложные рассуждения, генерация писем, отчётов."
                  value={config.model_reasoning}
                  models={models}
                  onChange={(v) => setConfig({ ...config, model_reasoning: v })}
                />
                <ModelSelector
                  label="Проверочная модель"
                  description="Повторная экстракция для автоверификации."
                  value={config.verify_model_1}
                  models={models}
                  onChange={(v) => setConfig({ ...config, verify_model_1: v })}
                />
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <Field
                    label="Embedding модель"
                    hint="Для векторной памяти; параметры из registry"
                  >
                    <select
                      value={config.embedding_model}
                      onChange={(e) =>
                        setConfig({
                          ...config,
                          embedding_model: e.target.value,
                        })
                      }
                      className={selectCls}
                    >
                      {registryModels
                        .filter((m) => m.modalities.includes("embedding"))
                        .map((m) => (
                          <option key={m.name} value={m.name}>
                            {m.name}
                            {m.embedding_dimension
                              ? ` · ${m.embedding_dimension}`
                              : ""}
                          </option>
                        ))}
                    </select>
                    {embeddingProfile && (
                      <p className="text-[11px] text-slate-500 mt-1">
                        Коллекция: {embeddingProfile.collection_name} ·{" "}
                        {embeddingProfile.dimension} ·{" "}
                        {embeddingProfile.distance_metric}
                      </p>
                    )}
                  </Field>
                  <Field
                    label="Reranker модель"
                    hint="Применяется к top-K кандидатам поиска"
                  >
                    <select
                      value={config.reranker_model ?? ""}
                      onChange={(e) =>
                        setConfig({
                          ...config,
                          reranker_model: e.target.value || null,
                        })
                      }
                      className={selectCls}
                    >
                      <option value="">Не использовать</option>
                      {registryModels
                        .filter((m) => m.modalities.includes("rerank"))
                        .map((m) => (
                          <option key={m.name} value={m.name}>
                            {m.name}
                          </option>
                        ))}
                    </select>
                  </Field>
                </div>
                <SaveRow
                  saving={configSaving}
                  saved={configSaved}
                  onSave={handleSaveConfig}
                  saveLabel="Сохранить выбор моделей"
                />
              </div>
            )}
          </SectionCard>

          {/* TurboQuant */}
          <SectionCard
            title="TurboQuant"
            subtitle="Optional vLLM KV-cache профиль для long-context reasoning. Включайте только после benchmark."
          >
            {config && (
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <label className="flex items-center gap-2 rounded-md bg-slate-900/50 p-3 text-sm text-slate-200">
                  <input
                    type="checkbox"
                    checked={config.turboquant_enabled}
                    onChange={(e) =>
                      setConfig({
                        ...config,
                        turboquant_enabled: e.target.checked,
                      })
                    }
                  />
                  Включить профиль
                </label>
                <Field label="KV-cache dtype">
                  <input
                    className={inputCls}
                    value={config.turboquant_kv_cache_dtype}
                    onChange={(e) =>
                      setConfig({
                        ...config,
                        turboquant_kv_cache_dtype: e.target.value,
                      })
                    }
                  />
                </Field>
                <Field label="Max model len (токенов)">
                  <input
                    className={inputCls}
                    type="number"
                    value={config.turboquant_max_model_len}
                    onChange={(e) =>
                      setConfig({
                        ...config,
                        turboquant_max_model_len: Number(e.target.value),
                      })
                    }
                  />
                </Field>
              </div>
            )}
          </SectionCard>

          {/* Installed models */}
          <SectionCard
            title="Установленные модели"
            action={
              <button
                onClick={loadModels}
                disabled={loadingModels}
                className="text-xs text-slate-400 hover:text-slate-200 px-2 py-1 rounded hover:bg-slate-700"
              >
                {loadingModels ? "Загрузка..." : "Обновить"}
              </button>
            }
          >
            {loadingModels ? (
              <div className="text-sm text-slate-400 py-4 text-center">
                Загрузка списка моделей…
              </div>
            ) : models.length === 0 ? (
              <p className="text-sm text-slate-500">
                Нет установленных моделей
              </p>
            ) : (
              <div className="divide-y divide-slate-700">
                {models.map((m) => (
                  <div
                    key={m.name}
                    className="flex items-center justify-between py-2.5"
                  >
                    <div className="min-w-0">
                      <p className="text-sm font-mono font-medium text-slate-200 truncate">
                        {m.name}
                      </p>
                      <p className="text-xs text-slate-400 mt-0.5">
                        {[m.parameter_size, m.family, fmtBytes(m.size)]
                          .filter(Boolean)
                          .join(" · ")}
                      </p>
                    </div>
                    <button
                      onClick={() => handleDelete(m.name)}
                      disabled={deletingModel === m.name}
                      className="ml-4 px-2 py-1 text-xs text-red-400 hover:text-red-300 hover:bg-red-950/30 rounded disabled:opacity-40 shrink-0"
                    >
                      {deletingModel === m.name ? "…" : "Удалить"}
                    </button>
                  </div>
                ))}
              </div>
            )}
          </SectionCard>

          {/* Pull new model */}
          <SectionCard title="Загрузить новую модель">
            <div className="flex gap-2 mb-3">
              <input
                type="text"
                value={pullName}
                onChange={(e) => setPullName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && !pulling && handlePull()}
                placeholder="например: llama3.2:3b, qwen3:8b"
                disabled={pulling}
                className={`flex-1 ${inputCls}`}
              />
              <button
                onClick={handlePull}
                disabled={pulling || !pullName.trim()}
                className={btnPrimary}
              >
                {pulling ? "Загрузка..." : "Pull"}
              </button>
            </div>
            {pullLog.length > 0 && (
              <div
                ref={pullLogRef}
                className="bg-slate-900 text-slate-300 text-xs font-mono rounded-md p-3 h-32 overflow-y-auto"
              >
                {pullLog.map((line, i) => (
                  <div key={i}>{line}</div>
                ))}
              </div>
            )}
            <p className="text-xs text-slate-400 mt-2">
              Библиотека:&nbsp;
              <a
                href="https://ollama.com/library"
                target="_blank"
                rel="noreferrer"
                className="underline hover:text-slate-300"
              >
                ollama.com/library
              </a>
            </p>
          </SectionCard>
        </div>
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
          <SectionCard
            title="OpenClaw Gateway"
            subtitle="Gateway, режим чата, allowlist, Telegram и статус."
            action={
              <Link href="/settings/openclaw" className={btnSecondary}>
                Открыть
              </Link>
            }
          />

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
                      await fetch(`${API}/api/telegram/stop`, {
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
