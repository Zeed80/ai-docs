"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders, mutFetch } from "@/lib/auth";

const API = getApiBaseUrl();

// ── Types ──────────────────────────────────────────────────────────────────

interface LlamaCppStatus {
  running: boolean;
  url: string;
  model_loaded: string | null;
  ctx_size: number | null;
  slots_idle: number | null;
  slots_processing: number | null;
  version: string | null;
  kv_cache_type: string | null;
}

interface LlamaCppConfig {
  url: string;
  model: string;
  ctx_size: number;
  kv_cache_type: string;
  n_gpu_layers: number;
  parallel: number;
  flash_attn: boolean;
}

interface GgufModel {
  name: string;
  path: string;
  size_bytes: number;
  size_human: string;
  active: boolean;
}

interface CatalogEntry {
  id: string;
  name: string;
  description: string;
  repo: string;
  filename: string;
  size_human: string;
  quant: string;
  params: string;
  ctx: number;
  tags: string[];
  downloaded: boolean;
  local_path: string | null;
}

interface DownloadStatus {
  model_id: string;
  status: string;
  progress_bytes: number;
  total_bytes: number;
  progress_pct: number;
  error: string | null;
}

const KV_CACHE_OPTIONS = [
  { value: "f16", label: "f16 — полная точность" },
  { value: "q8_0", label: "q8_0 — TurboQuant (рекомендуется)" },
  { value: "q4_0", label: "q4_0 — максимальная компрессия" },
];

type Tab = "status" | "models" | "catalog" | "config";

// ── Helpers ────────────────────────────────────────────────────────────────

function humanBytes(b: number): string {
  if (b === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let v = b;
  for (const u of units) {
    if (v < 1024) return `${v.toFixed(1)} ${u}`;
    v /= 1024;
  }
  return `${v.toFixed(1)} TB`;
}

function TagBadge({ tag }: { tag: string }) {
  const colors: Record<string, string> = {
    recommended: "bg-green-100 text-green-800",
    fast: "bg-blue-100 text-blue-800",
    powerful: "bg-purple-100 text-purple-800",
    russian: "bg-orange-100 text-orange-800",
    balanced: "bg-teal-100 text-teal-800",
    "long-context": "bg-yellow-100 text-yellow-800",
    tiny: "bg-gray-100 text-gray-600",
  };
  return (
    <span
      className={`text-xs px-1.5 py-0.5 rounded font-medium ${colors[tag] ?? "bg-gray-100 text-gray-600"}`}
    >
      {tag}
    </span>
  );
}

// ── Main Page ──────────────────────────────────────────────────────────────

export default function LlamaCppPage() {
  const [tab, setTab] = useState<Tab>("status");
  const [status, setStatus] = useState<LlamaCppStatus | null>(null);
  const [config, setConfig] = useState<LlamaCppConfig | null>(null);
  const [draft, setDraft] = useState<LlamaCppConfig | null>(null);
  const [models, setModels] = useState<GgufModel[]>([]);
  const [catalog, setCatalog] = useState<CatalogEntry[]>([]);
  const [downloads, setDownloads] = useState<Record<string, DownloadStatus>>(
    {},
  );
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{
    ok: boolean;
    response?: string;
    error?: string;
  } | null>(null);
  const [toast, setToast] = useState<{ text: string; ok: boolean } | null>(
    null,
  );
  const [deleting, setDeleting] = useState<string | null>(null);
  const [customUrl, setCustomUrl] = useState("");
  const sseRefs = useRef<Record<string, EventSource>>({});

  const showToast = useCallback((text: string, ok: boolean) => {
    setToast({ text, ok });
    setTimeout(() => setToast(null), 4000);
  }, []);

  const loadStatus = useCallback(async () => {
    try {
      const s = await fetch(`${API}/api/llamacpp/status`).then((r) => r.json());
      setStatus(s);
    } catch {
      setStatus(null);
    }
  }, []);

  const loadModels = useCallback(async () => {
    try {
      const m = await fetch(`${API}/api/llamacpp/models`).then((r) => r.json());
      setModels(Array.isArray(m) ? m : []);
    } catch {
      setModels([]);
    }
  }, []);

  const loadCatalog = useCallback(async () => {
    try {
      const c = await fetch(`${API}/api/llamacpp/catalog`).then((r) =>
        r.json(),
      );
      setCatalog(Array.isArray(c) ? c : []);
    } catch {
      setCatalog([]);
    }
  }, []);

  const loadConfig = useCallback(async () => {
    try {
      const c: LlamaCppConfig = await fetch(`${API}/api/llamacpp/config`).then(
        (r) => r.json(),
      );
      setConfig(c);
      setDraft(c);
    } catch {}
  }, []);

  useEffect(() => {
    loadStatus();
    loadConfig();
    loadModels();
    loadCatalog();
    const iv = setInterval(loadStatus, 10000);
    return () => clearInterval(iv);
  }, [loadStatus, loadConfig, loadModels, loadCatalog]);

  // SSE tracking for active downloads
  function startSSE(modelId: string) {
    if (sseRefs.current[modelId]) return;
    const es = new EventSource(
      `${API}/api/llamacpp/download/${modelId}/stream`,
    );
    sseRefs.current[modelId] = es;
    es.onmessage = (e) => {
      const d: DownloadStatus & { status: string } = JSON.parse(e.data);
      setDownloads((prev) => ({
        ...prev,
        [modelId]: { ...d, model_id: modelId },
      }));
      if (d.status === "done") {
        es.close();
        delete sseRefs.current[modelId];
        loadModels();
        loadCatalog();
        showToast(`Модель ${modelId} загружена успешно`, true);
      } else if (d.status === "error") {
        es.close();
        delete sseRefs.current[modelId];
        showToast(`Ошибка загрузки ${modelId}: ${d.error ?? "unknown"}`, false);
      }
    };
    es.onerror = () => {
      es.close();
      delete sseRefs.current[modelId];
    };
  }

  async function startDownload(modelId: string, url?: string) {
    try {
      await mutFetch(`${API}/api/llamacpp/download`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        body: JSON.stringify({ model_id: modelId, url: url || undefined }),
      });
      setDownloads((prev) => ({
        ...prev,
        [modelId]: {
          model_id: modelId,
          status: "downloading",
          progress_bytes: 0,
          total_bytes: 0,
          progress_pct: 0,
          error: null,
        },
      }));
      startSSE(modelId);
      showToast("Загрузка началась…", true);
    } catch {
      showToast("Не удалось начать загрузку", false);
    }
  }

  async function cancelDownload(modelId: string) {
    await mutFetch(`${API}/api/llamacpp/download/${modelId}`, {
      method: "DELETE",
      headers: await csrfHeaders(),
    });
    setDownloads((prev) => ({
      ...prev,
      [modelId]: { ...prev[modelId], status: "cancelled" },
    }));
  }

  async function activateModel(path: string) {
    try {
      const updated: LlamaCppConfig = await mutFetch(
        `${API}/api/llamacpp/models/activate`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(await csrfHeaders()),
          },
          body: JSON.stringify({ path }),
        },
      ).then((r) => r.json());
      setConfig(updated);
      setDraft(updated);
      setModels((prev) => prev.map((m) => ({ ...m, active: m.path === path })));
      showToast(
        "Модель активирована. Перезапустите llama-server для применения.",
        true,
      );
    } catch {
      showToast("Ошибка активации модели", false);
    }
  }

  async function deleteModel(filename: string) {
    setDeleting(filename);
    try {
      await mutFetch(
        `${API}/api/llamacpp/models/${encodeURIComponent(filename)}`,
        {
          method: "DELETE",
          headers: await csrfHeaders(),
        },
      );
      await loadModels();
      await loadCatalog();
      showToast(`Модель ${filename} удалена`, true);
    } catch {
      showToast("Ошибка удаления модели", false);
    } finally {
      setDeleting(null);
    }
  }

  async function saveConfig() {
    if (!draft || !config) return;
    setSaving(true);
    try {
      const changes: Partial<LlamaCppConfig> = {};
      for (const k of Object.keys(draft) as Array<keyof LlamaCppConfig>) {
        if (draft[k] !== config[k])
          (changes as Record<string, unknown>)[k] = draft[k];
      }
      if (!Object.keys(changes).length) {
        showToast("Изменений нет", true);
        return;
      }
      const updated: LlamaCppConfig = await mutFetch(
        `${API}/api/llamacpp/config`,
        {
          method: "PATCH",
          headers: {
            "Content-Type": "application/json",
            ...(await csrfHeaders()),
          },
          body: JSON.stringify(changes),
        },
      ).then((r) => r.json());
      setConfig(updated);
      setDraft(updated);
      showToast(
        "Конфигурация сохранена. Перезапустите llama-server для применения.",
        true,
      );
    } catch {
      showToast("Ошибка сохранения конфигурации", false);
    } finally {
      setSaving(false);
    }
  }

  async function runTest() {
    setTesting(true);
    setTestResult(null);
    try {
      const r = await mutFetch(`${API}/api/llamacpp/test`, {
        method: "POST",
        headers: await csrfHeaders(),
      }).then((res) => res.json());
      setTestResult(r);
    } catch {
      setTestResult({ ok: false, error: "Сетевая ошибка" });
    } finally {
      setTesting(false);
    }
  }

  function setDraftField(field: keyof LlamaCppConfig, value: unknown) {
    setDraft((d) => (d ? { ...d, [field]: value } : d));
  }

  const hasChanges =
    draft && config
      ? Object.keys(draft).some(
          (k) =>
            draft[k as keyof LlamaCppConfig] !==
            config[k as keyof LlamaCppConfig],
        )
      : false;

  // ── Tab: Status ────────────────────────────────────────────────────────────

  const StatusTab = () => (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h2 className="font-semibold text-lg">Статус llama-server</h2>
        <div className="flex gap-2">
          <button
            onClick={runTest}
            disabled={testing || !status?.running}
            className="text-sm px-3 py-1.5 border rounded hover:bg-gray-50 disabled:opacity-50"
          >
            {testing ? "Тест…" : "Тест генерации"}
          </button>
          <button
            onClick={loadStatus}
            className="text-sm text-blue-600 hover:underline"
          >
            ↺ Обновить
          </button>
        </div>
      </div>

      {status && (
        <div className="border rounded-lg overflow-hidden">
          <div
            className={`px-4 py-3 flex items-center gap-3 ${status.running ? "bg-green-50 border-b border-green-200" : "bg-red-50 border-b border-red-200"}`}
          >
            <span
              className={`w-3 h-3 rounded-full ${status.running ? "bg-green-500" : "bg-red-400"}`}
            />
            <span className="font-semibold">
              {status.running ? "Сервер запущен" : "Сервер недоступен"}
            </span>
            <span className="text-sm text-gray-500 ml-auto">{status.url}</span>
          </div>
          <div className="p-4 grid grid-cols-2 gap-y-3 gap-x-6 text-sm">
            {status.running ? (
              <>
                <div className="text-gray-500">Загруженная модель</div>
                <div
                  className="font-mono text-xs truncate font-medium"
                  title={status.model_loaded ?? ""}
                >
                  {status.model_loaded
                    ? status.model_loaded.split("/").pop()
                    : "—"}
                </div>
                <div className="text-gray-500">Контекст</div>
                <div>
                  {status.ctx_size
                    ? `${status.ctx_size.toLocaleString()} токенов`
                    : "—"}
                </div>
                <div className="text-gray-500">KV-кэш</div>
                <div>{status.kv_cache_type ?? "—"}</div>
                <div className="text-gray-500">Слоты (idle / active)</div>
                <div>
                  {status.slots_idle ?? "—"} / {status.slots_processing ?? "—"}
                </div>
                {status.version && (
                  <>
                    <div className="text-gray-500">Версия сборки</div>
                    <div>{status.version}</div>
                  </>
                )}
              </>
            ) : (
              <div className="col-span-2 text-sm text-gray-600 space-y-2">
                <p>Сервер не запущен или недоступен.</p>
                <p className="text-xs">Запустите профиль:</p>
                <code className="block bg-gray-100 rounded px-3 py-2 text-xs font-mono">
                  docker compose --profile embedded-llamacpp up -d
                </code>
                <p className="text-xs text-gray-400">
                  Или укажите URL внешнего сервера в разделе «Конфигурация».
                </p>
              </div>
            )}
          </div>
        </div>
      )}

      {testResult && (
        <div
          className={`p-4 border rounded-lg text-sm ${testResult.ok ? "bg-green-50 border-green-200" : "bg-red-50 border-red-200"}`}
        >
          <div className="font-medium mb-1">
            {testResult.ok ? "Тест пройден" : "Ошибка теста"}
          </div>
          {testResult.ok ? (
            <div>
              <span className="text-gray-500">Ответ: </span>
              <span className="font-mono">{testResult.response}</span>
            </div>
          ) : (
            <div className="text-red-700">{testResult.error}</div>
          )}
        </div>
      )}

      <div className="border rounded-lg p-4 bg-gray-50 text-sm space-y-2">
        <p className="font-medium">Команды управления</p>
        <div className="space-y-1.5 text-xs font-mono">
          <div>
            <span className="text-gray-400"># Запуск</span>
            <code className="block bg-white border rounded px-2 py-1 mt-0.5">
              docker compose --profile embedded-llamacpp up -d
            </code>
          </div>
          <div>
            <span className="text-gray-400"># Остановка</span>
            <code className="block bg-white border rounded px-2 py-1 mt-0.5">
              docker compose --profile embedded-llamacpp stop llama-server
            </code>
          </div>
          <div>
            <span className="text-gray-400"># Логи</span>
            <code className="block bg-white border rounded px-2 py-1 mt-0.5">
              docker compose --profile embedded-llamacpp logs -f llama-server
            </code>
          </div>
        </div>
      </div>
    </div>
  );

  // ── Tab: Local Models ──────────────────────────────────────────────────────

  const ModelsTab = () => (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="font-semibold text-lg">Локальные модели</h2>
        <button
          onClick={loadModels}
          className="text-sm text-blue-600 hover:underline"
        >
          ↺ Обновить
        </button>
      </div>

      {models.length === 0 ? (
        <div className="border rounded-lg p-6 text-center text-sm text-gray-500 space-y-2">
          <p>GGUF-файлы не найдены.</p>
          <p className="text-xs">
            Перейдите во вкладку «Каталог» для загрузки готовых моделей.
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {models.map((m) => (
            <div
              key={m.path}
              className={`border rounded-lg p-4 flex items-center gap-4 ${m.active ? "border-blue-400 bg-blue-50" : "hover:bg-gray-50"}`}
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-sm font-medium truncate">
                    {m.name}
                  </span>
                  {m.active && (
                    <span className="text-xs bg-blue-600 text-white px-2 py-0.5 rounded">
                      активна
                    </span>
                  )}
                </div>
                <div className="text-xs text-gray-400 mt-0.5 truncate">
                  {m.path}
                </div>
              </div>
              <div className="text-sm text-gray-500 whitespace-nowrap">
                {m.size_human}
              </div>
              <div className="flex gap-2">
                {!m.active && (
                  <button
                    onClick={() => activateModel(m.path)}
                    className="text-xs px-3 py-1 bg-blue-600 text-white rounded hover:bg-blue-700"
                  >
                    Активировать
                  </button>
                )}
                <button
                  onClick={() => {
                    if (confirm(`Удалить ${m.name}?`)) deleteModel(m.name);
                  }}
                  disabled={deleting === m.name}
                  className="text-xs px-2 py-1 border border-red-300 text-red-600 rounded hover:bg-red-50 disabled:opacity-50"
                >
                  {deleting === m.name ? "…" : "Удалить"}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="border rounded-lg p-4 space-y-3">
        <p className="text-sm font-medium">Добавить модель по URL</p>
        <p className="text-xs text-gray-500">
          Укажите прямую ссылку на .gguf файл (HuggingFace, direct link).
        </p>
        <div className="flex gap-2">
          <input
            type="url"
            value={customUrl}
            onChange={(e) => setCustomUrl(e.target.value)}
            placeholder="https://huggingface.co/.../model.gguf"
            className="flex-1 border rounded px-3 py-2 text-sm font-mono"
          />
          <button
            onClick={() => {
              if (!customUrl) return;
              const id = `custom_${Date.now()}`;
              startDownload(id, customUrl);
              setCustomUrl("");
            }}
            disabled={!customUrl}
            className="px-4 py-2 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 disabled:opacity-50"
          >
            Загрузить
          </button>
        </div>
      </div>
    </div>
  );

  // ── Tab: Catalog ───────────────────────────────────────────────────────────

  const CatalogTab = () => (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="font-semibold text-lg">Каталог моделей</h2>
        <button
          onClick={loadCatalog}
          className="text-sm text-blue-600 hover:underline"
        >
          ↺ Обновить
        </button>
      </div>
      <p className="text-sm text-gray-500">
        Готовые GGUF-модели с HuggingFace. Нажмите «Загрузить» — файл сохранится
        в том{" "}
        <code className="text-xs font-mono bg-gray-100 px-1 rounded">
          llamacpp_models
        </code>
        .
      </p>

      <div className="grid gap-3">
        {catalog.map((entry) => {
          const dl = downloads[entry.id];
          const isDownloading = dl?.status === "downloading";
          const isDone = dl?.status === "done" || entry.downloaded;
          return (
            <div
              key={entry.id}
              className={`border rounded-lg p-4 space-y-3 ${isDone ? "border-green-300 bg-green-50" : ""}`}
            >
              <div className="flex items-start justify-between gap-4">
                <div className="flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-semibold text-sm">{entry.name}</span>
                    <span className="text-xs font-mono bg-gray-100 px-1.5 py-0.5 rounded">
                      {entry.quant}
                    </span>
                    <span className="text-xs text-gray-500">
                      {entry.params}
                    </span>
                    <span className="text-xs text-gray-400">
                      ctx {(entry.ctx / 1024).toFixed(0)}K
                    </span>
                    {entry.tags.map((t) => (
                      <TagBadge key={t} tag={t} />
                    ))}
                  </div>
                  <p className="text-sm text-gray-600 mt-1">
                    {entry.description}
                  </p>
                  <p className="text-xs text-gray-400 mt-1 font-mono">
                    {entry.filename}
                  </p>
                </div>
                <div className="text-right flex-shrink-0 space-y-1">
                  <div className="text-sm font-medium text-gray-700">
                    {entry.size_human}
                  </div>
                  {isDone ? (
                    <div className="flex gap-1 justify-end">
                      {entry.local_path && (
                        <button
                          onClick={() => activateModel(entry.local_path!)}
                          className="text-xs px-2 py-1 bg-blue-600 text-white rounded hover:bg-blue-700"
                        >
                          Активировать
                        </button>
                      )}
                      <span className="text-xs text-green-700 font-medium">
                        Загружено
                      </span>
                    </div>
                  ) : isDownloading ? (
                    <button
                      onClick={() => cancelDownload(entry.id)}
                      className="text-xs px-2 py-1 border border-red-300 text-red-600 rounded hover:bg-red-50"
                    >
                      Отмена
                    </button>
                  ) : (
                    <button
                      onClick={() => startDownload(entry.id)}
                      className="text-xs px-3 py-1.5 bg-blue-600 text-white rounded hover:bg-blue-700"
                    >
                      Загрузить
                    </button>
                  )}
                </div>
              </div>

              {/* Progress bar */}
              {isDownloading && dl && (
                <div className="space-y-1">
                  <div className="flex justify-between text-xs text-gray-500">
                    <span>
                      {humanBytes(dl.progress_bytes)} /{" "}
                      {dl.total_bytes > 0 ? humanBytes(dl.total_bytes) : "?"}
                    </span>
                    <span>{dl.progress_pct.toFixed(1)}%</span>
                  </div>
                  <div className="w-full bg-gray-200 rounded-full h-2">
                    <div
                      className="bg-blue-500 h-2 rounded-full transition-all duration-300"
                      style={{ width: `${dl.progress_pct}%` }}
                    />
                  </div>
                </div>
              )}

              {dl?.status === "error" && (
                <div className="text-xs text-red-600 bg-red-50 border border-red-200 rounded px-2 py-1">
                  Ошибка: {dl.error}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );

  // ── Tab: Config ────────────────────────────────────────────────────────────

  const ConfigTab = () => (
    <div className="space-y-5">
      <h2 className="font-semibold text-lg">Конфигурация запуска</h2>
      <p className="text-sm text-gray-500">
        Параметры применяются при следующем запуске llama-server. Сохраняются в
        Redis.
      </p>

      {draft && (
        <>
          <div className="space-y-1">
            <label className="text-sm font-medium">URL сервера</label>
            <input
              type="text"
              value={draft.url}
              onChange={(e) => setDraftField("url", e.target.value)}
              className="w-full border rounded px-3 py-2 text-sm font-mono"
              placeholder="http://llama-server:8080"
            />
            <p className="text-xs text-gray-400">
              Docker-сеть:{" "}
              <code className="font-mono">http://llama-server:8080</code>{" "}
              &nbsp;|&nbsp; Хост:{" "}
              <code className="font-mono">http://localhost:11436</code>
            </p>
          </div>

          <div className="space-y-1">
            <label className="text-sm font-medium">Путь к модели</label>
            <input
              type="text"
              value={draft.model}
              onChange={(e) => setDraftField("model", e.target.value)}
              className="w-full border rounded px-3 py-2 text-sm font-mono"
              placeholder="/models/model.gguf"
            />
            {models.length > 0 && (
              <select
                className="w-full border rounded px-3 py-1.5 text-sm mt-1"
                value={draft.model}
                onChange={(e) => setDraftField("model", e.target.value)}
              >
                <option value="">— выбрать из загруженных —</option>
                {models.map((m) => (
                  <option key={m.path} value={m.path}>
                    {m.name} ({m.size_human})
                  </option>
                ))}
              </select>
            )}
          </div>

          <div className="space-y-2">
            <label className="text-sm font-medium">
              Контекст:{" "}
              <span className="font-mono">
                {draft.ctx_size.toLocaleString()} токенов
              </span>
            </label>
            <input
              type="range"
              min={512}
              max={131072}
              step={512}
              value={draft.ctx_size}
              onChange={(e) =>
                setDraftField("ctx_size", Number(e.target.value))
              }
              className="w-full"
            />
            <div className="flex justify-between text-xs text-gray-400">
              <span>512</span>
              <span>8K</span>
              <span>32K</span>
              <span>64K</span>
              <span>128K</span>
            </div>
          </div>

          <div className="space-y-1">
            <label className="text-sm font-medium">KV-кэш (TurboQuant)</label>
            <select
              value={draft.kv_cache_type}
              onChange={(e) => setDraftField("kv_cache_type", e.target.value)}
              className="w-full border rounded px-3 py-2 text-sm"
            >
              {KV_CACHE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
            <p className="text-xs text-gray-400">
              q8_0 сокращает память KV-кэша в 2× (TurboQuant), q4_0 в 4× с
              небольшой потерей качества.
            </p>
          </div>

          <div className="space-y-2">
            <label className="text-sm font-medium">
              GPU слои:{" "}
              <span className="font-mono">
                {draft.n_gpu_layers === -1 ? "все (-1)" : draft.n_gpu_layers}
              </span>
            </label>
            <input
              type="range"
              min={-1}
              max={128}
              step={1}
              value={draft.n_gpu_layers}
              onChange={(e) =>
                setDraftField("n_gpu_layers", Number(e.target.value))
              }
              className="w-full"
            />
            <div className="flex justify-between text-xs text-gray-400">
              <span>CPU (-1=все)</span>
              <span>32</span>
              <span>64</span>
              <span>128</span>
            </div>
          </div>

          <div className="space-y-2">
            <label className="text-sm font-medium">
              Параллельных слотов:{" "}
              <span className="font-mono">{draft.parallel}</span>
            </label>
            <input
              type="range"
              min={1}
              max={16}
              step={1}
              value={draft.parallel}
              onChange={(e) =>
                setDraftField("parallel", Number(e.target.value))
              }
              className="w-full"
            />
            <div className="flex justify-between text-xs text-gray-400">
              <span>1</span>
              <span>4</span>
              <span>8</span>
              <span>16</span>
            </div>
          </div>

          <div className="flex items-center gap-3">
            <input
              id="flash-attn"
              type="checkbox"
              checked={draft.flash_attn}
              onChange={(e) => setDraftField("flash_attn", e.target.checked)}
              className="w-4 h-4"
            />
            <label
              htmlFor="flash-attn"
              className="text-sm font-medium cursor-pointer"
            >
              Flash Attention
            </label>
            <span className="text-xs text-gray-400">
              Ускоряет на совместимых GPU (~15–25%)
            </span>
          </div>

          <div className="flex gap-3 pt-2 border-t">
            <button
              onClick={saveConfig}
              disabled={saving || !hasChanges}
              className="px-5 py-2 bg-blue-600 text-white rounded text-sm font-medium hover:bg-blue-700 disabled:opacity-50"
            >
              {saving ? "Сохранение…" : "Сохранить"}
            </button>
            {hasChanges && (
              <button
                onClick={() => setDraft(config)}
                className="px-4 py-2 border rounded text-sm hover:bg-gray-50"
              >
                Отменить
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );

  // ── Render ─────────────────────────────────────────────────────────────────

  const TABS: { id: Tab; label: string; badge?: number }[] = [
    { id: "status", label: "Статус" },
    { id: "models", label: "Модели", badge: models.length || undefined },
    {
      id: "catalog",
      label: "Каталог",
      badge: catalog.filter((c) => !c.downloaded).length || undefined,
    },
    { id: "config", label: "Конфигурация" },
  ];

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <div>
        <h1 className="text-xl font-bold">llama.cpp</h1>
        <p className="text-sm text-gray-500 mt-1">
          Локальный AI-бэкенд — быстрый вывод GGUF, TurboQuant KV-кэш, MTP,
          GPU/CPU
        </p>
      </div>

      {/* Tab nav */}
      <div className="flex gap-0 border-b">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors flex items-center gap-1.5 ${
              tab === t.id
                ? "border-blue-600 text-blue-600"
                : "border-transparent text-gray-500 hover:text-gray-800"
            }`}
          >
            {t.label}
            {t.badge != null && t.badge > 0 && (
              <span className="text-xs bg-gray-200 text-gray-600 px-1.5 py-0.5 rounded-full">
                {t.badge}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div>
        {tab === "status" && <StatusTab />}
        {tab === "models" && <ModelsTab />}
        {tab === "catalog" && <CatalogTab />}
        {tab === "config" && <ConfigTab />}
      </div>

      {/* Toast */}
      {toast && (
        <div
          className={`fixed bottom-6 right-6 z-50 px-4 py-3 rounded-lg shadow-lg text-sm font-medium transition-all ${
            toast.ok ? "bg-green-600 text-white" : "bg-red-600 text-white"
          }`}
        >
          {toast.text}
        </div>
      )}
    </div>
  );
}
