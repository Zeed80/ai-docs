"use client";

/**
 * W2: «каким браузером» сотрудник видится сайтам — постоянные профили
 * (cookies пройденных проверок и входов), пул прокси, к которым профили
 * привязаны навсегда, и самопроверка того, что видит детектор ботов.
 */

import { useCallback, useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";

const BASE = `${getApiBaseUrl()}/api/web-search`;

type Profile = {
  name: string;
  size_bytes: number;
  created: number | null;
  last_used: number | null;
  proxy_id: string | null;
  open: boolean;
  in_use: boolean;
};

type ProxyView = {
  id: string;
  server: string;
  username: string;
  password_mask: string;
  password_set: boolean;
  kind: string;
  country: string | null;
  locale: string | null;
  timezone: string | null;
  enabled: boolean;
};

type ProxyDraft = {
  id: string;
  server: string;
  username: string;
  password: string;
  passwordSet: boolean;
  passwordMask: string;
  kind: string;
  country: string;
  locale: string;
  timezone: string;
  enabled: boolean;
};

type Selftest = {
  ok: boolean;
  error: string | null;
  url: string;
  channel: string;
  headless: boolean;
  probe: Record<string, unknown>;
  failed: { test: string; result: string }[];
  checked: number;
};

const field =
  "min-w-0 w-full rounded-md border border-border bg-background px-3 py-2 text-sm " +
  "focus:outline-none focus:ring-2 focus:ring-primary";

const PROFILE_LABELS: Record<string, string> = {
  reader: "Чтение страниц",
  login: "Сайты со входом",
};

function detail(body: unknown, status: number): string {
  const d = (body as { detail?: unknown })?.detail;
  if (typeof d === "string") return d;
  if (d && typeof d === "object" && "message" in d)
    return String((d as { message: unknown }).message);
  return `Ошибка ${status}`;
}

function size(bytes: number): string {
  if (bytes > 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} МБ`;
  return `${Math.max(1, Math.round(bytes / 1024))} КБ`;
}

function toDraft(p: ProxyView): ProxyDraft {
  return {
    id: p.id,
    server: p.server,
    username: p.username,
    password: "",
    passwordSet: p.password_set,
    passwordMask: p.password_mask,
    kind: p.kind,
    country: p.country || "",
    locale: p.locale || "",
    timezone: p.timezone || "",
    enabled: p.enabled,
  };
}

async function call(path: string, init?: RequestInit) {
  const r = await fetch(`${BASE}${path}`, {
    credentials: "include",
    cache: "no-store",
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...csrfHeaders(),
      ...(init?.headers || {}),
    },
  });
  const body = await r.json().catch(() => null);
  if (!r.ok) throw new Error(detail(body, r.status));
  return body;
}

export function BrowserIdentity() {
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [profiles, setProfiles] = useState<Profile[] | null>(null);
  const [proxies, setProxies] = useState<ProxyDraft[]>([]);
  const [locale, setLocale] = useState("ru-RU");
  const [timezone, setTimezone] = useState("Europe/Moscow");
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [report, setReport] = useState<Selftest | null>(null);

  const load = useCallback(async () => {
    try {
      const [listed, settings] = await Promise.all([
        call("/browser/profiles").catch((e) => {
          setError(String((e as Error).message));
          return { profiles: [] };
        }),
        call("/settings"),
      ]);
      setProfiles(listed.profiles || []);
      setProxies((settings.proxies || []).map(toDraft));
      setLocale(settings.browser_locale || "ru-RU");
      setTimezone(settings.browser_timezone || "Europe/Moscow");
    } catch (e) {
      setError(String((e as Error).message));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function forget(p: Profile) {
    if (
      !window.confirm(
        `Очистить профиль «${PROFILE_LABELS[p.name] || p.name}»?\n\n` +
          "Сайты снова увидят нового посетителя: пройденные проверки, согласия и входы пропадут.",
      )
    )
      return;
    setError(null);
    try {
      await call(`/browser/profiles/${encodeURIComponent(p.name)}`, {
        method: "DELETE",
      });
      setNotice("Профиль очищен.");
      await load();
    } catch (e) {
      setError(String((e as Error).message));
    }
  }

  function update(i: number, patch: Partial<ProxyDraft>) {
    setProxies((list) =>
      list.map((p, j) => (j === i ? { ...p, ...patch } : p)),
    );
  }

  async function saveProxies() {
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const body = await call("/settings", {
        method: "PATCH",
        body: JSON.stringify({
          browser_locale: locale.trim(),
          browser_timezone: timezone.trim(),
          proxies: proxies.map((p) => ({
            id: p.id.trim(),
            server: p.server.trim(),
            username: p.username.trim(),
            ...(p.password ? { password: p.password } : {}),
            kind: p.kind,
            country: p.country.trim() || null,
            locale: p.locale.trim() || null,
            timezone: p.timezone.trim() || null,
            enabled: p.enabled,
          })),
        }),
      });
      setProxies((body.proxies || []).map(toDraft));
      setNotice("Прокси сохранены.");
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setSaving(false);
    }
  }

  async function selftest() {
    setTesting(true);
    setReport(null);
    setError(null);
    try {
      setReport(
        await call("/browser/selftest", { method: "POST", body: "{}" }),
      );
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setTesting(false);
    }
  }

  return (
    <div className="max-w-3xl min-w-0 space-y-6">
      <div>
        <h2 className="text-lg font-semibold">Как сайты видят сотрудника</h2>
        <p className="mt-1 break-words text-sm text-muted-foreground">
          Сотрудник открывает сайты в настоящем Chrome и возвращается к ним как
          тот же посетитель: профиль хранит пройденные проверки на бота,
          согласия и входы. Профиль закреплён за одним прокси, и его язык и
          часовой пояс совпадают с прокси. CAPTCHA сотрудник не решает — он
          передаёт сессию вам.
        </p>
      </div>

      {error && (
        <p role="alert" className="break-words text-sm text-red-500">
          {error}
        </p>
      )}
      {notice && <p className="text-sm text-green-600">{notice}</p>}

      <section className="space-y-2" aria-label="Профили браузера">
        <h3 className="font-medium">Ваши профили</h3>
        {profiles === null ? null : profiles.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Профилей пока нет — появятся при первом чтении страницы.
          </p>
        ) : (
          <ul className="divide-y divide-border rounded-md border border-border">
            {profiles.map((p) => (
              <li
                key={p.name}
                className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2"
              >
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium">
                    {PROFILE_LABELS[p.name] || p.name}
                  </div>
                  <div className="break-words text-xs text-muted-foreground">
                    {size(p.size_bytes)}
                    {p.last_used &&
                      ` · последний раз ${new Date(p.last_used * 1000).toLocaleString("ru-RU")}`}
                    {` · ${p.proxy_id ? `прокси ${p.proxy_id}` : "напрямую"}`}
                    {p.in_use && " · сейчас открыт"}
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => void forget(p)}
                  disabled={p.in_use}
                  className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
                >
                  Очистить
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="space-y-3" aria-label="Прокси браузера">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="font-medium">Прокси</h3>
          <button
            type="button"
            onClick={() =>
              setProxies((list) => [
                ...list,
                {
                  id: `proxy-${list.length + 1}`,
                  server: "",
                  username: "",
                  password: "",
                  passwordSet: false,
                  passwordMask: "",
                  kind: "residential",
                  country: "",
                  locale: "",
                  timezone: "",
                  enabled: true,
                },
              ])
            }
            className="rounded-md border border-border px-3 py-1.5 text-sm"
          >
            Добавить прокси
          </button>
        </div>
        <p className="text-xs text-muted-foreground">
          Без прокси сайты видят адрес сервера. Домашние и мобильные адреса
          блокируют реже. Нужен HTTP-прокси с методом CONNECT; запрещённые
          адреса отсекаются до прокси.
        </p>
        {proxies.map((p, i) => (
          <div
            key={i}
            className="space-y-2 rounded-lg border border-border p-3 min-w-0"
          >
            <div className="flex flex-wrap items-center gap-3">
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={p.enabled}
                  onChange={(e) => update(i, { enabled: e.target.checked })}
                />
                Включён
              </label>
              <select
                value={p.kind}
                onChange={(e) => update(i, { kind: e.target.value })}
                aria-label="Тип прокси"
                className="rounded-md border border-border bg-background px-2 py-1 text-sm"
              >
                <option value="residential">домашний</option>
                <option value="mobile">мобильный</option>
                <option value="datacenter">серверный</option>
              </select>
              <button
                type="button"
                onClick={() =>
                  setProxies((list) => list.filter((_, j) => j !== i))
                }
                className="ml-auto rounded-md border border-border px-3 py-1 text-xs text-red-600"
              >
                Удалить
              </button>
            </div>
            <div className="grid gap-2 sm:grid-cols-2">
              <input
                className={field}
                aria-label="Имя прокси"
                value={p.id}
                onChange={(e) => update(i, { id: e.target.value })}
              />
              <input
                className={`${field} font-mono`}
                aria-label="Адрес прокси"
                placeholder="http://host:port"
                value={p.server}
                onChange={(e) => update(i, { server: e.target.value })}
              />
              <input
                className={field}
                aria-label="Логин прокси"
                placeholder="Логин"
                autoComplete="off"
                value={p.username}
                onChange={(e) => update(i, { username: e.target.value })}
              />
              <input
                className={field}
                type="password"
                autoComplete="new-password"
                aria-label="Пароль прокси"
                placeholder={
                  p.passwordSet ? `сохранён: ${p.passwordMask}` : "Пароль"
                }
                value={p.password}
                onChange={(e) => update(i, { password: e.target.value })}
              />
              <input
                className={field}
                aria-label="Страна"
                placeholder="Страна (RU)"
                value={p.country}
                onChange={(e) => update(i, { country: e.target.value })}
              />
              <input
                className={field}
                aria-label="Язык профиля"
                placeholder={`Язык (${locale})`}
                value={p.locale}
                onChange={(e) => update(i, { locale: e.target.value })}
              />
              <input
                className={`${field} sm:col-span-2`}
                aria-label="Часовой пояс"
                placeholder={`Часовой пояс (${timezone})`}
                value={p.timezone}
                onChange={(e) => update(i, { timezone: e.target.value })}
              />
            </div>
          </div>
        ))}
        <div className="grid gap-2 sm:grid-cols-2">
          <label className="block text-sm">
            <span className="text-muted-foreground">Язык без прокси</span>
            <input
              className={field}
              value={locale}
              onChange={(e) => setLocale(e.target.value)}
            />
          </label>
          <label className="block text-sm">
            <span className="text-muted-foreground">
              Часовой пояс без прокси
            </span>
            <input
              className={field}
              value={timezone}
              onChange={(e) => setTimezone(e.target.value)}
            />
          </label>
        </div>
        <button
          type="button"
          onClick={() => void saveProxies()}
          disabled={saving}
          className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
        >
          {saving ? "Сохранение…" : "Сохранить прокси"}
        </button>
      </section>

      <section className="space-y-2" aria-label="Самопроверка браузера">
        <div className="flex flex-wrap items-center gap-3">
          <h3 className="font-medium">Самопроверка</h3>
          <button
            type="button"
            onClick={() => void selftest()}
            disabled={testing}
            className="rounded-md border border-border px-3 py-1.5 text-sm disabled:opacity-50"
          >
            {testing ? "Проверка…" : "Проверить, что видят сайты"}
          </button>
        </div>
        <p className="text-xs text-muted-foreground">
          Открывает bot.sannysoft.com в вашем профиле через ваш прокси. Стоит
          запускать после обновления браузера.
        </p>
        {report && (
          <div className="space-y-2 rounded-lg border border-border p-3 text-sm min-w-0">
            <div>
              {report.failed.length === 0 ? "✅" : "⚠️"} Проверок:{" "}
              {report.checked}, не пройдено: {report.failed.length} ·{" "}
              {report.channel}
              {report.headless ? " · без окна" : " · в окне"}
            </div>
            {report.error && (
              <p className="break-words text-xs text-amber-600">
                Тестовый сайт не открылся ({report.error}) — ниже только то,
                что браузер сообщает о себе.
              </p>
            )}
            {report.failed.length > 0 && (
              <ul className="list-disc pl-5 text-xs">
                {report.failed.map((f) => (
                  <li key={f.test} className="break-words">
                    {f.test}: {f.result}
                  </li>
                ))}
              </ul>
            )}
            <dl className="grid grid-cols-1 gap-x-3 gap-y-1 text-xs sm:grid-cols-[max-content_1fr]">
              {Object.entries(report.probe).map(([k, v]) => (
                <div key={k} className="contents">
                  <dt className="text-muted-foreground">{k}</dt>
                  <dd className="break-all font-mono">{JSON.stringify(v)}</dd>
                </div>
              ))}
            </dl>
          </div>
        )}
      </section>
    </div>
  );
}
