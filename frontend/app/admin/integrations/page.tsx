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
  mail_domain: string | null;
  webmail_url: string | null;
}

/** Company mail domain (tells internal correspondents from external ones in
 * email rules) and the webmail link shown on personal mailbox cards. */
function MailServerSection() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState<MailServerIntegration>({
    mail_domain: "",
    webmail_url: "",
  });
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    fetch(`${API}/api/admin/integrations/mail-server`, {
      credentials: "include",
    })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((d: MailServerIntegration) =>
        setForm({
          mail_domain: d.mail_domain ?? "",
          webmail_url: d.webmail_url ?? "",
        }),
      )
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  async function save() {
    setSaving(true);
    setSaved(false);
    setError(null);
    try {
      const res = await fetch(`${API}/api/admin/integrations/mail-server`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json", ...csrfHeaders() },
        body: JSON.stringify(form),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${res.status}`);
      }
      const d: MailServerIntegration = await res.json();
      setForm({
        mail_domain: d.mail_domain ?? "",
        webmail_url: d.webmail_url ?? "",
      });
      setSaved(true);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  if (loading)
    return <p className="text-sm text-muted-foreground">Загрузка...</p>;

  const field = (
    key: keyof MailServerIntegration,
    label: string,
    placeholder: string,
  ) => (
    <div>
      <label className="text-xs text-muted-foreground block mb-1">
        {label}
      </label>
      <input
        type="text"
        value={form[key] ?? ""}
        onChange={(e) => setForm((f) => ({ ...f, [key]: e.target.value }))}
        placeholder={placeholder}
        className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background"
      />
    </div>
  );

  return (
    <div className="rounded-lg border border-border p-4 space-y-3">
      <h2 className="text-base font-semibold">Почта компании</h2>
      <p className="text-xs text-muted-foreground">
        Домен отличает внутреннюю переписку от внешней в правилах почты. Ссылка
        на вебмейл показывается в карточке личного ящика.
      </p>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {field("mail_domain", "Домен почты", "example.com")}
        {field("webmail_url", "Ссылка на вебмейл", "https://mail.example.com")}
      </div>

      {error && <p className="text-xs text-destructive">Ошибка: {error}</p>}
      {saved && <p className="text-xs text-green-600">Сохранено</p>}

      <div className="flex gap-2 pt-1">
        <button
          onClick={save}
          disabled={saving}
          className="px-3 py-1.5 rounded bg-primary text-primary-foreground text-sm disabled:opacity-50"
        >
          {saving ? "Сохранение..." : "Сохранить"}
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
  yandex: {
    url: "https://oauth.yandex.ru/client/new",
    label: "Яндекс OAuth → Создать приложение",
  },
  mailru: {
    url: "https://o2.mail.ru/app/",
    label: "Mail.ru OAuth → Мои приложения",
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
