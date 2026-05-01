"use client";

import { getApiBaseUrl } from "@/lib/api-base";

import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

const API = getApiBaseUrl();

type AgentWsMode = "legacy" | "openclaw";
type GatewayBind = "loopback" | "lan" | "tailnet" | "auto" | "custom";
type GatewayAuth = "none" | "token" | "password" | "trusted-proxy";
type TelegramDmPolicy = "pairing" | "allowlist" | "open" | "disabled";
type SessionDmScope =
  | "main"
  | "per-peer"
  | "per-channel-peer"
  | "per-account-channel-peer";

interface OpenClawSettings {
  first_run_completed: boolean;
  agent_ws_mode: AgentWsMode;
  openclaw_ws_url: string;
  openclaw_http_url: string;
  legacy_ws_url: string;
  fallback_to_legacy: boolean;
  gateway_bind: GatewayBind;
  gateway_auth: GatewayAuth;
  gateway_token_configured: boolean;
  gateway_token_env: string;
  dashboard_url: string;
  strict_allowlist: boolean;
  model_primary: string;
  model_fallbacks: string[];
  model_allowlist: string[];
  image_max_dimension_px: number;
  telegram_enabled: boolean;
  telegram_bot_token_configured: boolean;
  telegram_bot_token_env: string;
  telegram_dm_policy: TelegramDmPolicy;
  telegram_allow_from: string[];
  telegram_groups_require_mention: boolean;
  session_dm_scope: SessionDmScope;
  notes: string;
}

interface OpenClawStatus {
  settings: OpenClawSettings;
  gateway_available: boolean;
  gateway_status: string;
  gateway_detail: Record<string, unknown> | null;
  registry_tools: number;
  approval_gates: number;
  supported_scenarios: string[];
  official_config_available: boolean;
  official_config_path: string | null;
  config_warnings: string[];
  control_available: boolean;
  control_note: string;
}

interface OfficialConfigResult {
  written: boolean;
  path: string | null;
  config: Record<string, unknown>;
  warnings: string[];
}

const emptySettings: OpenClawSettings = {
  first_run_completed: false,
  agent_ws_mode: "legacy",
  openclaw_ws_url: "ws://localhost:18789",
  openclaw_http_url: "http://localhost:18789",
  legacy_ws_url: "ws://localhost:8000/ws/chat",
  fallback_to_legacy: true,
  gateway_bind: "lan",
  gateway_auth: "token",
  gateway_token_configured: false,
  gateway_token_env: "OPENCLAW_GATEWAY_TOKEN",
  dashboard_url: "http://localhost:18789/",
  strict_allowlist: true,
  model_primary: "",
  model_fallbacks: [],
  model_allowlist: [],
  image_max_dimension_px: 1200,
  telegram_enabled: false,
  telegram_bot_token_configured: false,
  telegram_bot_token_env: "TELEGRAM_BOT_TOKEN",
  telegram_dm_policy: "pairing",
  telegram_allow_from: [],
  telegram_groups_require_mention: true,
  session_dm_scope: "per-channel-peer",
  notes: "",
};

function toLines(values: string[]): string {
  return values.join("\n");
}

function fromLines(value: string): string[] {
  return value
    .split(/\r?\n|,/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export default function OpenClawSettingsPage() {
  const [settings, setSettings] = useState<OpenClawSettings>(emptySettings);
  const [status, setStatus] = useState<OpenClawStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [applyResult, setApplyResult] = useState<OfficialConfigResult | null>(null);

  const readiness = useMemo(() => {
    const items = [
      settings.model_primary ? "Модель выбрана" : "Не выбрана основная модель",
      settings.gateway_token_configured || settings.gateway_auth === "none"
        ? "Gateway auth готов"
        : "Не отмечен Gateway token",
      settings.telegram_enabled
        ? settings.telegram_bot_token_configured
          ? "Telegram token готов"
          : "Telegram включен без token"
        : "Telegram выключен",
      status?.registry_tools ? "Skill registry загружен" : "Skill registry не найден",
      status?.official_config_available
        ? "Official config доступен"
        : "Official config не смонтирован",
    ];
    return items;
  }, [settings, status]);

  async function load() {
    setError(null);
    try {
      const [settingsResponse, statusResponse] = await Promise.all([
        fetch(`${API}/api/openclaw/settings`),
        fetch(`${API}/api/openclaw/status`),
      ]);
      if (!settingsResponse.ok) throw new Error(await settingsResponse.text());
      setSettings(await settingsResponse.json());
      if (statusResponse.ok) setStatus(await statusResponse.json());
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  async function save(nextSettings = settings) {
    setSaving(true);
    setError(null);
    try {
      const response = await fetch(`${API}/api/openclaw/settings`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(nextSettings),
      });
      if (!response.ok) throw new Error(await response.text());
      setSettings(await response.json());
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
      await refreshStatus();
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function reset() {
    setSaving(true);
    setError(null);
    try {
      const response = await fetch(`${API}/api/openclaw/settings/reset`, {
        method: "POST",
      });
      if (!response.ok) throw new Error(await response.text());
      setSettings(await response.json());
      setApplyResult(null);
      await refreshStatus();
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function refreshStatus() {
    const response = await fetch(`${API}/api/openclaw/status`);
    if (response.ok) setStatus(await response.json());
  }

  async function applyOfficialConfig() {
    setSaving(true);
    setError(null);
    setApplyResult(null);
    try {
      await save(settings);
      const response = await fetch(`${API}/api/openclaw/official-config/apply`, {
        method: "POST",
      });
      if (!response.ok) throw new Error(await response.text());
      const result = (await response.json()) as OfficialConfigResult;
      setApplyResult(result);
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  if (loading) {
    return <div className="p-6 text-sm text-slate-400">Загрузка...</div>;
  }

  return (
    <div className="mx-auto max-w-6xl space-y-6 p-6">
      <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-100">OpenClaw</h1>
          <p className="mt-1 text-sm text-slate-400">
            Первый запуск, Gateway, модели, Telegram и strict-интеграция.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            onClick={refreshStatus}
            className="rounded-md bg-slate-700 px-3 py-2 text-sm text-slate-100 hover:bg-slate-600"
          >
            Проверить
          </button>
          <button
            onClick={() => save()}
            disabled={saving}
            className="rounded-md bg-blue-600 px-4 py-2 text-sm text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {saved ? "Сохранено" : saving ? "Сохранение..." : "Сохранить"}
          </button>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-red-800 bg-red-950/40 p-3 text-sm text-red-300">
          {error}
        </div>
      )}

      <section className="rounded-lg border border-slate-700 bg-slate-800 p-5">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-5">
          <Metric
            label="Первый запуск"
            value={settings.first_run_completed ? "завершен" : "требуется"}
            accent={settings.first_run_completed ? "text-emerald-400" : "text-amber-400"}
          />
          <Metric
            label="Gateway"
            value={status?.gateway_available ? "online" : "offline"}
            accent={status?.gateway_available ? "text-emerald-400" : "text-amber-400"}
          />
          <Metric label="Статус" value={status?.gateway_status ?? "-"} />
          <Metric label="Tools" value={String(status?.registry_tools ?? "-")} />
          <Metric label="Approval gates" value={String(status?.approval_gates ?? "-")} />
        </div>
        <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-2">
          <div className="rounded-md bg-slate-900/60 p-3 text-xs text-slate-400">
            <div className="mb-2 font-semibold text-slate-200">Готовность</div>
            {readiness.map((item) => (
              <div key={item}>{item}</div>
            ))}
          </div>
          <div className="rounded-md bg-slate-900/60 p-3 text-xs text-slate-400">
            <div className="mb-2 font-semibold text-slate-200">Предупреждения</div>
            {(status?.config_warnings.length ? status.config_warnings : ["Нет"]).map(
              (warning) => (
                <div key={warning}>{warning}</div>
              ),
            )}
          </div>
        </div>
        <div className="mt-3 text-xs text-slate-500">
          Сценарии: {status?.supported_scenarios.join(", ") || "-"}
        </div>
      </section>

      <Section title="Чат и Gateway">
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <SelectField
            label="Режим чата"
            value={settings.agent_ws_mode}
            values={[
              ["legacy", "FastAPI legacy"],
              ["openclaw", "Official OpenClaw Gateway"],
            ]}
            onChange={(agent_ws_mode) =>
              setSettings({ ...settings, agent_ws_mode: agent_ws_mode as AgentWsMode })
            }
          />
          <SelectField
            label="Bind Gateway"
            value={settings.gateway_bind}
            values={[
              ["lan", "lan"],
              ["loopback", "loopback"],
              ["tailnet", "tailnet"],
              ["auto", "auto"],
              ["custom", "custom"],
            ]}
            onChange={(gateway_bind) =>
              setSettings({ ...settings, gateway_bind: gateway_bind as GatewayBind })
            }
          />
          <SelectField
            label="Auth Gateway"
            value={settings.gateway_auth}
            values={[
              ["token", "token"],
              ["none", "none"],
              ["password", "password"],
              ["trusted-proxy", "trusted-proxy"],
            ]}
            onChange={(gateway_auth) =>
              setSettings({ ...settings, gateway_auth: gateway_auth as GatewayAuth })
            }
          />
          <TextField
            label="Env для Gateway token"
            value={settings.gateway_token_env}
            onChange={(gateway_token_env) =>
              setSettings({ ...settings, gateway_token_env })
            }
          />
          <TextField
            label="OpenClaw WebSocket URL"
            value={settings.openclaw_ws_url}
            onChange={(openclaw_ws_url) =>
              setSettings({ ...settings, openclaw_ws_url })
            }
          />
          <TextField
            label="OpenClaw HTTP URL"
            value={settings.openclaw_http_url}
            onChange={(openclaw_http_url) =>
              setSettings({ ...settings, openclaw_http_url })
            }
          />
          <TextField
            label="Legacy WebSocket URL"
            value={settings.legacy_ws_url}
            onChange={(legacy_ws_url) =>
              setSettings({ ...settings, legacy_ws_url })
            }
          />
          <TextField
            label="Dashboard URL"
            value={settings.dashboard_url}
            onChange={(dashboard_url) =>
              setSettings({ ...settings, dashboard_url })
            }
          />
        </div>
        <div className="mt-4 flex flex-wrap gap-3">
          <Toggle
            label="Fallback в FastAPI"
            checked={settings.fallback_to_legacy}
            onChange={(fallback_to_legacy) =>
              setSettings({ ...settings, fallback_to_legacy })
            }
          />
          <Toggle
            label="Strict allowlist"
            checked={settings.strict_allowlist}
            onChange={(strict_allowlist) =>
              setSettings({ ...settings, strict_allowlist })
            }
          />
          <Toggle
            label="Gateway token настроен"
            checked={settings.gateway_token_configured}
            onChange={(gateway_token_configured) =>
              setSettings({ ...settings, gateway_token_configured })
            }
          />
        </div>
      </Section>

      <Section title="Модели">
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <TextField
            label="Основная модель"
            value={settings.model_primary}
            onChange={(model_primary) =>
              setSettings({ ...settings, model_primary })
            }
          />
          <NumberField
            label="Максимальный размер изображения, px"
            value={settings.image_max_dimension_px}
            onChange={(image_max_dimension_px) =>
              setSettings({ ...settings, image_max_dimension_px })
            }
          />
          <TextAreaField
            label="Fallback-модели"
            value={toLines(settings.model_fallbacks)}
            onChange={(value) =>
              setSettings({ ...settings, model_fallbacks: fromLines(value) })
            }
          />
          <TextAreaField
            label="Allowlist моделей"
            value={toLines(settings.model_allowlist)}
            onChange={(value) =>
              setSettings({ ...settings, model_allowlist: fromLines(value) })
            }
          />
        </div>
      </Section>

      <Section title="Telegram">
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <TextField
            label="Env для Telegram token"
            value={settings.telegram_bot_token_env}
            onChange={(telegram_bot_token_env) =>
              setSettings({ ...settings, telegram_bot_token_env })
            }
          />
          <SelectField
            label="DM policy"
            value={settings.telegram_dm_policy}
            values={[
              ["pairing", "pairing"],
              ["allowlist", "allowlist"],
              ["open", "open"],
              ["disabled", "disabled"],
            ]}
            onChange={(telegram_dm_policy) =>
              setSettings({
                ...settings,
                telegram_dm_policy: telegram_dm_policy as TelegramDmPolicy,
              })
            }
          />
          <TextAreaField
            label="Allow from"
            value={toLines(settings.telegram_allow_from)}
            onChange={(value) =>
              setSettings({ ...settings, telegram_allow_from: fromLines(value) })
            }
          />
          <SelectField
            label="Область DM-сессий"
            value={settings.session_dm_scope}
            values={[
              ["per-channel-peer", "per-channel-peer"],
              ["per-account-channel-peer", "per-account-channel-peer"],
              ["per-peer", "per-peer"],
              ["main", "main"],
            ]}
            onChange={(session_dm_scope) =>
              setSettings({
                ...settings,
                session_dm_scope: session_dm_scope as SessionDmScope,
              })
            }
          />
        </div>
        <div className="mt-4 flex flex-wrap gap-3">
          <Toggle
            label="Telegram включен"
            checked={settings.telegram_enabled}
            onChange={(telegram_enabled) =>
              setSettings({ ...settings, telegram_enabled })
            }
          />
          <Toggle
            label="Telegram token настроен"
            checked={settings.telegram_bot_token_configured}
            onChange={(telegram_bot_token_configured) =>
              setSettings({ ...settings, telegram_bot_token_configured })
            }
          />
          <Toggle
            label="В группах требовать mention"
            checked={settings.telegram_groups_require_mention}
            onChange={(telegram_groups_require_mention) =>
              setSettings({ ...settings, telegram_groups_require_mention })
            }
          />
        </div>
      </Section>

      <Section title="Official config">
        <div className="flex flex-wrap gap-2">
          <button
            onClick={applyOfficialConfig}
            disabled={saving}
            className="rounded-md bg-emerald-600 px-4 py-2 text-sm text-white hover:bg-emerald-700 disabled:opacity-50"
          >
            Применить official config
          </button>
          <button
            onClick={() => {
              const next = { ...settings, first_run_completed: true };
              setSettings(next);
              save(next);
            }}
            disabled={saving}
            className="rounded-md bg-slate-700 px-3 py-2 text-sm text-slate-100 hover:bg-slate-600 disabled:opacity-50"
          >
            Завершить первый запуск
          </button>
          <button
            onClick={reset}
            disabled={saving}
            className="rounded-md bg-slate-700 px-3 py-2 text-sm text-slate-100 hover:bg-slate-600 disabled:opacity-50"
          >
            Сбросить настройки
          </button>
        </div>
        <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-2">
          <Command value="make openclaw-official-up" />
          <Command value="make openclaw-official-down" />
          <Command value="make openclaw-official-logs" />
          <Command value="make openclaw-official-dashboard" />
        </div>
        <div className="mt-3 text-xs text-slate-500">
          Config path: {status?.official_config_path || "-"}
        </div>
        {applyResult && (
          <div className="mt-3 rounded-md bg-slate-900/60 p-3 text-xs text-slate-300">
            {applyResult.written
              ? `Записано: ${applyResult.path}`
              : "Config не записан"}
            {applyResult.warnings.length > 0 && (
              <div className="mt-2 text-amber-300">
                {applyResult.warnings.join("; ")}
              </div>
            )}
          </div>
        )}
        <p className="mt-3 text-xs text-slate-500">{status?.control_note}</p>
      </Section>

      <Section title="Заметки">
        <TextAreaField
          label="Заметки оператора"
          value={settings.notes}
          rows={4}
          onChange={(notes) => setSettings({ ...settings, notes })}
        />
      </Section>
    </div>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <section className="rounded-lg border border-slate-700 bg-slate-800 p-5">
      <h2 className="mb-4 text-lg font-semibold text-slate-100">{title}</h2>
      {children}
    </section>
  );
}

function Metric({
  label,
  value,
  accent = "text-slate-100",
}: {
  label: string;
  value: string;
  accent?: string;
}) {
  return (
    <div className="rounded-md bg-slate-900/60 p-3">
      <p className="text-xs text-slate-500">{label}</p>
      <p className={`mt-1 truncate text-sm font-semibold ${accent}`}>{value}</p>
    </div>
  );
}

function TextField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm text-slate-300">{label}</span>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-100"
      />
    </label>
  );
}

function NumberField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm text-slate-300">{label}</span>
      <input
        type="number"
        min={256}
        max={8192}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-100"
      />
    </label>
  );
}

function TextAreaField({
  label,
  value,
  rows = 3,
  onChange,
}: {
  label: string;
  value: string;
  rows?: number;
  onChange: (value: string) => void;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm text-slate-300">{label}</span>
      <textarea
        value={value}
        rows={rows}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-100"
      />
    </label>
  );
}

function SelectField({
  label,
  value,
  values,
  onChange,
}: {
  label: string;
  value: string;
  values: [string, string][];
  onChange: (value: string) => void;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm text-slate-300">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-100"
      >
        {values.map(([optionValue, labelText]) => (
          <option key={optionValue} value={optionValue}>
            {labelText}
          </option>
        ))}
      </select>
    </label>
  );
}

function Toggle({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <label className="flex items-center gap-2 rounded-md bg-slate-900/60 px-3 py-2 text-sm text-slate-200">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      {label}
    </label>
  );
}

function Command({ value }: { value: string }) {
  return (
    <code className="block rounded-md bg-slate-950 px-3 py-2 text-xs text-slate-300">
      {value}
    </code>
  );
}
