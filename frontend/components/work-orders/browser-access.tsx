"use client";

/**
 * W3: «Доступ браузера» поручения. Чтение сайтов выдаётся само при первом
 * действии; действия на сайтах (отправка форм, вход, файлы) выдаёт здесь
 * владелец поручения или руководитель, отзывает тоже здесь. Каждую отправку
 * человек всё равно подтверждает по карточке.
 */

import { useCallback, useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";

const API = getApiBaseUrl();

type Grant = {
  id: string;
  granted_by: string;
  automatic: boolean;
  actions: string[];
  allowed_hosts: string[];
  secret_ids: string[];
  max_actions: number;
  used_actions: number;
  expires_at: string;
  revoked_at: string | null;
  reason: string;
  active: boolean;
};

type Secret = {
  id: string;
  label: string;
  origin: string;
  revoked_at: string | null;
};

// Same set as READ_GRANT_ACTIONS on the backend: what reading needs.
const READ_ACTIONS = [
  "browser_fetch",
  "desktop_snapshot",
  "desktop_start",
  "desktop_observe",
  "desktop_prepare_submit",
  "desktop_read",
  "desktop_navigate",
  "desktop_tabs",
  "desktop_downloads",
  "desktop_save_download",
  "desktop_close",
  "desktop_handoff",
  "desktop_open",
  "desktop_search",
  "desktop_scroll",
  "desktop_press",
  "desktop_hover",
  "desktop_select",
  "desktop_wait",
  "desktop_back",
  "desktop_forward",
];

const PRESETS: Record<
  string,
  { label: string; extra: string[]; hint: string }
> = {
  act: {
    label: "Действия на сайтах",
    extra: ["desktop_click", "desktop_type", "desktop_upload"],
    hint: "Отправка форм и заявок, загрузка файлов. Каждую отправку вы подтверждаете по карточке.",
  },
  login: {
    label: "Вход на сайт",
    extra: [
      "desktop_click",
      "desktop_type",
      "desktop_upload",
      "desktop_fill_secret",
    ],
    hint: "То же и вход под сохранённым паролем (Настройки → Браузер). Пароль сотрудник не видит.",
  },
};

function kindOf(grant: Grant): string {
  if (grant.automatic) return "Чтение сайтов — выдано автоматически";
  if (grant.actions.includes("desktop_fill_secret")) return "Вход на сайт";
  if (
    grant.actions.includes("desktop_click") ||
    grant.actions.includes("desktop_type")
  )
    return "Действия на сайтах";
  return "Чтение";
}

function errorText(body: unknown, status: number): string {
  const d = (body as { detail?: unknown })?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d))
    return d.map((x) => (x as { msg?: string }).msg || "").join("; ");
  return `Ошибка ${status}`;
}

const input =
  "min-w-0 w-full rounded-md border border-slate-700 bg-slate-950 px-2 py-1.5 text-sm";

export function BrowserAccess({ workOrderId }: { workOrderId: string }) {
  const [grants, setGrants] = useState<Grant[] | null>(null);
  const [secrets, setSecrets] = useState<Secret[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [preset, setPreset] = useState<keyof typeof PRESETS>("act");
  const [hosts, setHosts] = useState("");
  const [maxActions, setMaxActions] = useState("100");
  const [ttlHours, setTtlHours] = useState("4");
  const [reason, setReason] = useState("");
  const [secretIds, setSecretIds] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await fetch(
        `${API}/api/work-orders/${workOrderId}/computer-grants`,
        {
          credentials: "include",
          cache: "no-store",
        },
      );
      const body = await r.json().catch(() => null);
      if (!r.ok) {
        setError(errorText(body, r.status));
        return;
      }
      setGrants(Array.isArray(body) ? (body as Grant[]) : []);
    } catch (e) {
      setError(String(e));
    }
  }, [workOrderId]);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 10000);
    return () => clearInterval(timer);
  }, [load]);

  useEffect(() => {
    if (!open || preset !== "login") return;
    void fetch(`${API}/api/browser-secrets`, {
      credentials: "include",
      cache: "no-store",
    })
      .then((r) => (r.ok ? r.json() : []))
      .then((rows: Secret[]) => setSecrets(rows.filter((s) => !s.revoked_at)))
      .catch(() => setSecrets([]));
  }, [open, preset]);

  async function grant() {
    setBusy(true);
    setError(null);
    try {
      const r = await fetch(
        `${API}/api/work-orders/${workOrderId}/computer-grants`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json", ...csrfHeaders() },
          body: JSON.stringify({
            actions: [...READ_ACTIONS, ...PRESETS[preset].extra],
            allowed_hosts: hosts
              .split(/[\s,]+/)
              .map((h) => h.trim())
              .filter(Boolean),
            max_actions: Number(maxActions) || 100,
            ttl_seconds: Math.round((Number(ttlHours) || 4) * 3600),
            reason: reason.trim() || PRESETS[preset].label,
            secret_ids: preset === "login" ? secretIds : [],
          }),
        },
      );
      const body = await r.json().catch(() => null);
      if (!r.ok) {
        setError(errorText(body, r.status));
        return;
      }
      setOpen(false);
      setHosts("");
      setReason("");
      setSecretIds([]);
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function revoke(g: Grant) {
    if (
      !window.confirm(
        `Отозвать «${kindOf(g)}»? Открытые сессии браузера закроются.`,
      )
    )
      return;
    setError(null);
    const r = await fetch(
      `${API}/api/work-orders/${workOrderId}/computer-grants/${g.id}/revoke`,
      { method: "POST", credentials: "include", headers: csrfHeaders() },
    );
    if (!r.ok) setError(errorText(await r.json().catch(() => null), r.status));
    await load();
  }

  const active = (grants || []).filter((g) => g.active);
  const past = (grants || []).filter((g) => !g.active);

  return (
    <div className="bg-slate-900 border border-slate-700 rounded-xl p-4 space-y-3 min-w-0">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-medium">Доступ браузера</h3>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="rounded-md border border-slate-600 px-3 py-1 text-xs hover:border-slate-400"
        >
          {open ? "Отмена" : "Выдать доступ"}
        </button>
      </div>
      <p className="text-xs text-slate-500">
        Читать сайты сотрудник может сам. Отправлять формы, входить и загружать
        файлы — только по доступу отсюда, и каждую отправку вы подтверждаете.
      </p>
      {error && (
        <p role="alert" className="text-xs text-red-300 break-words">
          {error}
        </p>
      )}

      {open && (
        <div
          className="space-y-2 rounded-lg border border-slate-700 p-3"
          aria-label="Новый доступ"
        >
          <label className="block text-xs">
            <span className="text-slate-400">Что разрешить</span>
            <select
              className={input}
              value={preset}
              onChange={(e) =>
                setPreset(e.target.value as keyof typeof PRESETS)
              }
            >
              {Object.entries(PRESETS).map(([key, p]) => (
                <option key={key} value={key}>
                  {p.label}
                </option>
              ))}
            </select>
          </label>
          <p className="text-xs text-slate-500">{PRESETS[preset].hint}</p>
          <label className="block text-xs">
            <span className="text-slate-400">
              Сайты (через запятую; * — любые)
            </span>
            <input
              className={`${input} font-mono`}
              value={hosts}
              onChange={(e) => setHosts(e.target.value)}
              placeholder="portal.supplier.ru"
            />
          </label>
          {preset === "login" && (
            <fieldset className="text-xs space-y-1">
              <legend className="text-slate-400">Пароли</legend>
              {secrets.length === 0 ? (
                <p className="text-slate-500">
                  Нет сохранённых паролей — добавьте в «Настройки → Браузер».
                </p>
              ) : (
                secrets.map((s) => (
                  <label
                    key={s.id}
                    className="flex items-center gap-2 break-all"
                  >
                    <input
                      type="checkbox"
                      checked={secretIds.includes(s.id)}
                      onChange={(e) =>
                        setSecretIds((ids) =>
                          e.target.checked
                            ? [...ids, s.id]
                            : ids.filter((x) => x !== s.id),
                        )
                      }
                    />
                    {s.label} · {s.origin}
                  </label>
                ))
              )}
            </fieldset>
          )}
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="block text-xs">
              <span className="text-slate-400">Действий не больше</span>
              <input
                className={input}
                type="number"
                min={1}
                max={1000}
                value={maxActions}
                onChange={(e) => setMaxActions(e.target.value)}
              />
            </label>
            <label className="block text-xs">
              <span className="text-slate-400">На сколько часов</span>
              <input
                className={input}
                type="number"
                min={1}
                max={24}
                value={ttlHours}
                onChange={(e) => setTtlHours(e.target.value)}
              />
            </label>
          </div>
          <label className="block text-xs">
            <span className="text-slate-400">Зачем (для журнала)</span>
            <input
              className={input}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </label>
          <button
            type="button"
            onClick={() => void grant()}
            disabled={busy || !hosts.trim()}
            className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
          >
            Выдать
          </button>
        </div>
      )}

      {grants !== null && active.length === 0 && (
        <p className="text-xs text-slate-500">Активных доступов нет.</p>
      )}
      <ul className="space-y-2" aria-label="Доступы браузера">
        {active.map((g) => (
          <li
            key={g.id}
            className="rounded-lg border border-slate-700 p-2 text-xs space-y-1"
          >
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{kindOf(g)}</span>
              <span className="text-slate-400 break-all">
                {g.allowed_hosts.includes("*")
                  ? "любые сайты"
                  : g.allowed_hosts.join(", ")}
              </span>
              <button
                type="button"
                onClick={() => void revoke(g)}
                className="ml-auto rounded-md border border-slate-600 px-2 py-0.5 hover:border-red-400"
              >
                Отозвать
              </button>
            </div>
            <div className="text-slate-500">
              {g.used_actions} из {g.max_actions} действий · до{" "}
              {new Date(g.expires_at).toLocaleString("ru-RU")}
            </div>
          </li>
        ))}
      </ul>
      {past.length > 0 && (
        <details className="text-xs text-slate-500">
          <summary>Прошлые доступы: {past.length}</summary>
          <ul className="mt-1 space-y-1">
            {past.map((g) => (
              <li key={g.id} className="break-words">
                {kindOf(g)} · {g.revoked_at ? "отозван" : "закончился"} ·{" "}
                {g.used_actions}/{g.max_actions}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
