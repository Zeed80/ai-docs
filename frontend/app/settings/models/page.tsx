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
// Width-less base so callers can size selects without a `w-full` conflict
// (Tailwind can't predictably override `w-full` with `w-32`/`flex-1`).
const selectBase =
  "rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500";
const select = `w-full ${selectBase}`;
const btn = "px-3 py-1.5 rounded text-sm font-medium transition-colors";
const btnPrimary = `${btn} bg-blue-600 hover:bg-blue-700 text-white disabled:opacity-50`;
const btnSecondary = `${btn} bg-slate-700 hover:bg-slate-600 text-slate-200`;
const btnDanger = `${btn} bg-red-700 hover:bg-red-600 text-white`;

// ── Types ─────────────────────────────────────────────────────────────────────

type Provider = "ollama" | "llamacpp" | "vllm";
type Tab = "assignment" | "overview" | "library" | "parameters" | "gpu";
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

const LOCAL_PROVIDERS = [
  "ollama",
  "llamacpp",
  "vllm",
  "openai_compatible",
  "lmstudio",
];
const THINKING_DISABLE_SUPPORTED_PROVIDERS = [
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

function VllmVersionPanel() {
  const [status, setStatus] = useState<Record<string, unknown> | null>(null);
  const [tag, setTag] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(() => {
    fetch(`${API}/api/local-models/vllm/image-status`, {
      credentials: "include",
    })
      .then((r) => r.json())
      .then(setStatus)
      .catch(() => {});
  }, []);
  useEffect(() => load(), [load]);

  const update = async () => {
    const target = tag.trim();
    if (!target) return;
    if (
      !confirm(
        `Обновить vLLM до образа "${target}"?\n\nБудет скачан новый образ ` +
          `(несколько ГБ) и пересоздан контейнер vllm-server с сохранением ` +
          `конфигурации. Движок перезагрузит модель (GPU). Это может занять ` +
          `несколько минут.`,
      )
    ) {
      return;
    }
    setBusy(true);
    setMsg(
      "Скачивание образа и пересоздание контейнера — это может занять несколько минут…",
    );
    try {
      const r = await fetch(`${API}/api/local-models/vllm/update`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        credentials: "include",
        body: JSON.stringify({ image: target, start: true }),
      });
      const d = await r.json();
      if (r.ok) {
        setMsg(
          `Готово: ${d.image} · ${d.status}` +
            (d.healthy === false ? " · health timeout — проверьте логи" : ""),
        );
        setTag("");
        load();
      } else {
        setMsg(`Ошибка: ${d.detail ?? r.status}`);
      }
    } catch (e) {
      setMsg(`Ошибка: ${e}`);
    }
    setBusy(false);
  };

  if (!status) return null;
  const current =
    (status.current_image as string) ||
    (status.configured_image as string) ||
    (status.default_image as string) ||
    "—";
  return (
    <div className={card}>
      <div className={cardH}>
        <span className="text-sm font-medium text-slate-100">
          vLLM — версия движка
        </span>
        <span className="text-xs text-slate-500">
          pull + пересоздание контейнера
        </span>
      </div>
      <div className="p-4 space-y-2 text-sm">
        <div className="text-xs text-slate-400 font-mono break-all">
          образ: {current} · {status.running ? "running" : "stopped"}
        </div>
        <div className="flex items-center gap-2">
          <input
            className={input}
            placeholder="напр. v0.25.1 или repo:tag"
            value={tag}
            onChange={(e) => setTag(e.target.value)}
            disabled={busy}
          />
          <button
            onClick={update}
            disabled={busy || !tag.trim() || !status.docker_available}
            className={btnPrimary}
          >
            {busy ? "..." : "Обновить"}
          </button>
        </div>
        {msg && <div className="text-xs text-amber-300">{msg}</div>}
        {!status.docker_available && (
          <div className="text-xs text-red-400">
            Docker socket недоступен — обновление невозможно.
          </div>
        )}
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
      <VllmVersionPanel />
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

// ── Providers config (keys + local nodes) ───────────────────────────────────

interface ProviderInstanceT {
  id: string;
  kind: string;
  name: string;
  base_url: string | null;
  default_base_url?: string;
  extra?: { headers?: Record<string, string>; body?: Record<string, unknown> };
  enabled: boolean;
  is_local: boolean;
  api_key_set: boolean;
  api_key_mask: string;
  last_check_ok: boolean | null;
  last_error: string | null;
}
interface KnownKindT {
  kind: string;
  is_local: boolean;
  default_base_url: string;
  requires_api_key: boolean;
}

async function provFetch(path: string, init?: RequestInit) {
  return fetch(`${API}/api/providers${path}`, {
    credentials: "include",
    ...init,
    headers: {
      ...(init && init.method && init.method !== "GET"
        ? { "Content-Type": "application/json", ...(await csrfHeaders()) }
        : {}),
      ...(init?.headers || {}),
    },
  });
}

function ProvidersConfigPanel() {
  const [instances, setInstances] = useState<ProviderInstanceT[]>([]);
  const [kinds, setKinds] = useState<KnownKindT[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [selCloud, setSelCloud] = useState<string | null>(null);

  const flash = (m: string) => {
    setMsg(m);
    window.setTimeout(() => setMsg(null), 2500);
  };
  const load = useCallback(async () => {
    try {
      const r = await provFetch("");
      if (r.ok) {
        const d = await r.json();
        setInstances(d.instances || []);
        setKinds(d.known_kinds || []);
      }
    } catch {
      /* ignore */
    }
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  const localKinds = kinds.filter((k) => k.is_local).map((k) => k.kind);
  const cloud = instances
    .filter((i) => !i.is_local)
    .sort((a, b) => providerLabel(a.kind).localeCompare(providerLabel(b.kind)));
  const localNodesByKind = (kind: string) =>
    instances.filter((i) => i.is_local && i.kind === kind);

  // Default-select the first configured cloud provider (or the first one).
  useEffect(() => {
    if (selCloud === null && cloud.length) {
      setSelCloud(cloud.find((c) => c.api_key_set)?.id ?? cloud[0].id);
    }
  }, [cloud, selCloud]);
  const selectedCloud = cloud.find((c) => c.id === selCloud) || null;

  const test = async (id: string) => {
    setBusy(id);
    try {
      const r = await provFetch(`/${id}/test`, { method: "POST" });
      const d = await r.json().catch(() => ({}));
      flash(d.ok ? `OK · моделей: ${d.model_count}` : `Ошибка: ${d.error}`);
      load();
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className={card}>
      <div className={cardH}>
        <span className="text-sm font-medium text-slate-100">
          Провайдеры и API-ключи
        </span>
        {msg && <span className="text-xs text-emerald-400">{msg}</span>}
      </div>
      <div className="p-4 space-y-5">
        {/* Cloud providers — master/detail */}
        <div>
          <div className="text-xs uppercase tracking-wide text-slate-500 mb-2">
            Облачные провайдеры — выберите слева, настройте справа
          </div>
          <div className="flex flex-col sm:flex-row gap-3 rounded-md border border-slate-700 bg-slate-900/40">
            {/* list */}
            <div className="sm:w-56 sm:max-h-80 sm:overflow-y-auto border-b sm:border-b-0 sm:border-r border-slate-700 p-2 space-y-0.5">
              {cloud.map((c) => (
                <button
                  key={c.id}
                  onClick={() => setSelCloud(c.id)}
                  className={`w-full text-left px-2 py-1.5 rounded text-sm flex items-center gap-2 ${
                    c.id === selCloud
                      ? "bg-blue-600/20 text-blue-200"
                      : "text-slate-300 hover:bg-slate-800"
                  }`}
                >
                  <StatusDotT ok={c.last_check_ok} />
                  <span className="flex-1 truncate">
                    {providerLabel(c.kind)}
                  </span>
                  <span
                    className={`text-[10px] ${
                      c.api_key_set ? "text-emerald-400" : "text-slate-600"
                    }`}
                  >
                    {c.api_key_set ? "ключ ✓" : "нет"}
                  </span>
                </button>
              ))}
            </div>
            {/* detail */}
            <div className="flex-1 p-3">
              {selectedCloud ? (
                <CloudProviderDetail
                  inst={selectedCloud}
                  busy={busy === selectedCloud.id}
                  onTest={() => test(selectedCloud.id)}
                  onChanged={load}
                  flash={flash}
                  setBusy={setBusy}
                />
              ) : (
                <div className="text-sm text-slate-500">
                  Выберите провайдера слева.
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Local nodes */}
        <div>
          <div className="text-xs uppercase tracking-wide text-slate-500 mb-2">
            Локальные узлы — можно добавить адрес Ollama/vLLM на другой машине
          </div>
          <div className="space-y-3">
            {localKinds.map((kind) => (
              <LocalKindBlock
                key={kind}
                kind={kind}
                nodes={localNodesByKind(kind)}
                busy={busy}
                onTest={test}
                onChanged={load}
                flash={flash}
              />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function StatusDotT({ ok }: { ok: boolean | null }) {
  const c =
    ok === true
      ? "text-emerald-400"
      : ok === false
        ? "text-red-400"
        : "text-slate-600";
  return <span className={c}>●</span>;
}

function CloudProviderDetail({
  inst,
  busy,
  onTest,
  onChanged,
  flash,
  setBusy,
}: {
  inst: ProviderInstanceT;
  busy: boolean;
  onTest: () => void;
  onChanged: () => void;
  flash: (m: string) => void;
  setBusy: (s: string | null) => void;
}) {
  const [key, setKey] = useState("");
  const [baseUrl, setBaseUrl] = useState(inst.base_url || "");
  const [showAdv, setShowAdv] = useState(false);
  const [headersText, setHeadersText] = useState("");
  const [bodyText, setBodyText] = useState("");
  useEffect(() => {
    setKey("");
    setBaseUrl(inst.base_url || "");
    const h = inst.extra?.headers ?? {};
    const b = inst.extra?.body ?? {};
    setHeadersText(Object.keys(h).length ? JSON.stringify(h, null, 2) : "");
    setBodyText(Object.keys(b).length ? JSON.stringify(b, null, 2) : "");
    setShowAdv(Object.keys(h).length > 0 || Object.keys(b).length > 0);
  }, [inst.id, inst.base_url, inst.extra]);

  const save = async () => {
    // Validate the optional JSON blocks before sending.
    let headers: Record<string, unknown> = {};
    let bodyParams: Record<string, unknown> = {};
    try {
      headers = headersText.trim() ? JSON.parse(headersText) : {};
      bodyParams = bodyText.trim() ? JSON.parse(bodyText) : {};
    } catch {
      flash("Доп. параметры: некорректный JSON");
      return;
    }
    setBusy(inst.id);
    try {
      const body: Record<string, unknown> = {
        base_url: baseUrl,
        extra: { headers, body: bodyParams },
      };
      if (key) body.api_key = key;
      const r = await provFetch(`/${inst.id}`, {
        method: "PUT",
        body: JSON.stringify(body),
      });
      if (r.ok) {
        setKey("");
        flash("Сохранено");
        onChanged();
      } else flash("Ошибка сохранения");
    } finally {
      setBusy(null);
    }
  };
  const fetchModels = async () => {
    setBusy(inst.id);
    try {
      const r = await provFetch(`/${inst.id}/refresh-models`, {
        method: "POST",
      });
      const d = await r.json().catch(() => ({}));
      flash(r.ok ? `Подтянуто моделей: ${d.count}` : `Ошибка: ${d.detail}`);
      onChanged();
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <StatusDotT ok={inst.last_check_ok} />
        <span className="text-sm font-semibold text-slate-100">
          {providerLabel(inst.kind)}
        </span>
        <span className="text-xs text-slate-500">
          {inst.api_key_set ? `ключ: ${inst.api_key_mask}` : "ключ не задан"}
        </span>
      </div>

      <label className="block text-xs text-slate-400">
        API-ключ
        <input
          className={`${input} mt-1`}
          type="password"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          placeholder={
            inst.api_key_set
              ? "•••••• (введите, чтобы заменить)"
              : "Введите API-ключ"
          }
        />
      </label>

      <label className="block text-xs text-slate-400">
        Адрес API (base URL)
        <input
          className={`${input} mt-1`}
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          placeholder="https://api.example.com/v1"
        />
      </label>

      <div>
        <button
          className="text-xs text-blue-400 hover:text-blue-300"
          onClick={() => setShowAdv((v) => !v)}
        >
          {showAdv ? "▾" : "▸"} Доп. параметры (заголовки / тело запроса)
        </button>
        {showAdv && (
          <div className="mt-2 grid grid-cols-1 gap-2">
            <label className="block text-xs text-slate-400">
              Доп. HTTP-заголовки (JSON)
              <textarea
                className={`${input} mt-1 font-mono text-xs`}
                rows={3}
                value={headersText}
                onChange={(e) => setHeadersText(e.target.value)}
                placeholder={'{\n  "OpenAI-Organization": "org-..."\n}'}
              />
            </label>
            <label className="block text-xs text-slate-400">
              Доп. параметры запроса (JSON) — добавляются в тело каждого запроса
              <textarea
                className={`${input} mt-1 font-mono text-xs`}
                rows={3}
                value={bodyText}
                onChange={(e) => setBodyText(e.target.value)}
                placeholder={'{\n  "reasoning_effort": "low"\n}'}
              />
            </label>
            <p className="text-[11px] text-slate-600">
              Применяются ко всем вызовам этого провайдера. Стандартные
              temperature / top-p / max-tokens — во вкладке «Параметры».
            </p>
          </div>
        )}
      </div>

      {inst.last_error && (
        <div className="text-xs text-red-400">
          Последняя ошибка: {inst.last_error}
        </div>
      )}

      <div className="flex flex-wrap gap-2 pt-1">
        <button className={btnPrimary} disabled={busy} onClick={save}>
          Сохранить
        </button>
        <button className={btnSecondary} disabled={busy} onClick={onTest}>
          {busy ? "…" : "Проверить"}
        </button>
        <button
          className={btnSecondary}
          disabled={busy || !inst.api_key_set}
          onClick={fetchModels}
        >
          Подтянуть модели
        </button>
      </div>
    </div>
  );
}

function LocalKindBlock({
  kind,
  nodes,
  busy,
  onTest,
  onChanged,
  flash,
}: {
  kind: string;
  nodes: ProviderInstanceT[];
  busy: string | null;
  onTest: (id: string) => void;
  onChanged: () => void;
  flash: (m: string) => void;
}) {
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("http://");
  const add = async () => {
    const r = await provFetch("", {
      method: "POST",
      body: JSON.stringify({
        kind,
        name: name || `${kind} — узел`,
        base_url: url,
      }),
    });
    if (r.ok) {
      setAdding(false);
      setName("");
      setUrl("http://");
      flash("Узел добавлен");
      onChanged();
    } else {
      const d = await r.json().catch(() => ({}));
      alert(`Ошибка: ${d.detail || r.status}`);
    }
  };
  const remove = async (id: string) => {
    if (!confirm("Удалить узел?")) return;
    await provFetch(`/${id}`, { method: "DELETE" });
    flash("Узел удалён");
    onChanged();
  };
  return (
    <div className="rounded-md border border-slate-700 bg-slate-900/40 px-3 py-2">
      <div className="flex items-center justify-between mb-1">
        <span className="text-sm text-slate-200">{providerLabel(kind)}</span>
        <button
          className="text-xs text-blue-400 hover:text-blue-300"
          onClick={() => setAdding((v) => !v)}
        >
          + добавить узел
        </button>
      </div>
      {nodes.map((n) => (
        <div key={n.id} className="flex items-center gap-2 py-1 text-sm">
          <StatusDotT ok={n.last_check_ok} />
          <span className="text-slate-300 w-40 truncate">{n.name}</span>
          <span className="text-xs text-slate-500 flex-1 truncate">
            {n.base_url || "(адрес по умолчанию)"}
          </span>
          <button
            className="text-xs text-slate-400 hover:text-slate-200"
            disabled={busy === n.id}
            onClick={() => onTest(n.id)}
          >
            проверить
          </button>
          {!n.name.includes("(default)") && (
            <button
              className="text-xs text-red-400 hover:text-red-300"
              onClick={() => remove(n.id)}
            >
              удалить
            </button>
          )}
        </div>
      ))}
      {adding && (
        <div className="flex flex-wrap items-center gap-2 mt-2">
          <input
            className={`${input} w-44`}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Имя (GPU-сервер 2)"
          />
          <input
            className={`${input} flex-1 min-w-[200px]`}
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="http://192.168.1.50:11434"
          />
          <button className={btnPrimary} onClick={add}>
            Добавить
          </button>
        </div>
      )}
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
      {/* Provider keys & nodes */}
      <ProvidersConfigPanel />

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
  const [pullName, setPullName] = useState("");
  const [pulling, setPulling] = useState(false);
  const [pullStatus, setPullStatus] = useState<string | null>(null);
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

  const doDeleteOllama = async (name: string) => {
    if (!confirm(`Удалить модель «${name}» из Ollama?`)) return;
    try {
      const r = await fetch(
        `${API}/api/local-models/ollama/models/${encodeURIComponent(name)}`,
        {
          method: "DELETE",
          headers: await csrfHeaders(),
          credentials: "include",
        },
      );
      if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        alert(`Ошибка: ${data.detail || r.status}`);
      }
      loadLocal();
    } catch (e) {
      alert(`Ошибка: ${e}`);
    }
  };

  const doPullOllama = async () => {
    const name = pullName.trim();
    if (!name) return;
    setPulling(true);
    setPullStatus("Запуск…");
    try {
      const r = await fetch(`${API}/api/local-models/ollama/pull`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        credentials: "include",
        body: JSON.stringify({ name }),
      });
      if (!r.ok || !r.body) {
        setPullStatus(`Ошибка: ${r.status}`);
        setPulling(false);
        return;
      }
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n");
        buf = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const j = JSON.parse(line);
            if (j.error) setPullStatus(`Ошибка: ${j.error}`);
            else setPullStatus(j.status || "…");
          } catch {
            /* ignore partial */
          }
        }
      }
      setPullStatus("Готово");
      setPullName("");
      loadLocal();
    } catch (e) {
      setPullStatus(`Ошибка: ${e}`);
    }
    setPulling(false);
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
            {p === "ollama" && (
              <div className="px-4 py-3 border-b border-slate-800 flex items-center gap-2">
                <input
                  className={`${input} flex-1`}
                  placeholder="Загрузить из реестра Ollama (напр. qwen3:8b)"
                  value={pullName}
                  onChange={(e) => setPullName(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && doPullOllama()}
                  disabled={pulling}
                />
                <button
                  className={btnPrimary}
                  onClick={doPullOllama}
                  disabled={pulling || !pullName.trim()}
                >
                  {pulling ? "Загрузка…" : "Pull"}
                </button>
                {pullStatus && (
                  <span className="text-xs text-slate-400 truncate max-w-[12rem]">
                    {pullStatus}
                  </span>
                )}
              </div>
            )}
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
                    <div className="flex items-center gap-2 shrink-0">
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
                      {p === "ollama" && (
                        <button
                          onClick={() => doDeleteOllama(name)}
                          className={btnDanger}
                          title="Удалить модель из Ollama"
                        >
                          Удалить
                        </button>
                      )}
                    </div>
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
                  number | undefined;
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

// ── Agent Tab ─────────────────────────────────────────────────────────────────

const PROVIDER_DISPLAY: Record<string, string> = {
  ollama: "Ollama",
  llamacpp: "llama.cpp",
  vllm: "vLLM",
  openai_compatible: "OpenAI-совм.",
  lmstudio: "LM Studio",
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
const providerLabel = (p: string) => PROVIDER_DISPLAY[p] ?? p;

// Two-step cascade: pick a provider, then a model of that provider. Far clearer
// than one flat list mixing every provider's models. `value` is a catalog key.
// Human-readable name of a missing capability, for the warning text.
const MODALITY_LABEL: Record<string, string> = {
  tool_calling: "вызов инструментов",
  vision: "распознавание изображений",
  text: "текст",
  embedding: "эмбеддинги",
  rerank: "переранжирование",
};

function ProviderModelSelect({
  value,
  options,
  onChange,
  placeholder,
  allowEmpty,
  statuses,
  requiredModality,
}: {
  value: string;
  options: CatalogEntry[];
  onChange: (key: string) => void;
  placeholder?: string;
  allowEmpty?: boolean;
  statuses?: Record<string, boolean>; // provider -> running (for the ●/○ hint)
  requiredModality?: string; // capability the slot needs; mismatch → warn, not hide
}) {
  const providers = Array.from(new Set(options.map((o) => o.provider)));
  const current = options.find((o) => o.key === value);
  // Remember the chosen provider locally so it survives an empty model value.
  // Without this, an optional (allowEmpty) field whose model is cleared would
  // snap the provider back to providers[0], making it impossible to switch
  // provider before picking a model.
  const [provOverride, setProvOverride] = useState<string | null>(null);
  const selectedProvider =
    current?.provider ??
    (provOverride && providers.includes(provOverride)
      ? provOverride
      : (providers[0] ?? ""));
  const models = options.filter((o) => o.provider === selectedProvider);

  const dot = (p: string) => {
    if (!statuses || !(p in statuses)) return "";
    return statuses[p] ? " ●" : " ○";
  };

  const onProvider = (p: string) => {
    if (p === selectedProvider) return;
    setProvOverride(p);
    const first = options.find((o) => o.provider === p);
    // For required fields auto-pick the first model so the result is always
    // valid; optional fields stay empty (provider remembered via provOverride)
    // until the user chooses a model.
    onChange(allowEmpty ? "" : (first?.key ?? ""));
  };

  const unsuitable = (o: CatalogEntry) =>
    !!requiredModality && !o.modalities?.includes(requiredModality);
  const selectedUnsuitable = !!current && unsuitable(current);
  const reqLabel =
    (requiredModality &&
      (MODALITY_LABEL[requiredModality] ?? requiredModality)) ||
    "";

  return (
    <div className="flex flex-col gap-1 min-w-0">
      <div className="flex gap-2 min-w-0">
        <select
          className={`${selectBase} w-32 shrink-0`}
          value={selectedProvider}
          onChange={(e) => onProvider(e.target.value)}
        >
          {providers.length === 0 && <option value="">— нет —</option>}
          {providers.map((p) => (
            <option key={p} value={p}>
              {providerLabel(p)}
              {dot(p)}
            </option>
          ))}
        </select>
        <select
          className={`${selectBase} flex-1 min-w-0`}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        >
          {(allowEmpty || !value) && (
            <option value="">{placeholder ?? "— модель —"}</option>
          )}
          {models.map((c) => (
            <option key={c.key} value={c.key}>
              {unsuitable(c) ? "⚠ " : ""}
              {c.provider_model}
              {c.vram_gb_estimate ? ` · ${c.vram_gb_estimate} GB` : ""}
            </option>
          ))}
        </select>
      </div>
      {selectedUnsuitable && (
        <span className="text-xs text-amber-400">
          ⚠ Модель не заявляет «{reqLabel}» — может работать хуже для этой роли.
          Выбор разрешён.
        </span>
      )}
    </div>
  );
}

interface SlotItem {
  slot: string;
  group: string;
  label: string;
  hint: string;
  model: string | null;
  current_model?: string | null;
  local_only: boolean; // EFFECTIVE policy (base minus admin cloud opt-in)
  cloud_optionable?: boolean; // confidential slot that can be opened to cloud
  cloud_allowed?: boolean; // admin opted this slot into cloud models
  required_modality?: string | null; // capability the slot needs (backend = source)
  thinking_capable?: boolean; // slot supports a per-assignment reasoning toggle
  thinking_enabled?: boolean | null; // current override (null = model default)
  thinking_supported_by_slot?: boolean;
  thinking_supported_by_model?: boolean;
  thinking_model_default?: boolean | null;
  thinking_override?: boolean | null;
  thinking_effective?: boolean | null;
  thinking_source?: "slot" | "model" | "unsupported";
  thinking_disable_supported?: boolean;
  thinking_warning?: string | null;
}
interface AssignmentIssue {
  slot: string;
  model: string | null;
  code: string;
  message: string;
  severity: "warning" | "error";
}
interface AssignmentDiffItem {
  slot: string;
  old_model: string | null;
  new_model: string | null;
  affected: string[];
}
interface ProvModel extends CatalogEntry {
  thinking_supported: boolean;
  thinking_enabled: boolean;
  loaded?: boolean;
  node?: string | null;
}

// Required modality per slot is provided by the backend (`required_modality`)
// — single source of truth, no frontend copy to drift.
const GROUP_ICON: Record<string, string> = {
  Документы: "📄",
  Агент: "🤖",
  Поиск: "🔎",
  Оцифровка: "📐",
};

function AssignmentTab() {
  const [slots, setSlots] = useState<SlotItem[]>([]);
  const [draft, setDraft] = useState<Record<string, string | null>>({});
  const [models, setModels] = useState<ProvModel[]>([]);
  const [running, setRunning] = useState<Record<string, boolean>>({});
  const [loading, setLoading] = useState(true);
  const [msg, setMsg] = useState<string | null>(null);
  const [selProv, setSelProv] = useState<string>("");
  const [diff, setDiff] = useState<AssignmentDiffItem[]>([]);
  const [warnings, setWarnings] = useState<AssignmentIssue[]>([]);
  const [errors, setErrors] = useState<AssignmentIssue[]>([]);
  const [lastRevision, setLastRevision] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState<"validate" | "apply" | "rollback" | null>(
    null,
  );

  const flash = (m: string) => {
    setMsg(m);
    window.setTimeout(() => setMsg(null), 2200);
  };

  const load = useCallback(async () => {
    try {
      const [sl, md, st] = await Promise.all([
        fetch(`${API}/api/providers/assignment-draft`, {
          credentials: "include",
        }),
        fetch(`${API}/api/providers/live-models`, { credentials: "include" }),
        fetch(`${API}/api/local-models/status`, { credentials: "include" }),
      ]);
      if (sl.ok) {
        const d = await sl.json();
        const nextSlots: SlotItem[] = d.slots || [];
        setSlots(nextSlots);
        setDraft(
          Object.fromEntries(nextSlots.map((s) => [s.slot, s.model ?? null])),
        );
        setDiff(d.diff || []);
        setWarnings(d.warnings || []);
        setErrors(d.errors || []);
        setDirty(false);
      }
      if (md.ok) {
        const m: ProvModel[] = await md.json();
        setModels(m);
        setSelProv((cur) => cur || m[0]?.provider || "");
      }
      if (st.ok) {
        const s = await st.json();
        const p = s.providers || {};
        setRunning({
          ollama: !!p.ollama?.running,
          llamacpp: !!p.llamacpp?.running,
          vllm: !!p.vllm?.running,
        });
      }
    } catch {
      /* ignore */
    }
    setLoading(false);
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  const isLocal = (c: ProvModel) => LOCAL_PROVIDERS.includes(c.provider);
  // A physically loaded model is always selectable, even if the catalog marks it
  // disabled (the catalog "disabled" only declutters models that aren't present).
  const selectable = (c: ProvModel) => c.loaded || c.status !== "disabled";
  // Show EVERY loaded model for the slot — never hide by capability. Models that
  // lack the slot's required modality are still selectable but flagged with a
  // warning (see ProviderModelSelect). local_only stays a hard rule: confidential
  // slots must not offer cloud models.
  const optsFor = (slot: SlotItem): CatalogEntry[] => {
    return models.filter(
      (c) => selectable(c) && (!slot.local_only || isLocal(c)),
    );
  };

  const setDraftModel = (slot: string, model: string) => {
    setDraft((prev) => ({ ...prev, [slot]: model || null }));
    setDirty(true);
    setDiff([]);
    setWarnings([]);
    setErrors([]);
  };

  const validateDraft = async () => {
    setBusy("validate");
    try {
      const r = await fetch(`${API}/api/providers/assignment-draft/validate`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        credentials: "include",
        body: JSON.stringify({ slots: draft }),
      });
      if (r.ok) {
        const d = await r.json();
        setSlots(d.slots || []);
        setDiff(d.diff || []);
        setWarnings(d.warnings || []);
        setErrors(d.errors || []);
        flash("Проверка завершена");
      } else {
        const d = await r.json().catch(() => ({}));
        alert(`Ошибка: ${d.detail || r.status}`);
      }
    } catch (e) {
      alert(`Ошибка: ${e}`);
    } finally {
      setBusy(null);
    }
  };

  const applyDraft = async (confirmWarnings = false) => {
    setBusy("apply");
    try {
      const r = await fetch(`${API}/api/providers/assignment-draft/apply`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        credentials: "include",
        body: JSON.stringify({
          slots: draft,
          confirm_warnings: confirmWarnings,
        }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.status === 409 && d.detail?.warnings && !confirmWarnings) {
        setWarnings(d.detail.warnings);
        if (
          confirm(
            "Есть предупреждения по назначению моделей. Применить всё равно?",
          )
        ) {
          await applyDraft(true);
        }
        return;
      }
      if (!r.ok) {
        alert(`Ошибка: ${JSON.stringify(d.detail || d)}`);
        return;
      }
      setLastRevision(d.revision_id || null);
      flash("Назначения применены");
      load();
    } catch (e) {
      alert(`Ошибка: ${e}`);
    } finally {
      setBusy(null);
    }
  };

  const rollback = async () => {
    if (!lastRevision) return;
    setBusy("rollback");
    try {
      const r = await fetch(
        `${API}/api/providers/assignments/${lastRevision}/rollback`,
        {
          method: "POST",
          headers: await csrfHeaders(),
          credentials: "include",
        },
      );
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        alert(`Ошибка: ${d.detail || r.status}`);
        return;
      }
      flash("Последнее изменение откачено");
      setLastRevision(null);
      load();
    } catch (e) {
      alert(`Ошибка: ${e}`);
    } finally {
      setBusy(null);
    }
  };

  const toggleThinking = async (key: string, enabled: boolean) => {
    try {
      await fetch(
        `${API}/api/providers/models/${encodeURIComponent(key)}/thinking`,
        {
          method: "PATCH",
          headers: {
            "Content-Type": "application/json",
            ...(await csrfHeaders()),
          },
          credentials: "include",
          body: JSON.stringify({ enabled }),
        },
      );
      flash(enabled ? "Рассуждение включено" : "Рассуждение выключено");
      load();
    } catch (e) {
      alert(`Ошибка: ${e}`);
    }
  };

  // Per-assignment reasoning (tri-state): null = model default, true/false force.
  const setSlotThinking = async (slot: string, enabled: boolean | null) => {
    try {
      await fetch(`${API}/api/providers/slots/${slot}/thinking`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        credentials: "include",
        body: JSON.stringify({ enabled }),
      });
      flash(
        enabled === null
          ? "Рассуждение: по умолчанию"
          : enabled
            ? "Рассуждение включено для слота"
            : "Рассуждение выключено для слота",
      );
      load();
    } catch (e) {
      alert(`Ошибка: ${e}`);
    }
  };

  // Protected setting: opt a confidential slot into cloud models.
  const setSlotCloud = async (slot: string, allowed: boolean) => {
    if (
      allowed &&
      !confirm(
        "Разрешить облачные модели для этого слота? Содержимое этой задачи " +
          "(например, счета или чертежи) сможет уходить во внешний облачный " +
          "провайдер. Это осознанное ослабление конфиденциальности.",
      )
    ) {
      return;
    }
    try {
      await fetch(`${API}/api/providers/slots/${slot}/allow-cloud`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        credentials: "include",
        body: JSON.stringify({ allowed }),
      });
      flash(
        allowed
          ? "Облако разрешено для слота"
          : "Слот снова только для локальных моделей",
      );
      load();
    } catch (e) {
      alert(`Ошибка: ${e}`);
    }
  };

  const delModel = async (m: ProvModel) => {
    if (m.provider !== "ollama") {
      alert(
        "Удаление поддерживается только для Ollama. Для llama.cpp/vLLM — во вкладке «Библиотека».",
      );
      return;
    }
    if (!confirm(`Удалить модель ${m.provider_model} из Ollama?`)) return;
    try {
      const r = await fetch(
        `${API}/api/local-models/ollama/models/${encodeURIComponent(m.provider_model)}`,
        {
          method: "DELETE",
          headers: await csrfHeaders(),
          credentials: "include",
        },
      );
      if (r.ok) {
        flash("Модель удалена");
        load();
      } else alert("Не удалось удалить");
    } catch (e) {
      alert(`Ошибка: ${e}`);
    }
  };
  const modelByKey = (key: string | null) =>
    key ? models.find((m) => m.key === key) : undefined;
  const providerCanDisableThinking = (provider?: string) =>
    !provider || THINKING_DISABLE_SUPPORTED_PROVIDERS.includes(provider);
  const thinkingText = (
    effective: boolean | null | undefined,
    source: string | undefined,
  ) => {
    if (effective === null || effective === undefined)
      return "не поддерживается";
    const state = effective ? "вкл" : "выкл";
    const src = source === "slot" ? "слот" : "модель";
    return `${state} · ${src}`;
  };

  if (loading) return <div className="text-sm text-slate-500">Загрузка…</div>;

  const groups = ["Документы", "Агент", "Поиск", "Оцифровка"];
  const provList = Array.from(new Set(models.map((m) => m.provider)));
  const provModels = models.filter(
    (m) => m.provider === selProv && selectable(m),
  );

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <p className="text-xs text-slate-600">
          <span className="text-emerald-400">●</span> запущен ·{" "}
          <span className="text-slate-500">○</span> остановлен — vLLM и
          llama.cpp стартуют по требованию. Изменения сначала попадают в
          черновик.
        </p>
        <div className="flex items-center gap-2">
          {msg && <span className="text-xs text-emerald-400">{msg}</span>}
          {lastRevision && (
            <button
              className={`${btnSecondary} text-xs`}
              disabled={busy === "rollback"}
              onClick={rollback}
            >
              Откатить последнее
            </button>
          )}
          <button
            className={`${btnSecondary} text-xs`}
            disabled={!dirty || busy !== null}
            onClick={() => {
              setDraft(
                Object.fromEntries(
                  slots.map((s) => [
                    s.slot,
                    s.current_model ?? s.model ?? null,
                  ]),
                ),
              );
              setDiff([]);
              setWarnings([]);
              setErrors([]);
              setDirty(false);
            }}
          >
            Сбросить
          </button>
          <button
            className={`${btnSecondary} text-xs`}
            disabled={!dirty || busy !== null}
            onClick={validateDraft}
          >
            {busy === "validate" ? "Проверка…" : "Проверить"}
          </button>
          <button
            className={`${btnPrimary} text-xs`}
            disabled={!dirty || busy !== null}
            onClick={() => applyDraft(false)}
          >
            {busy === "apply" ? "Применение…" : "Применить"}
          </button>
        </div>
      </div>

      {(diff.length > 0 || warnings.length > 0 || errors.length > 0) && (
        <div className={card}>
          <div className={cardH}>
            <span className="text-sm font-semibold text-slate-100">
              Проверка черновика
            </span>
            <span className="text-xs text-slate-500">
              {diff.length} изменений · {warnings.length} предупреждений ·{" "}
              {errors.length} ошибок
            </span>
          </div>
          <div className="p-3 space-y-2 text-xs">
            {errors.map((e, i) => (
              <div key={`e-${i}`} className="text-red-300">
                {e.slot}: {e.message}
              </div>
            ))}
            {warnings.map((w, i) => (
              <div key={`w-${i}`} className="text-amber-300">
                {w.slot}: {w.message}
              </div>
            ))}
            {diff.map((d) => (
              <div key={d.slot} className="text-slate-400">
                <span className="text-slate-200">{d.slot}</span>:{" "}
                {d.old_model ?? "—"} → {d.new_model ?? "—"} ·{" "}
                {d.affected.join(", ")}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Slots */}
      {groups.map((g) => {
        const gslots = slots.filter((s) => s.group === g);
        if (!gslots.length) return null;
        return (
          <div key={g} className={card}>
            <div className={cardH}>
              <span className="text-sm font-semibold text-slate-100">
                {GROUP_ICON[g]} {g}
              </span>
              {g === "Документы" && (
                <span className="text-xs text-slate-500">
                  🔒 конфиденциально — только локальные модели
                </span>
              )}
              {g === "Агент" && (
                <span className="text-xs text-slate-500">
                  можно облачные модели (на ваш выбор)
                </span>
              )}
              {g === "Оцифровка" && (
                <span className="text-xs text-slate-500">
                  🔒 конфиденциально — только локальные модели · метод «по
                  описанию»
                </span>
              )}
            </div>
            <div className="p-4 space-y-3">
              {gslots.map((s) => {
                const chosen = modelByKey(s.current_model ?? s.model);
                const draftValue = draft[s.slot] ?? "";
                const draftChosen = modelByKey(draftValue);
                const slotSupportsThinking =
                  s.thinking_supported_by_slot ?? s.thinking_capable ?? false;
                const selectedSupportsThinking =
                  !!draftChosen?.thinking_supported ||
                  (!draftChosen && !!s.thinking_supported_by_model);
                const thinkingOverride =
                  s.thinking_override ?? s.thinking_enabled ?? null;
                const selectedModelDefault =
                  draftChosen?.thinking_enabled ??
                  s.thinking_model_default ??
                  false;
                const selectedThinkingCapable =
                  slotSupportsThinking && selectedSupportsThinking;
                const effectiveThinking = selectedThinkingCapable
                  ? (thinkingOverride ?? selectedModelDefault)
                  : null;
                const thinkingSource = !selectedThinkingCapable
                  ? "unsupported"
                  : thinkingOverride === null
                    ? "model"
                    : "slot";
                const disableSupported = providerCanDisableThinking(
                  draftChosen?.provider,
                );
                const thinkingWarning =
                  selectedThinkingCapable &&
                  effectiveThinking === false &&
                  !disableSupported
                    ? "API этого провайдера может игнорировать выключение reasoning"
                    : s.thinking_warning;
                return (
                  <div
                    key={s.slot}
                    className="grid grid-cols-1 sm:grid-cols-[200px_1fr] gap-2 sm:items-start"
                  >
                    <div className="min-w-0">
                      <div className="text-sm text-slate-200">{s.label}</div>
                      <div className="text-xs text-slate-500">{s.hint}</div>
                    </div>
                    <div className="flex items-center gap-3 min-w-0">
                      <div className="flex-1 min-w-0">
                        <ProviderModelSelect
                          value={draftValue}
                          options={optsFor(s)}
                          statuses={running}
                          requiredModality={s.required_modality ?? undefined}
                          onChange={(v) => setDraftModel(s.slot, v)}
                        />
                      </div>
                      {draftChosen?.key !== chosen?.key && (
                        <span className="text-xs text-amber-400 whitespace-nowrap">
                          черновик
                        </span>
                      )}
                      {slotSupportsThinking && (
                        <div className="flex flex-col items-end gap-1 text-xs text-slate-300">
                          {selectedThinkingCapable ? (
                            <label
                              className="flex items-center gap-1.5 whitespace-nowrap"
                              title="Override режима рассуждения для этого назначения. Одна модель может думать в одном слоте и не думать в другом."
                            >
                              слот
                              <select
                                className="rounded border border-slate-600 bg-slate-900 px-1 py-0.5 text-xs text-slate-200"
                                value={
                                  thinkingOverride === true
                                    ? "on"
                                    : thinkingOverride === false
                                      ? "off"
                                      : "default"
                                }
                                onChange={(e) =>
                                  setSlotThinking(
                                    s.slot,
                                    e.target.value === "on"
                                      ? true
                                      : e.target.value === "off"
                                        ? false
                                        : null,
                                  )
                                }
                              >
                                <option value="default">модель</option>
                                <option value="on">вкл</option>
                                <option value="off">выкл</option>
                              </select>
                            </label>
                          ) : (
                            <span className="text-slate-600 whitespace-nowrap">
                              reasoning недоступен
                            </span>
                          )}
                          <span className="text-slate-500 whitespace-nowrap">
                            эффективно:{" "}
                            {thinkingText(effectiveThinking, thinkingSource)}
                          </span>
                          {thinkingWarning && (
                            <span className="max-w-52 text-right text-amber-400">
                              {thinkingWarning}
                            </span>
                          )}
                        </div>
                      )}
                    </div>
                    {s.cloud_optionable && (
                      <label className="sm:col-span-2 mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-400">
                        <input
                          type="checkbox"
                          checked={!!s.cloud_allowed}
                          onChange={(e) =>
                            setSlotCloud(s.slot, e.target.checked)
                          }
                        />
                        <span
                          className={s.cloud_allowed ? "text-amber-300" : ""}
                        >
                          разрешить облачные модели для этого слота
                        </span>
                        <span className="text-slate-500">
                          — по умолчанию только локально (конфиденциально)
                        </span>
                      </label>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}

      {/* Per-provider models + thinking toggle */}
      <div className={card}>
        <div className={cardH}>
          <span className="text-sm font-semibold text-slate-100">
            Модели провайдеров
          </span>
          <span className="text-xs text-slate-500">
            все загруженные модели · скачать новые — во вкладке «Библиотека»
          </span>
        </div>
        <div className="flex flex-col sm:flex-row">
          {/* provider list */}
          <div className="sm:w-44 border-b sm:border-b-0 sm:border-r border-slate-700 p-2 space-y-1">
            {provList.map((p) => (
              <button
                key={p}
                onClick={() => setSelProv(p)}
                className={`w-full text-left px-2 py-1.5 rounded text-sm flex items-center gap-2 ${
                  p === selProv
                    ? "bg-blue-600/20 text-blue-200"
                    : "text-slate-300 hover:bg-slate-800"
                }`}
              >
                {p in running && (
                  <span
                    className={
                      running[p] ? "text-emerald-400" : "text-slate-600"
                    }
                  >
                    {running[p] ? "●" : "○"}
                  </span>
                )}
                <span className="flex-1 truncate">{providerLabel(p)}</span>
              </button>
            ))}
          </div>
          {/* models of selected provider */}
          <div className="flex-1 p-2">
            {provModels.length === 0 && (
              <div className="text-xs text-slate-500 px-2 py-3">
                Нет загруженных моделей. Добавьте их во вкладке «Библиотека» или
                подтяните облачные во вкладке «Провайдеры».
              </div>
            )}
            <table className="w-full text-sm">
              <tbody>
                {provModels.map((m) => (
                  <tr key={m.key} className="border-b border-slate-800">
                    <td className="py-1.5 pr-2">
                      <div className="text-slate-200">{m.provider_model}</div>
                      <div className="text-xs text-slate-600">
                        {m.modalities.join(", ")}
                        {m.vram_gb_estimate
                          ? ` · ${m.vram_gb_estimate} GB`
                          : ""}
                        {m.node ? ` · ${m.node}` : ""}
                      </div>
                    </td>
                    <td className="py-1.5 px-2 whitespace-nowrap">
                      <span
                        className={`text-xs px-2 py-0.5 rounded ${
                          m.loaded
                            ? "bg-emerald-700/40 text-emerald-300"
                            : "bg-slate-700/40 text-slate-400"
                        }`}
                      >
                        {m.loaded ? "загружена" : m.status}
                      </span>
                    </td>
                    <td className="py-1.5 px-2 whitespace-nowrap">
                      {m.thinking_supported ? (
                        <label className="inline-flex items-center gap-2 text-xs text-slate-300 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={m.thinking_enabled}
                            onChange={(e) =>
                              toggleThinking(m.key, e.target.checked)
                            }
                          />
                          По умолчанию модели
                        </label>
                      ) : (
                        <span className="text-xs text-slate-600">без CoT</span>
                      )}
                    </td>
                    <td className="py-1.5 pl-2 text-right whitespace-nowrap">
                      {m.provider === "ollama" && m.loaded && (
                        <button
                          className="text-xs text-red-400 hover:text-red-300"
                          onClick={() => delModel(m)}
                          title="Удалить модель из Ollama"
                        >
                          удалить
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}

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
    { id: "overview", label: "Провайдеры" },
    { id: "assignment", label: "Назначение" },
    { id: "library", label: "Библиотека" },
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
          {tab === "assignment" && <AssignmentTab />}
          {tab === "library" && <LibraryTab />}
          {tab === "parameters" && <ParametersTab />}
          {tab === "gpu" && <GPUTab status={status} />}
        </div>
      </div>
    </div>
  );
}
