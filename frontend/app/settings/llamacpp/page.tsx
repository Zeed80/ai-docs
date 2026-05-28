"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders, mutFetch } from "@/lib/auth";

const API = getApiBaseUrl();

// ── Shared style tokens (dark-first, matching settings/page.tsx) ────────────
const inputCls =
  "w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50";
const selectCls =
  "w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500";
const cardCls = "border border-slate-700 rounded-lg overflow-hidden";
const cardHeaderCls =
  "px-4 py-2 bg-slate-800 border-b border-slate-700 flex items-center justify-between";
const rowHoverCls = "hover:bg-slate-700/50 transition-colors";

// ── Types ──────────────────────────────────────────────────────────────────

interface Status {
  running: boolean;
  url: string;
  model_loaded: string | null;
  ctx_size: number | null;
  slots_idle: number | null;
  slots_processing: number | null;
  version: string | null;
  kv_cache_type: string | null;
  vision: boolean;
  mmproj_path: string | null;
}
interface Config {
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
  is_mmproj: boolean;
}
interface HFFile {
  filename: string;
  size_bytes: number;
  size_human: string;
  quant: string;
  is_split: boolean;
  split_group: string | null;
  part_index: number | null;
  total_parts: number | null;
}
interface HFModel {
  repo_id: string;
  author: string;
  model_name: string;
  downloads: number;
  likes: number;
  tags: string[];
  gated: boolean;
  files: HFFile[];
}
interface MSModel {
  repo_id: string;
  name: string;
  downloads: number;
  stars: number;
  tags: string[];
  files: HFFile[];
}
interface DownloadStatus {
  download_id: string;
  repo_id: string;
  filename: string;
  status: string;
  progress_bytes: number;
  total_bytes: number;
  progress_pct: number;
  error: string | null;
}
interface TokensStatus {
  huggingface_set: boolean;
  modelscope_set: boolean;
}

type Tab = "search" | "local" | "config" | "tokens";
type Source = "huggingface" | "modelscope";

const QUANTS = [
  "Q2_K",
  "Q3_K_M",
  "Q4_0",
  "Q4_K_M",
  "Q4_K_S",
  "Q5_K_M",
  "Q6_K",
  "Q8_0",
  "F16",
  "BF16",
  "IQ4_XS",
];
const KV_OPTIONS = [
  { value: "f16", label: "f16 — полная точность" },
  { value: "q8_0", label: "q8_0 — TurboQuant 2× (рекомендуется)" },
  { value: "q4_0", label: "q4_0 — TurboQuant 4×" },
];

function humanBytes(b: number): string {
  if (!b) return "—";
  const u = ["B", "KB", "MB", "GB"];
  let v = b;
  for (const unit of u) {
    if (v < 1024) return `${v.toFixed(1)} ${unit}`;
    v /= 1024;
  }
  return `${v.toFixed(1)} TB`;
}
function fmtNum(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)}K`;
  return String(n);
}
function quantColor(q: string): string {
  if (q.includes("Q8") || q.includes("F16") || q.includes("BF16"))
    return "bg-green-900/60 text-green-300";
  if (q.includes("Q6") || q.includes("Q5"))
    return "bg-teal-900/60 text-teal-300";
  if (q.includes("Q4_K_M") || q.includes("Q4_K_L"))
    return "bg-blue-900/60 text-blue-300";
  if (q.includes("Q4")) return "bg-indigo-900/60 text-indigo-300";
  if (q.includes("Q3")) return "bg-orange-900/60 text-orange-300";
  if (q.includes("Q2") || q.includes("IQ")) return "bg-red-900/60 text-red-300";
  return "bg-slate-700 text-slate-300";
}

// ── Search Tab ─────────────────────────────────────────────────────────────

const SS_KEY = "llamacpp_search_state";

function SearchTab({
  onDownloadStart,
}: {
  onDownloadStart: (dl: DownloadStatus) => void;
}) {
  const [source, setSource] = useState<Source>("huggingface");
  const [inputQ, setInputQ] = useState("");
  const [quant, setQuant] = useState("");
  const [maxGb, setMaxGb] = useState(100);
  const [sort, setSort] = useState("downloads");
  const [loading, setLoading] = useState(false);
  const [hfResults, setHfResults] = useState<HFModel[]>([]);
  const [msResults, setMsResults] = useState<MSModel[]>([]);
  const [expanded, setExpanded] = useState<Record<string, HFFile[]>>({});
  const [loadingFiles, setLoadingFiles] = useState<Record<string, boolean>>({});
  const [downloading, setDownloading] = useState<Record<string, boolean>>({});
  const [toast, setToast] = useState<string | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const restoredRef = useRef(false);

  // Restore from sessionStorage on mount
  useEffect(() => {
    if (restoredRef.current) return;
    restoredRef.current = true;
    try {
      const raw = sessionStorage.getItem(SS_KEY);
      if (!raw) return;
      const s = JSON.parse(raw);
      if (s.source) setSource(s.source);
      if (s.inputQ) setInputQ(s.inputQ);
      if (s.quant !== undefined) setQuant(s.quant);
      if (s.maxGb !== undefined) setMaxGb(s.maxGb);
      if (s.sort) setSort(s.sort);
      if (Array.isArray(s.hfResults)) setHfResults(s.hfResults);
      if (Array.isArray(s.msResults)) setMsResults(s.msResults);
    } catch {}
  }, []);

  // Persist to sessionStorage whenever key state changes
  useEffect(() => {
    try {
      sessionStorage.setItem(
        SS_KEY,
        JSON.stringify({
          source,
          inputQ,
          quant,
          maxGb,
          sort,
          hfResults,
          msResults,
        }),
      );
    } catch {}
  }, [source, inputQ, quant, maxGb, sort, hfResults, msResults]);

  const showToast = (msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(null), 3500);
  };

  async function search(query: string) {
    if (!query.trim()) return;
    setLoading(true);
    setHfResults([]);
    setMsResults([]);
    try {
      if (source === "huggingface") {
        const params = new URLSearchParams({ q: query, sort, limit: "24" });
        if (quant) params.set("quant", quant);
        if (maxGb < 100) params.set("max_gb", String(maxGb));
        const r = await fetch(`${API}/api/llamacpp/hf/search?${params}`).then(
          (r) => r.json(),
        );
        setHfResults(Array.isArray(r) ? r : []);
      } else {
        const resp = await fetch(
          `${API}/api/llamacpp/ms/search?q=${encodeURIComponent(query)}&limit=24`,
        );
        if (resp.status === 401) {
          const err = await resp.json().catch(() => ({}));
          showToast(
            err.detail ||
              "ModelScope: необходим токен — добавьте его во вкладке Токены",
          );
        } else {
          const r = await resp.json();
          setMsResults(Array.isArray(r) ? r : []);
        }
      }
    } catch {
      showToast("Ошибка поиска");
    } finally {
      setLoading(false);
    }
  }

  function handleInput(v: string) {
    setInputQ(v);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => search(v), 600);
  }

  async function loadFiles(repoId: string) {
    if (expanded[repoId] !== undefined) {
      setExpanded((p) => {
        const n = { ...p };
        delete n[repoId];
        return n;
      });
      return;
    }
    setLoadingFiles((p) => ({ ...p, [repoId]: true }));
    try {
      const ep =
        source === "huggingface"
          ? `${API}/api/llamacpp/hf/model/${encodeURIComponent(repoId)}/files?${quant ? `quant=${quant}&` : ""}max_gb=${maxGb}&include_split=true`
          : `${API}/api/llamacpp/ms/model/${encodeURIComponent(repoId)}/files`;
      const files: HFFile[] = await fetch(ep).then((r) => r.json());
      setExpanded((p) => ({
        ...p,
        [repoId]: Array.isArray(files) ? files : [],
      }));
    } catch {
      showToast("Не удалось загрузить список файлов");
    } finally {
      setLoadingFiles((p) => ({ ...p, [repoId]: false }));
    }
  }

  async function startDownload(repoId: string, filename: string) {
    const key = `${repoId}/${filename}`;
    setDownloading((p) => ({ ...p, [key]: true }));
    try {
      const res = await mutFetch(`${API}/api/llamacpp/download`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        body: JSON.stringify({ repo_id: repoId, filename, source }),
      });
      const data = await res.json();
      if (!res.ok) {
        showToast(data.detail || "Ошибка загрузки");
        return;
      }
      showToast(`Загрузка ${filename} начата…`);
      onDownloadStart({
        download_id: data.download_id,
        repo_id: repoId,
        filename,
        status: "downloading",
        progress_bytes: 0,
        total_bytes: 0,
        progress_pct: 0,
        error: null,
      });
    } catch {
      showToast("Ошибка запуска загрузки");
    } finally {
      setDownloading((p) => ({ ...p, [key]: false }));
    }
  }

  const results = source === "huggingface" ? hfResults : msResults;

  return (
    <div className="space-y-4">
      {/* Source toggle */}
      <div className="flex gap-1 p-1 bg-slate-700 rounded-lg w-fit">
        {(["huggingface", "modelscope"] as Source[]).map((s) => (
          <button
            key={s}
            onClick={() => {
              setSource(s);
              setHfResults([]);
              setMsResults([]);
            }}
            className={`px-4 py-1.5 rounded-md text-sm font-medium transition-colors ${
              source === s
                ? "bg-slate-900 shadow text-slate-100"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            {s === "huggingface" ? "🤗 HuggingFace" : "🌐 ModelScope"}
          </button>
        ))}
      </div>

      {/* Search bar */}
      <div className="flex gap-2">
        <div className="relative flex-1">
          <input
            type="text"
            value={inputQ}
            onChange={(e) => handleInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") search(inputQ);
            }}
            placeholder={
              source === "huggingface"
                ? "Qwen2.5, Llama, Mistral, gemma…"
                : "Qwen, deepseek…"
            }
            className={inputCls + " pr-10"}
          />
          {loading && (
            <div className="absolute right-3 top-3 w-4 h-4 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
          )}
        </div>
        <button
          onClick={() => search(inputQ)}
          className="px-4 py-2.5 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700"
        >
          Найти
        </button>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-2 items-center">
        <span className="text-xs text-slate-400 font-medium">Фильтры:</span>
        <select
          value={quant}
          onChange={(e) => setQuant(e.target.value)}
          className="border border-slate-600 bg-slate-800 text-slate-200 rounded px-2 py-1 text-xs"
        >
          <option value="">Любой квант</option>
          {QUANTS.map((q) => (
            <option key={q} value={q}>
              {q}
            </option>
          ))}
        </select>
        <select
          value={String(maxGb)}
          onChange={(e) => setMaxGb(Number(e.target.value))}
          className="border border-slate-600 bg-slate-800 text-slate-200 rounded px-2 py-1 text-xs"
        >
          <option value="100">Любой размер</option>
          <option value="3">до 3 GB</option>
          <option value="5">до 5 GB</option>
          <option value="8">до 8 GB</option>
          <option value="12">до 12 GB</option>
          <option value="20">до 20 GB</option>
        </select>
        {source === "huggingface" && (
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value)}
            className="border border-slate-600 bg-slate-800 text-slate-200 rounded px-2 py-1 text-xs"
          >
            <option value="downloads">По популярности</option>
            <option value="likes">По лайкам</option>
            <option value="lastModified">По дате</option>
          </select>
        )}
        {(quant || maxGb < 100) && (
          <button
            onClick={() => {
              setQuant("");
              setMaxGb(100);
            }}
            className="text-xs text-red-400 hover:underline"
          >
            Сбросить
          </button>
        )}
      </div>

      {/* Empty state */}
      {results.length === 0 && !loading && inputQ && (
        <div className="text-sm text-slate-500 py-8 text-center">
          {source === "huggingface"
            ? "Ничего не найдено. Попробуйте другой запрос."
            : "ModelScope: модели не найдены или API временно недоступен."}
        </div>
      )}

      {/* Results */}
      <div className="space-y-2">
        {(results as (HFModel | MSModel)[]).map((m) => {
          const repoId = m.repo_id;
          const isHF = source === "huggingface";
          const hf = m as HFModel;
          const ms = m as MSModel;
          const files = expanded[repoId];
          const isExpanded = files !== undefined;
          const isLoadingFiles = loadingFiles[repoId];

          return (
            <div key={repoId} className={cardCls}>
              {/* Model header */}
              <div className={`p-4 flex items-start gap-3 ${rowHoverCls}`}>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-mono text-sm font-semibold text-blue-400 truncate">
                      {repoId}
                    </span>
                    {isHF && hf.gated && (
                      <span className="text-xs bg-yellow-900/60 text-yellow-300 px-1.5 py-0.5 rounded">
                        🔒 gated
                      </span>
                    )}
                    {isHF && hf.files.length > 0 && (
                      <span className="text-xs text-slate-500">
                        {hf.files.length} файлов в кэше
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-3 mt-1 text-xs text-slate-500">
                    <span>⬇ {fmtNum(isHF ? hf.downloads : ms.downloads)}</span>
                    <span>
                      {isHF ? `♥ ${fmtNum(hf.likes)}` : `★ ${fmtNum(ms.stars)}`}
                    </span>
                    {isHF &&
                      hf.tags.slice(0, 3).map((t) => (
                        <span
                          key={t}
                          className="bg-slate-700 px-1.5 py-0.5 rounded"
                        >
                          {t}
                        </span>
                      ))}
                  </div>
                </div>
                <button
                  onClick={() => loadFiles(repoId)}
                  disabled={isLoadingFiles}
                  className="text-xs px-3 py-1.5 border border-slate-600 rounded-md hover:bg-slate-700 text-slate-300 whitespace-nowrap disabled:opacity-50"
                >
                  {isLoadingFiles ? "…" : isExpanded ? "▲ Скрыть" : "▼ Файлы"}
                </button>
              </div>

              {/* File list */}
              {isExpanded && (
                <div className="border-t border-slate-700 bg-slate-900/50">
                  {files.length === 0 ? (
                    <div className="px-4 py-3 text-sm text-slate-500">
                      GGUF-файлы не найдены в этом репозитории.
                    </div>
                  ) : (
                    files.map((f) => {
                      const dlKey = `${repoId}/${f.filename}`;
                      const isDl = downloading[dlKey];
                      const displayName = f.is_split
                        ? f.filename.replace(/-\d+-of-\d+/, "")
                        : f.filename;
                      return (
                        <div
                          key={f.filename}
                          className={`flex items-center gap-3 px-4 py-2.5 border-b border-slate-700/50 last:border-0 ${rowHoverCls}`}
                        >
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-2 flex-wrap">
                              <span
                                className={`text-xs px-1.5 py-0.5 rounded font-mono font-semibold ${quantColor(f.quant)}`}
                              >
                                {f.quant}
                              </span>
                              {f.is_split && (
                                <span className="text-xs bg-orange-900/60 text-orange-300 px-1.5 py-0.5 rounded">
                                  {f.total_parts} частей
                                </span>
                              )}
                              <span className="text-sm font-mono truncate text-slate-200">
                                {displayName}
                              </span>
                            </div>
                            {f.is_split && (
                              <p className="text-xs text-slate-500 mt-0.5">
                                Загрузит все {f.total_parts} части в один запуск
                              </p>
                            )}
                          </div>
                          <span className="text-xs text-slate-500 whitespace-nowrap">
                            {f.size_human}
                          </span>
                          <button
                            onClick={() => startDownload(repoId, f.filename)}
                            disabled={isDl}
                            className="text-xs px-3 py-1 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50 whitespace-nowrap"
                          >
                            {isDl ? "…" : "⬇ Загрузить"}
                          </button>
                        </div>
                      );
                    })
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {toast && (
        <div className="fixed bottom-6 right-6 bg-slate-700 text-slate-100 text-sm px-4 py-2 rounded-lg shadow-lg z-50 border border-slate-600">
          {toast}
        </div>
      )}
    </div>
  );
}

// ── Downloads Panel ────────────────────────────────────────────────────────

function DownloadsPanel({
  downloads,
  onDone,
}: {
  downloads: DownloadStatus[];
  onDone: () => void;
}) {
  const active = downloads.filter((d) =>
    ["downloading", "pending"].includes(d.status),
  );
  if (active.length === 0 && downloads.length === 0) return null;

  return (
    <div className={cardCls}>
      <div className={cardHeaderCls}>
        <span className="text-sm font-medium text-slate-200">
          Загрузки{active.length > 0 && ` (${active.length} активных)`}
        </span>
        {downloads.some((d) => d.status === "done") && (
          <button
            onClick={onDone}
            className="text-xs text-slate-400 hover:text-slate-200"
          >
            Очистить завершённые
          </button>
        )}
      </div>
      {downloads.map((d) => (
        <div
          key={d.download_id}
          className="px-4 py-3 border-b border-slate-700 last:border-0"
        >
          <div className="flex items-center justify-between mb-1">
            <div className="min-w-0">
              <span className="text-xs font-mono text-slate-300 truncate block">
                {d.filename}
              </span>
              <span className="text-xs text-slate-500">{d.repo_id}</span>
            </div>
            <span
              className={`text-xs font-medium ml-3 ${
                d.status === "done"
                  ? "text-green-400"
                  : d.status === "error"
                    ? "text-red-400"
                    : d.status === "cancelled"
                      ? "text-slate-500"
                      : "text-blue-400"
              }`}
            >
              {d.status === "done"
                ? "✓ Готово"
                : d.status === "error"
                  ? "✗ Ошибка"
                  : d.status === "cancelled"
                    ? "Отменено"
                    : `${d.progress_pct.toFixed(1)}%`}
            </span>
          </div>
          {["downloading", "pending"].includes(d.status) && (
            <div className="w-full bg-slate-700 rounded-full h-1.5">
              <div
                className="bg-blue-500 h-1.5 rounded-full transition-all"
                style={{ width: `${d.progress_pct}%` }}
              />
            </div>
          )}
          {d.status === "downloading" && (
            <div className="text-xs text-slate-500 mt-0.5">
              {humanBytes(d.progress_bytes)} /{" "}
              {d.total_bytes > 0 ? humanBytes(d.total_bytes) : "?"}
            </div>
          )}
          {d.error && (
            <div className="text-xs text-red-400 mt-1">{d.error}</div>
          )}
        </div>
      ))}
    </div>
  );
}

// ── Local Models Tab ───────────────────────────────────────────────────────

function LocalTab({
  models,
  vision,
  mmproj_path,
  onRefresh,
  onActivate,
  onDelete,
}: {
  models: GgufModel[];
  vision: boolean;
  mmproj_path: string | null;
  onRefresh: () => void;
  onActivate: (path: string) => void;
  onDelete: (name: string) => void;
}) {
  const [customUrl, setCustomUrl] = useState("");
  const [downloading, setDownloading] = useState(false);

  const mainModels = models.filter((m) => !m.is_mmproj);
  const mmproj = models.filter((m) => m.is_mmproj);

  async function downloadFromUrl() {
    if (!customUrl) return;
    setDownloading(true);
    try {
      const filename =
        customUrl.split("/").pop()?.split("?")[0] || `model_${Date.now()}.gguf`;
      await mutFetch(`${API}/api/llamacpp/download`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        body: JSON.stringify({
          repo_id: "custom",
          filename,
          source: "url",
          url: customUrl,
        }),
      });
      setCustomUrl("");
    } finally {
      setDownloading(false);
    }
  }

  function ModelRow({
    m,
    showActivate,
  }: {
    m: GgufModel;
    showActivate: boolean;
  }) {
    const isLoadedMmproj = m.is_mmproj && mmproj_path === m.path;
    return (
      <div
        className={`${cardCls} p-3 flex items-center gap-3 ${
          m.active || isLoadedMmproj
            ? "border-blue-500 bg-blue-900/20"
            : rowHoverCls
        }`}
      >
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-mono text-sm font-medium truncate text-slate-200">
              {m.name}
            </span>
            {m.active && (
              <span className="text-xs bg-blue-600 text-white px-2 py-0.5 rounded">
                активна
              </span>
            )}
            {isLoadedMmproj && (
              <span className="text-xs bg-purple-700 text-white px-2 py-0.5 rounded">
                загружен
              </span>
            )}
          </div>
          <div className="text-xs text-slate-500 mt-0.5 flex gap-3">
            <span>{m.size_human}</span>
            <span className="truncate">{m.path}</span>
          </div>
        </div>
        <div className="flex gap-2 flex-shrink-0">
          {showActivate && !m.active && (
            <button
              onClick={() => onActivate(m.path)}
              className="text-xs px-3 py-1 bg-blue-600 text-white rounded hover:bg-blue-700"
            >
              Активировать
            </button>
          )}
          <button
            onClick={() => {
              if (confirm(`Удалить ${m.name}?`)) onDelete(m.name);
            }}
            className="text-xs px-2 py-1 border border-red-800 text-red-400 rounded hover:bg-red-900/30"
          >
            ✕
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="font-semibold text-slate-100">Локальные модели</h2>
        <button
          onClick={onRefresh}
          className="text-sm text-blue-400 hover:text-blue-300"
        >
          ↺ Обновить
        </button>
      </div>

      {models.length === 0 ? (
        <div className={`${cardCls} p-8 text-center text-sm text-slate-500`}>
          <p className="text-2xl mb-2">📂</p>
          <p>GGUF-файлы не найдены. Перейдите в «Поиск» и загрузите модель.</p>
        </div>
      ) : (
        <>
          {mainModels.length > 0 && (
            <div className="space-y-2">
              <p className="text-xs font-medium text-slate-400 uppercase tracking-wide">
                Языковые модели
              </p>
              {mainModels.map((m) => (
                <ModelRow key={m.path} m={m} showActivate={true} />
              ))}
            </div>
          )}

          {mmproj.length > 0 && (
            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <p className="text-xs font-medium text-slate-400 uppercase tracking-wide">
                  Vision-проектор (mmproj)
                </p>
                {vision ? (
                  <span className="text-xs bg-purple-700 text-white px-2 py-0.5 rounded">
                    👁 vision включён
                  </span>
                ) : (
                  <span className="text-xs bg-slate-700 text-slate-400 px-2 py-0.5 rounded">
                    vision выключен
                  </span>
                )}
              </div>
              <p className="text-xs text-slate-500">
                mmproj-файлы определяются автоматически при запуске
                llama-server. Файл должен находиться в той же папке, что и
                основная модель.
              </p>
              {mmproj.map((m) => (
                <ModelRow key={m.path} m={m} showActivate={false} />
              ))}
            </div>
          )}
        </>
      )}

      <div className={`${cardCls} p-4 space-y-2`}>
        <p className="text-sm font-medium text-slate-200">
          Добавить по прямой ссылке
        </p>
        <div className="flex gap-2">
          <input
            type="url"
            value={customUrl}
            onChange={(e) => setCustomUrl(e.target.value)}
            placeholder="https://huggingface.co/.../model.gguf"
            className={inputCls + " font-mono text-xs"}
          />
          <button
            onClick={downloadFromUrl}
            disabled={!customUrl || downloading}
            className="px-3 py-2 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 disabled:opacity-50"
          >
            {downloading ? "…" : "Загрузить"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Config Tab ─────────────────────────────────────────────────────────────

function ConfigTab({
  config,
  models,
  onSave,
}: {
  config: Config;
  models: GgufModel[];
  onSave: (c: Partial<Config>) => void;
}) {
  const [draft, setDraft] = useState(config);
  useEffect(() => setDraft(config), [config]);

  const hasChanges = JSON.stringify(draft) !== JSON.stringify(config);
  function set<K extends keyof Config>(k: K, v: Config[K]) {
    setDraft((d) => ({ ...d, [k]: v }));
  }

  return (
    <div className="space-y-5">
      <div>
        <h2 className="font-semibold text-slate-100">
          Конфигурация llama-server
        </h2>
        <p className="text-sm text-slate-400 mt-1">
          Применяется при следующем запуске сервера.
        </p>
      </div>

      <div className="space-y-1">
        <label className="text-sm font-medium text-slate-300">
          URL сервера
        </label>
        <input
          type="text"
          value={draft.url}
          onChange={(e) => set("url", e.target.value)}
          className={inputCls + " font-mono"}
        />
        <p className="text-xs text-slate-500">
          Docker:{" "}
          <code className="text-slate-400">http://llama-server:8080</code> |
          Хост: <code className="text-slate-400">http://localhost:11436</code>
        </p>
      </div>

      <div className="space-y-1">
        <label className="text-sm font-medium text-slate-300">
          Активная модель
        </label>
        {models.length > 0 ? (
          <select
            value={draft.model}
            onChange={(e) => set("model", e.target.value)}
            className={selectCls}
          >
            <option value="">— не выбрана —</option>
            {models.map((m) => (
              <option key={m.path} value={m.path}>
                {m.name} ({m.size_human})
              </option>
            ))}
          </select>
        ) : (
          <input
            type="text"
            value={draft.model}
            onChange={(e) => set("model", e.target.value)}
            placeholder="/llamacpp-models/model.gguf"
            className={inputCls + " font-mono"}
          />
        )}
      </div>

      <div className="space-y-2">
        <label className="text-sm font-medium text-slate-300">
          Контекст:{" "}
          <span className="font-mono text-slate-100">
            {draft.ctx_size.toLocaleString()} токенов
          </span>
        </label>
        <input
          type="range"
          min={512}
          max={131072}
          step={512}
          value={draft.ctx_size}
          onChange={(e) => set("ctx_size", Number(e.target.value))}
          className="w-full accent-blue-500"
        />
        <div className="flex justify-between text-xs text-slate-500">
          <span>512</span>
          <span>8K</span>
          <span>32K</span>
          <span>64K</span>
          <span>128K</span>
        </div>
      </div>

      <div className="space-y-1">
        <label className="text-sm font-medium text-slate-300">
          KV-кэш (TurboQuant)
        </label>
        <select
          value={draft.kv_cache_type}
          onChange={(e) => set("kv_cache_type", e.target.value)}
          className={selectCls}
        >
          {KV_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>

      <div className="space-y-2">
        <label className="text-sm font-medium text-slate-300">
          GPU слои:{" "}
          <span className="font-mono text-slate-100">
            {draft.n_gpu_layers === -1 ? "все (-1)" : draft.n_gpu_layers}
          </span>
        </label>
        <input
          type="range"
          min={-1}
          max={128}
          step={1}
          value={draft.n_gpu_layers}
          onChange={(e) => set("n_gpu_layers", Number(e.target.value))}
          className="w-full accent-blue-500"
        />
        <div className="flex justify-between text-xs text-slate-500">
          <span>CPU (−1=все)</span>
          <span>32</span>
          <span>64</span>
          <span>128</span>
        </div>
      </div>

      <div className="space-y-2">
        <label className="text-sm font-medium text-slate-300">
          Слотов параллельно:{" "}
          <span className="font-mono text-slate-100">{draft.parallel}</span>
        </label>
        <input
          type="range"
          min={1}
          max={16}
          step={1}
          value={draft.parallel}
          onChange={(e) => set("parallel", Number(e.target.value))}
          className="w-full accent-blue-500"
        />
      </div>

      <label className="flex items-center gap-3 cursor-pointer">
        <input
          id="fa"
          type="checkbox"
          checked={draft.flash_attn}
          onChange={(e) => set("flash_attn", e.target.checked)}
          className="w-4 h-4 accent-blue-500"
        />
        <span className="text-sm text-slate-200">
          Flash Attention (~15–25% быстрее на совместимых GPU)
        </span>
      </label>

      <div className="flex gap-3 pt-2 border-t border-slate-700">
        <button
          onClick={() => onSave(draft)}
          disabled={!hasChanges}
          className="px-5 py-2 bg-blue-600 text-white rounded text-sm font-medium hover:bg-blue-700 disabled:opacity-40"
        >
          Сохранить
        </button>
        {hasChanges && (
          <button
            onClick={() => setDraft(config)}
            className="px-4 py-2 border border-slate-600 text-slate-300 rounded text-sm hover:bg-slate-700"
          >
            Отменить
          </button>
        )}
      </div>

      <div className={`${cardCls} p-4 space-y-2 bg-slate-900/50`}>
        <p className="font-medium text-xs text-slate-500 uppercase tracking-wide">
          Команды управления
        </p>
        {[
          ["Запуск", "docker compose --profile embedded-llamacpp up -d"],
          [
            "Остановка",
            "docker compose --profile embedded-llamacpp stop llama-server",
          ],
          [
            "Логи",
            "docker compose --profile embedded-llamacpp logs -f llama-server",
          ],
        ].map(([label, cmd]) => (
          <div key={label}>
            <span className="text-xs text-slate-500"># {label}</span>
            <code className="block bg-slate-950 border border-slate-700 rounded px-2 py-1 text-xs font-mono mt-0.5 text-slate-300">
              {cmd}
            </code>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Tokens Tab ─────────────────────────────────────────────────────────────

function TokensTab() {
  const [tokens, setTokens] = useState<TokensStatus | null>(null);
  const [hfToken, setHfToken] = useState("");
  const [msToken, setMsToken] = useState("");
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);

  useEffect(() => {
    fetch(`${API}/api/llamacpp/tokens`)
      .then((r) => r.json())
      .then(setTokens)
      .catch(() => {});
  }, []);

  async function save() {
    setSaving(true);
    setMsg(null);
    try {
      const body: Record<string, string> = {};
      if (hfToken) body.huggingface = hfToken;
      if (msToken) body.modelscope = msToken;
      const r = await mutFetch(`${API}/api/llamacpp/tokens`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        body: JSON.stringify(body),
      }).then((r) => r.json());
      setTokens(r);
      setHfToken("");
      setMsToken("");
      setMsg({ text: "Токены сохранены", ok: true });
    } catch {
      setMsg({ text: "Ошибка сохранения", ok: false });
    } finally {
      setSaving(false);
    }
  }

  async function removeToken(provider: "huggingface" | "modelscope") {
    const r = await mutFetch(`${API}/api/llamacpp/tokens/${provider}`, {
      method: "DELETE",
      headers: await csrfHeaders(),
    }).then((r) => r.json());
    setTokens(r);
    setMsg({ text: `Токен ${provider} удалён`, ok: true });
  }

  return (
    <div className="space-y-6 max-w-lg">
      <div>
        <h2 className="font-semibold text-slate-100">API-токены</h2>
        <p className="text-sm text-slate-400 mt-1">
          Нужны для загрузки gated-моделей (Llama 3, Mistral и др.) и увеличения
          лимитов API. Хранятся в Redis, не передаются третьим лицам.
        </p>
      </div>

      {[
        {
          provider: "huggingface" as const,
          label: "🤗 HuggingFace",
          value: hfToken,
          setValue: setHfToken,
          set: tokens?.huggingface_set,
          hint: "Создать токен: huggingface.co → Settings → Access Tokens → Read",
          placeholder: "hf_…",
        },
        {
          provider: "modelscope" as const,
          label: "🌐 ModelScope",
          value: msToken,
          setValue: setMsToken,
          set: tokens?.modelscope_set,
          hint: "Создать токен: modelscope.cn → Профиль → Токены доступа",
          placeholder: "ms_…",
        },
      ].map(({ provider, label, value, setValue, set, hint, placeholder }) => (
        <div key={provider} className={`${cardCls} p-4 space-y-2`}>
          <div className="flex items-center justify-between">
            <label className="font-medium text-sm text-slate-200">
              {label}
            </label>
            {set && (
              <div className="flex items-center gap-2">
                <span className="text-xs text-green-400 font-medium">
                  ✓ Установлен
                </span>
                <button
                  onClick={() => removeToken(provider)}
                  className="text-xs text-red-400 hover:underline"
                >
                  Удалить
                </button>
              </div>
            )}
          </div>
          <input
            type="password"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={set ? "Введите новый токен для замены" : placeholder}
            className={inputCls + " font-mono"}
          />
          <p className="text-xs text-slate-500">{hint}</p>
        </div>
      ))}

      {(hfToken || msToken) && (
        <button
          onClick={save}
          disabled={saving}
          className="px-5 py-2 bg-blue-600 text-white rounded text-sm font-medium hover:bg-blue-700 disabled:opacity-50"
        >
          {saving ? "Сохранение…" : "Сохранить токены"}
        </button>
      )}
      {msg && (
        <p className={`text-sm ${msg.ok ? "text-green-400" : "text-red-400"}`}>
          {msg.text}
        </p>
      )}
    </div>
  );
}

// ── Main Page ──────────────────────────────────────────────────────────────

export default function LlamaCppPage() {
  const [tab, setTab] = useState<Tab>("search");
  const [status, setStatus] = useState<Status | null>(null);
  const [config, setConfig] = useState<Config | null>(null);
  const [models, setModels] = useState<GgufModel[]>([]);
  const [downloads, setDownloads] = useState<DownloadStatus[]>([]);
  const [toast, setToast] = useState<{ text: string; ok: boolean } | null>(
    null,
  );
  const sseRefs = useRef<Record<string, EventSource>>({});

  const showToast = useCallback((text: string, ok = true) => {
    setToast({ text, ok });
    setTimeout(() => setToast(null), 4000);
  }, []);

  const loadStatus = useCallback(async () => {
    try {
      const s = await fetch(`${API}/api/llamacpp/status`).then((r) => r.json());
      setStatus(s);
    } catch {}
  }, []);

  const loadModels = useCallback(async () => {
    try {
      setModels(
        await fetch(`${API}/api/llamacpp/models`).then((r) => r.json()),
      );
    } catch {}
  }, []);

  const loadConfig = useCallback(async () => {
    try {
      const c = await fetch(`${API}/api/llamacpp/config`).then((r) => r.json());
      setConfig(c);
    } catch {}
  }, []);

  function startSSE(dl: DownloadStatus) {
    const id = dl.download_id;
    if (sseRefs.current[id]) return;
    const es = new EventSource(
      `${API}/api/llamacpp/download/${encodeURIComponent(id)}/stream`,
    );
    sseRefs.current[id] = es;
    es.onmessage = (e) => {
      const d = JSON.parse(e.data);
      setDownloads((prev) =>
        prev.map((x) => (x.download_id === id ? { ...x, ...d } : x)),
      );
      if (d.status === "done") {
        es.close();
        delete sseRefs.current[id];
        loadModels();
        showToast(`✓ ${dl.filename} загружен`);
      } else if (["error", "cancelled"].includes(d.status)) {
        es.close();
        delete sseRefs.current[id];
        if (d.status === "error") showToast(`✗ Ошибка: ${d.error}`, false);
      }
    };
    es.onerror = () => {
      es.close();
      delete sseRefs.current[id];
    };
  }

  // Initial load + restore active downloads
  useEffect(() => {
    loadStatus();
    loadConfig();
    loadModels();
    const iv = setInterval(loadStatus, 10000);

    // Restore active downloads from backend (survives page navigation)
    fetch(`${API}/api/llamacpp/downloads`)
      .then((r) => r.json())
      .then((existing: DownloadStatus[]) => {
        if (!Array.isArray(existing) || existing.length === 0) return;
        setDownloads(existing);
        for (const dl of existing) {
          if (["downloading", "pending"].includes(dl.status)) {
            startSSE(dl);
          }
        }
      })
      .catch(() => {});

    return () => {
      clearInterval(iv);
      for (const es of Object.values(sseRefs.current)) es.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadStatus, loadConfig, loadModels]);

  function handleDownloadStart(dl: DownloadStatus) {
    setDownloads((prev) => {
      const exists = prev.find((x) => x.download_id === dl.download_id);
      return exists ? prev : [dl, ...prev];
    });
    startSSE(dl);
  }

  async function activateModel(path: string) {
    try {
      const r = await mutFetch(`${API}/api/llamacpp/models/activate`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        body: JSON.stringify({ path }),
      }).then((r) => r.json());
      setConfig(r);
      loadModels();
      showToast("Модель активирована. Перезапустите llama-server.");
    } catch {
      showToast("Ошибка активации", false);
    }
  }

  async function deleteModel(filename: string) {
    try {
      await mutFetch(
        `${API}/api/llamacpp/models/${encodeURIComponent(filename)}`,
        {
          method: "DELETE",
          headers: await csrfHeaders(),
        },
      );
      loadModels();
      showToast(`Модель ${filename} удалена`);
    } catch {
      showToast("Ошибка удаления", false);
    }
  }

  async function saveConfig(cfg: Partial<Config>) {
    try {
      const r = await mutFetch(`${API}/api/llamacpp/config`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        body: JSON.stringify(cfg),
      }).then((r) => r.json());
      setConfig(r);
      showToast("Конфигурация сохранена. Перезапустите llama-server.");
    } catch {
      showToast("Ошибка сохранения", false);
    }
  }

  const TABS: { id: Tab; label: string; badge?: number }[] = [
    { id: "search", label: "🔍 Поиск моделей" },
    { id: "local", label: "📂 Локальные", badge: models.length || undefined },
    { id: "config", label: "⚙ Настройки" },
    { id: "tokens", label: "🔑 Токены" },
  ];

  return (
    <div className="max-w-4xl mx-auto space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-slate-100">llama.cpp</h1>
          <p className="text-sm text-slate-400 mt-0.5">
            Локальный GGUF-бэкенд · TurboQuant · Flash Attention
          </p>
        </div>
        <div className="flex items-center gap-2 text-sm">
          <span
            className={`w-2.5 h-2.5 rounded-full ${status?.running ? "bg-green-500" : "bg-slate-600"}`}
          />
          <span className="text-slate-300">
            {status?.running
              ? status.model_loaded
                ? status.model_loaded.split("/").pop()?.slice(0, 30)
                : "API-only"
              : "Выключен"}
          </span>
          {status?.running && (
            <span className="text-xs text-slate-500">
              · слоты {status.slots_idle ?? "?"}/
              {(status.slots_idle ?? 0) + (status.slots_processing ?? 0)}
            </span>
          )}
          {status?.running && status.ctx_size && (
            <span className="text-xs text-slate-500">
              · {(status.ctx_size / 1024).toFixed(0)}K ctx
            </span>
          )}
          {status?.running && (
            <span
              className={`text-xs px-1.5 py-0.5 rounded ${
                status.vision
                  ? "bg-purple-800 text-purple-200"
                  : "bg-slate-700 text-slate-400"
              }`}
            >
              {status.vision ? "👁 vision" : "text-only"}
            </span>
          )}
        </div>
      </div>

      {/* Active downloads — always visible */}
      {downloads.length > 0 && (
        <DownloadsPanel
          downloads={downloads}
          onDone={() =>
            setDownloads((p) => p.filter((d) => d.status !== "done"))
          }
        />
      )}

      {/* Tab nav */}
      <div className="flex gap-0 border-b border-slate-700">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors flex items-center gap-1.5 ${
              tab === t.id
                ? "border-blue-500 text-blue-400"
                : "border-transparent text-slate-500 hover:text-slate-200"
            }`}
          >
            {t.label}
            {t.badge != null && (
              <span className="text-xs bg-slate-700 text-slate-300 px-1.5 py-0.5 rounded-full">
                {t.badge}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* Tab content — always rendered, hidden when inactive to preserve state */}
      <div className={tab !== "search" ? "hidden" : ""}>
        <SearchTab onDownloadStart={handleDownloadStart} />
      </div>
      <div className={tab !== "local" ? "hidden" : ""}>
        <LocalTab
          models={models}
          vision={status?.vision ?? false}
          mmproj_path={status?.mmproj_path ?? null}
          onRefresh={loadModels}
          onActivate={activateModel}
          onDelete={deleteModel}
        />
      </div>
      <div className={tab !== "config" ? "hidden" : ""}>
        {config && (
          <ConfigTab config={config} models={models} onSave={saveConfig} />
        )}
      </div>
      <div className={tab !== "tokens" ? "hidden" : ""}>
        <TokensTab />
      </div>

      {/* Toast */}
      {toast && (
        <div
          className={`fixed bottom-6 right-6 z-50 px-4 py-3 rounded-lg shadow-lg text-sm font-medium border ${
            toast.ok
              ? "bg-green-800 text-green-100 border-green-700"
              : "bg-red-800 text-red-100 border-red-700"
          }`}
        >
          {toast.text}
        </div>
      )}
    </div>
  );
}
