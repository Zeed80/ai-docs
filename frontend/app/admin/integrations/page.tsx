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
