"use client";

import { useCallback, useEffect, useState } from "react";

import { getApiBaseUrl } from "@/lib/api-base";
import { apiFetch, mutFetch } from "@/lib/auth";

const API = getApiBaseUrl();
const BASE = `${API}/api/web-search`;

interface Settings {
  provider: string;
  endpoint: string | null;
  endpoint_effective: string | null;
  api_key_mask: string;
  api_key_set: boolean;
  fallback_provider: string | null;
  fallback_endpoint: string | null;
  fallback_endpoint_effective: string | null;
  fallback_api_key_mask: string;
  fallback_api_key_set: boolean;
  searxng_engines: string[];
  browser_url: string;
  browsing_enabled: boolean;
  supported_providers: string[];
  default_endpoints: Record<string, string>;
}

interface TestResult {
  ok: boolean;
  provider: string;
  result_count: number;
  diagnostics: string[];
  sample: { title: string; url: string; snippet?: string | null }[];
}

const PROVIDER_LABELS: Record<string, string> = {
  searxng: "SearXNG (self-hosted, без ключа)",
  tavily: "Tavily (внешний API)",
  serper: "Serper (внешний API)",
  brave: "Brave Search (внешний API)",
  custom: "Свой endpoint",
};

const NEEDS_KEY = new Set(["tavily", "serper", "brave", "custom"]);

export default function WebSearchSettingsPage() {
  const [s, setS] = useState<Settings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [test, setTest] = useState<TestResult | null>(null);

  // Editable form state (secrets are write-only; blank means "leave unchanged").
  const [provider, setProvider] = useState("searxng");
  const [endpoint, setEndpoint] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [fallbackProvider, setFallbackProvider] = useState("");
  const [fallbackEndpoint, setFallbackEndpoint] = useState("");
  const [fallbackApiKey, setFallbackApiKey] = useState("");
  const [engines, setEngines] = useState("");
  const [browserUrl, setBrowserUrl] = useState("");
  const [browsingEnabled, setBrowsingEnabled] = useState(true);

  const applyToForm = useCallback((data: Settings) => {
    setProvider(data.provider);
    setEndpoint(data.endpoint || "");
    setApiKey("");
    setFallbackProvider(data.fallback_provider || "");
    setFallbackEndpoint(data.fallback_endpoint || "");
    setFallbackApiKey("");
    setEngines((data.searxng_engines || []).join(", "));
    setBrowserUrl(data.browser_url);
    setBrowsingEnabled(data.browsing_enabled);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await apiFetch(`${BASE}/settings`);
      const data: Settings = await res.json();
      setS(data);
      applyToForm(data);
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setLoading(false);
    }
  }, [applyToForm]);

  useEffect(() => {
    void load();
  }, [load]);

  async function save() {
    setSaving(true);
    setMsg(null);
    setErr(null);
    try {
      const patch: Record<string, unknown> = {
        provider,
        endpoint,
        fallback_provider: fallbackProvider || null,
        fallback_endpoint: fallbackEndpoint,
        searxng_engines: engines
          .split(",")
          .map((x) => x.trim())
          .filter(Boolean),
        browser_url: browserUrl,
        browsing_enabled: browsingEnabled,
      };
      // Only send secrets when the admin typed something new.
      if (apiKey.trim()) patch.api_key = apiKey.trim();
      if (fallbackApiKey.trim()) patch.fallback_api_key = fallbackApiKey.trim();

      const res = await mutFetch(`${BASE}/settings`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`);
      setS(data);
      applyToForm(data);
      setMsg("Настройки сохранены.");
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setSaving(false);
    }
  }

  async function runTest() {
    setTesting(true);
    setTest(null);
    setErr(null);
    try {
      const res = await mutFetch(`${BASE}/settings/test`, { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`);
      setTest(data);
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setTesting(false);
    }
  }

  if (loading)
    return <div className="text-sm text-muted-foreground">Загрузка…</div>;

  const providers = s?.supported_providers || ["searxng"];
  const effectiveEndpoint =
    endpoint || s?.default_endpoints?.[provider] || "(не задан)";

  return (
    <div className="space-y-6 max-w-3xl">
      <div>
        <h2 className="text-lg font-semibold">Веб-поиск и просмотр страниц</h2>
        <p className="text-sm text-muted-foreground mt-1">
          Света ищет в интернете и открывает сайты «как человек» через свой
          headless-браузер со stealth-режимом (рендерит JS, проходит базовую
          анти-бот защиту). По умолчанию поиск идёт через self-hosted SearXNG —
          без API-ключей и сторонних сервисов.
        </p>
      </div>

      {err && (
        <div className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-600">
          {err}
        </div>
      )}
      {msg && (
        <div className="rounded-md border border-green-500/40 bg-green-500/10 px-3 py-2 text-sm text-green-600">
          {msg}
        </div>
      )}

      {/* Primary search engine */}
      <section className="rounded-lg border border-border p-4 space-y-3">
        <h3 className="font-medium">Основной поисковый движок</h3>
        <label className="block text-sm">
          <span className="text-muted-foreground">Провайдер</span>
          <select
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
            className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          >
            {providers.map((p) => (
              <option key={p} value={p}>
                {PROVIDER_LABELS[p] || p}
              </option>
            ))}
          </select>
        </label>

        <label className="block text-sm">
          <span className="text-muted-foreground">
            Endpoint (пусто → по умолчанию:{" "}
            {s?.default_endpoints?.[provider] || "—"})
          </span>
          <input
            value={endpoint}
            onChange={(e) => setEndpoint(e.target.value)}
            placeholder={s?.default_endpoints?.[provider] || ""}
            className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm font-mono"
          />
          <span className="text-xs text-muted-foreground">
            Эффективный: <code>{effectiveEndpoint}</code>
          </span>
        </label>

        {NEEDS_KEY.has(provider) && (
          <label className="block text-sm">
            <span className="text-muted-foreground">
              API-ключ{" "}
              {s?.api_key_set && (
                <span className="text-xs">
                  (сохранён: {s.api_key_mask}; пусто — оставить как есть)
                </span>
              )}
            </span>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="••••••••"
              className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm font-mono"
            />
          </label>
        )}

        {provider === "searxng" && (
          <label className="block text-sm">
            <span className="text-muted-foreground">
              Движки SearXNG через запятую (пусто → набор по умолчанию)
            </span>
            <input
              value={engines}
              onChange={(e) => setEngines(e.target.value)}
              placeholder="google, bing, duckduckgo, yandex"
              className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm font-mono"
            />
          </label>
        )}
      </section>

      {/* Fallback */}
      <section className="rounded-lg border border-border p-4 space-y-3">
        <h3 className="font-medium">Резервный движок (fallback)</h3>
        <p className="text-xs text-muted-foreground">
          Используется, если основной вернул ошибку или ноль результатов.
        </p>
        <label className="block text-sm">
          <span className="text-muted-foreground">Провайдер</span>
          <select
            value={fallbackProvider}
            onChange={(e) => setFallbackProvider(e.target.value)}
            className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          >
            <option value="">— выключен —</option>
            {providers.map((p) => (
              <option key={p} value={p}>
                {PROVIDER_LABELS[p] || p}
              </option>
            ))}
          </select>
        </label>
        {fallbackProvider && (
          <>
            <label className="block text-sm">
              <span className="text-muted-foreground">
                Endpoint (опционально)
              </span>
              <input
                value={fallbackEndpoint}
                onChange={(e) => setFallbackEndpoint(e.target.value)}
                placeholder={s?.default_endpoints?.[fallbackProvider] || ""}
                className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm font-mono"
              />
            </label>
            {NEEDS_KEY.has(fallbackProvider) && (
              <label className="block text-sm">
                <span className="text-muted-foreground">
                  API-ключ{" "}
                  {s?.fallback_api_key_set && (
                    <span className="text-xs">
                      (сохранён: {s.fallback_api_key_mask})
                    </span>
                  )}
                </span>
                <input
                  type="password"
                  value={fallbackApiKey}
                  onChange={(e) => setFallbackApiKey(e.target.value)}
                  placeholder="••••••••"
                  className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm font-mono"
                />
              </label>
            )}
          </>
        )}
      </section>

      {/* Browsing */}
      <section className="rounded-lg border border-border p-4 space-y-3">
        <h3 className="font-medium">Просмотр страниц «как человек»</h3>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={browsingEnabled}
            onChange={(e) => setBrowsingEnabled(e.target.checked)}
          />
          <span>Разрешить агенту открывать и читать страницы</span>
        </label>
        <label className="block text-sm">
          <span className="text-muted-foreground">URL браузер-сервиса</span>
          <input
            value={browserUrl}
            onChange={(e) => setBrowserUrl(e.target.value)}
            placeholder="http://web-browser:8093"
            className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm font-mono"
          />
        </label>
      </section>

      <div className="flex items-center gap-3">
        <button
          onClick={() => void save()}
          disabled={saving}
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
        >
          {saving ? "Сохранение…" : "Сохранить"}
        </button>
        <button
          onClick={() => void runTest()}
          disabled={testing}
          className="rounded-md border border-border px-4 py-2 text-sm font-medium disabled:opacity-50"
        >
          {testing ? "Проверка…" : "Проверить поиск"}
        </button>
      </div>

      {test && (
        <section className="rounded-lg border border-border p-4 space-y-2">
          <div className="text-sm">
            {test.ok ? "✅" : "⚠️"} Провайдер <b>{test.provider}</b>,
            результатов: {test.result_count}
            {test.diagnostics.length > 0 && (
              <span className="text-muted-foreground">
                {" "}
                ({test.diagnostics.join(", ")})
              </span>
            )}
          </div>
          <ul className="space-y-1 text-sm">
            {test.sample.map((r, i) => (
              <li key={i} className="truncate">
                <a
                  href={r.url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-primary hover:underline"
                >
                  {r.title || r.url}
                </a>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
