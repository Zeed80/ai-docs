"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { ProviderModelPicker } from "@/components/models/picker/ProviderModelPicker";
import { providerBarColor, providerLabel } from "@/lib/models/labels";
import { detailText } from "@/lib/models/format";
import { RoutingChains } from "@/components/models/telemetry/RoutingChains";
import { useToast } from "@/components/ui/primitives/Toast";
import { SlotThinkingControl } from "@/components/models/assignment/SlotThinkingControl";
import { RevisionHistory } from "@/components/models/assignment/RevisionHistory";
import { DiffPanel } from "@/components/models/assignment/DiffPanel";
import { GpuPanel } from "@/components/models/infra/GpuPanel";
import { ParametersPanel } from "@/components/models/catalog/ParametersPanel";
import { LibraryPanel } from "@/components/models/catalog/LibraryPanel";
import { CloudConnectSheet } from "@/components/models/infra/CloudConnectSheet";
import {
  SlotHealthStrip,
  type SlotHealth,
} from "@/components/models/assignment/SlotHealthStrip";
import { useCurrentUser } from "@/lib/auth-context";
import { hasRole } from "@/lib/rbac";
import type {
  CatalogModel,
  Modality,
  ThinkingLevel,
} from "@/lib/models/types";

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
  // Приходят из /live-models и /models; до этого не запрашивались и не
  // показывались, хотя лежат в каталоге моделей с самого начала.
  max_context_tokens?: number | null;
  supports_tool_calling?: boolean;
  supports_structured_output?: boolean;
  cost_per_1k_input?: number | null;
  cost_per_1k_output?: number | null;
  notes?: string | null;
  availability?: "available" | "missing" | "unknown";
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
// Russian labels for the canonical reasoning-effort levels. Keyed loosely
// (Record<string,...>) since level lists come from the backend catalog, not
// a frontend enum.
const THINKING_LEVEL_LABEL: Record<string, string> = {
  low: "низкая",
  medium: "средняя",
  high: "высокая",
};

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


const PROFILE_LABELS: Record<string, string> = {
  anti_hallucination: "Без галлюцинаций",
  structured_reasoning: "Структ. рассуждение",
  balanced: "Баланс",
  creative: "Творческий",
};

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
              className={`${providerBarColor(p)} transition-all`}
              style={{ width: `${w}%` }}
              title={`${providerLabel(p)}: ${a.vram_used_gb.toFixed(1)} GB`}
            />
          ) : null;
        })}
      </div>
      <div className="flex gap-3 flex-wrap text-xs text-slate-400">
        {Object.entries(allocations).map(([p, a]) => (
          <span key={p} className="flex items-center gap-1">
            <span
              className={`w-2 h-2 rounded-full ${providerBarColor(p)}`}
            />
            {providerLabel(p)}: {a.vram_used_gb.toFixed(1)}{" "}
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
            {providerLabel(name)}
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
  const toast = useToast();
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
      if (!r.ok) toast.error("Не удалось выполнить команду", detailText(data));
    } catch (e) {
      toast.error("Не удалось выполнить действие", String(e));
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
          title={`${a} ${providerLabel(provider)} container`}
        >
          {busy === a ? "..." : a === "start" ? "▶" : a === "stop" ? "■" : "↻"}
        </button>
      ))}
    </div>
  );
}

// ── Tokens & server config ─────────────────────────────────────────────────────

function TokensPanel() {
  const toast = useToast();
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
    } catch (e) {
      toast.error("Не удалось обновить состояние серверов", String(e));
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
  const toast = useToast();
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
      } else toast.error("Настройки не сохранены", detailText(d));
    } catch (e) {
      toast.error("Не удалось выполнить действие", String(e));
    }
    setSaving(false);
  };

  if (!config) return null;

  return (
    <div className={card}>
      <div className={cardH}>
        <span className="text-sm font-medium text-slate-100">
          {providerLabel(provider)} — настройки сервера
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
  const toast = useToast();
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
        toast.ok(
          `Применено: ${d.applied?.join(", ") || "—"}`,
          d.skipped?.length ? `пропущено: ${d.skipped.join(", ")}` : undefined,
        );
      else toast.error("Пресет не применён", detailText(d));
    } catch (e) {
      toast.error("Не удалось выполнить действие", String(e));
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
  // "unset" | "set" | "corrupt" — испорченный ключ (например после смены
  // app_secret_key) раньше выглядел как отсутствующий.
  api_key_state?: "unset" | "set" | "corrupt";
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
  const toast = useToast();
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
    } catch (e) {
      toast.error("Список провайдеров не загрузился", String(e));
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
                      c.api_key_state === "corrupt"
                        ? "text-amber-400"
                        : c.api_key_set
                          ? "text-emerald-400"
                          : "text-slate-600"
                    }`}
                  >
                    {c.api_key_state === "corrupt"
                      ? "ключ повреждён"
                      : c.api_key_set
                        ? "ключ ✓"
                        : "нет"}
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
  const [connectOpen, setConnectOpen] = useState(false);
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
          {inst.api_key_state === "corrupt"
            ? "ключ сохранён, но не расшифровывается — введите его заново (обычно после смены APP_SECRET_KEY)"
            : inst.api_key_set
              ? `ключ: ${inst.api_key_mask}`
              : "ключ не задан"}
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
        {/* Отдельная кнопка для связного потока: ключ → проверка → загрузка.
            По отдельности эти три шага и раньше были на экране, но человеку
            приходилось догадываться о порядке и о том, что после ключа нужно
            ещё и подтянуть каталог. */}
        <button className={btnSecondary} onClick={() => setConnectOpen(true)}>
          Подключить пошагово
        </button>
      </div>
      <CloudConnectSheet
        open={connectOpen}
        onClose={() => setConnectOpen(false)}
        instanceId={inst.id}
        kind={inst.kind}
        hasKey={Boolean(inst.api_key_set)}
        onConnected={onChanged}
      />
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
  const toast = useToast();
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
      toast.error("Узел не добавлен", detailText(d));
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



// ── GPU Budget Tab ────────────────────────────────────────────────────────────


// ── Main Page ─────────────────────────────────────────────────────────────────

// ── Agent Tab ─────────────────────────────────────────────────────────────────


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
  // За одним переключателем может стоять несколько ролей; если их
  // значения разошлись, показывается состояние первой.
  thinking_mixed?: boolean;
  thinking_warning?: string | null;
  thinking_levels?: string[]; // reasoning-effort levels the SELECTED model supports (empty = none)
  thinking_level_override?: string | null; // this slot's explicit level override
  thinking_level_effective?: string | null; // resolved level actually in effect
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
  thinking_levels?: string[]; // reasoning-effort levels this model accepts (empty = on/off only)
  thinking_level_default?: string | null;
  loaded?: boolean;
  node?: string | null;
  // Pinned node for this model (null = router picks). Rendered as a selector
  // when the provider kind has more than one node — e.g. Ollama GPU vs CPU.
  preferred_instance?: string | null;
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
  const toast = useToast();
  const [slots, setSlots] = useState<SlotItem[]>([]);
  const [health, setHealth] = useState<Record<string, SlotHealth>>({});
  const [draft, setDraft] = useState<Record<string, string | null>>({});
  // Разрешение облака по слотам — часть черновика, а не отдельное
  // немедленное действие: применяется вместе с моделью.
  const [draftCloud, setDraftCloud] = useState<Record<string, boolean>>({});
  const [models, setModels] = useState<ProvModel[]>([]);
  // Nodes of each local provider kind — a kind can have several (e.g. the GPU
  // Ollama and the CPU-only one), and a model can be pinned to one of them.
  const [nodes, setNodes] = useState<ProviderInstanceT[]>([]);
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
      const [sl, md, st, nd] = await Promise.all([
        fetch(`${API}/api/providers/assignment-draft`, {
          credentials: "include",
        }),
        fetch(`${API}/api/providers/live-models`, { credentials: "include" }),
        fetch(`${API}/api/local-models/status`, { credentials: "include" }),
        fetch(`${API}/api/providers`, { credentials: "include" }),
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
      // Здоровье слотов — отдельным запросом и молча: это справочная строка,
      // из-за которой экран не должен падать.
      try {
        const hr = await fetch(`${API}/api/providers/slots/health`, {
          credentials: "include",
        });
        if (hr.ok) {
          const list: SlotHealth[] = await hr.json();
          setHealth(Object.fromEntries(list.map((h) => [h.slot, h])));
        }
      } catch {
        /* строка здоровья не обязательна */
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
      if (nd.ok) {
        const d = await nd.json();
        setNodes(d.instances || []);
      }
    } catch (e) {
      toast.error("Узлы провайдеров не загрузились", String(e));
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

  // Показываем модели ВСЕХ провайдеров, включая облачных: провайдер теперь
  // выбирается явно первым шагом, и этот выбор сам означает решение об облаке.
  // Прежний фильтр по local_only прятал облачные модели у конфиденциальных
  // слотов — и человек не понимал, почему их нет в списке, пока не находил
  // отдельную галочку «разрешить облако» под карточкой.
  const allModelsFor = (_slot: SlotItem): CatalogEntry[] =>
    models.filter(selectable);

  // Решение об облаке едет вместе с моделью в черновике: применится вместе с
  // ней, одним подтверждением, и попадёт в ту же ревизию.
  const setDraftModel = (slot: string, model: string, cloud?: boolean) => {
    setDraft((prev) => ({ ...prev, [slot]: model || null }));
    if (cloud !== undefined) {
      setDraftCloud((prev) => ({ ...prev, [slot]: cloud }));
    }
    setDirty(true);
    setDiff([]);
    setWarnings([]);
    setErrors([]);
  };

  // Черновик в форме, которую ждёт сервер: слот теперь несёт не только модель,
  // но и решение об облаке — раньше оно применялось отдельным немедленным
  // запросом, из-за чего в одной карточке было два разных поведения.
  const draftPayload = () =>
    Object.fromEntries(
      Object.entries(draft).map(([slot, model]) => [
        slot,
        draftCloud[slot] === undefined
          ? { model }
          : { model, allow_cloud: draftCloud[slot] },
      ]),
    );

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
        body: JSON.stringify({ slots: draftPayload() }),
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
        toast.error("Проверка не прошла", String(d.detail || r.status));
      }
    } catch (e) {
      toast.error("Не удалось выполнить действие", String(e));
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
          slots: draftPayload(),
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
        toast.error("Назначения не применены", detailText(d));
        return;
      }
      setLastRevision(d.revision_id || null);
      flash("Назначения применены");
      load();
    } catch (e) {
      toast.error("Не удалось выполнить действие", String(e));
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
        toast.error("Проверка не прошла", String(d.detail || r.status));
        return;
      }
      flash("Последнее изменение откачено");
      setLastRevision(null);
      load();
    } catch (e) {
      toast.error("Не удалось выполнить действие", String(e));
    } finally {
      setBusy(null);
    }
  };

  const toggleThinking = async (
    key: string,
    enabled: boolean,
    level?: string | null,
  ) => {
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
          body: JSON.stringify({ enabled, level: level ?? null }),
        },
      );
      flash(enabled ? "Рассуждение включено" : "Рассуждение выключено");
      load();
    } catch (e) {
      toast.error("Не удалось выполнить действие", String(e));
    }
  };

  // Enabled nodes of one local provider kind, in listing order.
  const localNodes = (kind: string) =>
    nodes.filter((n) => n.is_local && n.kind === kind && n.enabled);

  // Pin a model to a specific node of its provider kind (e.g. the GPU Ollama
  // vs the CPU-only one). Empty string clears the pin — the router then picks
  // whichever enabled node actually hosts the model.
  const setModelNode = async (key: string, instanceName: string) => {
    try {
      await fetch(
        `${API}/api/providers/models/${encodeURIComponent(key)}/preferred-instance`,
        {
          method: "PATCH",
          headers: {
            "Content-Type": "application/json",
            ...(await csrfHeaders()),
          },
          credentials: "include",
          body: JSON.stringify({ instance_name: instanceName || null }),
        },
      );
      flash(
        instanceName ? `Модель закреплена за «${instanceName}»` : "Узел выбирается автоматически",
      );
      load();
    } catch (e) {
      toast.error("Не удалось выполнить действие", String(e));
    }
  };

  // Reasoning-effort level for a model's default toggle (low/medium/high) —
  // only rendered when the model declares thinking_levels. Reuses the same
  // PATCH endpoint as toggleThinking, keeping enabled=true implicit.
  const setModelThinkingLevel = async (key: string, level: string) => {
    await toggleThinking(key, true, level);
  };

  // Per-assignment reasoning (tri-state): null = model default, true/false force.
  const setSlotThinking = async (
    slot: string,
    enabled: boolean | null,
    level?: string | null,
  ) => {
    try {
      await fetch(`${API}/api/providers/slots/${slot}/thinking`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        credentials: "include",
        body: JSON.stringify({ enabled, level: level ?? null }),
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
      toast.error("Не удалось выполнить действие", String(e));
    }
  };

  // Protected setting: opt a confidential slot into cloud models.
  const delModel = async (m: ProvModel) => {
    if (m.provider !== "ollama") {
      toast.error(
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
      } else toast.error("Модель не удалена");
    } catch (e) {
      toast.error("Не удалось выполнить действие", String(e));
    }
  };
  const modelByKey = (key: string | null) =>
    key ? models.find((m) => m.key === key) : undefined;
  const providerCanDisableThinking = (provider?: string) =>
    !provider || THINKING_DISABLE_SUPPORTED_PROVIDERS.includes(provider);
  if (loading) return <div className="text-sm text-slate-500">Загрузка…</div>;

  // Порядок групп берём из ответа сервера, а не из литерала: слот новой
  // группы (или переименованной) просто не отрисовывался, без единого следа.
  // Порядок в _SLOTS на бэкенде уже осмысленный, сохранение порядка появления
  // даёт тот же результат и не теряет слоты.
  const groups = Array.from(new Set(slots.map((s) => s.group)));
  const provList = Array.from(new Set(models.map((m) => m.provider)));
  const provModels = models.filter(
    (m) => m.provider === selProv && selectable(m),
  );

  return (
    <div className="space-y-6">
      <RoutingChains />
      <RevisionHistory onRolledBack={load} />
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
            {/* Плоская строка «слот: старое → новое» не отвечала на главный
                вопрос — заработает ли это. Панель прогоняет тот же пробный
                вызов, что и проверка после применения, но в режиме dry_run:
                он резолвит каталог, политику, узел и параметры рассуждения,
                ничего не отправляя провайдеру. */}
            <DiffPanel
              diff={diff.map((d) => ({
                ...d,
                label: slots.find((s) => s.slot === d.slot)?.label,
              }))}
              draftFor={(slot) => ({
                model: draft[slot] ?? undefined,
                thinking: slots.find((s) => s.slot === slot)?.thinking_override,
                thinking_level: slots.find((s) => s.slot === slot)
                  ?.thinking_level_override,
              })}
            />
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
              {gslots.every((s) => s.local_only) && (
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
                const disableSupported = providerCanDisableThinking(
                  draftChosen?.provider,
                );
                const thinkingWarning =
                  selectedThinkingCapable &&
                  effectiveThinking === false &&
                  !disableSupported
                    ? "API этого провайдера может игнорировать выключение reasoning"
                    : s.thinking_warning;
                // Reasoning-effort level (low/medium/high) — only relevant
                // when reasoning is actually ON for the currently selected
                // model and that model declares levels at all.
                const selectedThinkingLevels = draftChosen
                  ? (draftChosen.thinking_levels ?? [])
                  : (s.thinking_levels ?? []);
                const thinkingLevelOverride = s.thinking_level_override ?? null;
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
                        {/* Два шага: провайдер, затем его модель. Единый
                            список всех моделей выглядел короче, но прятал
                            главное решение — локально или в облако. Оно
                            принималось отдельной галочкой сбоку, которую надо
                            было заметить и связать с выбором. Теперь выбор
                            облачного провайдера и есть это решение. */}
                        <ProviderModelPicker
                          models={allModelsFor(s) as unknown as CatalogModel[]}
                          value={draftValue || null}
                          confidential={Boolean(s.cloud_optionable)}
                          requiredModality={
                            (s.required_modality as Modality | null) ?? null
                          }
                          onChange={(v, opts) =>
                            setDraftModel(s.slot, v, opts.cloud)
                          }
                        />
                      </div>
                      {draftChosen?.key !== chosen?.key && (
                        <span className="text-xs text-amber-400 whitespace-nowrap">
                          черновик
                        </span>
                      )}
                      <SlotThinkingControl
                        state={{
                          supportedBySlot: slotSupportsThinking,
                          supportedByModel: selectedSupportsThinking,
                          modelDefault: selectedModelDefault,
                          override: thinkingOverride,
                          effective: effectiveThinking,
                          disableSupported: s.thinking_disable_supported ?? true,
                          mixed: Boolean(s.thinking_mixed),
                          warning: thinkingWarning ?? null,
                          levels: selectedThinkingLevels as ThinkingLevel[],
                          levelOverride:
                            (thinkingLevelOverride as ThinkingLevel | null) ?? null,
                          levelEffective:
                            (s.thinking_level_effective as ThinkingLevel | null) ??
                            null,
                        }}
                        onChange={(enabled) => setSlotThinking(s.slot, enabled)}
                        onLevelChange={(level) =>
                          setSlotThinking(s.slot, thinkingOverride, level)
                        }
                      />
                    </div>
                    <div className="sm:col-span-2">
                      <SlotHealthStrip health={health[s.slot] ?? null} />
                    </div>
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
                      {localNodes(m.provider).length > 1 && (
                        <select
                          className="rounded border border-slate-600 bg-slate-900 px-1 py-0.5 text-xs text-slate-200"
                          title="На каком узле выполнять эту модель (например GPU или CPU)"
                          value={m.preferred_instance ?? ""}
                          onChange={(e) => setModelNode(m.key, e.target.value)}
                        >
                          <option value="">Узел: авто</option>
                          {localNodes(m.provider).map((n) => (
                            <option key={n.id} value={n.name}>
                              {n.name}
                            </option>
                          ))}
                        </select>
                      )}
                    </td>
                    <td className="py-1.5 px-2 whitespace-nowrap">
                      {m.thinking_supported ? (
                        <div className="flex items-center gap-2">
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
                          {m.thinking_enabled &&
                            (m.thinking_levels?.length ?? 0) > 0 && (
                              <select
                                className="rounded border border-slate-600 bg-slate-900 px-1 py-0.5 text-xs text-slate-200"
                                title="Сила размышления (reasoning effort) по умолчанию для этой модели"
                                value={m.thinking_level_default ?? "medium"}
                                onChange={(e) =>
                                  setModelThinkingLevel(m.key, e.target.value)
                                }
                              >
                                {m.thinking_levels!.map((lvl) => (
                                  <option key={lvl} value={lvl}>
                                    {THINKING_LEVEL_LABEL[lvl] ?? lvl}
                                  </option>
                                ))}
                              </select>
                            )}
                        </div>
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

function ModelsPageInner() {
  const currentUser = useCurrentUser();
  const isAdmin = !!currentUser && hasRole(currentUser.roles, "admin");
  // Дефолтная вкладка — «Назначение»: это ежедневный сценарий, а открывался
  // экран на «Провайдерах», куда заходят при первичной настройке.
  const [tab, setTab] = useState<Tab>("assignment");
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
    { id: "assignment", label: "Назначение" },
    { id: "overview", label: "Провайдеры" },
    { id: "library", label: "Библиотека" },
    { id: "parameters", label: "Параметры" },
    { id: "gpu", label: "GPU Бюджет" },
  ];

  // Каждый эндпоинт раздела требует роли admin, а гарда на странице не было:
  // не-админ видел пустые списки и ошибки запросов вместо объяснения.
  if (currentUser && !isAdmin) {
    return (
      <div className="min-h-screen bg-slate-950 text-slate-100">
        <div className="mx-auto max-w-2xl px-4 py-16 text-center">
          <h1 className="text-xl font-semibold text-slate-100">
            Раздел доступен администраторам
          </h1>
          <p className="mt-2 text-sm text-slate-400">
            Здесь настраиваются узлы провайдеров, ключи доступа и то, какая
            модель отвечает за какую задачу. Если настройка нужна — попросите
            администратора.
          </p>
        </div>
      </div>
    );
  }

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
          {tab === "library" && <LibraryPanel />}
          {tab === "parameters" && <ParametersPanel />}
          {tab === "gpu" && <GpuPanel status={status} />}
        </div>
      </div>
    </div>
  );
}

export default function ModelsPage() {
  // ToastProvider живёт в корневом layout — здесь он больше не нужен.
  return <ModelsPageInner />;
}
