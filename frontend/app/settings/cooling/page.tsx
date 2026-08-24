"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { getApiBaseUrl } from "@/lib/api-base";
import { apiFetch, mutFetch } from "@/lib/auth";

import { SetupGuide } from "./setup-guide";

const API = getApiBaseUrl();
const BASE = `${API}/api/cooling`;
const POLL_MS = 5000;

interface CurvePoint {
  t: number;
  pct: number;
}

interface ChannelConfig {
  mode?: string;
  sensor?: string;
  curve?: CurvePoint[];
  pct?: number;
  min_pct?: number;
  allow_stop?: boolean;
  stop_below_c?: number | null;
}

interface FanChannel {
  id: string;
  label: string;
  kind: "gpu" | "mobo";
  controllable: boolean;
  control_reason: string | null;
  min_pct: number;
  max_pct: number;
  has_tach: boolean;
  default_sensor: string;
  rpm: number | null;
  pwm_pct: number | null;
  mode: string;
  failed_reason: string | null;
  target_pct: number | null;
  config: ChannelConfig | null;
}

interface FansResponse {
  control_enabled: boolean;
  hwmon_allowed: boolean;
  env_defaults?: { control_enabled: boolean; allow_hwmon: boolean };
  config: { enabled?: boolean; preset?: string; channels?: Record<string, ChannelConfig> };
  presets: Record<string, { label: string; curves: Record<string, CurvePoint[]> }>;
  custom_presets: Record<string, { label: string; config: unknown }>;
  channels: FanChannel[];
  temperatures: Record<string, number>;
  emergency: Record<string, number>;
  loop: { running: boolean; tick_s: number; last_tick_ts: number | null; last_error: string | null };
  safety: {
    hard_floor_pct: number;
    emergency_c: Record<string, number>;
    emergency_hold_s: number;
    temp_hysteresis_c: number;
    max_step_down_pct: number;
  };
}

interface FanEvent {
  ts: number;
  level: string;
  channel: string | null;
  code: string;
  message: string;
}

const SENSOR_LABELS: Record<string, string> = {
  gpu: "GPU",
  gpu_mem: "Память GPU",
  cpu: "CPU",
};

const MODE_LABELS: Record<string, string> = {
  auto: "прошивка",
  manual: "под управлением",
  failed: "авария",
};

function levelClass(level: string): string {
  if (level === "critical" || level === "error") return "text-red-600";
  if (level === "warn") return "text-amber-600";
  return "text-muted-foreground";
}

/** Small inline chart of a curve; keeps the editor honest about its shape. */
function CurvePreview({ points }: { points: CurvePoint[] }) {
  if (points.length < 2) return null;
  const W = 260;
  const H = 90;
  const ts = points.map((p) => p.t);
  const tMin = Math.min(...ts);
  const tMax = Math.max(...ts);
  const span = tMax - tMin || 1;
  const d = points
    .map((p, i) => {
      const x = ((p.t - tMin) / span) * W;
      const y = H - (p.pct / 100) * H;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg width={W} height={H} className="border rounded bg-muted/30" role="img" aria-label="Форма кривой">
      <path d={d} fill="none" stroke="currentColor" strokeWidth="2" className="text-blue-500" />
      {/* fill=currentColor so the labels follow the theme's text colour */}
      <g className="text-muted-foreground" fill="currentColor" fontSize="9">
        <text x="3" y="10">100%</text>
        <text x="3" y={H - 3}>0%</text>
        <text x={W - 32} y={H - 3}>{tMax}°C</text>
      </g>
    </svg>
  );
}

export default function CoolingSettingsPage() {
  const [data, setData] = useState<FansResponse | null>(null);
  const [events, setEvents] = useState<FanEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<CurvePoint[]>([]);
  const [preview, setPreview] = useState<CurvePoint[] | null>(null);
  const [presetName, setPresetName] = useState("");
  const [manualDraft, setManualDraft] = useState<Record<string, number>>({});
  const manualTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const editing = useRef(false);

  const load = useCallback(async () => {
    try {
      const res = await apiFetch(`${BASE}/fans`);
      const body: FansResponse = await res.json();
      if (!res.ok) throw new Error((body as unknown as { detail?: string })?.detail || `HTTP ${res.status}`);
      setData(body);
      setErr(null);
      if (!editing.current) {
        const first = body.channels.find((c) => c.controllable);
        setSelected((prev) => prev ?? first?.id ?? null);
      }
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadEvents = useCallback(async () => {
    try {
      const res = await apiFetch(`${BASE}/fans/events`);
      const body = await res.json();
      if (res.ok) setEvents((body.events || []).slice().reverse());
    } catch {
      /* the events log is informational; a failure here is not worth a banner */
    }
  }, []);

  useEffect(() => {
    const timers = manualTimers.current;
    return () => Object.values(timers).forEach(clearTimeout);
  }, []);

  useEffect(() => {
    void load();
    void loadEvents();
    const id = setInterval(() => {
      if (document.visibilityState === "visible") {
        void load();
        void loadEvents();
      }
    }, POLL_MS);
    return () => clearInterval(id);
  }, [load, loadEvents]);

  const channel = useMemo(
    () => data?.channels.find((c) => c.id === selected) || null,
    [data, selected],
  );

  // Seed the editor from whatever the channel currently runs.
  useEffect(() => {
    if (!channel) return;
    const curve = channel.config?.curve;
    if (curve?.length) {
      setDraft(curve.map((p) => ({ ...p })));
    } else {
      const preset = data?.presets?.balanced?.curves?.[channel.kind];
      setDraft((preset || []).map((p) => ({ ...p })));
    }
    setPreview(null);
  }, [channel?.id, data?.presets]); // eslint-disable-line react-hooks/exhaustive-deps

  async function call(path: string, body?: unknown, method = "POST") {
    setBusy(true);
    setMsg(null);
    setErr(null);
    try {
      const res = await mutFetch(`${BASE}${path}`, {
        method,
        headers: { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const payload = await res.json();
      if (!res.ok) throw new Error(payload?.detail || `HTTP ${res.status}`);
      await load();
      await loadEvents();
      return payload;
    } catch (e) {
      setErr(String((e as Error).message || e));
      return null;
    } finally {
      setBusy(false);
    }
  }

  // Quick manual speed: same debounce as the status-bar popover, so dragging
  // the slider does not fire a write per pixel.
  function setManual(channelId: string, pct: number) {
    setManualDraft((prev) => ({ ...prev, [channelId]: pct }));
    const timers = manualTimers.current;
    if (timers[channelId]) clearTimeout(timers[channelId]);
    timers[channelId] = setTimeout(() => {
      void call("/fans/manual", { channel_id: channelId, pct });
    }, 400);
  }

  async function setControl(patch: { enabled?: boolean; allow_hwmon?: boolean }) {
    const out = await call("/control", patch);
    if (out) {
      setManualDraft({});
      setMsg(
        patch.enabled === false
          ? "Управление выключено, вентиляторы возвращены прошивке."
          : "Настройка сохранена.",
      );
    }
  }

  async function applyPreset(name: string) {
    const out = await call(`/presets/${encodeURIComponent(name)}/apply`);
    // Drop the manual drafts: the preset owns the speed now, and a leftover
    // draft would keep the slider showing a number nothing is driving.
    if (out) {
      setManualDraft({});
      setMsg(`Пресет «${name}» применён.`);
    }
  }

  async function revertAll() {
    const out = await call("/fans/mode", { scope: "all" });
    if (out) {
      setManualDraft({});
      setMsg("Все каналы возвращены под управление прошивки.");
    }
  }

  async function runPreview() {
    if (!channel) return;
    const out = await call("/fans/preview", {
      channels: { [channel.id]: { mode: "curve", sensor: channel.default_sensor, curve: draft } },
      temps: [30, 40, 50, 60, 70, 80, 90, 100],
    });
    if (out) {
      setPreview(out.preview?.[channel.id] || []);
      setMsg("Проверка выполнена — железо не тронуто.");
    }
  }

  async function applyCurve() {
    if (!channel || !data) return;
    const channels: Record<string, ChannelConfig> = {};
    for (const c of data.channels) {
      if (!c.controllable) continue;
      channels[c.id] =
        c.id === channel.id
          ? { mode: "curve", sensor: channel.default_sensor, curve: draft }
          : (c.config as ChannelConfig) || {
              mode: "curve",
              sensor: c.default_sensor,
              curve: data.presets?.balanced?.curves?.[c.kind] || [],
            };
    }
    const out = await call("/fans/config", { enabled: true, preset: "custom", channels });
    if (out) {
      setManualDraft({});
      setMsg("Кривая применена.");
    }
  }

  async function savePreset() {
    if (!presetName.trim() || !data) return;
    const channels: Record<string, ChannelConfig> = {};
    for (const c of data.channels) {
      if (c.controllable && c.config) channels[c.id] = c.config;
    }
    const out = await call("/presets", {
      name: presetName.trim(),
      label: presetName.trim(),
      config: { enabled: true, preset: presetName.trim(), channels },
    });
    if (out) {
      setPresetName("");
      setMsg("Пресет сохранён.");
    }
  }

  if (loading) return <div className="text-sm text-muted-foreground">Загрузка…</div>;

  const emergencies = Object.entries(data?.emergency || {});
  const controllable = (data?.channels || []).filter((c) => c.controllable);

  return (
    <div className="space-y-6 max-w-4xl">
      <div>
        <h2 className="text-lg font-semibold">Охлаждение и вентиляторы</h2>
        <p className="text-sm text-muted-foreground mt-1">
          Управление оборотами по температуре. Контур работает внутри служебного
          контейнера рядом с железом, поэтому продолжает держать обороты, даже
          если backend или очередь задач остановлены. Аварийный обгон и возврат
          вентиляторов прошивке при остановке контура отключить нельзя.
        </p>
      </div>

      {err && <div className="rounded border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-600">{err}</div>}
      {msg && <div className="rounded border border-green-500/40 bg-green-500/10 px-3 py-2 text-sm text-green-600">{msg}</div>}

      {emergencies.length > 0 && (
        <div className="rounded border border-red-500/60 bg-red-500/15 px-3 py-2 text-sm text-red-600">
          <b>Аварийный обгон активен.</b>{" "}
          {emergencies.map(([s, left]) => `${SENSOR_LABELS[s] || s}: ещё ${Math.round(left)} с`).join(", ")}.
          Вентиляторы принудительно на 100%, кривые игнорируются.
        </div>
      )}

      {/* --- Switches ---------------------------------------------------- */}
      <section className="space-y-2">
        <h3 className="text-sm font-semibold">Управление</h3>
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            checked={!!data?.control_enabled}
            disabled={busy}
            onChange={(e) => void setControl({ enabled: e.target.checked })}
            className="mt-0.5 accent-blue-500"
          />
          <span>
            Разрешить управление оборотами
            <span className="block text-xs text-muted-foreground">
              Выключено — обороты только показываются. При выключении все каналы,
              которыми мы распоряжались, немедленно возвращаются прошивке.
            </span>
          </span>
        </label>
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            checked={!!data?.hwmon_allowed}
            disabled={busy}
            onChange={(e) => void setControl({ allow_hwmon: e.target.checked })}
            className="mt-0.5 accent-blue-500"
          />
          <span>
            Разрешить вентиляторы материнской платы
            <span className="block text-xs text-muted-foreground">
              Нужен драйвер, отдающий pwm на запись. Если его нет, каналы всё равно
              останутся только для чтения — см. инструкцию ниже.
            </span>
          </span>
        </label>
        {!data?.control_enabled && (
          <div className="rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-600">
            Управление выключено — показываются только обороты.
          </div>
        )}
      </section>


      {/* --- Channels --------------------------------------------------- */}
      <section className="space-y-2">
        <h3 className="text-sm font-semibold">Каналы</h3>
        <div className="overflow-x-auto">
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="text-left text-xs text-muted-foreground border-b">
                <th className="py-1 pr-3">Канал</th>
                <th className="py-1 pr-3">Обороты</th>
                <th className="py-1 pr-3">Мощность</th>
                <th className="py-1 pr-3">Режим</th>
                <th className="py-1 pr-3">Ручные обороты</th>
                <th className="py-1">Состояние</th>
              </tr>
            </thead>
            <tbody>
              {(data?.channels || []).map((c) => (
                <tr
                  key={c.id}
                  className={`border-b align-top ${c.controllable ? "cursor-pointer hover:bg-muted/40" : "opacity-60"} ${
                    c.id === selected ? "bg-muted/50" : ""
                  }`}
                  onClick={() => c.controllable && setSelected(c.id)}
                >
                  <td className="py-1.5 pr-3">
                    <div>{c.label}</div>
                    <div className="text-[11px] text-muted-foreground font-mono">{c.id}</div>
                  </td>
                  <td className="py-1.5 pr-3 tabular-nums">{c.rpm === null ? "—" : `${c.rpm} об/мин`}</td>
                  <td className="py-1.5 pr-3 tabular-nums">
                    {c.pwm_pct === null ? "—" : `${Math.round(c.pwm_pct)}%`}
                    {c.target_pct !== null && <span className="text-muted-foreground"> → {Math.round(c.target_pct)}%</span>}
                  </td>
                  <td className="py-1.5 pr-3">{MODE_LABELS[c.mode] || c.mode}</td>
                  <td className="py-1.5 pr-3" onClick={(e) => e.stopPropagation()}>
                    {c.controllable ? (
                      <div className="flex items-center gap-2">
                        <input
                          type="range"
                          min={Math.round(Math.max(data?.safety.hard_floor_pct ?? 20, c.min_pct))}
                          max={Math.round(c.max_pct)}
                          step={1}
                          disabled={busy || !data?.control_enabled}
                          value={Math.round(
                            manualDraft[c.id] ??
                              c.target_pct ??
                              c.pwm_pct ??
                              c.min_pct,
                          )}
                          onChange={(e) => setManual(c.id, Number(e.target.value))}
                          className="w-28 accent-blue-500 disabled:opacity-40"
                          aria-label={`Обороты: ${c.label}`}
                        />
                        <span className="tabular-nums w-9 text-right">
                          {Math.round(
                            manualDraft[c.id] ?? c.target_pct ?? c.pwm_pct ?? c.min_pct,
                          )}
                          %
                        </span>
                      </div>
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </td>
                  <td className="py-1.5 text-xs">
                    {c.failed_reason ? (
                      <span className="text-red-600">{c.failed_reason}</span>
                    ) : c.controllable ? (
                      <span className="text-green-700">управляем ({Math.round(c.min_pct)}–{Math.round(c.max_pct)}%)</span>
                    ) : (
                      <span className="text-muted-foreground">{c.control_reason}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="text-xs text-muted-foreground">
          Температуры:{" "}
          {Object.entries(data?.temperatures || {})
            .map(([k, v]) => `${SENSOR_LABELS[k] || k} ${Math.round(v)}°C`)
            .join(" · ") || "нет данных"}
          {" · "}контур: {data?.loop.running ? `работает, шаг ${data.loop.tick_s} с` : "остановлен"}
          {data?.loop.last_error && <span className="text-red-600"> · ошибка: {data.loop.last_error}</span>}
        </div>
      </section>

      {/* --- Presets ---------------------------------------------------- */}
      <section className="space-y-2">
        <h3 className="text-sm font-semibold">Пресеты</h3>
        <div className="flex flex-wrap gap-2">
          {Object.entries(data?.presets || {}).map(([name, p]) => (
            <button
              key={name}
              disabled={busy || !data?.control_enabled || controllable.length === 0}
              onClick={() => applyPreset(name)}
              className={`rounded border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50 ${
                data?.config?.preset === name ? "border-blue-500 bg-blue-500/10 text-blue-500" : ""
              }`}
            >
              {p.label}
            </button>
          ))}
          {Object.entries(data?.custom_presets || {}).map(([name, p]) => (
            <button
              key={name}
              disabled={busy || !data?.control_enabled}
              onClick={() => applyPreset(name)}
              className="rounded border border-dashed px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
            >
              {p.label || name}
            </button>
          ))}
          <button
            disabled={busy}
            onClick={revertAll}
            className="rounded border border-amber-500/60 px-3 py-1.5 text-sm text-amber-600 hover:bg-amber-500/10 disabled:opacity-50"
          >
            Вернуть всё прошивке
          </button>
        </div>
        <div className="flex gap-2 items-center">
          <input
            value={presetName}
            onChange={(e) => setPresetName(e.target.value)}
            placeholder="Имя своего пресета…"
            className="rounded border px-2 py-1 text-sm w-56"
          />
          <button
            disabled={busy || !presetName.trim()}
            onClick={savePreset}
            className="rounded border px-3 py-1 text-sm hover:bg-muted disabled:opacity-50"
          >
            Сохранить текущую настройку
          </button>
        </div>
      </section>

      {/* --- Curve editor ------------------------------------------------ */}
      {channel && (
        <section className="space-y-3">
          <h3 className="text-sm font-semibold">
            Кривая: {channel.label}{" "}
            <span className="font-normal text-muted-foreground">
              (датчик {SENSOR_LABELS[channel.default_sensor] || channel.default_sensor})
            </span>
          </h3>
          <div className="flex flex-wrap gap-6">
            <div className="space-y-1">
              {draft.map((p, i) => (
                <div key={i} className="flex items-center gap-2 text-sm">
                  <input
                    type="number"
                    value={p.t}
                    onFocus={() => (editing.current = true)}
                    onBlur={() => (editing.current = false)}
                    onChange={(e) => {
                      const next = draft.slice();
                      next[i] = { ...next[i], t: Number(e.target.value) };
                      setDraft(next);
                    }}
                    className="w-20 rounded border px-2 py-1 tabular-nums"
                  />
                  <span className="text-muted-foreground">°C →</span>
                  <input
                    type="number"
                    min={0}
                    max={100}
                    value={p.pct}
                    onFocus={() => (editing.current = true)}
                    onBlur={() => (editing.current = false)}
                    onChange={(e) => {
                      const next = draft.slice();
                      next[i] = { ...next[i], pct: Number(e.target.value) };
                      setDraft(next);
                    }}
                    className="w-20 rounded border px-2 py-1 tabular-nums"
                  />
                  <span className="text-muted-foreground">%</span>
                  <button
                    onClick={() => setDraft(draft.filter((_, j) => j !== i))}
                    className="text-muted-foreground hover:text-red-600"
                    aria-label="Удалить точку"
                  >
                    ×
                  </button>
                </div>
              ))}
              <button
                onClick={() =>
                  setDraft([...draft, { t: (draft.at(-1)?.t ?? 40) + 10, pct: Math.min(100, (draft.at(-1)?.pct ?? 40) + 15) }])
                }
                className="text-sm text-blue-600 hover:underline"
              >
                + точка
              </button>
            </div>
            <div className="space-y-2">
              <CurvePreview points={[...draft].sort((a, b) => a.t - b.t)} />
              <div className="text-xs text-muted-foreground">
                Ниже {Math.round(Math.max(data?.safety.hard_floor_pct ?? 20, channel.min_pct))}% обороты
                не опустятся: это предел этого канала.
              </div>
            </div>
          </div>
          <div className="flex gap-2">
            <button
              disabled={busy || draft.length < 2}
              onClick={runPreview}
              className="rounded border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
            >
              Проверить (без записи)
            </button>
            <button
              disabled={busy || draft.length < 2 || !data?.control_enabled}
              onClick={applyCurve}
              className="rounded border border-blue-500 bg-blue-500/10 px-3 py-1.5 text-sm text-blue-500 hover:bg-blue-500/20 disabled:opacity-50"
            >
              Применить
            </button>
          </div>
          {preview && (
            <div className="text-xs font-mono text-muted-foreground">
              {preview.map((p) => `${p.t}°→${Math.round(p.pct)}%`).join("  ")}
            </div>
          )}
        </section>
      )}

      {/* --- Safety + journal -------------------------------------------- */}
      <section className="space-y-2">
        <h3 className="text-sm font-semibold">Защиты</h3>
        <div className="text-xs text-muted-foreground grid grid-cols-2 gap-x-6 gap-y-1 max-w-xl">
          <div>Нижний предел оборотов</div>
          <div>{data?.safety.hard_floor_pct}%</div>
          <div>Аварийный обгон</div>
          <div>
            {Object.entries(data?.safety.emergency_c || {})
              .map(([s, v]) => `${SENSOR_LABELS[s] || s} ${v}°C`)
              .join(" · ")}{" "}
            (держится {data?.safety.emergency_hold_s} с)
          </div>
          <div>Гистерезис / скорость снижения</div>
          <div>
            {data?.safety.temp_hysteresis_c}°C / не более {data?.safety.max_step_down_pct}% за шаг
          </div>
        </div>
      </section>

      <SetupGuide />

      <section className="space-y-2">
        <h3 className="text-sm font-semibold">Журнал</h3>
        {events.length === 0 ? (
          <div className="text-xs text-muted-foreground">Событий нет.</div>
        ) : (
          <ul className="text-xs space-y-0.5 max-h-64 overflow-y-auto">
            {events.map((e, i) => (
              <li key={i} className={levelClass(e.level)}>
                <span className="font-mono">{new Date(e.ts * 1000).toLocaleTimeString("ru-RU")}</span>{" "}
                {e.channel && <span className="font-mono">[{e.channel}]</span>} {e.message}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
