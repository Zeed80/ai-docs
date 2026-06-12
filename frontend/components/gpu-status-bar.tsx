"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { mutFetch } from "@/lib/auth";

export const GPU_BAR_STORAGE_KEY = "gpu_status_bar_enabled";
export const GPU_BAR_TOGGLE_EVENT = "gpu-statusbar-changed";
const POWER_PRESETS_STORAGE_KEY = "gpu_power_presets";

const POLL_INTERVAL_MS = 5000;
const MAX_CONSECUTIVE_FAILURES = 3;
const APPLY_DEBOUNCE_MS = 400;

interface GpuTelemetry {
  name: string | null;
  driver_version: string | null;
  utilization_pct: number | null;
  temp_gpu_c: number | null;
  temp_mem_c: number | null;
  temp_mem_junction_c: number | null;
  power_draw_w: number | null;
  power_limit_w: number | null;
  power_limit_min_w: number | null;
  power_limit_max_w: number | null;
  power_limit_default_w: number | null;
  fan_pct: number | null;
  vram_total_gb: number | null;
  vram_used_gb: number | null;
  vram_free_gb: number | null;
  clock_sm_mhz: number | null;
  clock_mem_mhz: number | null;
  source: string;
}

interface GpuTelemetryResponse {
  available: boolean;
  ts: number;
  gpu: GpuTelemetry | null;
}

interface PowerPreset {
  name: string;
  watts: number;
}

export function isGpuBarEnabled(): boolean {
  try {
    return localStorage.getItem(GPU_BAR_STORAGE_KEY) !== "0";
  } catch {
    return true;
  }
}

export function setGpuBarEnabled(enabled: boolean) {
  try {
    localStorage.setItem(GPU_BAR_STORAGE_KEY, enabled ? "1" : "0");
  } catch {}
  window.dispatchEvent(new Event(GPU_BAR_TOGGLE_EVENT));
}

function loadUserPresets(): PowerPreset[] {
  try {
    const raw = localStorage.getItem(POWER_PRESETS_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as PowerPreset[];
    return parsed.filter(
      (p) => typeof p.name === "string" && typeof p.watts === "number",
    );
  } catch {
    return [];
  }
}

function saveUserPresets(presets: PowerPreset[]) {
  try {
    localStorage.setItem(POWER_PRESETS_STORAGE_KEY, JSON.stringify(presets));
  } catch {}
}

type Level = "ok" | "warn" | "crit";

function tempGpuLevel(t: number): Level {
  return t >= 80 ? "crit" : t >= 70 ? "warn" : "ok";
}

function tempMemLevel(t: number): Level {
  // GDDR6X junction throttles around 100-104 °C
  return t >= 96 ? "crit" : t >= 86 ? "warn" : "ok";
}

function utilLevel(u: number): Level {
  return u >= 95 ? "crit" : u >= 80 ? "warn" : "ok";
}

function vramLevel(used: number, total: number): Level {
  if (total <= 0) return "ok";
  const pct = (used / total) * 100;
  return pct >= 97 ? "crit" : pct >= 90 ? "warn" : "ok";
}

function powerLevel(draw: number, limit: number | null): Level {
  if (limit && draw >= limit * 0.95) return "warn";
  return "ok";
}

const LEVEL_CLASS: Record<"dark" | "light", Record<Level, string>> = {
  dark: { ok: "text-slate-400", warn: "text-amber-400", crit: "text-red-400" },
  light: { ok: "text-slate-500", warn: "text-amber-600", crit: "text-red-500" },
};

// ── Power limit popover ─────────────────────────────────────────────────────

function PowerLimitPopover({
  variant,
  data,
  onApplied,
  onClose,
}: {
  variant: "dark" | "light";
  data: GpuTelemetry;
  onApplied: () => void;
  onClose: () => void;
}) {
  const min = data.power_limit_min_w ?? 100;
  const max = data.power_limit_max_w ?? 450;
  const def = data.power_limit_default_w ?? max;
  const clamp = useCallback(
    (w: number) => Math.min(max, Math.max(min, Math.round(w))),
    [min, max],
  );

  const [target, setTarget] = useState(clamp(data.power_limit_w ?? def));
  const [inputValue, setInputValue] = useState(String(target));
  const [userPresets, setUserPresets] = useState<PowerPreset[]>([]);
  const [presetName, setPresetName] = useState("");
  const [status, setStatus] = useState<
    | { kind: "idle" }
    | { kind: "applying" }
    | { kind: "ok"; watts: number }
    | { kind: "error"; message: string }
  >({ kind: "idle" });
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setUserPresets(loadUserPresets());
  }, []);

  // Close on outside click / Escape.
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        onClose();
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  const apply = useCallback(
    async (watts: number) => {
      setStatus({ kind: "applying" });
      try {
        const r = await mutFetch("/api/local-models/gpu-power-limit", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ watts }),
        });
        if (!r.ok) {
          const detail = (await r.json().catch(() => null)) as {
            detail?: string;
          } | null;
          throw new Error(detail?.detail || `HTTP ${r.status}`);
        }
        const body = (await r.json()) as { power_limit_w: number };
        setStatus({ kind: "ok", watts: body.power_limit_w });
        setTarget(clamp(body.power_limit_w));
        setInputValue(String(Math.round(body.power_limit_w)));
        onApplied();
      } catch (e) {
        setStatus({
          kind: "error",
          message: e instanceof Error ? e.message : "не удалось применить",
        });
      }
    },
    [clamp, onApplied],
  );

  // Immediate (debounced) apply used by the slider and the number input.
  const setAndApply = useCallback(
    (watts: number) => {
      const w = clamp(watts);
      setTarget(w);
      setInputValue(String(w));
      if (debounceRef.current) clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(() => void apply(w), APPLY_DEBOUNCE_MS);
    },
    [clamp, apply],
  );

  useEffect(() => {
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, []);

  const builtinPresets: PowerPreset[] = [
    { name: "Тихий", watts: clamp(Math.round((def * 0.55) / 5) * 5) },
    { name: "Баланс", watts: clamp(Math.round((def * 0.8) / 5) * 5) },
    { name: "Максимум", watts: clamp(def) },
  ];

  const savePreset = () => {
    const name = presetName.trim();
    if (!name) return;
    const next = [
      ...userPresets.filter((p) => p.name !== name),
      { name, watts: target },
    ];
    setUserPresets(next);
    saveUserPresets(next);
    setPresetName("");
  };

  const removePreset = (name: string) => {
    const next = userPresets.filter((p) => p.name !== name);
    setUserPresets(next);
    saveUserPresets(next);
  };

  const dark = variant === "dark";
  const panel = dark
    ? "bg-slate-800 border-slate-600 text-slate-200 shadow-xl"
    : "bg-white border-slate-200 text-slate-700 shadow-xl";
  const chipBase = dark
    ? "border-slate-600 hover:bg-slate-700 text-slate-300"
    : "border-slate-300 hover:bg-slate-100 text-slate-600";
  const chipActive = dark
    ? "bg-blue-900/60 border-blue-600 text-blue-200"
    : "bg-blue-100 border-blue-400 text-blue-700";
  const inputCls = dark
    ? "bg-slate-900 border-slate-600 text-slate-200"
    : "bg-white border-slate-300 text-slate-700";
  const muted = dark ? "text-slate-400" : "text-slate-500";

  const isActive = (w: number) => Math.abs(w - target) < 3;

  return (
    <div
      ref={rootRef}
      className={`absolute right-2 top-full mt-1 z-50 w-72 rounded-lg border p-3 text-xs font-sans whitespace-normal cursor-default ${panel}`}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="flex items-center justify-between mb-2">
        <span className="font-semibold">Лимит мощности GPU</span>
        <button
          onClick={onClose}
          className={`${muted} hover:opacity-70 text-base leading-none`}
          title="Закрыть"
        >
          ×
        </button>
      </div>

      {/* Built-in presets */}
      <div className="flex flex-wrap gap-1.5 mb-2">
        {builtinPresets.map((p) => (
          <button
            key={p.name}
            onClick={() => setAndApply(p.watts)}
            className={`px-2 py-1 rounded border transition-colors ${
              isActive(p.watts) ? chipActive : chipBase
            }`}
            title={`${p.watts} Вт`}
          >
            {p.name} · {p.watts}W
          </button>
        ))}
      </div>

      {/* User presets */}
      {userPresets.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mb-2">
          {userPresets.map((p) => (
            <span
              key={p.name}
              className={`flex items-center gap-1 px-2 py-1 rounded border ${
                isActive(p.watts) ? chipActive : chipBase
              }`}
            >
              <button
                onClick={() => setAndApply(p.watts)}
                title={`${p.watts} Вт`}
              >
                {p.name} · {p.watts}W
              </button>
              <button
                onClick={() => removePreset(p.name)}
                className={`${muted} hover:text-red-400`}
                title="Удалить пресет"
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}

      {/* Slider + numeric input (applies immediately, debounced) */}
      <div className="flex items-center gap-2 mb-2">
        <input
          type="range"
          min={min}
          max={max}
          step={5}
          value={target}
          onChange={(e) => setAndApply(Number(e.target.value))}
          className="flex-1 accent-blue-500"
        />
        <input
          type="number"
          min={min}
          max={max}
          value={inputValue}
          onChange={(e) => {
            setInputValue(e.target.value);
            const w = Number(e.target.value);
            if (Number.isFinite(w) && w >= min && w <= max) setAndApply(w);
          }}
          onBlur={() => {
            const w = Number(inputValue);
            if (Number.isFinite(w)) setAndApply(w);
            else setInputValue(String(target));
          }}
          className={`w-16 px-1.5 py-0.5 rounded border text-right font-mono ${inputCls}`}
        />
        <span className={muted}>Вт</span>
      </div>
      <div className={`flex justify-between mb-2 ${muted}`}>
        <span>мин {Math.round(min)}</span>
        <span>заводской {Math.round(def)}</span>
        <span>макс {Math.round(max)}</span>
      </div>

      {/* Save current value as a user preset */}
      <div className="flex items-center gap-1.5 mb-1.5">
        <input
          type="text"
          value={presetName}
          onChange={(e) => setPresetName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") savePreset();
          }}
          placeholder="Имя пресета…"
          className={`flex-1 px-1.5 py-0.5 rounded border ${inputCls}`}
        />
        <button
          onClick={savePreset}
          disabled={!presetName.trim()}
          className={`px-2 py-0.5 rounded border disabled:opacity-40 ${chipBase}`}
        >
          Сохранить {target}W
        </button>
      </div>

      {/* Status line */}
      <div className="h-4">
        {status.kind === "applying" && (
          <span className="text-blue-400 animate-pulse">применяю…</span>
        )}
        {status.kind === "ok" && (
          <span className="text-green-500">
            ✓ лимит {Math.round(status.watts)} Вт
          </span>
        )}
        {status.kind === "error" && (
          <span className="text-red-400" title={status.message}>
            ошибка: {status.message}
          </span>
        )}
      </div>
      <p className={`mt-1 ${muted}`}>
        Применяется сразу. Сбрасывается при перезагрузке сервера.
      </p>
    </div>
  );
}

// ── Status bar ──────────────────────────────────────────────────────────────

export function GpuStatusBar({ variant }: { variant: "dark" | "light" }) {
  const [enabled, setEnabled] = useState(true);
  const [data, setData] = useState<GpuTelemetry | null>(null);
  const failuresRef = useRef(0);
  const [hidden, setHidden] = useState(false);
  const [powerOpen, setPowerOpen] = useState(false);

  // Toggle state: localStorage + cross-tab "storage" + same-tab custom event.
  useEffect(() => {
    const sync = () => setEnabled(isGpuBarEnabled());
    sync();
    window.addEventListener("storage", sync);
    window.addEventListener(GPU_BAR_TOGGLE_EVENT, sync);
    return () => {
      window.removeEventListener("storage", sync);
      window.removeEventListener(GPU_BAR_TOGGLE_EVENT, sync);
    };
  }, []);

  const fetchTelemetry = useCallback(async () => {
    if (document.visibilityState !== "visible") return;
    try {
      const r = await mutFetch("/api/local-models/gpu-telemetry", {
        method: "GET",
      });
      if (!r.ok) throw new Error(String(r.status));
      const body = (await r.json()) as GpuTelemetryResponse;
      if (!body.available || !body.gpu) throw new Error("unavailable");
      failuresRef.current = 0;
      setData(body.gpu);
      setHidden(false);
    } catch {
      failuresRef.current += 1;
      if (failuresRef.current >= MAX_CONSECUTIVE_FAILURES) {
        setHidden(true); // collapse silently; polling keeps going to auto-recover
      }
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    void fetchTelemetry();
    const interval = setInterval(() => void fetchTelemetry(), POLL_INTERVAL_MS);
    const onVisible = () => {
      if (document.visibilityState === "visible") void fetchTelemetry();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [enabled, fetchTelemetry]);

  if (!enabled || hidden || !data) return null;

  const cls = LEVEL_CLASS[variant];
  const sep = variant === "dark" ? "text-slate-600" : "text-slate-300";
  const border = variant === "dark" ? "border-slate-700" : "border-slate-200";

  const memTemp = data.temp_mem_junction_c ?? data.temp_mem_c;
  const canManagePower =
    data.source === "sidecar" &&
    data.power_limit_min_w != null &&
    data.power_limit_max_w != null;

  const tooltip = [
    data.name,
    data.driver_version ? `драйвер ${data.driver_version}` : null,
    data.fan_pct != null ? `вентилятор ${Math.round(data.fan_pct)}%` : null,
    data.clock_sm_mhz != null
      ? `GPU ${Math.round(data.clock_sm_mhz)} МГц`
      : null,
    data.clock_mem_mhz != null
      ? `память ${Math.round(data.clock_mem_mhz)} МГц`
      : null,
    data.power_limit_w != null
      ? `лимит ${Math.round(data.power_limit_w)} Вт`
      : null,
    data.temp_mem_junction_c != null
      ? "T памяти: junction (gddr6)"
      : data.temp_mem_c != null
        ? "T памяти: nvidia-smi"
        : null,
  ]
    .filter(Boolean)
    .join(" · ");

  const segments: React.ReactNode[] = [];
  const push = (node: React.ReactNode, key: string) => {
    if (segments.length > 0) {
      segments.push(
        <span key={`sep-${key}`} className={sep}>
          ·
        </span>,
      );
    }
    segments.push(node);
  };

  if (data.utilization_pct != null) {
    push(
      <span key="util" className={cls[utilLevel(data.utilization_pct)]}>
        GPU {Math.round(data.utilization_pct)}%
      </span>,
      "util",
    );
  }
  if (data.temp_gpu_c != null) {
    push(
      <span key="temp" className={cls[tempGpuLevel(data.temp_gpu_c)]}>
        {Math.round(data.temp_gpu_c)}°
      </span>,
      "temp",
    );
  }
  if (memTemp != null) {
    push(
      <span
        key="mem"
        className={cls[tempMemLevel(memTemp)]}
        title="Температура памяти (junction)"
      >
        M {Math.round(memTemp)}°
      </span>,
      "mem",
    );
  }
  if (data.vram_used_gb != null && data.vram_total_gb != null) {
    push(
      <span
        key="vram"
        className={cls[vramLevel(data.vram_used_gb, data.vram_total_gb)]}
        title="VRAM занято / всего"
      >
        {data.vram_used_gb.toFixed(1)}/{Math.round(data.vram_total_gb)}G
      </span>,
      "vram",
    );
  }
  if (data.power_draw_w != null) {
    const powerLabel = `${Math.round(data.power_draw_w)}W`;
    push(
      canManagePower ? (
        <button
          key="power"
          onClick={() => setPowerOpen((v) => !v)}
          className={`${cls[powerLevel(data.power_draw_w, data.power_limit_w)]} underline decoration-dotted underline-offset-2 hover:opacity-75 transition-opacity`}
          title={`Потребление ${powerLabel}, лимит ${data.power_limit_w != null ? Math.round(data.power_limit_w) : "?"} Вт — нажмите для управления`}
        >
          {powerLabel}
        </button>
      ) : (
        <span
          key="power"
          className={cls[powerLevel(data.power_draw_w, data.power_limit_w)]}
        >
          {powerLabel}
        </span>
      ),
      "power",
    );
  }

  if (segments.length === 0) return null;

  return (
    <div className="relative">
      <div
        className={`px-4 py-1 border-b ${border} flex items-center gap-1.5 text-[10px] font-mono leading-none whitespace-nowrap overflow-hidden`}
        title={tooltip || undefined}
      >
        {segments}
      </div>
      {powerOpen && canManagePower && (
        <PowerLimitPopover
          variant={variant}
          data={data}
          onApplied={() => void fetchTelemetry()}
          onClose={() => setPowerOpen(false)}
        />
      )}
    </div>
  );
}
