"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";

const API = getApiBaseUrl();

// ── Style tokens ──────────────────────────────────────────────────────────────
const card = "border border-slate-700 rounded-lg overflow-hidden";
const cardH =
  "px-4 py-2 bg-slate-800 border-b border-slate-700 flex items-center justify-between";
const input =
  "w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50";
const select =
  "w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500";
const btn = "px-3 py-1.5 rounded text-sm font-medium transition-colors";
const btnPrimary = `${btn} bg-blue-600 hover:bg-blue-700 text-white disabled:opacity-50`;
const btnSecondary = `${btn} bg-slate-700 hover:bg-slate-600 text-slate-200`;
const btnDanger = `${btn} bg-red-700 hover:bg-red-600 text-white`;

// ── Types ─────────────────────────────────────────────────────────────────────

type Provider = "ollama" | "llamacpp" | "vllm";
type Tab = "overview" | "library" | "routing" | "parameters" | "gpu";
type Source = "local" | "huggingface" | "modelscope";

interface CatalogEntry {
  key: string;
  provider: string;
  provider_model: string;
  modalities: string[];
  local_only: boolean;
  vram_gb_estimate: number | null;
  status: string;
}

interface RepoFile {
  filename?: string;
  rfilename?: string;
  size_human?: string;
  quant?: string;
  is_split?: boolean;
}

interface RoutingTask {
  task: string;
  label: string;
  models: string[];
  profile: string;
  local_only: boolean;
  allow_cloud: boolean;
  confidential_locked: boolean;
  required_modalities: string[];
  params: {
    temperature?: number;
    top_p?: number;
    repeat_penalty?: number;
  };
}

const LOCAL_PROVIDERS = [
  "ollama",
  "llamacpp",
  "vllm",
  "openai_compatible",
  "lmstudio",
];

interface ProviderStatus {
  running: boolean;
  url?: string;
  models?: string[];
  model_loaded?: string | null;
  model_count?: number;
  error?: string;
  gpu_memory_utilization?: number;
  max_model_len?: number;
  dtype?: string;
}

interface AllStatus {
  providers: {
    ollama: ProviderStatus;
    llamacpp: ProviderStatus;
    vllm: ProviderStatus;
  };
  gpu: {
    total_gb: number;
    used_gb: number;
    free_gb: number;
    driver_version?: string;
  } | null;
  vram_allocations: Record<
    string,
    {
      vram_used_gb: number;
      vram_limit_gb: number | null;
      running: boolean;
      models: { name: string; vram_gb: number }[];
    }
  >;
  total_vram_gb: number;
}

interface ModelItem {
  name?: string;
  repo_id?: string;
  model_name?: string;
  author?: string;
  path?: string;
  size_bytes?: number;
  size_human?: string;
  format?: string;
  active?: boolean;
  downloads?: number;
  likes?: number;
  stars?: number;
  tags?: string[];
  gated?: boolean;
  source?: string;
  vram_gb_estimate?: number;
}

interface Profile {
  name: string;
  description?: string;
  builtin: boolean;
  temperature?: number;
  top_p?: number;
  top_k?: number;
  repeat_penalty?: number;
}

interface ProviderDefaults {
  defaults: Record<string, Record<string, unknown>>;
  total_vram_gb: number;
}

const PROVIDER_LABELS: Record<Provider, string> = {
  ollama: "Ollama",
  llamacpp: "llama.cpp",
  vllm: "vLLM",
};

const PROFILE_LABELS: Record<string, string> = {
  anti_hallucination: "Без галлюцинаций",
  structured_reasoning: "Структ. рассуждение",
  balanced: "Баланс",
  creative: "Творческий",
};

function humanBytes(b: number): string {
  if (!b) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let v = b;
  for (const u of units) {
    if (v < 1024) return `${v.toFixed(1)} ${u}`;
    v /= 1024;
  }
  return `${v.toFixed(1)} TB`;
}

function VRAMBar({
  used,
  total,
  allocations,
}: {
  used: number;
  total: number;
  allocations: AllStatus["vram_allocations"];
}) {
  const pct = total > 0 ? Math.min((used / total) * 100, 100) : 0;
  const colors: Record<string, string> = {
    ollama: "bg-blue-500",
    llamacpp: "bg-emerald-500",
    vllm: "bg-purple-500",
  };
  return (
    <div className="space-y-2">
      <div className="flex justify-between text-xs text-slate-400">
        <span>VRAM использование</span>
        <span>
          {used.toFixed(1)} / {total.toFixed(0)} GB ({pct.toFixed(0)}%)
        </span>
      </div>
      <div className="h-4 rounded bg-slate-800 overflow-hidden flex">
        {Object.entries(allocations).map(([p, a]) => {
          const w = total > 0 ? (a.vram_used_gb / total) * 100 : 0;
          return w > 0 ? (
            <div
              key={p}
              className={`${colors[p] ?? "bg-slate-500"} transition-all`}
              style={{ width: `${w}%` }}
              title={`${PROVIDER_LABELS[p as Provider] ?? p}: ${a.vram_used_gb.toFixed(1)} GB`}
            />
          ) : null;
        })}
      </div>
      <div className="flex gap-3 flex-wrap text-xs text-slate-400">
        {Object.entries(allocations).map(([p, a]) => (
          <span key={p} className="flex items-center gap-1">
            <span
              className={`w-2 h-2 rounded-full ${colors[p] ?? "bg-slate-500"}`}
            />
            {PROVIDER_LABELS[p as Provider] ?? p}: {a.vram_used_gb.toFixed(1)}{" "}
            GB
          </span>
        ))}
        <span className="flex items-center gap-1">
          <span className="w-2 h-2 rounded-full bg-slate-700" />
          Свободно: {Math.max(0, total - used).toFixed(1)} GB
        </span>
      </div>
    </div>
  );
}

function ProviderCard({
  name,
  status,
  onManage,
}: {
  name: Provider;
  status: ProviderStatus;
  onManage: () => void;
}) {
  const running = status.running;
  const modelCount = status.models?.length ?? (status.model_loaded ? 1 : 0);
  return (
    <div className={`${card} flex flex-col`}>
      <div className={cardH}>
        <div className="flex items-center gap-2">
          <span
            className={`w-2 h-2 rounded-full ${running ? "bg-emerald-400" : "bg-slate-500"}`}
          />
          <span className="text-sm font-medium text-slate-100">
            {PROVIDER_LABELS[name]}
          </span>
        </div>
        <span
          className={`text-xs px-1.5 py-0.5 rounded ${running ? "bg-emerald-900 text-emerald-300" : "bg-slate-700 text-slate-400"}`}
        >
          {running ? "Online" : "Offline"}
        </span>
      </div>
      <div className="p-3 flex-1 space-y-2 text-xs text-slate-400">
        {running ? (
          <>
            <div>
              Моделей: <span className="text-slate-200">{modelCount}</span>
            </div>
            {status.model_loaded && (
              <div className="truncate" title={status.model_loaded}>
                Активна:{" "}
                <span className="text-slate-200">
                  {status.model_loaded.split("/").pop()?.split("\\").pop()}
                </span>
              </div>
            )}
            {name === "vllm" && status.gpu_memory_utilization && (
              <div>
                GPU util:{" "}
                <span className="text-slate-200">
                  {(status.gpu_memory_utilization * 100).toFixed(0)}%
                </span>
              </div>
            )}
          </>
        ) : (
          <div className="text-slate-500">
            {status.error ? status.error.slice(0, 60) : "Сервис не запущен"}
          </div>
        )}
      </div>
      <div className="px-3 pb-3 space-y-2">
        <button onClick={onManage} className={`w-full ${btnSecondary}`}>
          Управление
        </button>
        <ServerControls provider={name} />
      </div>
    </div>
  );
}

function ServerControls({ provider }: { provider: Provider }) {
  const [busy, setBusy] = useState<string | null>(null);
  const act = async (action: "start" | "stop" | "restart") => {
    setBusy(action);
    try {
      const r = await fetch(
        `${API}/api/local-models/${provider}/server/${action}`,
        {
          method: "POST",
          headers: await csrfHeaders(),
          credentials: "include",
        },
      );
      const data = await r.json().catch(() => ({}));
      if (!r.ok) alert(`Ошибка: ${data.detail || r.statusText}`);
    } catch (e) {
      alert(`Ошибка: ${e}`);
    }
    setBusy(null);
  };
  return (
    <div className="flex gap-1">
      {(["start", "stop", "restart"] as const).map((a) => (
        <button
          key={a}
          onClick={() => act(a)}
          disabled={busy !== null}
          className={`${btn} flex-1 text-xs bg-slate-800 hover:bg-slate-700 text-slate-300 disabled:opacity-50`}
          title={`${a} ${PROVIDER_LABELS[provider]} container`}
        >
          {busy === a ? "..." : a === "start" ? "▶" : a === "stop" ? "■" : "↻"}
        </button>
      ))}
    </div>
  );
}

// ── Tokens & server config ─────────────────────────────────────────────────────

function TokensPanel() {
  const [hf, setHf] = useState("");
  const [ms, setMs] = useState("");
  const [present, setPresent] = useState<{
    huggingface: boolean;
    modelscope: boolean;
  }>({ huggingface: false, modelscope: false });
  const [saving, setSaving] = useState(false);

  const load = useCallback(() => {
    fetch(`${API}/api/local-models/tokens`, { credentials: "include" })
      .then((r) => r.json())
      .then(setPresent)
      .catch(() => {});
  }, []);
  useEffect(() => load(), [load]);

  const save = async () => {
    setSaving(true);
    try {
      const body: Record<string, string> = {};
      if (hf) body.huggingface = hf;
      if (ms) body.modelscope = ms;
      const r = await fetch(`${API}/api/local-models/tokens`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        credentials: "include",
        body: JSON.stringify(body),
      });
      if (r.ok) {
        setHf("");
        setMs("");
        load();
      }
    } catch {
      /* ignore */
    }
    setSaving(false);
  };

  const del = async (p: "huggingface" | "modelscope") => {
    await fetch(`${API}/api/local-models/tokens/${p}`, {
      method: "DELETE",
      headers: await csrfHeaders(),
      credentials: "include",
    });
    load();
  };

  return (
    <div className={card}>
      <div className={cardH}>
        <span className="text-sm font-medium text-slate-100">
          Токены доступа (для gated-моделей)
        </span>
      </div>
      <div className="p-4 space-y-3">
        {[
          {
            id: "huggingface" as const,
            label: "🤗 HuggingFace",
            val: hf,
            set: setHf,
          },
          {
            id: "modelscope" as const,
            label: "🌐 ModelScope",
            val: ms,
            set: setMs,
          },
        ].map(({ id, label, val, set }) => (
          <div key={id} className="flex items-center gap-2">
            <span className="w-32 text-sm text-slate-300">{label}</span>
            <input
              type="password"
              className={input}
              placeholder={
                present[id] ? "✓ установлен — введите для замены" : "токен"
              }
              value={val}
              onChange={(e) => set(e.target.value)}
            />
            {present[id] && (
              <button onClick={() => del(id)} className={btnDanger}>
                Удалить
              </button>
            )}
          </div>
        ))}
        <button
          onClick={save}
          disabled={saving || (!hf && !ms)}
          className={btnPrimary}
        >
          {saving ? "Сохранение..." : "Сохранить токены"}
        </button>
      </div>
    </div>
  );
}

function ServerConfigPanel({ provider }: { provider: "llamacpp" | "vllm" }) {
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [saving, setSaving] = useState(false);

  const load = useCallback(() => {
    fetch(`${API}/api/local-models/${provider}/config`, {
      credentials: "include",
    })
      .then((r) => r.json())
      .then((d) => {
        setConfig(d.config || {});
        setDraft(d.config || {});
      })
      .catch(() => {});
  }, [provider]);
  useEffect(() => load(), [load]);

  const dirty = JSON.stringify(config) !== JSON.stringify(draft);

  const save = async () => {
    setSaving(true);
    try {
      const r = await fetch(`${API}/api/local-models/${provider}/config`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        credentials: "include",
        body: JSON.stringify({ config: draft }),
      });
      const d = await r.json();
      if (r.ok) {
        setConfig(d.config);
        setDraft(d.config);
      } else alert(`Ошибка: ${d.detail}`);
    } catch (e) {
      alert(`Ошибка: ${e}`);
    }
    setSaving(false);
  };

  if (!config) return null;

  return (
    <div className={card}>
      <div className={cardH}>
        <span className="text-sm font-medium text-slate-100">
          {PROVIDER_LABELS[provider]} — настройки сервера
        </span>
        <span className="text-xs text-slate-500">
          применяются после рестарта
        </span>
      </div>
      <div className="p-4 space-y-2">
        {Object.entries(draft).map(([k, v]) => (
          <div key={k} className="flex items-center gap-2">
            <span className="w-44 text-xs text-slate-400 font-mono">{k}</span>
            {typeof v === "boolean" ? (
              <input
                type="checkbox"
                checked={v}
                onChange={(e) =>
                  setDraft((d) => ({ ...d, [k]: e.target.checked }))
                }
                className="accent-blue-500"
              />
            ) : (
              <input
                className={input}
                value={String(v ?? "")}
                onChange={(e) => {
                  const raw = e.target.value;
                  const num = Number(raw);
                  setDraft((d) => ({
                    ...d,
                    [k]: raw !== "" && !Number.isNaN(num) ? num : raw,
                  }));
                }}
              />
            )}
          </div>
        ))}
        <div className="flex gap-2 pt-1">
          <button
            onClick={save}
            disabled={!dirty || saving}
            className={btnPrimary}
          >
            {saving ? "..." : "Сохранить"}
          </button>
          {dirty && (
            <button onClick={() => setDraft(config)} className={btnSecondary}>
              Отменить
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function ServersPanel() {
  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
      <TokensPanel />
      <ServerConfigPanel provider="llamacpp" />
      <ServerConfigPanel provider="vllm" />
    </div>
  );
}

interface PresetItem {
  name: string;
  label: string;
  description: string;
  tasks: string[];
}

function PresetsPanel() {
  const [presets, setPresets] = useState<PresetItem[]>([]);
  const [selected, setSelected] = useState("");
  const [applying, setApplying] = useState(false);

  useEffect(() => {
    fetch(`${API}/api/local-models/presets`, { credentials: "include" })
      .then((r) => r.json())
      .then((d) => {
        setPresets(d.presets || []);
        if (d.presets?.[0]) setSelected(d.presets[0].name);
      })
      .catch(() => {});
  }, []);

  const apply = async () => {
    if (!selected) return;
    if (
      !confirm(
        "Применить пресет? Он перезапишет маршрутизацию указанных задач и VRAM-лимиты.",
      )
    )
      return;
    setApplying(true);
    try {
      const r = await fetch(
        `${API}/api/local-models/presets/${selected}/apply`,
        {
          method: "POST",
          headers: await csrfHeaders(),
          credentials: "include",
        },
      );
      const d = await r.json();
      if (r.ok)
        alert(
          `Применено: ${d.applied?.join(", ") || "—"}${
            d.skipped?.length ? ` · пропущено: ${d.skipped.join(", ")}` : ""
          }`,
        );
      else alert(`Ошибка: ${d.detail}`);
    } catch (e) {
      alert(`Ошибка: ${e}`);
    }
    setApplying(false);
  };

  const current = presets.find((p) => p.name === selected);

  return (
    <div className={card}>
      <div className={cardH}>
        <span className="text-sm font-medium text-slate-100">
          Пресеты под железо
        </span>
      </div>
      <div className="p-4 space-y-3">
        <div className="flex gap-2 items-center flex-wrap">
          <select
            className={`${select} flex-1 min-w-48`}
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
          >
            {presets.map((p) => (
              <option key={p.name} value={p.name}>
                {p.label}
              </option>
            ))}
          </select>
          <button
            onClick={apply}
            disabled={applying || !selected}
            className={btnPrimary}
          >
            {applying ? "..." : "Применить"}
          </button>
        </div>
        {current && (
          <div className="text-xs text-slate-500">
            {current.description}
            <div className="mt-1 text-slate-600">
              Задачи: {current.tasks.join(", ")}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

interface TelemetryRow {
  task: string;
  model: string;
  calls: number;
  errors: number;
  avg_latency_ms: number;
  tokens_in: number;
  tokens_out: number;
}

function TelemetryPanel() {
  const [rows, setRows] = useState<TelemetryRow[]>([]);
  const [totals, setTotals] = useState<{ calls: number; errors: number }>({
    calls: 0,
    errors: 0,
  });

  const load = useCallback(() => {
    fetch(`${API}/api/local-models/telemetry/summary`, {
      credentials: "include",
    })
      .then((r) => r.json())
      .then((d) => {
        setRows(d.by_model || []);
        setTotals(d.totals || { calls: 0, errors: 0 });
      })
      .catch(() => {});
  }, []);
  useEffect(() => load(), [load]);

  const reset = async () => {
    if (!confirm("Сбросить статистику использования?")) return;
    await fetch(`${API}/api/local-models/telemetry/reset`, {
      method: "POST",
      headers: await csrfHeaders(),
      credentials: "include",
    });
    load();
  };

  return (
    <div className={card}>
      <div className={cardH}>
        <span className="text-sm font-medium text-slate-100">
          Использование моделей
        </span>
        <div className="flex items-center gap-3">
          <span className="text-xs text-slate-400">
            {totals.calls} вызовов · {totals.errors} ошибок
          </span>
          <button
            onClick={reset}
            className="text-xs text-slate-500 hover:text-slate-300"
          >
            Сброс
          </button>
        </div>
      </div>
      <div className="p-4">
        {rows.length === 0 ? (
          <div className="text-sm text-slate-500">
            Пока нет данных — статистика появится после AI-вызовов.
          </div>
        ) : (
          <div className="space-y-1">
            <div className="grid grid-cols-12 gap-2 text-xs text-slate-500 pb-1 border-b border-slate-800">
              <span className="col-span-3">Задача</span>
              <span className="col-span-4">Модель</span>
              <span className="col-span-1 text-right">N</span>
              <span className="col-span-1 text-right">err</span>
              <span className="col-span-3 text-right">ср. латентность</span>
            </div>
            {rows.slice(0, 20).map((r) => (
              <div
                key={`${r.task}|${r.model}`}
                className="grid grid-cols-12 gap-2 text-xs text-slate-300 py-0.5"
              >
                <span className="col-span-3 truncate">
                  {PROFILE_LABELS[r.task] ?? r.task}
                </span>
                <span className="col-span-4 truncate font-mono text-slate-400">
                  {r.model}
                </span>
                <span className="col-span-1 text-right">{r.calls}</span>
                <span
                  className={`col-span-1 text-right ${r.errors ? "text-red-400" : ""}`}
                >
                  {r.errors}
                </span>
                <span className="col-span-3 text-right font-mono">
                  {r.avg_latency_ms} ms
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Overview Tab ─────────────────────────────────────────────────────────────

function OverviewTab({
  status,
  onTabChange,
}: {
  status: AllStatus | null;
  onTabChange: (t: Tab) => void;
}) {
  if (!status)
    return <div className="text-slate-400 text-sm p-6">Загрузка...</div>;
  const { providers, gpu, vram_allocations, total_vram_gb } = status;
  const usedVram = Object.values(vram_allocations).reduce(
    (s, a) => s + a.vram_used_gb,
    0,
  );

  return (
    <div className="space-y-6">
      {/* VRAM summary */}
      <div className={card}>
        <div className={cardH}>
          <span className="text-sm font-medium text-slate-100">GPU VRAM</span>
          {gpu && (
            <span className="text-xs text-slate-400">
              RTX · {gpu.total_gb.toFixed(0)} GB · Драйвер{" "}
              {gpu.driver_version ?? "—"}
            </span>
          )}
        </div>
        <div className="p-4">
          <VRAMBar
            used={gpu?.used_gb ?? usedVram}
            total={gpu?.total_gb ?? total_vram_gb}
            allocations={vram_allocations}
          />
        </div>
      </div>

      {/* Provider cards */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        {(["ollama", "llamacpp", "vllm"] as Provider[]).map((p) => (
          <ProviderCard
            key={p}
            name={p}
            status={providers[p]}
            onManage={() => onTabChange("library")}
          />
        ))}
      </div>

      {/* Hardware presets */}
      <PresetsPanel />

      {/* Servers & tokens */}
      <ServersPanel />

      {/* Usage telemetry */}
      <TelemetryPanel />

      {/* Quick tips */}
      <div className="text-xs text-slate-500 space-y-1 bg-slate-900 rounded p-3 border border-slate-800">
        <div className="text-slate-400 font-medium mb-2">Быстрый старт</div>
        <div>
          • <b className="text-slate-300">Ollama</b>: запустить через{" "}
          <code className="bg-slate-800 px-1 rounded">
            docker compose --profile embedded-ollama up
          </code>
        </div>
        <div>
          • <b className="text-slate-300">llama.cpp</b>:{" "}
          <code className="bg-slate-800 px-1 rounded">
            docker compose --profile embedded-llamacpp up
          </code>{" "}
          → скачать GGUF во вкладке «Библиотека»
        </div>
        <div>
          • <b className="text-slate-300">vLLM</b>:{" "}
          <code className="bg-slate-800 px-1 rounded">
            docker compose --profile embedded-vllm up
          </code>{" "}
          → скачать модель AWQ/Safetensors
        </div>
      </div>
    </div>
  );
}

// ── Library Tab ───────────────────────────────────────────────────────────────

function LibraryTab() {
  const [source, setSource] = useState<Source>("local");
  const [provider, setProvider] = useState<Provider | "">("");
  const [query, setQuery] = useState("");
  const [format, setFormat] = useState("");
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<ModelItem[]>([]);
  const [localModels, setLocalModels] = useState<Record<Provider, ModelItem[]>>(
    {
      ollama: [],
      llamacpp: [],
      vllm: [],
    },
  );
  const [activating, setActivating] = useState<string | null>(null);
  const [downloading, setDownloading] = useState<
    Record<string, { pct: number; status: string }>
  >({});
  const [files, setFiles] = useState<Record<string, RepoFile[] | "loading">>(
    {},
  );
  const streamRefs = useRef<Record<string, EventSource>>({});

  const toggleFiles = async (repoId: string) => {
    if (files[repoId]) {
      setFiles((prev) => {
        const next = { ...prev };
        delete next[repoId];
        return next;
      });
      return;
    }
    setFiles((prev) => ({ ...prev, [repoId]: "loading" }));
    try {
      const params = new URLSearchParams({ source });
      const p = provider || "llamacpp";
      const r = await fetch(
        `${API}/api/local-models/${p}/model/${encodeURIComponent(repoId)}/files?${params}`,
        { credentials: "include" },
      );
      const data = await r.json();
      setFiles((prev) => ({ ...prev, [repoId]: data.files || [] }));
    } catch {
      setFiles((prev) => ({ ...prev, [repoId]: [] }));
    }
  };

  const loadLocal = useCallback(async () => {
    for (const p of ["ollama", "llamacpp", "vllm"] as Provider[]) {
      try {
        const r = await fetch(`${API}/api/local-models/${p}/models`, {
          credentials: "include",
        });
        if (r.ok) {
          const data = await r.json();
          setLocalModels((prev) => ({ ...prev, [p]: data.models || [] }));
        }
      } catch {
        /* ignore */
      }
    }
  }, []);

  useEffect(() => {
    loadLocal();
  }, [loadLocal]);

  const doSearch = async () => {
    if (!query.trim()) return;
    setSearching(true);
    try {
      const params = new URLSearchParams({ q: query, source, limit: "12" });
      if (provider) params.set("provider", provider);
      if (format) params.set("format", format);
      const r = await fetch(`${API}/api/local-models/search?${params}`, {
        credentials: "include",
      });
      const data = await r.json();
      setResults(data.results || []);
    } catch {
      setResults([]);
    }
    setSearching(false);
  };

  const doActivate = async (
    p: Provider,
    path: string,
    vramEstimate?: number,
  ) => {
    setActivating(path);
    try {
      const r = await fetch(`${API}/api/local-models/${p}/activate`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        credentials: "include",
        body: JSON.stringify({
          model_path: path,
          vram_gb_estimate: vramEstimate ?? 0,
        }),
      });
      const data = await r.json();
      if (!r.ok)
        alert(`Ошибка активации: ${data.detail || JSON.stringify(data)}`);
      else alert(`Активация: ${data.message ?? data.status}`);
      loadLocal();
    } catch (e) {
      alert(`Ошибка: ${e}`);
    }
    setActivating(null);
  };

  const doDownload = async (
    p: Provider,
    repoId: string,
    filename: string,
    src: string,
  ) => {
    const key = `${p}::${repoId}::${filename}`;
    try {
      const r = await fetch(`${API}/api/local-models/${p}/download`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        credentials: "include",
        body: JSON.stringify({ repo_id: repoId, filename, source: src }),
      });
      const data = await r.json();
      const did = data.download_id;
      if (!did) {
        alert("Ошибка запуска загрузки");
        return;
      }
      setDownloading((prev) => ({
        ...prev,
        [key]: { pct: 0, status: "pending" },
      }));
      const es = new EventSource(
        `${API}/api/local-models/${p}/download/${encodeURIComponent(did)}/stream`,
      );
      streamRefs.current[key] = es;
      es.onmessage = (ev) => {
        const d = JSON.parse(ev.data);
        setDownloading((prev) => ({
          ...prev,
          [key]: { pct: d.progress_pct ?? 0, status: d.status },
        }));
        if (
          d.status === "done" ||
          d.status === "completed" ||
          d.status === "error"
        ) {
          es.close();
          delete streamRefs.current[key];
          if (d.status !== "error") loadLocal();
        }
      };
    } catch (e) {
      alert(`Ошибка: ${e}`);
    }
  };

  return (
    <div className="space-y-4">
      {/* Source toggle */}
      <div className="flex gap-2 flex-wrap">
        {(["local", "huggingface", "modelscope"] as Source[]).map((s) => (
          <button
            key={s}
            onClick={() => setSource(s)}
            className={`${btn} ${source === s ? "bg-blue-600 text-white" : "bg-slate-700 text-slate-300"}`}
          >
            {s === "local"
              ? "Локальные"
              : s === "huggingface"
                ? "HuggingFace"
                : "ModelScope"}
          </button>
        ))}
      </div>

      {source !== "local" && (
        <div className="flex gap-2 flex-wrap items-end">
          <div className="flex-1 min-w-48">
            <input
              className={input}
              placeholder="Поиск модели..."
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && doSearch()}
            />
          </div>
          <select
            className={`${select} w-36`}
            value={provider}
            onChange={(e) => setProvider(e.target.value as Provider | "")}
          >
            <option value="">Все провайдеры</option>
            <option value="ollama">Ollama</option>
            <option value="llamacpp">llama.cpp</option>
            <option value="vllm">vLLM</option>
          </select>
          <select
            className={`${select} w-36`}
            value={format}
            onChange={(e) => setFormat(e.target.value)}
          >
            <option value="">Все форматы</option>
            <option value="gguf">GGUF</option>
            <option value="safetensors">Safetensors</option>
            <option value="awq">AWQ</option>
            <option value="gptq">GPTQ</option>
          </select>
          <button
            onClick={doSearch}
            disabled={searching}
            className={btnPrimary}
          >
            {searching ? "Поиск..." : "Найти"}
          </button>
        </div>
      )}

      {/* Search results */}
      {source !== "local" && results.length > 0 && (
        <div className="space-y-2">
          <div className="text-xs text-slate-400">
            Результаты ({results.length})
          </div>
          {results.map((m) => {
            const name = m.model_name ?? m.name ?? m.repo_id ?? "Unknown";
            const key = `${provider}::${m.repo_id}::${name}`;
            const dl = downloading[key];
            return (
              <div key={m.repo_id} className={`${card} p-3`}>
                <div className="flex items-start justify-between gap-3">
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium text-slate-100 truncate">
                      {name}
                    </div>
                    <div className="text-xs text-slate-500 truncate">
                      {m.repo_id}
                    </div>
                    <div className="flex gap-2 flex-wrap mt-1">
                      {m.downloads != null && (
                        <span className="text-xs text-slate-500">
                          ↓ {m.downloads.toLocaleString()}
                        </span>
                      )}
                      {m.likes != null && (
                        <span className="text-xs text-slate-500">
                          ♥ {m.likes}
                        </span>
                      )}
                      {m.gated && (
                        <span className="text-xs bg-amber-900 text-amber-300 px-1 rounded">
                          Gated
                        </span>
                      )}
                      {(m.tags ?? []).slice(0, 3).map((t) => (
                        <span
                          key={t}
                          className="text-xs bg-slate-800 text-slate-400 px-1 rounded"
                        >
                          {t}
                        </span>
                      ))}
                    </div>
                  </div>
                  <div className="flex gap-2 flex-shrink-0">
                    {provider && provider !== "ollama" && (
                      <button
                        onClick={() => toggleFiles(m.repo_id!)}
                        className={btnSecondary}
                      >
                        {files[m.repo_id!] ? "▲ Файлы" : "▼ Файлы"}
                      </button>
                    )}
                    {provider && (
                      <button
                        onClick={() =>
                          doDownload(
                            provider as Provider,
                            m.repo_id!,
                            name,
                            source,
                          )
                        }
                        disabled={!!dl && dl.status !== "error"}
                        className={btnSecondary}
                      >
                        {dl
                          ? dl.status === "done" || dl.status === "completed"
                            ? "✓"
                            : `${dl.pct.toFixed(0)}%`
                          : "↓ Скачать"}
                      </button>
                    )}
                  </div>
                </div>
                {dl && dl.status !== "done" && dl.status !== "completed" && (
                  <div className="mt-2">
                    <div className="h-1.5 rounded bg-slate-700">
                      <div
                        className="h-full rounded bg-blue-500 transition-all"
                        style={{ width: `${dl.pct}%` }}
                      />
                    </div>
                    <div className="text-xs text-slate-500 mt-1">
                      {dl.status} · {dl.pct.toFixed(1)}%
                    </div>
                  </div>
                )}
                {/* File / quant list */}
                {files[m.repo_id!] === "loading" && (
                  <div className="mt-2 text-xs text-slate-500">
                    Загрузка списка файлов...
                  </div>
                )}
                {Array.isArray(files[m.repo_id!]) && (
                  <div className="mt-2 space-y-1 border-t border-slate-800 pt-2">
                    {(files[m.repo_id!] as RepoFile[]).length === 0 && (
                      <div className="text-xs text-slate-600">
                        Файлы не найдены
                      </div>
                    )}
                    {(files[m.repo_id!] as RepoFile[]).map((f) => {
                      const fname = f.filename ?? f.rfilename ?? "";
                      const fkey = `${provider}::${m.repo_id}::${fname}`;
                      const fdl = downloading[fkey];
                      return (
                        <div
                          key={fname}
                          className="flex items-center justify-between gap-2 text-xs"
                        >
                          <div className="flex items-center gap-2 min-w-0">
                            {f.quant && (
                              <span className="bg-slate-800 text-slate-300 px-1 rounded font-mono">
                                {f.quant}
                              </span>
                            )}
                            <span className="truncate text-slate-400 font-mono">
                              {fname}
                            </span>
                            {f.size_human && (
                              <span className="text-slate-600">
                                {f.size_human}
                              </span>
                            )}
                          </div>
                          <button
                            onClick={() =>
                              doDownload(
                                provider as Provider,
                                m.repo_id!,
                                fname,
                                source,
                              )
                            }
                            disabled={!!fdl && fdl.status !== "error"}
                            className={`${btn} bg-slate-800 hover:bg-slate-700 text-slate-300 disabled:opacity-50`}
                          >
                            {fdl
                              ? fdl.status === "done" ||
                                fdl.status === "completed"
                                ? "✓"
                                : `${fdl.pct.toFixed(0)}%`
                              : "↓"}
                          </button>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Local models */}
      {(["ollama", "llamacpp", "vllm"] as Provider[]).map((p) => {
        const models = localModels[p];
        if (source !== "local" && !models.length) return null;
        return (
          <div key={p} className={card}>
            <div className={cardH}>
              <span className="text-sm font-medium text-slate-100">
                {PROVIDER_LABELS[p]} — локальные модели
              </span>
              <span className="text-xs text-slate-400">
                {models.length} моделей
              </span>
            </div>
            <div className="divide-y divide-slate-800">
              {models.length === 0 && (
                <div className="px-4 py-3 text-sm text-slate-500">
                  Нет загруженных моделей
                </div>
              )}
              {models.map((m, i) => {
                const name = m.name ?? m.path?.split("/").pop() ?? `model-${i}`;
                const aKey = `${p}::${name}`;
                return (
                  <div
                    key={name}
                    className="px-4 py-3 flex items-center justify-between gap-3 hover:bg-slate-800/40"
                  >
                    <div className="flex-1 min-w-0">
                      <div className="text-sm text-slate-100 truncate flex items-center gap-2">
                        {m.active && (
                          <span className="text-xs bg-blue-900 text-blue-300 px-1.5 py-0.5 rounded">
                            Активна
                          </span>
                        )}
                        {name}
                      </div>
                      <div className="text-xs text-slate-500 flex gap-3">
                        {m.size_human && <span>{m.size_human}</span>}
                        {m.format && <span>{m.format.toUpperCase()}</span>}
                        {m.vram_gb_estimate != null &&
                          m.vram_gb_estimate > 0 && (
                            <span>
                              ~{m.vram_gb_estimate.toFixed(1)} GB VRAM
                            </span>
                          )}
                      </div>
                    </div>
                    <button
                      onClick={() =>
                        doActivate(p, m.path ?? name, m.vram_gb_estimate)
                      }
                      disabled={activating === (m.path ?? name) || !!m.active}
                      className={
                        m.active
                          ? `${btn} bg-slate-800 text-slate-500 cursor-default`
                          : btnSecondary
                      }
                    >
                      {activating === (m.path ?? name)
                        ? "..."
                        : m.active
                          ? "Активна"
                          : "Активировать"}
                    </button>
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── Routing Tab ───────────────────────────────────────────────────────────────

function modelLabel(entry: CatalogEntry | undefined, key: string): string {
  if (!entry) return key;
  return `${entry.provider_model} · ${entry.provider}`;
}

function RoutingRow({
  task,
  catalog,
  profiles,
  onSaved,
}: {
  task: RoutingTask;
  catalog: CatalogEntry[];
  profiles: Profile[];
  onSaved: (t: RoutingTask) => void;
}) {
  const [draft, setDraft] = useState<RoutingTask>(task);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    setDraft(task);
    setDirty(false);
  }, [task]);

  const byKey = (k: string) => catalog.find((c) => c.key === k);

  // Models eligible for this task: must cover all required modalities, and
  // respect the local/cloud policy currently selected.
  const eligible = catalog.filter((c) => {
    const modOk = draft.required_modalities.every((m) =>
      c.modalities.includes(m),
    );
    const isLocal = LOCAL_PROVIDERS.includes(c.provider);
    const policyOk = draft.allow_cloud || isLocal;
    return modOk && policyOk;
  });

  const update = (patch: Partial<RoutingTask>) => {
    setDraft((d) => ({ ...d, ...patch }));
    setDirty(true);
  };

  const setPrimary = (key: string) => {
    const rest = draft.models.filter((m) => m !== key);
    update({ models: [key, ...rest] });
  };
  const addFallback = (key: string) => {
    if (!key || draft.models.includes(key)) return;
    update({ models: [...draft.models, key] });
  };
  const removeModel = (key: string) => {
    update({ models: draft.models.filter((m) => m !== key) });
  };

  const save = async () => {
    setSaving(true);
    try {
      const r = await fetch(`${API}/api/local-models/routing/${draft.task}`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        credentials: "include",
        body: JSON.stringify({
          models: draft.models,
          profile: draft.profile,
          local_only: draft.local_only,
          allow_cloud: draft.allow_cloud,
        }),
      });
      const data = await r.json();
      if (!r.ok) {
        alert(`Ошибка: ${data.detail || JSON.stringify(data)}`);
      } else {
        setDirty(false);
        onSaved({ ...draft, ...data });
      }
    } catch (e) {
      alert(`Ошибка: ${e}`);
    }
    setSaving(false);
  };

  const reset = async () => {
    setSaving(true);
    try {
      const r = await fetch(
        `${API}/api/local-models/routing/${draft.task}/reset`,
        {
          method: "POST",
          headers: await csrfHeaders(),
          credentials: "include",
        },
      );
      const data = await r.json();
      onSaved({ ...draft, ...data });
      setDirty(false);
    } catch {
      /* ignore */
    }
    setSaving(false);
  };

  const primary = draft.models[0];
  const fallbacks = draft.models.slice(1);

  const [bench, setBench] = useState<string | null>(null);
  const benchmark = async () => {
    if (!primary) return;
    setBench("...");
    try {
      const r = await fetch(`${API}/api/local-models/benchmark`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        credentials: "include",
        body: JSON.stringify({ model_key: primary, task: draft.task }),
      });
      const d = await r.json();
      setBench(d.ok ? `${d.latency_ms} ms` : `ошибка`);
    } catch {
      setBench("ошибка");
    }
  };

  return (
    <div className="px-4 py-3 space-y-2 hover:bg-slate-800/20">
      <div className="flex items-center gap-3 flex-wrap">
        <div className="w-40 shrink-0">
          <div className="text-sm text-slate-100">{draft.label}</div>
          <div className="text-xs text-slate-600 font-mono">
            {draft.required_modalities.join(", ") || "—"}
          </div>
        </div>

        {/* Primary model */}
        <select
          className={`${select} flex-1 min-w-48`}
          value={primary ?? ""}
          onChange={(e) => setPrimary(e.target.value)}
        >
          {!primary && <option value="">— выбрать модель —</option>}
          {eligible.map((c) => (
            <option key={c.key} value={c.key}>
              {modelLabel(c, c.key)}
              {c.vram_gb_estimate ? ` · ${c.vram_gb_estimate} GB` : ""}
            </option>
          ))}
        </select>

        {/* Profile */}
        <select
          className={`${select} w-44`}
          value={draft.profile}
          onChange={(e) => update({ profile: e.target.value })}
        >
          {profiles.map((p) => (
            <option key={p.name} value={p.name}>
              {PROFILE_LABELS[p.name] ?? p.name}
              {p.builtin ? "" : " (custom)"}
            </option>
          ))}
        </select>

        {/* Policy */}
        {draft.confidential_locked ? (
          <span
            className="text-xs bg-slate-800 text-slate-300 px-2 py-1 rounded whitespace-nowrap"
            title="Конфиденциальная задача — только локально"
          >
            🔒 local
          </span>
        ) : (
          <button
            onClick={() =>
              update({
                allow_cloud: !draft.allow_cloud,
                local_only: draft.allow_cloud,
              })
            }
            className={`${btn} whitespace-nowrap ${
              draft.allow_cloud
                ? "bg-purple-900 text-purple-200"
                : "bg-slate-700 text-slate-300"
            }`}
          >
            {draft.allow_cloud ? "☁ cloud ok" : "🔒 local"}
          </button>
        )}
      </div>

      {/* Fallback chain */}
      <div className="flex items-center gap-2 flex-wrap pl-40">
        <span className="text-xs text-slate-500">fallback:</span>
        {fallbacks.length === 0 && (
          <span className="text-xs text-slate-600">нет</span>
        )}
        {fallbacks.map((k) => (
          <span
            key={k}
            className="text-xs bg-slate-800 text-slate-300 px-1.5 py-0.5 rounded flex items-center gap-1"
          >
            {modelLabel(byKey(k), k)}
            <button
              onClick={() => removeModel(k)}
              className="text-slate-500 hover:text-red-400"
            >
              ✕
            </button>
          </span>
        ))}
        <select
          className={`${select} w-44 text-xs`}
          value=""
          onChange={(e) => addFallback(e.target.value)}
        >
          <option value="">+ добавить fallback</option>
          {eligible
            .filter((c) => !draft.models.includes(c.key))
            .map((c) => (
              <option key={c.key} value={c.key}>
                {modelLabel(c, c.key)}
              </option>
            ))}
        </select>

        <div className="flex-1" />
        {bench && (
          <span className="text-xs text-slate-400 font-mono">⏱ {bench}</span>
        )}
        <button
          onClick={benchmark}
          disabled={!primary || bench === "..."}
          className={`${btn} bg-slate-800 hover:bg-slate-700 text-slate-300 disabled:opacity-50`}
          title="Замер латентности основной модели"
        >
          ⏱
        </button>
        {dirty && (
          <button onClick={save} disabled={saving} className={btnPrimary}>
            {saving ? "..." : "Сохранить"}
          </button>
        )}
        <button onClick={reset} disabled={saving} className={btnSecondary}>
          Сброс
        </button>
      </div>
    </div>
  );
}

function RoutingTab() {
  const [tasks, setTasks] = useState<RoutingTask[]>([]);
  const [catalog, setCatalog] = useState<CatalogEntry[]>([]);
  const [profiles, setProfiles] = useState<Profile[]>([]);

  const load = useCallback(() => {
    fetch(`${API}/api/local-models/routing`, { credentials: "include" })
      .then((r) => r.json())
      .then((d) => {
        setTasks(d.tasks || []);
        setCatalog(d.catalog || []);
      })
      .catch(() => {});
    fetch(`${API}/api/local-models/parameter-profiles`, {
      credentials: "include",
    })
      .then((r) => r.json())
      .then((d) => setProfiles(d.profiles || []))
      .catch(() => {});
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="space-y-4">
      <div className="text-xs text-slate-500 bg-slate-900 rounded p-3 border border-slate-800">
        Для каждой задачи: основная модель, цепочка fallback (пробуются по
        порядку), профиль инференса и политика. Задачи с документами (OCR,
        чертежи, эмбеддинги, реранкинг) заблокированы на{" "}
        <b className="text-slate-300">🔒 local</b> — документы конфиденциальны.
      </div>
      <div className={card}>
        <div className={cardH}>
          <span className="text-sm font-medium text-slate-100">
            Маршрутизация задач
          </span>
          <span className="text-xs text-slate-500">
            {catalog.length} моделей в каталоге
          </span>
        </div>
        <div className="divide-y divide-slate-800">
          {tasks.map((t) => (
            <RoutingRow
              key={t.task}
              task={t}
              catalog={catalog}
              profiles={profiles}
              onSaved={(nt) =>
                setTasks((prev) =>
                  prev.map((x) => (x.task === nt.task ? nt : x)),
                )
              }
            />
          ))}
        </div>
      </div>
    </div>
  );
}

// ── Parameters Tab ───────────────────────────────────────────────────────────

function ParametersTab() {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [selected, setSelected] = useState("anti_hallucination");
  const [editing, setEditing] = useState<Partial<Profile> | null>(null);
  const [newName, setNewName] = useState("");
  const [saving, setSaving] = useState(false);
  const [defaults, setDefaults] = useState<ProviderDefaults | null>(null);

  useEffect(() => {
    fetch(`${API}/api/local-models/parameter-profiles`, {
      credentials: "include",
    })
      .then((r) => r.json())
      .then((d) => setProfiles(d.profiles || []))
      .catch(() => {});
    fetch(`${API}/api/local-models/provider-defaults`, {
      credentials: "include",
    })
      .then((r) => r.json())
      .then((d) => setDefaults(d))
      .catch(() => {});
  }, []);

  const current = profiles.find((p) => p.name === selected);

  const saveCustom = async () => {
    if (!editing || !newName.trim()) return;
    setSaving(true);
    try {
      await fetch(
        `${API}/api/local-models/parameter-profiles/${encodeURIComponent(newName)}`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(await csrfHeaders()),
          },
          credentials: "include",
          body: JSON.stringify({
            params: {
              ...editing,
              description: `Кастомный профиль: ${newName}`,
            },
          }),
        },
      );
      const r = await fetch(`${API}/api/local-models/parameter-profiles`, {
        credentials: "include",
      });
      const d = await r.json();
      setProfiles(d.profiles || []);
      setSelected(newName);
      setEditing(null);
      setNewName("");
    } catch {
      /* ignore */
    }
    setSaving(false);
  };

  const deleteProfile = async (name: string) => {
    if (!confirm(`Удалить профиль «${name}»?`)) return;
    await fetch(
      `${API}/api/local-models/parameter-profiles/${encodeURIComponent(name)}`,
      {
        method: "DELETE",
        headers: await csrfHeaders(),
        credentials: "include",
      },
    );
    const r = await fetch(`${API}/api/local-models/parameter-profiles`, {
      credentials: "include",
    });
    const d = await r.json();
    setProfiles(d.profiles || []);
    setSelected("anti_hallucination");
  };

  return (
    <div className="space-y-6">
      {/* Profile selector */}
      <div className={card}>
        <div className={cardH}>
          <span className="text-sm font-medium text-slate-100">
            Профили параметров инференса
          </span>
        </div>
        <div className="p-4 space-y-4">
          <div className="flex gap-2 flex-wrap">
            {profiles.map((p) => (
              <button
                key={p.name}
                onClick={() => {
                  setSelected(p.name);
                  setEditing(null);
                }}
                className={`${btn} ${selected === p.name ? "bg-blue-600 text-white" : "bg-slate-700 text-slate-300"}`}
              >
                {PROFILE_LABELS[p.name] ?? p.name}
              </button>
            ))}
          </div>

          {current && (
            <div className="space-y-3 pt-2 border-t border-slate-700">
              {current.description && (
                <div className="text-xs text-slate-400">
                  {current.description}
                </div>
              )}
              {(
                [
                  {
                    key: "temperature",
                    label: "Temperature",
                    min: 0,
                    max: 2,
                    step: 0.05,
                    desc: "0 = детерминировано, >1 = случайно",
                  },
                  {
                    key: "top_p",
                    label: "Top-P",
                    min: 0,
                    max: 1,
                    step: 0.05,
                    desc: "Nucleus sampling threshold",
                  },
                  {
                    key: "top_k",
                    label: "Top-K",
                    min: 1,
                    max: 100,
                    step: 1,
                    desc: "Ограничение выборки по топ-K токенов",
                  },
                  {
                    key: "repeat_penalty",
                    label: "Repeat Penalty",
                    min: 1,
                    max: 2,
                    step: 0.05,
                    desc: "Штраф за повтор (1 = нет)",
                  },
                ] as const
              ).map(({ key, label, min, max, step, desc }) => {
                const val = (editing ?? current)[key as keyof Profile] as
                  | number
                  | undefined;
                return (
                  <div key={key} className="space-y-1">
                    <div className="flex justify-between text-xs">
                      <span className="text-slate-300">{label}</span>
                      <span className="text-slate-400 font-mono">
                        {val?.toFixed(2) ?? "—"}
                      </span>
                    </div>
                    <input
                      type="range"
                      min={min}
                      max={max}
                      step={step}
                      value={val ?? 0}
                      disabled={current.builtin && !editing}
                      onChange={(e) => {
                        const v = parseFloat(e.target.value);
                        setEditing((prev) => ({
                          ...(prev ?? current),
                          [key]: v,
                        }));
                      }}
                      className="w-full accent-blue-500"
                    />
                    <div className="text-xs text-slate-600">{desc}</div>
                  </div>
                );
              })}

              {editing && (
                <div className="pt-2 border-t border-slate-700 space-y-2">
                  <input
                    className={input}
                    placeholder="Название нового профиля..."
                    value={newName}
                    onChange={(e) => setNewName(e.target.value)}
                  />
                  <div className="flex gap-2">
                    <button
                      onClick={saveCustom}
                      disabled={saving || !newName.trim()}
                      className={btnPrimary}
                    >
                      {saving ? "Сохранение..." : "Сохранить профиль"}
                    </button>
                    <button
                      onClick={() => setEditing(null)}
                      className={btnSecondary}
                    >
                      Отмена
                    </button>
                  </div>
                </div>
              )}
              {!editing && current.builtin && (
                <button
                  onClick={() => setEditing({ ...current })}
                  className={btnSecondary}
                >
                  Создать на основе этого...
                </button>
              )}
              {!editing && !current.builtin && (
                <button
                  onClick={() => deleteProfile(current.name)}
                  className={btnDanger}
                >
                  Удалить профиль
                </button>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Provider hardware defaults */}
      {defaults && (
        <div className={card}>
          <div className={cardH}>
            <span className="text-sm font-medium text-slate-100">
              Параметры провайдеров (RTX 3090 · {defaults.total_vram_gb} GB)
            </span>
          </div>
          <div className="p-4 grid grid-cols-1 sm:grid-cols-3 gap-4">
            {Object.entries(defaults.defaults).map(([provName, params]) => (
              <div
                key={provName}
                className="bg-slate-900 rounded p-3 border border-slate-700"
              >
                <div className="text-sm font-medium text-slate-100 mb-2">
                  {PROVIDER_LABELS[provName as Provider] ?? provName}
                </div>
                <div className="space-y-1">
                  {Object.entries(params as Record<string, unknown>).map(
                    ([k, v]) => (
                      <div key={k} className="flex justify-between text-xs">
                        <span className="text-slate-500">{k}</span>
                        <span className="text-slate-300 font-mono">
                          {String(v)}
                        </span>
                      </div>
                    ),
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ── GPU Budget Tab ────────────────────────────────────────────────────────────

function GPUTab({ status }: { status: AllStatus | null }) {
  const [limits, setLimits] = useState<Record<string, number>>({});
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!status) return;
    const init: Record<string, number> = {};
    for (const [p, a] of Object.entries(status.vram_allocations)) {
      init[p] = a.vram_limit_gb ?? 24;
    }
    setLimits(init);
  }, [status]);

  const saveLimits = async () => {
    setSaving(true);
    try {
      await fetch(`${API}/api/local-models/gpu-budget`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        credentials: "include",
        body: JSON.stringify(limits),
      });
    } catch {
      /* ignore */
    }
    setSaving(false);
  };

  if (!status)
    return <div className="text-slate-400 text-sm p-6">Загрузка...</div>;
  const { gpu, vram_allocations, total_vram_gb } = status;
  const total = gpu?.total_gb ?? total_vram_gb;
  const usedByProvider = Object.entries(vram_allocations);
  const colors: Record<string, string> = {
    ollama: "bg-blue-500",
    llamacpp: "bg-emerald-500",
    vllm: "bg-purple-500",
  };

  return (
    <div className="space-y-4">
      {/* GPU info */}
      {gpu && (
        <div className="grid grid-cols-3 gap-3">
          {[
            { label: "Всего VRAM", value: `${gpu.total_gb.toFixed(1)} GB` },
            { label: "Используется", value: `${gpu.used_gb.toFixed(1)} GB` },
            { label: "Свободно", value: `${gpu.free_gb.toFixed(1)} GB` },
          ].map(({ label, value }) => (
            <div
              key={label}
              className="bg-slate-900 rounded p-3 border border-slate-700 text-center"
            >
              <div className="text-xs text-slate-500">{label}</div>
              <div className="text-lg font-mono text-slate-100">{value}</div>
            </div>
          ))}
        </div>
      )}

      {/* Per-provider usage */}
      <div className={card}>
        <div className={cardH}>
          <span className="text-sm font-medium text-slate-100">
            Использование VRAM по провайдерам
          </span>
        </div>
        <div className="p-4 space-y-4">
          {usedByProvider.map(([p, a]) => {
            const pct = total > 0 ? (a.vram_used_gb / total) * 100 : 0;
            const limitPct =
              limits[p] != null && total > 0 ? (limits[p] / total) * 100 : 100;
            return (
              <div key={p} className="space-y-2">
                <div className="flex justify-between text-sm">
                  <span className="text-slate-200">
                    {PROVIDER_LABELS[p as Provider] ?? p}
                  </span>
                  <span className="text-slate-400">
                    {a.vram_used_gb.toFixed(1)} /{" "}
                    {(limits[p] ?? total).toFixed(0)} GB
                  </span>
                </div>
                <div className="relative h-3 rounded bg-slate-800">
                  {/* Limit indicator */}
                  {limits[p] != null && (
                    <div
                      className="absolute top-0 h-full border-r-2 border-amber-400"
                      style={{ left: `${limitPct}%` }}
                      title={`Лимит: ${limits[p]} GB`}
                    />
                  )}
                  {/* Usage bar */}
                  <div
                    className={`h-full rounded ${colors[p] ?? "bg-slate-500"} transition-all`}
                    style={{ width: `${pct}%` }}
                  />
                </div>
                <div className="flex items-center gap-3">
                  <span className="text-xs text-slate-500 w-16">
                    Лимит (GB)
                  </span>
                  <input
                    type="number"
                    min={1}
                    max={total}
                    step={1}
                    value={limits[p] ?? total}
                    onChange={(e) =>
                      setLimits((prev) => ({
                        ...prev,
                        [p]: parseFloat(e.target.value),
                      }))
                    }
                    className={`${input} w-24`}
                  />
                  <span className="text-xs text-slate-600">
                    (мягкий лимит — предупреждение при превышении)
                  </span>
                </div>
                {a.models.map((m) => (
                  <div
                    key={m.name}
                    className="flex justify-between text-xs text-slate-500 pl-4"
                  >
                    <span className="truncate">{m.name}</span>
                    <span>{m.vram_gb.toFixed(1)} GB</span>
                  </div>
                ))}
              </div>
            );
          })}
        </div>
        <div className="px-4 pb-4">
          <button onClick={saveLimits} disabled={saving} className={btnPrimary}>
            {saving ? "Сохранение..." : "Сохранить лимиты"}
          </button>
        </div>
      </div>

      <div className="text-xs text-slate-500 bg-slate-900 rounded p-3 border border-slate-800">
        <b className="text-slate-400">Как работают мягкие лимиты:</b> при
        попытке активировать модель, которая превысит лимит, система показывает
        предупреждение, но не блокирует — вы сами решаете. Для жёсткого
        ограничения — используйте отдельный Docker с device-memory limit.
      </div>
    </div>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function ModelsPage() {
  const [tab, setTab] = useState<Tab>("overview");
  const [status, setStatus] = useState<AllStatus | null>(null);
  const [loading, setLoading] = useState(true);

  const loadStatus = useCallback(async () => {
    try {
      const r = await fetch(`${API}/api/local-models/status`, {
        credentials: "include",
      });
      if (r.ok) setStatus(await r.json());
    } catch {
      /* ignore */
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    loadStatus();
    const t = setInterval(loadStatus, 30000);
    return () => clearInterval(t);
  }, [loadStatus]);

  const TABS: { id: Tab; label: string }[] = [
    { id: "overview", label: "Обзор" },
    { id: "library", label: "Библиотека" },
    { id: "routing", label: "Маршрутизация" },
    { id: "parameters", label: "Параметры" },
    { id: "gpu", label: "GPU Бюджет" },
  ];

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <div className="max-w-5xl mx-auto px-4 py-8 space-y-6">
        {/* Header */}
        <div>
          <h1 className="text-2xl font-bold text-slate-100">
            Модели и провайдеры
          </h1>
          <p className="text-sm text-slate-400 mt-1">
            Ollama, llama.cpp, vLLM и облачные провайдеры · библиотека,
            маршрутизация задач, GPU-бюджет
          </p>
        </div>

        {/* Tabs */}
        <div className="flex gap-1 border-b border-slate-700">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
                tab === t.id
                  ? "border-blue-500 text-blue-400"
                  : "border-transparent text-slate-400 hover:text-slate-200"
              }`}
            >
              {t.label}
            </button>
          ))}
          <div className="flex-1" />
          <button
            onClick={loadStatus}
            className="px-3 py-2 text-xs text-slate-500 hover:text-slate-300 transition-colors"
            title="Обновить"
          >
            {loading ? "..." : "↺"}
          </button>
        </div>

        {/* Tab content */}
        <div>
          {tab === "overview" && (
            <OverviewTab status={status} onTabChange={setTab} />
          )}
          {tab === "library" && <LibraryTab />}
          {tab === "routing" && <RoutingTab />}
          {tab === "parameters" && <ParametersTab />}
          {tab === "gpu" && <GPUTab status={status} />}
        </div>
      </div>
    </div>
  );
}
