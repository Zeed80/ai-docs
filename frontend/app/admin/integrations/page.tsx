"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { ProtectedRoute } from "@/components/auth/protected-route";

const API = getApiBaseUrl();

interface AuthentikIntegration {
  auth_enabled: boolean;
  external_url: string;
  admin_url: string;
  token_set: boolean;
  token_hint: string;
}

interface MailServerIntegration {
  configured: boolean;
  api_url: string | null;
  api_key_set: boolean;
  api_key_hint: string;
  mail_domain: string | null;
  webmail_url: string | null;
  imap_host: string | null;
  imap_port: number;
  smtp_host: string | null;
  smtp_port: number;
  default_quota_mb: number;
  verified?: boolean | null;
  verify_detail?: string | null;
}

interface DeployJob {
  status: "idle" | "requested" | "running" | "done" | "error";
  mail_domain: string | null;
  tag: string | null;
  current_step: string | null;
  log_tail: string;
  error: string | null;
  requested_by?: string | null;
}

interface DeployStatus {
  installed: boolean;
  agent_available: boolean;
  job: DeployJob | null;
  default_tag: string;
  suggested_domain: string | null;
  note: string | null;
}

/** Deployment of the mail server itself (a separate compose project).
 *
 * The backend cannot run docker compose, so the button only files a request; a
 * host agent executes infra/installer/install-mailcow.sh and streams progress
 * back. Everything it cannot do — DNS, firewall, DKIM, API key — is listed in
 * the linked guide, so the operator is never left guessing what is still manual.
 */
function MailcowDeploySection({ onDeployed }: { onDeployed: () => void }) {
  const [state, setState] = useState<DeployStatus | null>(null);
  const [domain, setDomain] = useState("");
  const [tz, setTz] = useState("Europe/Moscow");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  const wasRunning = useRef(false);

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/admin/mail-server/deploy/status`, {
        credentials: "include",
      });
      if (!res.ok) return;
      const d: DeployStatus = await res.json();
      setState(d);
      setDomain((v) => v || d.suggested_domain || "");
      const active =
        d.job?.status === "requested" || d.job?.status === "running";
      if (wasRunning.current && !active) {
        wasRunning.current = false;
        onDeployed(); // installation finished — refresh the connection form
      }
      if (active) wasRunning.current = true;
    } catch {
      /* сеть моргнула — следующий опрос покажет актуальное состояние */
    }
  }, [onDeployed]);

  useEffect(() => {
    load();
    timer.current = setInterval(load, 5000);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [load]);

  async function deploy() {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const res = await fetch(`${API}/api/admin/mail-server/deploy`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", ...csrfHeaders() },
        body: JSON.stringify({
          mail_domain: domain.trim(),
          timezone: tz.trim(),
        }),
      });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(d.detail ?? `HTTP ${res.status}`);
      setNote(d.note ?? null);
      await load();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function post(path: string) {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API}/api/admin/mail-server/deploy/${path}`, {
        method: "POST",
        credentials: "include",
        headers: csrfHeaders(),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${res.status}`);
      }
      await load();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  if (!state) return null;

  const job = state.job;
  const active = job?.status === "requested" || job?.status === "running";
  const guide = (
    <a
      href="/admin/integrations/mailcow-guide"
      className="text-primary hover:underline"
    >
      руководство по ручным шагам
    </a>
  );

  return (
    <div className="rounded-lg border border-border p-4 space-y-3">
      <h2 className="text-base font-semibold">
        Развёртывание почтового сервера
      </h2>

      {state.installed && !active ? (
        <p className="text-xs text-green-600">
          Mailcow развёрнут{job?.mail_domain ? ` (${job.mail_domain})` : ""}.
          Обновления — раздел «Обновления»; что настраивается вручную — {guide}.
        </p>
      ) : (
        <p className="text-xs text-muted-foreground">
          Разворачивает Mailcow (Postfix + Dovecot + Rspamd + SOGo) отдельным
          compose-проектом, подключает его к нашему Traefik и копирует
          TLS-сертификат на почтовые порты. DNS-записи, порты фаервола, DKIM и
          API-ключ придётся настроить руками — см. {guide}.
        </p>
      )}

      {!state.agent_available && state.note && (
        <p className="text-xs text-amber-600 bg-amber-50 dark:bg-amber-950 rounded px-2 py-1">
          {state.note}
        </p>
      )}

      {!state.installed && !active && (
        <div className="space-y-2">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className="text-xs text-muted-foreground block mb-1">
                Хост почтового сервера
              </label>
              <input
                type="text"
                value={domain}
                onChange={(e) => setDomain(e.target.value)}
                placeholder="mail.example.com"
                className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background font-mono"
              />
            </div>
            <div>
              <label className="text-xs text-muted-foreground block mb-1">
                Часовой пояс
              </label>
              <input
                type="text"
                value={tz}
                onChange={(e) => setTz(e.target.value)}
                className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background"
              />
            </div>
          </div>
          <p className="text-xs text-amber-600">
            Перед запуском заведите A-запись для этого хоста — без неё Traefik
            не получит сертификат, и почтовые клиенты не подключатся.
          </p>
          <button
            onClick={deploy}
            disabled={busy || !domain.trim()}
            className="px-3 py-1.5 rounded bg-primary text-primary-foreground text-sm disabled:opacity-50"
          >
            {busy ? "Отправка заявки..." : "Развернуть Mailcow"}
          </button>
        </div>
      )}

      {job && job.status !== "idle" && (
        <div className="rounded border border-border p-3 space-y-2">
          <div className="flex justify-between text-xs">
            <span className="text-muted-foreground">Состояние</span>
            <span className="font-mono">
              {job.status === "requested" && "заявка принята, ждём агента"}
              {job.status === "running" &&
                `выполняется: ${job.current_step ?? "…"}`}
              {job.status === "done" && "развёрнуто"}
              {job.status === "error" && "ошибка"}
            </span>
          </div>
          {job.error && <p className="text-xs text-destructive">{job.error}</p>}
          {job.log_tail && (
            <pre className="text-[11px] leading-tight bg-muted rounded p-2 overflow-x-auto max-h-48 overflow-y-auto whitespace-pre-wrap">
              {job.log_tail}
            </pre>
          )}
          <div className="flex gap-2">
            {job.status === "requested" && (
              <button
                onClick={() => post("cancel")}
                disabled={busy}
                className="px-3 py-1.5 rounded border border-border text-sm hover:bg-muted disabled:opacity-50"
              >
                Отменить заявку
              </button>
            )}
            {(job.status === "done" || job.status === "error") && (
              <button
                onClick={() => post("dismiss")}
                disabled={busy}
                className="px-3 py-1.5 rounded border border-border text-sm hover:bg-muted disabled:opacity-50"
              >
                Скрыть результат
              </button>
            )}
          </div>
          {job.status === "done" && (
            <p className="text-xs text-muted-foreground">
              Дальше вручную: DKIM-запись, порты фаервола, API-ключ и его белый
              список IP — {guide}. Затем заполните подключение ниже.
            </p>
          )}
        </div>
      )}

      {note && <p className="text-xs text-muted-foreground">{note}</p>}
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  );
}

function MailServerSection() {
  const [data, setData] = useState<MailServerIntegration | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({
    api_url: "",
    api_key: "",
    mail_domain: "",
    webmail_url: "",
    imap_host: "",
    imap_port: 993,
    smtp_host: "",
    smtp_port: 465,
    default_quota_mb: 1024,
  });
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{
    ok: boolean;
    detail: string;
  } | null>(null);

  function load() {
    setLoading(true);
    fetch(`${API}/api/admin/integrations/mail-server`, {
      credentials: "include",
    })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((d: MailServerIntegration) => {
        setData(d);
        setForm({
          api_url: d.api_url ?? "",
          api_key: "",
          mail_domain: d.mail_domain ?? "",
          webmail_url: d.webmail_url ?? "",
          imap_host: d.imap_host ?? "",
          imap_port: d.imap_port,
          smtp_host: d.smtp_host ?? "",
          smtp_port: d.smtp_port,
          default_quota_mb: d.default_quota_mb ?? 1024,
        });
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    load();
  }, []);

  async function save(verify = true) {
    setSaving(true);
    setSaved(false);
    setError(null);
    setTestResult(null);
    try {
      const body: Record<string, string | number | boolean> = {
        api_url: form.api_url,
        mail_domain: form.mail_domain,
        webmail_url: form.webmail_url,
        imap_host: form.imap_host,
        imap_port: form.imap_port,
        smtp_host: form.smtp_host,
        smtp_port: form.smtp_port,
        default_quota_mb: form.default_quota_mb,
        // Проверяем сразу после сохранения: раздельные кнопки означали, что
        // «сохранил и ушёл» с нерабочим ключом — узнаёшь об этом при первой
        // выдаче ящика.
        verify,
      };
      if (form.api_key.trim()) body.api_key = form.api_key.trim();
      const res = await fetch(`${API}/api/admin/integrations/mail-server`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json", ...csrfHeaders() },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${res.status}`);
      }
      const d: MailServerIntegration = await res.json();
      setData(d);
      setForm((f) => ({ ...f, api_key: "" }));
      setSaved(true);
      if (typeof d.verified === "boolean") {
        setTestResult({ ok: d.verified, detail: d.verify_detail ?? "" });
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  async function test() {
    setTesting(true);
    setTestResult(null);
    try {
      const res = await fetch(
        `${API}/api/admin/integrations/mail-server/test`,
        {
          method: "POST",
          credentials: "include",
          headers: csrfHeaders(),
        },
      );
      const d = await res.json();
      setTestResult(d);
    } catch (e: unknown) {
      setTestResult({
        ok: false,
        detail: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setTesting(false);
    }
  }

  if (loading)
    return <p className="text-sm text-muted-foreground">Загрузка...</p>;
  if (error && !data)
    return <p className="text-sm text-destructive">Ошибка: {error}</p>;
  if (!data) return null;

  const field = (
    key: keyof typeof form,
    label: string,
    opts?: { placeholder?: string; type?: string },
  ) => (
    <div>
      <label className="text-xs text-muted-foreground block mb-1">
        {label}
      </label>
      <input
        type={opts?.type ?? "text"}
        value={form[key]}
        onChange={(e) =>
          setForm((f) => ({
            ...f,
            [key]:
              opts?.type === "number" ? Number(e.target.value) : e.target.value,
          }))
        }
        placeholder={opts?.placeholder}
        className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background"
      />
    </div>
  );

  return (
    <div className="rounded-lg border border-border p-4 space-y-3">
      <h2 className="text-base font-semibold">Почтовый сервер (Mailcow)</h2>
      <p className="text-xs text-muted-foreground">
        Собственный self-hosted почтовый сервер (см.{" "}
        <code className="font-mono">infra/installer/install-mailcow.sh</code>
        ). API-ключ — Mailcow admin UI:{" "}
        <em>Configuration → Access → Edit administrator details → API</em>{" "}
        (Read-Write). Там же в Mailcow нужно внести IP/подсеть контейнера
        backend в белый список ключа — иначе валидный ключ отвечает 401/403.
        Пошаговое руководство по ручной части настройки —{" "}
        <a
          href="/admin/integrations/mailcow-guide"
          className="text-primary hover:underline"
        >
          Настройка Mailcow
        </a>
        . После сохранения на странице пользователя (
        <em>Пользователи → карточка → Корпоративная почта</em>) можно выдавать
        личные @{form.mail_domain || "домен"}-адреса.
      </p>

      {!data.configured && (
        <p className="text-xs text-amber-600 bg-amber-50 rounded px-2 py-1">
          Подключение не настроено — провижининг личных ящиков недоступен.
        </p>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {field("api_url", "Mailcow API URL", {
          placeholder: "https://mail.example.com",
        })}
        {field("mail_domain", "Домен почты", { placeholder: "example.com" })}
        {field("webmail_url", "Ссылка на вебмейл", {
          placeholder: "https://mail.example.com",
        })}
        <div>
          <label className="text-xs text-muted-foreground block mb-1">
            API-ключ{" "}
            {data.api_key_set ? (
              <span className="text-green-600">
                (задан: {data.api_key_hint})
              </span>
            ) : (
              <span className="text-red-500">(не задан)</span>
            )}
          </label>
          <input
            type="password"
            value={form.api_key}
            onChange={(e) =>
              setForm((f) => ({ ...f, api_key: e.target.value }))
            }
            placeholder={
              data.api_key_set
                ? "Оставьте пустым, чтобы не менять"
                : "Вставьте ключ"
            }
            className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background font-mono"
          />
        </div>
        {field("imap_host", "IMAP host", { placeholder: "mail.example.com" })}
        {field("imap_port", "IMAP порт", { type: "number" })}
        {field("smtp_host", "SMTP host", { placeholder: "mail.example.com" })}
        {field("smtp_port", "SMTP порт", { type: "number" })}
        {field("default_quota_mb", "Квота ящика по умолчанию, МБ", {
          type: "number",
        })}
      </div>

      {error && <p className="text-xs text-destructive">Ошибка: {error}</p>}
      {saved && <p className="text-xs text-green-600">Сохранено</p>}
      {testResult && (
        <p
          className={`text-xs ${testResult.ok ? "text-green-600" : "text-destructive"}`}
        >
          {testResult.ok ? "✓ " : "✗ "}
          {testResult.detail}
        </p>
      )}

      <div className="flex gap-2 pt-1">
        <button
          onClick={() => save(true)}
          disabled={saving}
          className="px-3 py-1.5 rounded bg-primary text-primary-foreground text-sm disabled:opacity-50"
        >
          {saving ? "Сохранение..." : "Сохранить и проверить"}
        </button>
        <button
          onClick={test}
          disabled={testing || !data.api_key_set}
          title={
            !data.api_key_set
              ? "Сначала сохраните API-ключ"
              : "Проверить соединение"
          }
          className="px-3 py-1.5 rounded border border-border text-sm hover:bg-muted disabled:opacity-50"
        >
          {testing ? "Проверка..." : "Проверить соединение"}
        </button>
      </div>
    </div>
  );
}

interface OAuthApp {
  provider: string;
  client_id: string | null;
  client_secret_set: boolean;
  client_secret_hint: string;
  redirect_uri: string | null;
  configured: boolean;
}

const OAUTH_PROVIDER_LABEL: Record<string, string> = {
  google: "Google (Gmail)",
  microsoft: "Microsoft (Outlook / Microsoft 365)",
  yandex: "Яндекс.Почта",
  mailru: "Mail.ru",
};

const OAUTH_PROVIDER_CONSOLE: Record<string, { url: string; label: string }> = {
  google: {
    url: "https://console.cloud.google.com/apis/credentials",
    label: "Google Cloud Console → Credentials",
  },
  microsoft: {
    url: "https://portal.azure.com",
    label: "Azure Portal → App registrations",
  },
};

/** One Client ID/Secret per provider (google, microsoft) — registered once
 * here, then every mailbox's own OAuth2 consent (Настройки → Почтовые
 * ящики) runs against it. See app/api/oauth.py + app/domain/oauth_mail.py. */
function OAuthAppsSection() {
  const [apps, setApps] = useState<OAuthApp[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [forms, setForms] = useState<
    Record<
      string,
      { client_id: string; client_secret: string; redirect_uri: string }
    >
  >({});
  const [saving, setSaving] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  function load() {
    setLoading(true);
    fetch(`${API}/api/admin/integrations/oauth-apps`, {
      credentials: "include",
    })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((list: OAuthApp[]) => {
        setApps(list);
        setForms((prev) => {
          const next = { ...prev };
          for (const a of list) {
            if (!next[a.provider]) {
              next[a.provider] = {
                client_id: a.client_id ?? "",
                client_secret: "",
                redirect_uri:
                  a.redirect_uri ?? `${API}/api/oauth/callback/${a.provider}`,
              };
            }
          }
          return next;
        });
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    load();
  }, []);

  async function save(provider: string) {
    setSaving(provider);
    setSaved(null);
    setError(null);
    try {
      const f = forms[provider];
      const body: Record<string, string> = {
        client_id: f.client_id.trim(),
        redirect_uri: f.redirect_uri.trim(),
      };
      if (f.client_secret.trim()) body.client_secret = f.client_secret.trim();
      const res = await fetch(
        `${API}/api/admin/integrations/oauth-apps/${provider}`,
        {
          method: "PUT",
          credentials: "include",
          headers: { "Content-Type": "application/json", ...csrfHeaders() },
          body: JSON.stringify(body),
        },
      );
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${res.status}`);
      }
      const updated: OAuthApp = await res.json();
      setApps((prev) =>
        prev.map((a) => (a.provider === provider ? updated : a)),
      );
      setForms((prev) => ({
        ...prev,
        [provider]: { ...prev[provider], client_secret: "" },
      }));
      setSaved(provider);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(null);
    }
  }

  if (loading)
    return <p className="text-sm text-muted-foreground">Загрузка...</p>;

  return (
    <div className="rounded-lg border border-border p-4 space-y-4">
      <h2 className="text-base font-semibold">
        OAuth2 для почты (Gmail / Microsoft 365)
      </h2>
      <p className="text-xs text-muted-foreground">
        Обычный пароль аккаунта больше не работает для входящих/исходящих у
        Gmail и, в большинстве организаций, у Microsoft 365 — единственная
        надёжная замена (кроме разового пароля приложения) — OAuth2. Заведите
        здесь Client ID/Secret приложения (один раз на провайдера), а каждый
        сотрудник подключает свой ящик кнопкой «Войти через Google/Microsoft» в
        Настройки → Почтовые ящики.
      </p>

      {error && <p className="text-xs text-destructive">Ошибка: {error}</p>}

      {["google", "microsoft", "yandex", "mailru"].map((provider) => {
        const app = apps.find((a) => a.provider === provider);
        const form = forms[provider] ?? {
          client_id: "",
          client_secret: "",
          redirect_uri: "",
        };
        const console_ = OAUTH_PROVIDER_CONSOLE[provider];
        return (
          <div
            key={provider}
            className="rounded border border-border p-3 space-y-2"
          >
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-medium">
                {OAUTH_PROVIDER_LABEL[provider]}
              </h3>
              <span
                className={`text-xs ${app?.configured ? "text-green-600" : "text-muted-foreground"}`}
              >
                {app?.configured ? "✓ настроено" : "не настроено"}
              </span>
            </div>
            <p className="text-xs text-muted-foreground">
              Создайте OAuth-приложение в{" "}
              <a
                href={console_.url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-primary hover:underline"
              >
                {console_.label} ↗
              </a>
              , добавьте redirect URI ниже в список разрешённых и вставьте
              Client ID/Secret сюда.
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label className="text-xs text-muted-foreground block mb-1">
                  Client ID
                </label>
                <input
                  type="text"
                  value={form.client_id}
                  onChange={(e) =>
                    setForms((p) => ({
                      ...p,
                      [provider]: { ...p[provider], client_id: e.target.value },
                    }))
                  }
                  className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background font-mono"
                />
              </div>
              <div>
                <label className="text-xs text-muted-foreground block mb-1">
                  Client Secret{" "}
                  {app?.client_secret_set ? (
                    <span className="text-green-600">
                      (задан: {app.client_secret_hint})
                    </span>
                  ) : (
                    <span className="text-red-500">(не задан)</span>
                  )}
                </label>
                <input
                  type="password"
                  value={form.client_secret}
                  onChange={(e) =>
                    setForms((p) => ({
                      ...p,
                      [provider]: {
                        ...p[provider],
                        client_secret: e.target.value,
                      },
                    }))
                  }
                  placeholder={
                    app?.client_secret_set
                      ? "Оставьте пустым, чтобы не менять"
                      : "Вставьте secret"
                  }
                  className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background font-mono"
                />
              </div>
            </div>
            <div>
              <label className="text-xs text-muted-foreground block mb-1">
                Redirect URI
              </label>
              <input
                type="text"
                value={form.redirect_uri}
                onChange={(e) =>
                  setForms((p) => ({
                    ...p,
                    [provider]: {
                      ...p[provider],
                      redirect_uri: e.target.value,
                    },
                  }))
                }
                className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background font-mono"
              />
              <p className="mt-1 text-xs text-muted-foreground">
                Должен байт-в-байт совпадать со значением, зарегистрированным у
                провайдера.
              </p>
            </div>
            {saved === provider && (
              <p className="text-xs text-green-600">Сохранено</p>
            )}
            <button
              onClick={() => save(provider)}
              disabled={
                saving === provider ||
                !form.client_id.trim() ||
                !form.redirect_uri.trim()
              }
              className="px-3 py-1.5 rounded bg-primary text-primary-foreground text-sm disabled:opacity-50"
            >
              {saving === provider ? "Сохранение..." : "Сохранить"}
            </button>
          </div>
        );
      })}
    </div>
  );
}

function IntegrationsContent() {
  const [data, setData] = useState<AuthentikIntegration | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [token, setToken] = useState("");
  const [externalUrl, setExternalUrl] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{
    ok: boolean;
    detail: string;
  } | null>(null);

  function load() {
    setLoading(true);
    fetch(`${API}/api/admin/integrations/authentik`, { credentials: "include" })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((d: AuthentikIntegration) => {
        setData(d);
        setExternalUrl(d.external_url);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    load();
  }, []);

  async function save() {
    setSaving(true);
    setSaved(false);
    setError(null);
    setTestResult(null);
    try {
      // Send api_token only when the admin typed one (otherwise leave unchanged).
      const body: Record<string, string> = { external_url: externalUrl };
      if (token.trim()) body.api_token = token.trim();
      const res = await fetch(`${API}/api/admin/integrations/authentik`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json", ...csrfHeaders() },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${res.status}`);
      }
      const d: AuthentikIntegration = await res.json();
      setData(d);
      setExternalUrl(d.external_url);
      setToken("");
      setSaved(true);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  async function test() {
    setTesting(true);
    setTestResult(null);
    try {
      const res = await fetch(`${API}/api/admin/integrations/authentik/test`, {
        method: "POST",
        credentials: "include",
        headers: csrfHeaders(),
      });
      const d = await res.json();
      setTestResult(d);
    } catch (e: unknown) {
      setTestResult({
        ok: false,
        detail: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setTesting(false);
    }
  }

  if (loading)
    return <p className="text-sm text-muted-foreground">Загрузка...</p>;
  if (error && !data)
    return <p className="text-sm text-destructive">Ошибка: {error}</p>;
  if (!data) return null;

  return (
    <div className="max-w-xl space-y-5">
      <MailcowDeploySection onDeployed={() => window.location.reload()} />
      <MailServerSection />
      <OAuthAppsSection />

      <div className="rounded-lg border border-border p-4 space-y-3">
        <h2 className="text-base font-semibold">Authentik (SSO)</h2>
        <p className="text-xs text-muted-foreground">
          Управление пользователями и группами выполняется в Authentik. Роли в
          приложении назначаются через группы (admins / managers / accountants /
          buyers / engineers / technologists).
        </p>

        {!data.auth_enabled && (
          <p className="text-xs text-amber-600 bg-amber-50 rounded px-2 py-1">
            AUTH_ENABLED=false — SSO выключен (dev-режим).
          </p>
        )}

        {data.admin_url ? (
          <a
            href={data.admin_url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 text-sm text-primary hover:underline"
          >
            Открыть админку Authentik ↗
          </a>
        ) : (
          <p className="text-xs text-muted-foreground">
            Укажите внешний URL Authentik ниже, чтобы появилась ссылка.
          </p>
        )}
      </div>

      <div className="rounded-lg border border-border p-4 space-y-3">
        <h3 className="text-sm font-medium">Настройка API</h3>
        <p className="text-xs text-muted-foreground">
          API-токен нужен, чтобы создавать пользователей и задавать пароли прямо
          из этой админки. Создайте токен в Authentik:{" "}
          <em>Directory → Tokens → Create</em> (intent “API”, пользователь
          akadmin) и вставьте его сюда.
        </p>

        <div>
          <label className="text-xs text-muted-foreground block mb-1">
            Внешний URL Authentik
          </label>
          <input
            type="url"
            value={externalUrl}
            onChange={(e) => setExternalUrl(e.target.value)}
            placeholder="https://example.com"
            className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background"
          />
          <p className="mt-1 text-xs text-muted-foreground">
            Это НЕ отдельный поддомен вроде <code>auth.example.com</code> —
            Traefik проксирует Authentik по путям (/application/, /if/, /flows/)
            на том же домене, что и само приложение. Укажите тот же адрес, на
            котором открыт этот сайт (например{" "}
            {typeof window !== "undefined"
              ? window.location.origin
              : "https://example.com"}
            ), иначе ссылка ниже не откроется — «сервер не найден».
          </p>
        </div>

        <div>
          <label className="text-xs text-muted-foreground block mb-1">
            API-токен{" "}
            {data.token_set ? (
              <span className="text-green-600">(задан: {data.token_hint})</span>
            ) : (
              <span className="text-red-500">(не задан)</span>
            )}
          </label>
          <input
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder={
              data.token_set
                ? "Оставьте пустым, чтобы не менять"
                : "Вставьте токен"
            }
            className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background font-mono"
          />
        </div>

        {error && <p className="text-xs text-destructive">Ошибка: {error}</p>}
        {saved && <p className="text-xs text-green-600">Сохранено</p>}
        {testResult && (
          <p
            className={`text-xs ${testResult.ok ? "text-green-600" : "text-destructive"}`}
          >
            {testResult.ok ? "✓ " : "✗ "}
            {testResult.detail}
          </p>
        )}

        <div className="flex gap-2 pt-1">
          <button
            onClick={save}
            disabled={saving}
            className="px-3 py-1.5 rounded bg-primary text-primary-foreground text-sm disabled:opacity-50"
          >
            {saving ? "Сохранение..." : "Сохранить"}
          </button>
          <button
            onClick={test}
            disabled={testing || !data.token_set}
            title={
              !data.token_set
                ? "Сначала сохраните токен"
                : "Проверить соединение"
            }
            className="px-3 py-1.5 rounded border border-border text-sm hover:bg-muted disabled:opacity-50"
          >
            {testing ? "Проверка..." : "Проверить соединение"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function AdminIntegrationsPage() {
  return (
    <ProtectedRoute requiredRoles={["admin"]}>
      <IntegrationsContent />
    </ProtectedRoute>
  );
}
