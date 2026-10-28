"use client";

import { useCallback, useEffect, useState } from "react";

import { getApiBaseUrl } from "@/lib/api-base";
import { apiFetch, mutFetch } from "@/lib/auth";

const API = getApiBaseUrl();
const BASE = `${API}/api/web-search`;

interface ProviderView {
  id: string;
  kind: string;
  enabled: boolean;
  role: "primary" | "fallback";
  weight: number;
  endpoint: string | null;
  endpoint_effective: string | null;
  api_key_mask: string;
  api_key_set: boolean;
  engines: string[];
  region: string | null;
  folder_id: string | null;
  mcp_tool: string | null;
  daily_quota: number | null;
  used_today: number | null;
}

interface Settings {
  browser_url: string;
  browsing_enabled: boolean;
  supported_providers: string[];
  default_endpoints: Record<string, string>;
  providers: ProviderView[];
  providers_saved: boolean;
  language: string;
  per_host_interval_s: number;
  search_cache_ttl_s: number;
  page_cache_ttl_s: number;
  research_concurrency: number;
}

interface TestResult {
  ok: boolean;
  provider: string;
  result_count: number;
  diagnostics: string[];
  sample: { title: string; url: string; snippet?: string | null }[];
}

// Editable provider row: the key is write-only, blank means "leave as stored".
interface ProviderDraft {
  id: string;
  kind: string;
  enabled: boolean;
  role: "primary" | "fallback";
  weight: string;
  endpoint: string;
  apiKey: string;
  apiKeySet: boolean;
  apiKeyMask: string;
  engines: string;
  region: string;
  folderId: string;
  mcpTool: string;
  dailyQuota: string;
  usedToday: number | null;
}

const KIND_LABELS: Record<string, string> = {
  searxng: "SearXNG (свой сервер, без ключа)",
  yandex: "Yandex Search API",
  tavily: "Tavily",
  serper: "Serper (Google)",
  brave: "Brave Search",
  exa: "Exa",
  jina: "Jina Search",
  custom: "Свой endpoint",
  mcp: "Инструмент MCP-сервера",
};

const NEEDS_KEY = new Set([
  "tavily",
  "serper",
  "brave",
  "exa",
  "yandex",
  "custom",
]);
const OPTIONAL_KEY = new Set(["jina", "searxng"]);
const HAS_REGION = new Set(["yandex", "serper", "brave", "exa", "jina"]);

const REGION_HINT: Record<string, string> = {
  yandex: "ru, com, tr, kk, be, uz",
  serper: "код страны: ru, us…",
  brave: "код страны: RU, US…",
  exa: "код страны: RU, US…",
  jina: "код страны: ru, us…",
};

const field =
  "mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm";

function toDraft(p: ProviderView): ProviderDraft {
  return {
    id: p.id,
    kind: p.kind,
    enabled: p.enabled,
    role: p.role,
    weight: String(p.weight),
    endpoint: p.endpoint || "",
    apiKey: "",
    apiKeySet: p.api_key_set,
    apiKeyMask: p.api_key_mask,
    engines: (p.engines || []).join(", "),
    region: p.region || "",
    folderId: p.folder_id || "",
    mcpTool: p.mcp_tool || "",
    dailyQuota: p.daily_quota == null ? "" : String(p.daily_quota),
    usedToday: p.used_today,
  };
}

function emptyDraft(existing: ProviderDraft[]): ProviderDraft {
  let n = existing.length + 1;
  while (existing.some((p) => p.id === `provider-${n}`)) n += 1;
  return {
    id: `provider-${n}`,
    kind: "yandex",
    enabled: true,
    role: "primary",
    weight: "1",
    endpoint: "",
    apiKey: "",
    apiKeySet: false,
    apiKeyMask: "",
    engines: "",
    region: "",
    folderId: "",
    mcpTool: "",
    dailyQuota: "",
    usedToday: null,
  };
}

function toPayload(p: ProviderDraft): Record<string, unknown> {
  const out: Record<string, unknown> = {
    id: p.id.trim(),
    kind: p.kind,
    enabled: p.enabled,
    role: p.role,
    weight: Number(p.weight) || 0,
    endpoint: p.endpoint.trim() || null,
    engines: p.engines
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean),
    region: p.region.trim() || null,
    folder_id: p.folderId.trim() || null,
    mcp_tool: p.mcpTool.trim() || null,
    daily_quota: p.dailyQuota.trim() === "" ? null : Number(p.dailyQuota),
  };
  if (p.apiKey.trim()) out.api_key = p.apiKey.trim();
  return out;
}

function errorText(data: unknown, status: number): string {
  const detail = (data as { detail?: unknown })?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail)
    return String((detail as { message: unknown }).message);
  if (Array.isArray(detail))
    return detail.map((d) => (d as { msg?: string }).msg || "").join("; ");
  return `HTTP ${status}`;
}

export default function WebSearchSettingsPage() {
  const [s, setS] = useState<Settings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [test, setTest] = useState<TestResult | null>(null);

  const [providers, setProviders] = useState<ProviderDraft[]>([]);
  const [language, setLanguage] = useState("ru");
  const [interval, setIntervalS] = useState("1.5");
  const [searchTtlH, setSearchTtlH] = useState("6");
  const [pageTtlH, setPageTtlH] = useState("24");
  const [concurrency, setConcurrency] = useState("4");
  const [browserUrl, setBrowserUrl] = useState("");
  const [browsingEnabled, setBrowsingEnabled] = useState(true);

  const applyToForm = useCallback((data: Settings) => {
    setProviders(data.providers.map(toDraft));
    setLanguage(data.language);
    setIntervalS(String(data.per_host_interval_s));
    setSearchTtlH(String(data.search_cache_ttl_s / 3600));
    setPageTtlH(String(data.page_cache_ttl_s / 3600));
    setConcurrency(String(data.research_concurrency));
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

  function update(index: number, patch: Partial<ProviderDraft>) {
    setProviders((list) =>
      list.map((p, i) => (i === index ? { ...p, ...patch } : p)),
    );
  }

  async function save() {
    setSaving(true);
    setMsg(null);
    setErr(null);
    try {
      const res = await mutFetch(`${BASE}/settings`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          providers: providers.map(toPayload),
          language: language.trim() || "ru",
          per_host_interval_s: Number(interval) || 0,
          search_cache_ttl_s: Math.round((Number(searchTtlH) || 0) * 3600),
          page_cache_ttl_s: Math.round((Number(pageTtlH) || 0) * 3600),
          research_concurrency: Number(concurrency) || 1,
          browser_url: browserUrl,
          browsing_enabled: browsingEnabled,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(errorText(data, res.status));
      setS(data);
      applyToForm(data);
      setMsg("Настройки сохранены.");
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setSaving(false);
    }
  }

  async function runTest(providerId: string | null) {
    setTesting(providerId ?? "*");
    setTest(null);
    setErr(null);
    try {
      const res = await mutFetch(`${BASE}/settings/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider_id: providerId }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(errorText(data, res.status));
      setTest(data);
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setTesting(null);
    }
  }

  async function clearCache() {
    setErr(null);
    setMsg(null);
    try {
      const res = await mutFetch(`${BASE}/settings/cache/clear`, {
        method: "POST",
      });
      const data = await res.json();
      if (!res.ok) throw new Error(errorText(data, res.status));
      setMsg(`Кэш очищен: удалено записей — ${data.removed}.`);
    } catch (e) {
      setErr(String((e as Error).message || e));
    }
  }

  if (loading)
    return <div className="text-sm text-muted-foreground">Загрузка…</div>;

  const kinds = s?.supported_providers || ["searxng"];
  const ids = providers.map((p) => p.id.trim());
  const savedIds = new Set((s?.providers || []).map((p) => p.id));

  return (
    <div className="space-y-6 max-w-3xl min-w-0">
      <div>
        <h2 className="text-lg font-semibold">Веб-поиск и просмотр страниц</h2>
        <p className="text-sm text-muted-foreground mt-1">
          Света ищет сразу во всех основных поисковиках и объединяет выдачу:
          страница, которую нашли несколько поисковиков, поднимается выше.
          Резервные поисковики спрашиваются, только если основные ничего не
          нашли. Сбой поисковика виден в ответе — молча другим он не
          подменяется.
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

      {!s?.providers_saved && (
        <div className="rounded-md border border-border px-3 py-2 text-xs text-muted-foreground">
          Список собран из прежних настроек «основной / резервный». После
          сохранения он хранится как есть.
        </div>
      )}

      <section className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="font-medium">Поисковики</h3>
          <button
            onClick={() => setProviders((list) => [...list, emptyDraft(list)])}
            className="rounded-md border border-border px-3 py-1.5 text-sm"
          >
            Добавить поисковик
          </button>
        </div>

        {providers.map((p, i) => {
          const duplicate = ids.filter((id) => id === p.id.trim()).length > 1;
          return (
            <div
              key={i}
              className="rounded-lg border border-border p-4 space-y-3 min-w-0"
            >
              <div className="flex flex-wrap items-center gap-3">
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={p.enabled}
                    onChange={(e) => update(i, { enabled: e.target.checked })}
                  />
                  <span>Включён</span>
                </label>
                <select
                  value={p.role}
                  onChange={(e) =>
                    update(i, {
                      role: e.target.value as ProviderDraft["role"],
                    })
                  }
                  className="rounded-md border border-border bg-background px-2 py-1 text-sm"
                  aria-label="Роль"
                >
                  <option value="primary">основной</option>
                  <option value="fallback">резервный</option>
                </select>
                {p.dailyQuota.trim() !== "" && (
                  <span className="text-xs text-muted-foreground">
                    сегодня: {p.usedToday ?? "—"} из {p.dailyQuota}
                  </span>
                )}
                <div className="ml-auto flex gap-2">
                  <button
                    onClick={() => void runTest(p.id.trim())}
                    disabled={testing !== null || !savedIds.has(p.id.trim())}
                    title={
                      savedIds.has(p.id.trim())
                        ? "Пробный запрос только к этому поисковику"
                        : "Сначала сохраните"
                    }
                    className="rounded-md border border-border px-3 py-1 text-xs disabled:opacity-50"
                  >
                    {testing === p.id.trim() ? "Проверка…" : "Проверить"}
                  </button>
                  <button
                    onClick={() =>
                      setProviders((list) => list.filter((_, j) => j !== i))
                    }
                    className="rounded-md border border-border px-3 py-1 text-xs text-red-600"
                  >
                    Удалить
                  </button>
                </div>
              </div>

              <div className="grid gap-3 sm:grid-cols-2">
                <label className="block text-sm min-w-0">
                  <span className="text-muted-foreground">Тип</span>
                  <select
                    value={p.kind}
                    onChange={(e) => update(i, { kind: e.target.value })}
                    className={field}
                  >
                    {kinds.map((k) => (
                      <option key={k} value={k}>
                        {KIND_LABELS[k] || k}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="block text-sm min-w-0">
                  <span className="text-muted-foreground">
                    Имя (латиница){duplicate && " — повторяется"}
                  </span>
                  <input
                    value={p.id}
                    onChange={(e) => update(i, { id: e.target.value })}
                    className={`${field} font-mono ${duplicate ? "border-red-500" : ""}`}
                  />
                </label>
              </div>

              {p.kind === "mcp" ? (
                <label className="block text-sm">
                  <span className="text-muted-foreground">
                    Инструмент MCP (mcp_&lt;сервер&gt;_&lt;инструмент&gt;;
                    серверы подключаются в настройках агента)
                  </span>
                  <input
                    value={p.mcpTool}
                    onChange={(e) => update(i, { mcpTool: e.target.value })}
                    placeholder="mcp_tavily_tavily_search"
                    className={`${field} font-mono`}
                  />
                </label>
              ) : (
                <label className="block text-sm min-w-0">
                  <span className="text-muted-foreground">
                    Endpoint (пусто —{" "}
                    {s?.default_endpoints?.[p.kind] || "не задан"})
                  </span>
                  <input
                    value={p.endpoint}
                    onChange={(e) => update(i, { endpoint: e.target.value })}
                    placeholder={s?.default_endpoints?.[p.kind] || ""}
                    className={`${field} font-mono`}
                  />
                </label>
              )}

              {(NEEDS_KEY.has(p.kind) || OPTIONAL_KEY.has(p.kind)) && (
                <label className="block text-sm">
                  <span className="text-muted-foreground">
                    API-ключ{OPTIONAL_KEY.has(p.kind) && " (необязательно)"}{" "}
                    {p.apiKeySet && (
                      <span className="text-xs">
                        (сохранён: {p.apiKeyMask}; пусто — оставить)
                      </span>
                    )}
                  </span>
                  <input
                    type="password"
                    value={p.apiKey}
                    onChange={(e) => update(i, { apiKey: e.target.value })}
                    placeholder="••••••••"
                    autoComplete="off"
                    className={`${field} font-mono`}
                  />
                </label>
              )}

              <div className="grid gap-3 sm:grid-cols-3">
                {p.kind === "searxng" && (
                  <label className="block text-sm sm:col-span-3">
                    <span className="text-muted-foreground">
                      Движки через запятую (пусто — набор SearXNG)
                    </span>
                    <input
                      value={p.engines}
                      onChange={(e) => update(i, { engines: e.target.value })}
                      placeholder="google, bing, duckduckgo, yandex"
                      className={`${field} font-mono`}
                    />
                  </label>
                )}
                {p.kind === "yandex" && (
                  <label className="block text-sm">
                    <span className="text-muted-foreground">
                      Каталог (folderId)
                    </span>
                    <input
                      value={p.folderId}
                      onChange={(e) => update(i, { folderId: e.target.value })}
                      placeholder="b1g…"
                      className={`${field} font-mono`}
                    />
                  </label>
                )}
                {HAS_REGION.has(p.kind) && (
                  <label className="block text-sm">
                    <span className="text-muted-foreground">Регион</span>
                    <input
                      value={p.region}
                      onChange={(e) => update(i, { region: e.target.value })}
                      placeholder={REGION_HINT[p.kind]}
                      className={field}
                    />
                  </label>
                )}
                <label className="block text-sm">
                  <span className="text-muted-foreground">Вес в выдаче</span>
                  <input
                    type="number"
                    min={0}
                    max={10}
                    step={0.1}
                    value={p.weight}
                    onChange={(e) => update(i, { weight: e.target.value })}
                    className={field}
                  />
                </label>
                <label className="block text-sm">
                  <span className="text-muted-foreground">
                    Запросов в сутки (пусто — без лимита)
                  </span>
                  <input
                    type="number"
                    min={0}
                    value={p.dailyQuota}
                    onChange={(e) => update(i, { dailyQuota: e.target.value })}
                    className={field}
                  />
                </label>
              </div>
            </div>
          );
        })}
        {providers.length === 0 && (
          <div className="rounded-lg border border-dashed border-border p-4 text-sm text-muted-foreground">
            Нет ни одного поисковика — агент не сможет искать в интернете.
          </div>
        )}
      </section>

      <section className="rounded-lg border border-border p-4 space-y-3">
        <h3 className="font-medium">Темп и кэш</h3>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block text-sm">
            <span className="text-muted-foreground">Язык поиска</span>
            <input
              value={language}
              onChange={(e) => setLanguage(e.target.value)}
              placeholder="ru"
              className={field}
            />
          </label>
          <label className="block text-sm">
            <span className="text-muted-foreground">
              Пауза между загрузками одного сайта, с
            </span>
            <input
              type="number"
              min={0}
              max={60}
              step={0.5}
              value={interval}
              onChange={(e) => setIntervalS(e.target.value)}
              className={field}
            />
          </label>
          <label className="block text-sm">
            <span className="text-muted-foreground">
              Хранить выдачу поиска, ч
            </span>
            <input
              type="number"
              min={0}
              step={1}
              value={searchTtlH}
              onChange={(e) => setSearchTtlH(e.target.value)}
              className={field}
            />
          </label>
          <label className="block text-sm">
            <span className="text-muted-foreground">
              Хранить прочитанные страницы, ч
            </span>
            <input
              type="number"
              min={0}
              step={1}
              value={pageTtlH}
              onChange={(e) => setPageTtlH(e.target.value)}
              className={field}
            />
          </label>
          <label className="block text-sm">
            <span className="text-muted-foreground">
              Страниц одновременно при исследовании
            </span>
            <input
              type="number"
              min={1}
              max={12}
              value={concurrency}
              onChange={(e) => setConcurrency(e.target.value)}
              className={field}
            />
          </label>
        </div>
        <button
          onClick={() => void clearCache()}
          className="rounded-md border border-border px-3 py-1.5 text-sm"
        >
          Очистить кэш
        </button>
      </section>

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
            className={`${field} font-mono`}
          />
        </label>
      </section>

      <div className="flex flex-wrap items-center gap-3">
        <button
          onClick={() => void save()}
          disabled={saving}
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
        >
          {saving ? "Сохранение…" : "Сохранить"}
        </button>
        <button
          onClick={() => void runTest(null)}
          disabled={testing !== null}
          className="rounded-md border border-border px-4 py-2 text-sm font-medium disabled:opacity-50"
        >
          {testing === "*" ? "Проверка…" : "Проверить поиск целиком"}
        </button>
      </div>

      {test && (
        <section className="rounded-lg border border-border p-4 space-y-2 min-w-0">
          <div className="text-sm">
            {test.ok ? "✅" : "⚠️"} Ответили: <b>{test.provider}</b>,
            результатов: {test.result_count}
            {test.diagnostics.length > 0 && (
              <span className="text-muted-foreground break-words">
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
