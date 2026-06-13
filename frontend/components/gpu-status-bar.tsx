"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { mutFetch } from "@/lib/auth";

export const GPU_BAR_STORAGE_KEY = "gpu_status_bar_enabled";
export const GPU_BAR_TOGGLE_EVENT = "gpu-statusbar-changed";
const POWER_PRESETS_STORAGE_KEY = "gpu_power_presets";
const CPU_PRESETS_STORAGE_KEY = "cpu_freq_presets";

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

interface CpuTelemetry {
  model: string | null;
  threads: number | null;
  utilization_pct: number | null;
  temp_c: number | null;
  power_draw_w: number | null;
  freq_mhz: number | null;
  freq_limit_mhz: number | null;
  freq_hw_min_mhz: number | null;
  freq_hw_max_mhz: number | null;
  boost: boolean | null;
  fan_rpm: number | null;
  fan_pct: number | null;
}

interface GpuTelemetryResponse {
  available: boolean;
  ts: number;
  gpu: GpuTelemetry | null;
  cpu: CpuTelemetry | null;
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

function loadUserPresets(storageKey: string): PowerPreset[] {
  try {
    const raw = localStorage.getItem(storageKey);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as PowerPreset[];
    return parsed.filter(
      (p) => typeof p.name === "string" && typeof p.watts === "number",
    );
  } catch {
    return [];
  }
}

function saveUserPresets(storageKey: string, presets: PowerPreset[]) {
  try {
    localStorage.setItem(storageKey, JSON.stringify(presets));
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

function tempCpuLevel(t: number): Level {
  // Ryzen Tctl throttles at ~95 °C
  return t >= 90 ? "crit" : t >= 75 ? "warn" : "ok";
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

function popoverTheme(variant: "dark" | "light") {
  const dark = variant === "dark";
  return {
    panel: dark
      ? "bg-slate-800 border-slate-600 text-slate-200 shadow-xl"
      : "bg-white border-slate-200 text-slate-700 shadow-xl",
    chipBase: dark
      ? "border-slate-600 hover:bg-slate-700 text-slate-300"
      : "border-slate-300 hover:bg-slate-100 text-slate-600",
    chipActive: dark
      ? "bg-blue-900/60 border-blue-600 text-blue-200"
      : "bg-blue-100 border-blue-400 text-blue-700",
    inputCls: dark
      ? "bg-slate-900 border-slate-600 text-slate-200"
      : "bg-white border-slate-300 text-slate-700",
    muted: dark ? "text-slate-400" : "text-slate-500",
  };
}

function useCloseOnOutside(
  rootRef: React.RefObject<HTMLDivElement | null>,
  onClose: () => void,
) {
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
  }, [rootRef, onClose]);
}

// ── GPU power limit popover ─────────────────────────────────────────────────

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
    setUserPresets(loadUserPresets(POWER_PRESETS_STORAGE_KEY));
  }, []);

  useCloseOnOutside(rootRef, onClose);

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
    saveUserPresets(POWER_PRESETS_STORAGE_KEY, next);
    setPresetName("");
  };

  const removePreset = (name: string) => {
    const next = userPresets.filter((p) => p.name !== name);
    setUserPresets(next);
    saveUserPresets(POWER_PRESETS_STORAGE_KEY, next);
  };

  const { panel, chipBase, chipActive, inputCls, muted } =
    popoverTheme(variant);
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
        Применяется сразу и восстанавливается при старте стека.
      </p>
    </div>
  );
}

// ── CPU frequency limit popover ─────────────────────────────────────────────

function CpuLimitPopover({
  variant,
  cpu,
  onApplied,
  onClose,
}: {
  variant: "dark" | "light";
  cpu: CpuTelemetry;
  onApplied: () => void;
  onClose: () => void;
}) {
  const min = cpu.freq_hw_min_mhz ?? 800;
  const max = cpu.freq_hw_max_mhz ?? 6000;
  const clamp = useCallback(
    (mhz: number) => Math.min(max, Math.max(min, Math.round(mhz))),
    [min, max],
  );
  const ghz = (mhz: number) => `${(mhz / 1000).toFixed(1)}ГГц`;

  const [target, setTarget] = useState(clamp(cpu.freq_limit_mhz ?? max));
  const [inputValue, setInputValue] = useState(String(target));
  const [boost, setBoost] = useState(cpu.boost ?? true);
  const [userPresets, setUserPresets] = useState<PowerPreset[]>([]);
  const [presetName, setPresetName] = useState("");
  const [status, setStatus] = useState<
    | { kind: "idle" }
    | { kind: "applying" }
    | { kind: "ok"; mhz: number }
    | { kind: "error"; message: string }
  >({ kind: "idle" });
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setUserPresets(loadUserPresets(CPU_PRESETS_STORAGE_KEY));
  }, []);

  useCloseOnOutside(rootRef, onClose);

  const post = useCallback(
    async (payload: { max_freq_mhz?: number; boost?: boolean }) => {
      setStatus({ kind: "applying" });
      try {
        const r = await mutFetch("/api/local-models/cpu-limit", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!r.ok) {
          const detail = (await r.json().catch(() => null)) as {
            detail?: string;
          } | null;
          throw new Error(detail?.detail || `HTTP ${r.status}`);
        }
        const body = (await r.json()) as {
          max_freq_mhz: number | null;
          boost: boolean | null;
        };
        if (body.max_freq_mhz != null) {
          setStatus({ kind: "ok", mhz: body.max_freq_mhz });
          setTarget(clamp(body.max_freq_mhz));
          setInputValue(String(Math.round(body.max_freq_mhz)));
        } else {
          setStatus({ kind: "ok", mhz: target });
        }
        if (body.boost != null) setBoost(body.boost);
        onApplied();
      } catch (e) {
        setStatus({
          kind: "error",
          message: e instanceof Error ? e.message : "не удалось применить",
        });
      }
    },
    [clamp, onApplied, target],
  );

  const setAndApply = useCallback(
    (mhz: number) => {
      const m = clamp(mhz);
      setTarget(m);
      setInputValue(String(m));
      if (debounceRef.current) clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(
        () => void post({ max_freq_mhz: m }),
        APPLY_DEBOUNCE_MS,
      );
    },
    [clamp, post],
  );

  useEffect(() => {
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, []);

  const round100 = (mhz: number) => clamp(Math.round(mhz / 100) * 100);
  const builtinPresets: PowerPreset[] = [
    { name: "Тихий", watts: round100(max * 0.6) },
    { name: "Баланс", watts: round100(max * 0.8) },
    { name: "Максимум", watts: max },
  ];

  const savePreset = () => {
    const name = presetName.trim();
    if (!name) return;
    const next = [
      ...userPresets.filter((p) => p.name !== name),
      { name, watts: target },
    ];
    setUserPresets(next);
    saveUserPresets(CPU_PRESETS_STORAGE_KEY, next);
    setPresetName("");
  };

  const removePreset = (name: string) => {
    const next = userPresets.filter((p) => p.name !== name);
    setUserPresets(next);
    saveUserPresets(CPU_PRESETS_STORAGE_KEY, next);
  };

  const { panel, chipBase, chipActive, inputCls, muted } =
    popoverTheme(variant);
  const isActive = (mhz: number) => Math.abs(mhz - target) < 60;

  return (
    <div
      ref={rootRef}
      className={`absolute right-2 top-full mt-1 z-50 w-72 rounded-lg border p-3 text-xs font-sans whitespace-normal cursor-default ${panel}`}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="flex items-center justify-between mb-2">
        <span className="font-semibold">Лимит CPU (частота)</span>
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
            title={`${p.watts} МГц`}
          >
            {p.name} · {ghz(p.watts)}
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
                title={`${p.watts} МГц`}
              >
                {p.name} · {ghz(p.watts)}
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

      {/* Slider + numeric input (MHz, applies immediately, debounced) */}
      <div className="flex items-center gap-2 mb-2">
        <input
          type="range"
          min={min}
          max={max}
          step={100}
          value={target}
          onChange={(e) => setAndApply(Number(e.target.value))}
          className="flex-1 accent-blue-500"
        />
        <input
          type="number"
          min={min}
          max={max}
          step={100}
          value={inputValue}
          onChange={(e) => {
            setInputValue(e.target.value);
            const m = Number(e.target.value);
            if (Number.isFinite(m) && m >= min && m <= max) setAndApply(m);
          }}
          onBlur={() => {
            const m = Number(inputValue);
            if (Number.isFinite(m)) setAndApply(m);
            else setInputValue(String(target));
          }}
          className={`w-20 px-1.5 py-0.5 rounded border text-right font-mono ${inputCls}`}
        />
        <span className={muted}>МГц</span>
      </div>
      <div className={`flex justify-between mb-2 ${muted}`}>
        <span>мин {ghz(min)}</span>
        <span>макс {ghz(max)}</span>
      </div>

      {/* Boost toggle */}
      <label className="flex items-center gap-2 mb-2 cursor-pointer select-none">
        <input
          type="checkbox"
          checked={boost}
          onChange={(e) => {
            setBoost(e.target.checked);
            void post({ boost: e.target.checked });
          }}
        />
        Turbo Boost (авторазгон выше базовой частоты)
      </label>

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
          Сохранить {ghz(target)}
        </button>
      </div>

      {/* Status line */}
      <div className="h-4">
        {status.kind === "applying" && (
          <span className="text-blue-400 animate-pulse">применяю…</span>
        )}
        {status.kind === "ok" && (
          <span className="text-green-500">✓ лимит {ghz(status.mhz)}</span>
        )}
        {status.kind === "error" && (
          <span className="text-red-400" title={status.message}>
            ошибка: {status.message}
          </span>
        )}
      </div>
      <p className={`mt-1 ${muted}`}>
        Прямой power limit на десктопных Ryzen недоступен из ОС — потребление
        ограничивается частотой и бустом. Применяется сразу, восстанавливается
        при старте стека.
      </p>
    </div>
  );
}

// ── Status bar ──────────────────────────────────────────────────────────────

export function GpuStatusBar({ variant }: { variant: "dark" | "light" }) {
  const [enabled, setEnabled] = useState(true);
  const [gpu, setGpu] = useState<GpuTelemetry | null>(null);
  const [cpu, setCpu] = useState<CpuTelemetry | null>(null);
  const failuresRef = useRef(0);
  const [hidden, setHidden] = useState(false);
  const [gpuLimitOpen, setGpuLimitOpen] = useState(false);
  const [cpuLimitOpen, setCpuLimitOpen] = useState(false);

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
      if (!body.available || (!body.gpu && !body.cpu)) {
        throw new Error("unavailable");
      }
      failuresRef.current = 0;
      setGpu(body.gpu);
      setCpu(body.cpu);
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

  if (!enabled || hidden || (!gpu && !cpu)) return null;

  const cls = LEVEL_CLASS[variant];
  const sep = variant === "dark" ? "text-slate-600" : "text-slate-300";
  const border = variant === "dark" ? "border-slate-700" : "border-slate-200";
  const innerBorder =
    variant === "dark" ? "border-slate-700/60" : "border-slate-100";
  const rowCls =
    "px-4 py-1 flex items-center gap-1.5 text-[10px] font-mono leading-none whitespace-nowrap overflow-hidden";

  const joinSegments = (nodes: React.ReactNode[]) => {
    const out: React.ReactNode[] = [];
    nodes.forEach((node, i) => {
      if (i > 0) {
        out.push(
          <span key={`sep-${i}`} className={sep}>
            ·
          </span>,
        );
      }
      out.push(node);
    });
    return out;
  };

  // ── GPU row ───────────────────────────────────────────────────────────────
  let gpuRow: React.ReactNode = null;
  if (gpu) {
    const memTemp = gpu.temp_mem_junction_c ?? gpu.temp_mem_c;
    const canManagePower =
      gpu.source === "sidecar" &&
      gpu.power_limit_min_w != null &&
      gpu.power_limit_max_w != null;

    const gpuTooltip = [
      gpu.name,
      gpu.driver_version ? `драйвер ${gpu.driver_version}` : null,
      gpu.clock_sm_mhz != null
        ? `GPU ${Math.round(gpu.clock_sm_mhz)} МГц`
        : null,
      gpu.clock_mem_mhz != null
        ? `память ${Math.round(gpu.clock_mem_mhz)} МГц`
        : null,
      gpu.power_limit_w != null
        ? `лимит ${Math.round(gpu.power_limit_w)} Вт`
        : null,
      gpu.temp_mem_junction_c != null
        ? "T памяти: junction (gddr6)"
        : gpu.temp_mem_c != null
          ? "T памяти: nvidia-smi"
          : null,
    ]
      .filter(Boolean)
      .join(" · ");

    const nodes: React.ReactNode[] = [];
    if (gpu.utilization_pct != null) {
      nodes.push(
        <span key="util" className={cls[utilLevel(gpu.utilization_pct)]}>
          GPU {Math.round(gpu.utilization_pct)}%
        </span>,
      );
    }
    if (gpu.temp_gpu_c != null) {
      nodes.push(
        <span key="temp" className={cls[tempGpuLevel(gpu.temp_gpu_c)]}>
          {Math.round(gpu.temp_gpu_c)}°
        </span>,
      );
    }
    if (memTemp != null) {
      nodes.push(
        <span
          key="mem"
          className={cls[tempMemLevel(memTemp)]}
          title="Температура памяти (junction)"
        >
          M {Math.round(memTemp)}°
        </span>,
      );
    }
    if (gpu.vram_used_gb != null && gpu.vram_total_gb != null) {
      nodes.push(
        <span
          key="vram"
          className={cls[vramLevel(gpu.vram_used_gb, gpu.vram_total_gb)]}
          title="VRAM занято / всего"
        >
          {gpu.vram_used_gb.toFixed(1)}/{Math.round(gpu.vram_total_gb)}G
        </span>,
      );
    }
    if (gpu.power_draw_w != null) {
      const powerLabel = `${Math.round(gpu.power_draw_w)}W`;
      nodes.push(
        canManagePower ? (
          <button
            key="power"
            onClick={() => {
              setCpuLimitOpen(false);
              setGpuLimitOpen((v) => !v);
            }}
            className={`${cls[powerLevel(gpu.power_draw_w, gpu.power_limit_w)]} underline decoration-dotted underline-offset-2 hover:opacity-75 transition-opacity`}
            title={`Потребление ${powerLabel}, лимит ${gpu.power_limit_w != null ? Math.round(gpu.power_limit_w) : "?"} Вт — нажмите для управления`}
          >
            {powerLabel}
          </button>
        ) : (
          <span
            key="power"
            className={cls[powerLevel(gpu.power_draw_w, gpu.power_limit_w)]}
          >
            {powerLabel}
          </span>
        ),
      );
    }
    if (gpu.fan_pct != null) {
      nodes.push(
        <span key="fan" className={cls.ok} title="Вентилятор GPU">
          F {Math.round(gpu.fan_pct)}%
        </span>,
      );
    }
    if (nodes.length > 0) {
      gpuRow = (
        <div className={rowCls} title={gpuTooltip || undefined}>
          {joinSegments(nodes)}
        </div>
      );
    }
  }

  // ── CPU row ───────────────────────────────────────────────────────────────
  let cpuRow: React.ReactNode = null;
  if (cpu) {
    const canManageCpu =
      cpu.freq_hw_min_mhz != null && cpu.freq_hw_max_mhz != null;
    const isCapped =
      cpu.freq_limit_mhz != null &&
      cpu.freq_hw_max_mhz != null &&
      cpu.freq_limit_mhz < cpu.freq_hw_max_mhz * 0.98;

    const cpuTooltip = [
      cpu.model,
      cpu.threads != null ? `${cpu.threads} потоков` : null,
      cpu.freq_limit_mhz != null
        ? `лимит ${(cpu.freq_limit_mhz / 1000).toFixed(1)} ГГц`
        : null,
      cpu.boost != null ? `буст ${cpu.boost ? "вкл" : "выкл"}` : null,
    ]
      .filter(Boolean)
      .join(" · ");

    const nodes: React.ReactNode[] = [];
    if (cpu.utilization_pct != null) {
      nodes.push(
        <span key="util" className={cls[utilLevel(cpu.utilization_pct)]}>
          CPU {Math.round(cpu.utilization_pct)}%
        </span>,
      );
    }
    if (cpu.temp_c != null) {
      nodes.push(
        <span key="temp" className={cls[tempCpuLevel(cpu.temp_c)]}>
          {Math.round(cpu.temp_c)}°
        </span>,
      );
    }
    if (cpu.freq_mhz != null) {
      const freqLabel = `${(cpu.freq_mhz / 1000).toFixed(1)}ГГц${isCapped ? "↓" : ""}`;
      nodes.push(
        canManageCpu ? (
          <button
            key="freq"
            onClick={() => {
              setGpuLimitOpen(false);
              setCpuLimitOpen((v) => !v);
            }}
            className={`${cls[isCapped ? "warn" : "ok"]} underline decoration-dotted underline-offset-2 hover:opacity-75 transition-opacity`}
            title={`Текущая частота, лимит ${cpu.freq_limit_mhz != null ? (cpu.freq_limit_mhz / 1000).toFixed(1) : "?"} ГГц — нажмите для управления`}
          >
            {freqLabel}
          </button>
        ) : (
          <span key="freq" className={cls.ok}>
            {freqLabel}
          </span>
        ),
      );
    }
    if (cpu.power_draw_w != null) {
      nodes.push(
        <span key="power" className={cls.ok} title="Потребление CPU (RAPL)">
          {Math.round(cpu.power_draw_w)}W
        </span>,
      );
    }
    if (cpu.fan_pct != null) {
      nodes.push(
        <span
          key="fan"
          className={cls.ok}
          title={`Вентилятор CPU${cpu.fan_rpm != null ? ` · ${cpu.fan_rpm} об/мин` : ""}`}
        >
          F {Math.round(cpu.fan_pct)}%
        </span>,
      );
    } else if (cpu.fan_rpm != null) {
      nodes.push(
        <span key="fan" className={cls.ok} title="Вентилятор CPU (об/мин)">
          F {cpu.fan_rpm}
        </span>,
      );
    }
    if (nodes.length > 0) {
      cpuRow = (
        <div
          className={`${rowCls} ${gpuRow ? `border-t ${innerBorder}` : ""}`}
          title={cpuTooltip || undefined}
        >
          {joinSegments(nodes)}
        </div>
      );
    }
  }

  if (!gpuRow && !cpuRow) return null;

  return (
    <div className="relative">
      <div className={`border-b ${border}`}>
        {gpuRow}
        {cpuRow}
      </div>
      {gpuLimitOpen && gpu && (
        <PowerLimitPopover
          variant={variant}
          data={gpu}
          onApplied={() => void fetchTelemetry()}
          onClose={() => setGpuLimitOpen(false)}
        />
      )}
      {cpuLimitOpen && cpu && (
        <CpuLimitPopover
          variant={variant}
          cpu={cpu}
          onApplied={() => void fetchTelemetry()}
          onClose={() => setCpuLimitOpen(false)}
        />
      )}
    </div>
  );
}
