"use client";

/**
 * Инфраструктура моделей: узлы провайдеров, серверы, ключи облака, пресеты и
 * телеметрия.
 *
 * Вынесено из монолита экрана моделей одним блоком: эти компоненты плотно
 * связаны между собой и относятся к одному вопросу — «куда ходить и чем
 * платить», отдельному от «какая модель за что отвечает».
 */

import { useCallback, useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { useToast } from "@/components/ui/primitives/Toast";
import { CloudConnectSheet } from "@/components/models/infra/CloudConnectSheet";
import {
  btn,
  card,
  cardHeader as cardH,
  input,
  select,
} from "@/components/ui/primitives/tokens";
import { detailText } from "@/lib/models/format";
import {
  PROFILE_LABELS,
  providerBarColor,
  providerLabel,
} from "@/lib/models/labels";
import type {
  AllStatus,
  LocalProviderKind as Provider,
  ModelsTab as Tab,
  ProviderInstance as ProviderInstanceT,
  ProviderStatus,
} from "@/lib/models/types";

const API = getApiBaseUrl();
const btnPrimary = `${btn} bg-blue-600 hover:bg-blue-700 text-white disabled:opacity-50`;
const btnSecondary = `${btn} bg-slate-700 hover:bg-slate-600 text-slate-200`;
const btnDanger = `${btn} bg-red-700 hover:bg-red-600 text-white`;

interface KnownKindT {
  kind: string;
  is_local: boolean;
  default_base_url: string;
  requires_api_key: boolean;
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

export function InfraPanel({
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
