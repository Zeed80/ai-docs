"use client";

import { useEffect, useState } from "react";
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
        После сохранения на странице пользователя (
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
      <MailServerSection />

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
