"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { getApiBaseUrl } from "@/lib/api-base";

const API = getApiBaseUrl();

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
}

interface AgentSkill {
  name: string;
  description: string;
  method: string;
  path: string;
  enabled: boolean;
  approval_required: boolean;
}

function fmtBytes(b: number) {
  if (b >= 1e9) return (b / 1e9).toFixed(1) + " GB";
  if (b >= 1e6) return (b / 1e6).toFixed(0) + " MB";
  return b + " B";
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
    <div>
      <label className="block text-sm font-medium text-slate-300 mb-1">
        {label}
      </label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full bg-slate-800 border border-slate-600 text-slate-200 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
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
      <p className="text-xs text-slate-400 mt-1">{description}</p>
    </div>
  );
}

export default function SettingsPage() {
  const [models, setModels] = useState<OllamaModel[]>([]);
  const [config, setConfig] = useState<AiConfig | null>(null);
  const [registryModels, setRegistryModels] = useState<RegistryModel[]>([]);
  const [embeddingProfile, setEmbeddingProfile] = useState<EmbeddingProfile | null>(null);
  const [embeddingStats, setEmbeddingStats] = useState<EmbeddingStats | null>(null);
  const [rebuildingEmbeddings, setRebuildingEmbeddings] = useState(false);
  const [indexingEmbeddings, setIndexingEmbeddings] = useState(false);
  const [rebuildMessage, setRebuildMessage] = useState<string | null>(null);
  const [loadingModels, setLoadingModels] = useState(true);
  const [pullName, setPullName] = useState("");
  const [pulling, setPulling] = useState(false);
  const [pullLog, setPullLog] = useState<string[]>([]);
  const [deletingModel, setDeletingModel] = useState<string | null>(null);
  const [configSaving, setConfigSaving] = useState(false);
  const [configSaved, setConfigSaved] = useState(false);
  const [configStatus, setConfigStatus] = useState<AiConfigStatus | null>(null);
  const [ntdConfig, setNtdConfig] = useState<NtdControlConfig | null>(null);
  const [ntdSaving, setNtdSaving] = useState(false);
  const [ntdSaved, setNtdSaved] = useState(false);
  const [agentConfig, setAgentConfig] = useState<AgentConfig | null>(null);
  const [agentSkills, setAgentSkills] = useState<AgentSkill[]>([]);
  const [agentSkillFilter, setAgentSkillFilter] = useState("");
  const [agentSaving, setAgentSaving] = useState(false);
  const [agentSaved, setAgentSaved] = useState(false);
  const [purgeConfirm, setPurgeConfirm] = useState("");
  const [purgeBusy, setPurgeBusy] = useState(false);
  const [purgeMessage, setPurgeMessage] = useState<string | null>(null);
  const pullLogRef = useRef<HTMLDivElement>(null);

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

  useEffect(() => {
    loadModels();
    loadConfig();
    loadCapabilities();
    loadEmbeddingProfile();
    loadEmbeddingStats();
    loadNtdConfig();
    loadAgentConfig();
    loadAgentSkills();
  }, []);

  useEffect(() => {
    pullLogRef.current?.scrollTo(0, pullLogRef.current.scrollHeight);
  }, [pullLog]);

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
              if (last.startsWith(status.split(" ")[0])) {
                return [...prev.slice(0, -1), msg];
              }
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
      setRebuildMessage(`Создано записей: ${data.created}; stale: ${data.stale_marked}`);
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
      setRebuildMessage(`Qdrant: indexed ${data.indexed}; failed ${data.failed}`);
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
    if (enabled) {
      exposed.add(name);
    } else {
      exposed.delete(name);
    }
    setAgentConfig({
      ...agentConfig,
      exposed_skills: Array.from(exposed).sort(),
    });
  }

  function updateAgentApprovalGate(name: string, enabled: boolean) {
    if (!agentConfig) return;
    const exposed = new Set(agentConfig.exposed_skills);
    const approvalGates = new Set(agentConfig.approval_gates);
    if (enabled) {
      exposed.add(name);
      approvalGates.add(name);
    } else {
      approvalGates.delete(name);
    }
    setAgentConfig({
      ...agentConfig,
      exposed_skills: Array.from(exposed).sort(),
      approval_gates: Array.from(approvalGates).sort(),
    });
  }

  async function handleDevelopmentPurge() {
    if (purgeConfirm !== "DELETE ALL DOCUMENT DATA") return;
    if (!confirm("Полностью удалить все документы и связанные записи БД?")) return;
    setPurgeBusy(true);
    setPurgeMessage(null);
    try {
      const r = await fetch(`${API}/api/documents/dev/purge-all`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          confirm: purgeConfirm,
          delete_files: true,
        }),
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

  const filteredAgentSkills = agentSkills.filter((skill) => {
    const query = agentSkillFilter.trim().toLowerCase();
    if (!query) return true;
    return (
      skill.name.toLowerCase().includes(query) ||
      skill.description.toLowerCase().includes(query) ||
      skill.path.toLowerCase().includes(query)
    );
  });

  return (
    <div className="p-6 max-w-3xl mx-auto space-y-6">
      <h1 className="text-2xl font-bold">Настройки</h1>

      <section className="bg-slate-800 border border-slate-700 rounded-lg p-6">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold">OpenClaw</h2>
            <p className="text-sm text-slate-400 mt-1">
              Gateway, режим чата, allowlist, fallback и статус.
            </p>
          </div>
          <Link
            href="/settings/openclaw"
            className="px-4 py-2 bg-slate-700 text-slate-100 text-sm rounded-lg hover:bg-slate-600"
          >
            Открыть
          </Link>
        </div>
      </section>

      {/* Model Config */}
      <section className="bg-slate-800 border border-slate-700 rounded-lg p-6">
        <div className="mb-4 flex items-start justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold">Выбор моделей</h2>
            <p className="mt-1 text-sm text-slate-400">
              Встроенный агент использует эти настройки напрямую, без official OpenClaw.
            </p>
          </div>
          <button
            onClick={() => {
              loadModels();
              loadConfigStatus();
            }}
            className="rounded-md bg-slate-700 px-3 py-2 text-xs text-slate-100 hover:bg-slate-600"
          >
            Проверить
          </button>
        </div>
        {configStatus && (
          <div
            className={`mb-4 rounded-md border p-3 text-sm ${
              configStatus.ok
                ? "border-emerald-800 bg-emerald-950/30 text-emerald-200"
                : "border-amber-800 bg-amber-950/30 text-amber-200"
            }`}
          >
            <div>
              Ollama: {configStatus.ollama_available ? "доступна" : "недоступна"} ·
              моделей: {configStatus.installed_models.length}
            </div>
            {configStatus.warnings.length > 0 && (
              <div className="mt-2 space-y-1 text-xs">
                {configStatus.warnings.map((warning) => (
                  <div key={warning}>{warning}</div>
                ))}
              </div>
            )}
          </div>
        )}
        {config && (
          <div className="space-y-4">
            <ModelSelector
              label="Модель агента (чат)"
              description="Используется для диалога и вызова инструментов. Рекомендуется модель с поддержкой tool calling."
              value={config.model_agent}
              models={models}
              onChange={(v) => setConfig({ ...config, model_agent: v })}
            />
            <ModelSelector
              label="Модель OCR / извлечения"
              description="Используется для распознавания и извлечения данных из документов. Работает только локально."
              value={config.model_ocr}
              models={models}
              onChange={(v) => setConfig({ ...config, model_ocr: v })}
            />
            <ModelSelector
              label="Модель reasoning"
              description="Используется для сложных рассуждений и генерации текста (письма, отчёты)."
              value={config.model_reasoning}
              models={models}
              onChange={(v) => setConfig({ ...config, model_reasoning: v })}
            />
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-1">
                  Embedding модель
                </label>
                <select
                  value={config.embedding_model}
                  onChange={(e) =>
                    setConfig({ ...config, embedding_model: e.target.value })
                  }
                  className="w-full bg-slate-800 border border-slate-600 text-slate-200 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                >
                  {registryModels
                    .filter((m) => m.modalities.includes("embedding"))
                    .map((m) => (
                      <option key={m.name} value={m.name}>
                        {m.name}
                        {m.embedding_dimension ? ` · ${m.embedding_dimension}` : ""}
                      </option>
                    ))}
                </select>
                <p className="text-xs text-slate-400 mt-1">
                  Используется для векторной памяти; параметры берутся из registry/discovery.
                </p>
                {embeddingProfile && (
                  <p className="text-[11px] text-slate-500 mt-1">
                    Коллекция: {embeddingProfile.collection_name} · {embeddingProfile.dimension} · {embeddingProfile.distance_metric}
                  </p>
                )}
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-1">
                  Reranker модель
                </label>
                <select
                  value={config.reranker_model ?? ""}
                  onChange={(e) =>
                    setConfig({
                      ...config,
                      reranker_model: e.target.value || null,
                    })
                  }
                  className="w-full bg-slate-800 border border-slate-600 text-slate-200 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
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
                <p className="text-xs text-slate-400 mt-1">
                  Применяется только к top-K кандидатам SQL/vector поиска.
                </p>
              </div>
            </div>
            <button
              onClick={handleSaveConfig}
              disabled={configSaving}
              className="px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 disabled:opacity-50"
            >
              {configSaved
                ? "Сохранено ✓"
                : configSaving
                  ? "Сохранение..."
                  : "Сохранить"}
            </button>
          </div>
        )}
      </section>

      {/* Built-in agent */}
      <section className="bg-slate-800 border border-slate-700 rounded-lg p-6">
        <div className="mb-4 flex items-start justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold">Встроенный агент</h2>
            <p className="mt-1 text-sm text-slate-400">
              Основной сотрудник без official OpenClaw: модель, память, лимиты и
              инструменты.
            </p>
          </div>
          {agentSaved && (
            <span className="text-xs text-emerald-400 pt-1">Сохранено</span>
          )}
        </div>
        {agentConfig && (
          <div className="space-y-4">
            <label className="flex items-center gap-2 text-sm text-slate-200">
              <input
                type="checkbox"
                checked={agentConfig.enabled}
                onChange={(e) =>
                  setAgentConfig({ ...agentConfig, enabled: e.target.checked })
                }
              />
              Включить встроенного агента
            </label>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <ModelSelector
                label="Модель агента"
                description="Используется для диалога, планирования и tool calling."
                value={agentConfig.model}
                models={models}
                onChange={(v) => setAgentConfig({ ...agentConfig, model: v })}
              />
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-1">
                  Имя сотрудника
                </label>
                <input
                  className="w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-200"
                  value={agentConfig.agent_name}
                  onChange={(e) =>
                    setAgentConfig({ ...agentConfig, agent_name: e.target.value })
                  }
                />
                <p className="mt-1 text-xs text-slate-400">
                  Используется в системном промпте, если промпт не переопределен.
                </p>
              </div>
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <input
                className="rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-200"
                value={agentConfig.ollama_url}
                onChange={(e) =>
                  setAgentConfig({ ...agentConfig, ollama_url: e.target.value })
                }
                placeholder="Ollama URL"
              />
              <input
                className="rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-200"
                value={agentConfig.backend_url}
                onChange={(e) =>
                  setAgentConfig({ ...agentConfig, backend_url: e.target.value })
                }
                placeholder="Backend URL"
              />
            </div>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <label className="text-xs text-slate-400">
                Temperature
                <input
                  className="mt-1 w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-200"
                  type="number"
                  min="0"
                  max="2"
                  step="0.05"
                  value={agentConfig.temperature}
                  onChange={(e) =>
                    setAgentConfig({
                      ...agentConfig,
                      temperature: Number(e.target.value),
                    })
                  }
                />
              </label>
              <label className="text-xs text-slate-400">
                Max steps
                <input
                  className="mt-1 w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-200"
                  type="number"
                  min="1"
                  max="30"
                  value={agentConfig.max_steps}
                  onChange={(e) =>
                    setAgentConfig({
                      ...agentConfig,
                      max_steps: Number(e.target.value),
                    })
                  }
                />
              </label>
              <label className="text-xs text-slate-400">
                LLM timeout
                <input
                  className="mt-1 w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-200"
                  type="number"
                  value={agentConfig.llm_timeout_seconds}
                  onChange={(e) =>
                    setAgentConfig({
                      ...agentConfig,
                      llm_timeout_seconds: Number(e.target.value),
                    })
                  }
                />
              </label>
              <label className="text-xs text-slate-400">
                Approval timeout
                <input
                  className="mt-1 w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-200"
                  type="number"
                  value={agentConfig.approval_timeout_seconds}
                  onChange={(e) =>
                    setAgentConfig({
                      ...agentConfig,
                      approval_timeout_seconds: Number(e.target.value),
                    })
                  }
                />
              </label>
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <label className="flex items-center gap-2 rounded-md bg-slate-900/50 p-3 text-sm text-slate-200">
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
                Подключать память
              </label>
              <select
                value={agentConfig.memory_mode}
                onChange={(e) =>
                  setAgentConfig({
                    ...agentConfig,
                    memory_mode: e.target.value as AgentConfig["memory_mode"],
                  })
                }
                className="rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-200"
              >
                <option value="sql">SQL</option>
                <option value="sql_vector">SQL + vector</option>
                <option value="sql_vector_rerank">SQL + vector + rerank</option>
                <option value="hybrid">Hybrid</option>
                <option value="graph">Graph</option>
              </select>
              <input
                className="rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-200"
                type="number"
                min="1"
                max="30"
                value={agentConfig.memory_top_k}
                onChange={(e) =>
                  setAgentConfig({
                    ...agentConfig,
                    memory_top_k: Number(e.target.value),
                  })
                }
              />
            </div>
            <div>
              <div className="mb-2 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                <div className="text-xs text-slate-400">
                  Инструменты: включено {agentConfig.exposed_skills.length} из{" "}
                  {agentSkills.length}; подтверждений {agentConfig.approval_gates.length}
                </div>
                <input
                  className="w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-xs text-slate-200 sm:w-64"
                  value={agentSkillFilter}
                  onChange={(e) => setAgentSkillFilter(e.target.value)}
                  placeholder="Поиск tools"
                />
              </div>
              <div className="max-h-80 overflow-auto rounded-md border border-slate-700">
                <table className="w-full text-left text-xs">
                  <thead className="sticky top-0 bg-slate-900 text-slate-400">
                    <tr>
                      <th className="w-24 px-3 py-2 font-medium">Агент</th>
                      <th className="w-28 px-3 py-2 font-medium">Подтв.</th>
                      <th className="px-3 py-2 font-medium">Tool</th>
                      <th className="hidden px-3 py-2 font-medium md:table-cell">
                        Endpoint
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800">
                    {filteredAgentSkills.map((skill) => {
                      const isExposed = agentConfig.exposed_skills.includes(skill.name);
                      const needsApproval = agentConfig.approval_gates.includes(skill.name);
                      return (
                        <tr key={skill.name} className="bg-slate-900/40">
                          <td className="px-3 py-2">
                            <input
                              type="checkbox"
                              checked={isExposed}
                              onChange={(e) =>
                                updateAgentSkill(skill.name, e.target.checked)
                              }
                            />
                          </td>
                          <td className="px-3 py-2">
                            <input
                              type="checkbox"
                              checked={needsApproval}
                              onChange={(e) =>
                                updateAgentApprovalGate(skill.name, e.target.checked)
                              }
                            />
                          </td>
                          <td className="px-3 py-2">
                            <div className="font-mono text-slate-200">{skill.name}</div>
                            <div className="mt-0.5 text-slate-500">
                              {skill.description || "Без описания"}
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
            <label className="text-xs text-slate-400">
              Системный промпт
              <textarea
                className="mt-1 h-28 w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-xs text-slate-200"
                value={agentConfig.system_prompt ?? ""}
                onChange={(e) =>
                  setAgentConfig({
                    ...agentConfig,
                    system_prompt: e.target.value || null,
                  })
                }
                placeholder="Пусто: используется базовый промпт openclaw/prompts/base.md"
              />
            </label>
            <div className="flex gap-2">
              <button
                onClick={handleSaveAgentConfig}
                disabled={agentSaving}
                className="px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 disabled:opacity-50"
              >
                {agentSaving ? "Сохранение..." : "Сохранить агента"}
              </button>
              <button
                onClick={handleResetAgentConfig}
                disabled={agentSaving}
                className="px-4 py-2 bg-slate-700 text-slate-100 text-sm rounded-lg hover:bg-slate-600 disabled:opacity-50"
              >
                Сбросить
              </button>
            </div>
          </div>
        )}
      </section>

      {/* TurboQuant */}
      <section className="bg-slate-800 border border-slate-700 rounded-lg p-6">
        <h2 className="text-lg font-semibold">TurboQuant</h2>
        <p className="text-sm text-slate-400 mt-1">
          Optional vLLM KV-cache профиль для long-context reasoning. Не используется для embeddings и rerankers.
        </p>
        {config && (
          <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
            <label className="flex items-center gap-2 rounded-md bg-slate-900/50 p-3 text-sm text-slate-200">
              <input
                type="checkbox"
                checked={config.turboquant_enabled}
                onChange={(e) =>
                  setConfig({ ...config, turboquant_enabled: e.target.checked })
                }
              />
              Включить профиль
            </label>
            <input
              className="rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-200"
              value={config.turboquant_kv_cache_dtype}
              onChange={(e) =>
                setConfig({ ...config, turboquant_kv_cache_dtype: e.target.value })
              }
            />
            <input
              className="rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-200"
              type="number"
              value={config.turboquant_max_model_len}
              onChange={(e) =>
                setConfig({
                  ...config,
                  turboquant_max_model_len: Number(e.target.value),
                })
              }
            />
          </div>
        )}
        <p className="mt-3 text-xs text-slate-500">
          Включайте только после benchmark baseline vs TurboQuant по качеству, VRAM и latency.
        </p>
      </section>

      {/* Development cleanup */}
      <section className="rounded-lg border border-red-900 bg-red-950/20 p-6">
        <h2 className="text-lg font-semibold text-red-100">
          Полная очистка документов
        </h2>
        <p className="mt-1 text-sm text-red-200/80">
          Dev-команда удаляет все документы, файлы и связанные записи: извлечения,
          память, граф, НТД-проверки, счета, техпроцессы, BOM и складские приемки,
          созданные от документов.
        </p>
        <div className="mt-4 flex flex-col gap-3 sm:flex-row">
          <input
            value={purgeConfirm}
            onChange={(event) => setPurgeConfirm(event.target.value)}
            placeholder='Введите "DELETE ALL DOCUMENT DATA"'
            className="flex-1 rounded-md border border-red-900 bg-slate-950 px-3 py-2 text-sm text-slate-100"
          />
          <button
            onClick={handleDevelopmentPurge}
            disabled={purgeBusy || purgeConfirm !== "DELETE ALL DOCUMENT DATA"}
            className="rounded-md bg-red-700 px-4 py-2 text-sm text-white hover:bg-red-600 disabled:opacity-50"
          >
            {purgeBusy ? "Очищаю..." : "Очистить все документы"}
          </button>
        </div>
        {purgeMessage && (
          <p className="mt-3 text-xs text-red-100">{purgeMessage}</p>
        )}
      </section>

      {/* Retrieval / embeddings */}
      <section className="bg-slate-800 border border-slate-700 rounded-lg p-6">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold">Векторная память</h2>
            <p className="text-sm text-slate-400 mt-1">
              Активный embedding profile определяет Qdrant-коллекцию, размерность и модель.
            </p>
          </div>
          <div className="flex gap-2">
            <button
              onClick={handleRebuildEmbeddings}
              disabled={rebuildingEmbeddings}
              className="px-3 py-2 bg-slate-700 text-slate-100 text-sm rounded-md hover:bg-slate-600 disabled:opacity-50"
            >
              {rebuildingEmbeddings ? "Готовлю..." : "Подготовить records"}
            </button>
            <button
              onClick={handleIndexEmbeddings}
              disabled={indexingEmbeddings}
              className="px-3 py-2 bg-blue-600 text-white text-sm rounded-md hover:bg-blue-700 disabled:opacity-50"
            >
              {indexingEmbeddings ? "Индексирую..." : "Индексировать в Qdrant"}
            </button>
          </div>
        </div>
        <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
          <div className="rounded-md bg-slate-900/50 p-3">
            <p className="text-xs text-slate-500">Модель</p>
            <p className="mt-1 text-sm font-mono text-slate-200">
              {embeddingStats?.active_model ?? embeddingProfile?.model_key ?? "—"}
            </p>
          </div>
          <div className="rounded-md bg-slate-900/50 p-3">
            <p className="text-xs text-slate-500">Коллекция</p>
            <p className="mt-1 text-sm font-mono text-slate-200 break-all">
              {embeddingStats?.active_collection ?? embeddingProfile?.collection_name ?? "—"}
            </p>
          </div>
          <div className="rounded-md bg-slate-900/50 p-3">
            <p className="text-xs text-slate-500">Записи</p>
            <p className="mt-1 text-sm text-slate-200">
              {embeddingStats ? `${embeddingStats.total}` : "—"}
            </p>
          </div>
        </div>
        {embeddingStats && (
          <p className="mt-3 text-xs text-slate-400">
            Статусы:{" "}
            {Object.entries(embeddingStats.counts_by_status)
              .map(([status, count]) => `${status}: ${count}`)
              .join(" · ") || "нет записей"}
          </p>
        )}
        {rebuildMessage && (
          <p className="mt-3 text-xs text-slate-300">{rebuildMessage}</p>
        )}
      </section>

      {/* NTD norm-control */}
      <section className="bg-slate-800 border border-slate-700 rounded-lg p-6">
        <div className="flex items-start justify-between gap-4 mb-4">
          <div>
            <h2 className="text-lg font-semibold">Нормоконтроль НТД</h2>
            <p className="text-sm text-slate-400 mt-1">
              Проверка документов по базе НТД может запускаться вручную из карточки
              документа или автоматически после обработки.
            </p>
          </div>
          {ntdSaved && (
            <span className="text-xs text-emerald-400 pt-1">Сохранено</span>
          )}
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <button
            onClick={() => handleSaveNtdConfig("manual")}
            disabled={ntdSaving}
            className={`text-left rounded-md border p-4 transition ${
              ntdConfig?.mode === "manual"
                ? "border-blue-500 bg-blue-950/30"
                : "border-slate-700 bg-slate-900/40 hover:border-slate-500"
            } disabled:opacity-50`}
          >
            <span className="block text-sm font-semibold text-slate-100">
              Проверять вручную
            </span>
            <span className="mt-1 block text-xs text-slate-400">
              Кнопка “Проверить на соответствие НТД” активна в review-документе.
            </span>
          </button>
          <button
            onClick={() => handleSaveNtdConfig("auto")}
            disabled={ntdSaving}
            className={`text-left rounded-md border p-4 transition ${
              ntdConfig?.mode === "auto"
                ? "border-blue-500 bg-blue-950/30"
                : "border-slate-700 bg-slate-900/40 hover:border-slate-500"
            } disabled:opacity-50`}
          >
            <span className="block text-sm font-semibold text-slate-100">
              Проверять автоматически
            </span>
            <span className="mt-1 block text-xs text-slate-400">
              Нормоконтроль запускается после extraction, без применения исправлений.
            </span>
          </button>
        </div>
        <p className="mt-3 text-xs text-slate-500">
          Текущий режим: {ntdConfig?.mode === "auto" ? "автоматический" : "ручной"}
          {ntdConfig?.updated_at
            ? ` · обновлено ${new Date(ntdConfig.updated_at).toLocaleString("ru-RU")}`
            : ""}
        </p>
      </section>

      {/* Installed Models */}
      <section className="bg-slate-800 border border-slate-700 rounded-lg p-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">Установленные модели</h2>
          <button
            onClick={loadModels}
            disabled={loadingModels}
            className="text-xs text-slate-400 hover:text-slate-200 px-2 py-1 rounded hover:bg-slate-700"
          >
            {loadingModels ? "Загрузка..." : "Обновить"}
          </button>
        </div>

        {loadingModels ? (
          <div className="text-sm text-slate-400 py-4 text-center">
            Загрузка списка моделей...
          </div>
        ) : models.length === 0 ? (
          <p className="text-sm text-slate-500">Нет установленных моделей</p>
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
                  {deletingModel === m.name ? "..." : "Удалить"}
                </button>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Pull new model */}
      <section className="bg-slate-800 border border-slate-700 rounded-lg p-6">
        <h2 className="text-lg font-semibold mb-4">Загрузить новую модель</h2>
        <div className="flex gap-2 mb-3">
          <input
            type="text"
            value={pullName}
            onChange={(e) => setPullName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !pulling && handlePull()}
            placeholder="например: llama3.2:3b, qwen3:8b"
            disabled={pulling}
            className="flex-1 bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
          />
          <button
            onClick={handlePull}
            disabled={pulling || !pullName.trim()}
            className="px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 disabled:opacity-50 whitespace-nowrap"
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
          Имена моделей:&nbsp;
          <a
            href="https://ollama.com/library"
            target="_blank"
            rel="noreferrer"
            className="underline hover:text-slate-300"
          >
            ollama.com/library
          </a>
        </p>
      </section>

      {/* About */}
      <section className="bg-slate-800 border border-slate-700 rounded-lg p-6">
        <h2 className="text-lg font-semibold mb-2">О системе</h2>
        <p className="text-sm text-slate-400">
          AI Manufacturing Workspace v0.1.0
        </p>
        <p className="text-sm text-slate-500 mt-1">
          AI-ассистент: Света · Backend: FastAPI · AI: Ollama
        </p>
      </section>
    </div>
  );
}
