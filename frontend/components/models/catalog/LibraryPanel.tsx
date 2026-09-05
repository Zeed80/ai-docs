"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { useToast } from "@/components/ui/primitives/Toast";
import {
  btn,
  card,
  cardHeader as cardH,
  input,
  select,
} from "@/components/ui/primitives/tokens";
import { detailText } from "@/lib/models/format";
import { providerLabel } from "@/lib/models/labels";
import type {
  LocalProviderKind as Provider,
  ModelItem,
  ModelSource as Source,
  RepoFile,
} from "@/lib/models/types";

const API = getApiBaseUrl();
const btnPrimary = `${btn} bg-blue-600 hover:bg-blue-700 text-white disabled:opacity-50`;
const btnSecondary = `${btn} bg-slate-700 hover:bg-slate-600 text-slate-200`;
const btnDanger = `${btn} bg-red-700 hover:bg-red-600 text-white`;

export function LibraryPanel() {
  const toast = useToast();
  const [source, setSource] = useState<Source>("local");
  const [provider, setProvider] = useState<Provider | "">("");
  const [query, setQuery] = useState("");
  const [format, setFormat] = useState("");
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<ModelItem[]>([]);
  const [localModels, setLocalModels] = useState<Record<Provider, ModelItem[]>>(
    {
      ollama: [],
      llamacpp: [],
      vllm: [],
    },
  );
  const [activating, setActivating] = useState<string | null>(null);
  const [pullName, setPullName] = useState("");
  const [pulling, setPulling] = useState(false);
  const [pullStatus, setPullStatus] = useState<string | null>(null);
  const [downloading, setDownloading] = useState<
    Record<string, { pct: number; status: string }>
  >({});
  const [files, setFiles] = useState<Record<string, RepoFile[] | "loading">>(
    {},
  );
  const streamRefs = useRef<Record<string, EventSource>>({});

  // Потоки прогресса закрывались только по терминальному статусу: уход с
  // вкладки во время скачивания оставлял соединение висеть, а браузер
  // продолжал получать события в размонтированный компонент.
  useEffect(() => {
    const streams = streamRefs.current;
    return () => {
      for (const es of Object.values(streams)) es.close();
    };
  }, []);

  const toggleFiles = async (repoId: string) => {
    if (files[repoId]) {
      setFiles((prev) => {
        const next = { ...prev };
        delete next[repoId];
        return next;
      });
      return;
    }
    setFiles((prev) => ({ ...prev, [repoId]: "loading" }));
    try {
      const params = new URLSearchParams({ source });
      const p = provider || "llamacpp";
      const r = await fetch(
        `${API}/api/local-models/${p}/model/${encodeURIComponent(repoId)}/files?${params}`,
        { credentials: "include" },
      );
      const data = await r.json();
      setFiles((prev) => ({ ...prev, [repoId]: data.files || [] }));
    } catch {
      setFiles((prev) => ({ ...prev, [repoId]: [] }));
    }
  };

  const loadLocal = useCallback(async () => {
    for (const p of ["ollama", "llamacpp", "vllm"] as Provider[]) {
      try {
        const r = await fetch(`${API}/api/local-models/${p}/models`, {
          credentials: "include",
        });
        if (r.ok) {
          const data = await r.json();
          setLocalModels((prev) => ({ ...prev, [p]: data.models || [] }));
        }
      } catch (e) {
        toast.error("Список локальных моделей не загрузился", String(e));
        /* ignore */
      }
    }
  }, []);

  useEffect(() => {
    loadLocal();
  }, [loadLocal]);

  const doSearch = async () => {
    if (!query.trim()) return;
    setSearching(true);
    try {
      const params = new URLSearchParams({ q: query, source, limit: "12" });
      if (provider) params.set("provider", provider);
      if (format) params.set("format", format);
      const r = await fetch(`${API}/api/local-models/search?${params}`, {
        credentials: "include",
      });
      const data = await r.json();
      setResults(data.results || []);
    } catch {
      setResults([]);
    }
    setSearching(false);
  };

  const doActivate = async (
    p: Provider,
    path: string,
    vramEstimate?: number,
  ) => {
    setActivating(path);
    try {
      const r = await fetch(`${API}/api/local-models/${p}/activate`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        credentials: "include",
        body: JSON.stringify({
          model_path: path,
          vram_gb_estimate: vramEstimate ?? 0,
        }),
      });
      const data = await r.json();
      if (!r.ok)
        toast.error("Модель не активирована", detailText(data));
      else toast.ok(String(data.message ?? data.status ?? "Готово"));
      loadLocal();
    } catch (e) {
      toast.error("Не удалось выполнить действие", String(e));
    }
    setActivating(null);
  };

  const doDeleteOllama = async (name: string) => {
    if (!confirm(`Удалить модель «${name}» из Ollama?`)) return;
    try {
      const r = await fetch(
        `${API}/api/local-models/ollama/models/${encodeURIComponent(name)}`,
        {
          method: "DELETE",
          headers: await csrfHeaders(),
          credentials: "include",
        },
      );
      if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        toast.error("Не удалось удалить модель", detailText(data));
      }
      loadLocal();
    } catch (e) {
      toast.error("Не удалось выполнить действие", String(e));
    }
  };

  const doPullOllama = async () => {
    const name = pullName.trim();
    if (!name) return;
    setPulling(true);
    setPullStatus("Запуск…");
    try {
      const r = await fetch(`${API}/api/local-models/ollama/pull`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        credentials: "include",
        body: JSON.stringify({ name }),
      });
      if (!r.ok || !r.body) {
        setPullStatus(`Ошибка: ${r.status}`);
        setPulling(false);
        return;
      }
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n");
        buf = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const j = JSON.parse(line);
            if (j.error) setPullStatus(`Ошибка: ${j.error}`);
            else setPullStatus(j.status || "…");
          } catch {
            /* ignore partial */
          }
        }
      }
      setPullStatus("Готово");
      setPullName("");
      loadLocal();
    } catch (e) {
      setPullStatus(`Ошибка: ${e}`);
    }
    setPulling(false);
  };

  const doDownload = async (
    p: Provider,
    repoId: string,
    filename: string,
    src: string,
  ) => {
    const key = `${p}::${repoId}::${filename}`;
    try {
      const r = await fetch(`${API}/api/local-models/${p}/download`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        credentials: "include",
        body: JSON.stringify({ repo_id: repoId, filename, source: src }),
      });
      const data = await r.json();
      const did = data.download_id;
      if (!did) {
        toast.error("Загрузка не запустилась");
        return;
      }
      setDownloading((prev) => ({
        ...prev,
        [key]: { pct: 0, status: "pending" },
      }));
      const es = new EventSource(
        `${API}/api/local-models/${p}/download/${encodeURIComponent(did)}/stream`,
      );
      streamRefs.current[key] = es;
      es.onmessage = (ev) => {
        const d = JSON.parse(ev.data);
        setDownloading((prev) => ({
          ...prev,
          [key]: { pct: d.progress_pct ?? 0, status: d.status },
        }));
        if (
          d.status === "done" ||
          d.status === "completed" ||
          d.status === "error"
        ) {
          es.close();
          delete streamRefs.current[key];
          if (d.status !== "error") loadLocal();
        }
      };
      // Без этого обработчика оборванный поток переподключался бесконечно, а
      // индикатор навсегда застывал на последнем известном проценте.
      es.onerror = () => {
        es.close();
        delete streamRefs.current[key];
        setDownloading((prev) => ({
          ...prev,
          [key]: { pct: prev[key]?.pct ?? 0, status: "error" },
        }));
      };
    } catch (e) {
      toast.error("Не удалось выполнить действие", String(e));
    }
  };

  return (
    <div className="space-y-4">
      {/* Source toggle */}
      <div className="flex gap-2 flex-wrap">
        {(["local", "huggingface", "modelscope"] as Source[]).map((s) => (
          <button
            key={s}
            onClick={() => setSource(s)}
            className={`${btn} ${source === s ? "bg-blue-600 text-white" : "bg-slate-700 text-slate-300"}`}
          >
            {s === "local"
              ? "Локальные"
              : s === "huggingface"
                ? "HuggingFace"
                : "ModelScope"}
          </button>
        ))}
      </div>

      {source !== "local" && (
        <div className="flex gap-2 flex-wrap items-end">
          <div className="flex-1 min-w-48">
            <input
              className={input}
              placeholder="Поиск модели..."
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && doSearch()}
            />
          </div>
          <select
            className={`${select} w-36`}
            value={provider}
            onChange={(e) => setProvider(e.target.value as Provider | "")}
          >
            <option value="">Все провайдеры</option>
            <option value="ollama">Ollama</option>
            <option value="llamacpp">llama.cpp</option>
            <option value="vllm">vLLM</option>
          </select>
          <select
            className={`${select} w-36`}
            value={format}
            onChange={(e) => setFormat(e.target.value)}
          >
            <option value="">Все форматы</option>
            <option value="gguf">GGUF</option>
            <option value="safetensors">Safetensors</option>
            <option value="awq">AWQ</option>
            <option value="gptq">GPTQ</option>
          </select>
          <button
            onClick={doSearch}
            disabled={searching}
            className={btnPrimary}
          >
            {searching ? "Поиск..." : "Найти"}
          </button>
        </div>
      )}

      {/* Search results */}
      {source !== "local" && results.length > 0 && (
        <div className="space-y-2">
          <div className="text-xs text-slate-400">
            Результаты ({results.length})
          </div>
          {results.map((m) => {
            const name = m.model_name ?? m.name ?? m.repo_id ?? "Unknown";
            const key = `${provider}::${m.repo_id}::${name}`;
            const dl = downloading[key];
            return (
              <div key={m.repo_id} className={`${card} p-3`}>
                <div className="flex items-start justify-between gap-3">
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium text-slate-100 truncate">
                      {name}
                    </div>
                    <div className="text-xs text-slate-500 truncate">
                      {m.repo_id}
                    </div>
                    <div className="flex gap-2 flex-wrap mt-1">
                      {m.downloads != null && (
                        <span className="text-xs text-slate-500">
                          ↓ {m.downloads.toLocaleString()}
                        </span>
                      )}
                      {m.likes != null && (
                        <span className="text-xs text-slate-500">
                          ♥ {m.likes}
                        </span>
                      )}
                      {m.gated && (
                        <span className="text-xs bg-amber-900 text-amber-300 px-1 rounded">
                          Gated
                        </span>
                      )}
                      {(m.tags ?? []).slice(0, 3).map((t) => (
                        <span
                          key={t}
                          className="text-xs bg-slate-800 text-slate-400 px-1 rounded"
                        >
                          {t}
                        </span>
                      ))}
                    </div>
                  </div>
                  <div className="flex gap-2 flex-shrink-0">
                    {provider && provider !== "ollama" && (
                      <button
                        onClick={() => toggleFiles(m.repo_id!)}
                        className={btnSecondary}
                      >
                        {files[m.repo_id!] ? "▲ Файлы" : "▼ Файлы"}
                      </button>
                    )}
                    {provider && (
                      <button
                        onClick={() =>
                          doDownload(
                            provider as Provider,
                            m.repo_id!,
                            name,
                            source,
                          )
                        }
                        disabled={!!dl && dl.status !== "error"}
                        className={btnSecondary}
                      >
                        {dl
                          ? dl.status === "done" || dl.status === "completed"
                            ? "✓"
                            : `${dl.pct.toFixed(0)}%`
                          : "↓ Скачать"}
                      </button>
                    )}
                  </div>
                </div>
                {dl && dl.status !== "done" && dl.status !== "completed" && (
                  <div className="mt-2">
                    <div className="h-1.5 rounded bg-slate-700">
                      <div
                        className="h-full rounded bg-blue-500 transition-all"
                        style={{ width: `${dl.pct}%` }}
                      />
                    </div>
                    <div className="text-xs text-slate-500 mt-1">
                      {dl.status} · {dl.pct.toFixed(1)}%
                    </div>
                  </div>
                )}
                {/* File / quant list */}
                {files[m.repo_id!] === "loading" && (
                  <div className="mt-2 text-xs text-slate-500">
                    Загрузка списка файлов...
                  </div>
                )}
                {Array.isArray(files[m.repo_id!]) && (
                  <div className="mt-2 space-y-1 border-t border-slate-800 pt-2">
                    {(files[m.repo_id!] as RepoFile[]).length === 0 && (
                      <div className="text-xs text-slate-600">
                        Файлы не найдены
                      </div>
                    )}
                    {(files[m.repo_id!] as RepoFile[]).map((f) => {
                      const fname = f.filename ?? f.rfilename ?? "";
                      const fkey = `${provider}::${m.repo_id}::${fname}`;
                      const fdl = downloading[fkey];
                      return (
                        <div
                          key={fname}
                          className="flex items-center justify-between gap-2 text-xs"
                        >
                          <div className="flex items-center gap-2 min-w-0">
                            {f.quant && (
                              <span className="bg-slate-800 text-slate-300 px-1 rounded font-mono">
                                {f.quant}
                              </span>
                            )}
                            <span className="truncate text-slate-400 font-mono">
                              {fname}
                            </span>
                            {f.size_human && (
                              <span className="text-slate-600">
                                {f.size_human}
                              </span>
                            )}
                          </div>
                          <button
                            onClick={() =>
                              doDownload(
                                provider as Provider,
                                m.repo_id!,
                                fname,
                                source,
                              )
                            }
                            disabled={!!fdl && fdl.status !== "error"}
                            className={`${btn} bg-slate-800 hover:bg-slate-700 text-slate-300 disabled:opacity-50`}
                          >
                            {fdl
                              ? fdl.status === "done" ||
                                fdl.status === "completed"
                                ? "✓"
                                : `${fdl.pct.toFixed(0)}%`
                              : "↓"}
                          </button>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Local models */}
      {(["ollama", "llamacpp", "vllm"] as Provider[]).map((p) => {
        const models = localModels[p];
        if (source !== "local" && !models.length) return null;
        return (
          <div key={p} className={card}>
            <div className={cardH}>
              <span className="text-sm font-medium text-slate-100">
                {providerLabel(p)} — локальные модели
              </span>
              <span className="text-xs text-slate-400">
                {models.length} моделей
              </span>
            </div>
            {p === "ollama" && (
              <div className="px-4 py-3 border-b border-slate-800 flex items-center gap-2">
                <input
                  className={`${input} flex-1`}
                  placeholder="Загрузить из реестра Ollama (напр. qwen3:8b)"
                  value={pullName}
                  onChange={(e) => setPullName(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && doPullOllama()}
                  disabled={pulling}
                />
                <button
                  className={btnPrimary}
                  onClick={doPullOllama}
                  disabled={pulling || !pullName.trim()}
                >
                  {pulling ? "Загрузка…" : "Pull"}
                </button>
                {pullStatus && (
                  <span className="text-xs text-slate-400 truncate max-w-[12rem]">
                    {pullStatus}
                  </span>
                )}
              </div>
            )}
            <div className="divide-y divide-slate-800">
              {models.length === 0 && (
                <div className="px-4 py-3 text-sm text-slate-500">
                  Нет загруженных моделей
                </div>
              )}
              {models.map((m, i) => {
                const name = m.name ?? m.path?.split("/").pop() ?? `model-${i}`;
                const aKey = `${p}::${name}`;
                return (
                  <div
                    key={name}
                    className="px-4 py-3 flex items-center justify-between gap-3 hover:bg-slate-800/40"
                  >
                    <div className="flex-1 min-w-0">
                      <div className="text-sm text-slate-100 truncate flex items-center gap-2">
                        {m.active && (
                          <span className="text-xs bg-blue-900 text-blue-300 px-1.5 py-0.5 rounded">
                            Активна
                          </span>
                        )}
                        {name}
                      </div>
                      <div className="text-xs text-slate-500 flex gap-3">
                        {m.size_human && <span>{m.size_human}</span>}
                        {m.format && <span>{m.format.toUpperCase()}</span>}
                        {m.vram_gb_estimate != null &&
                          m.vram_gb_estimate > 0 && (
                            <span>
                              ~{m.vram_gb_estimate.toFixed(1)} GB VRAM
                            </span>
                          )}
                      </div>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <button
                        onClick={() =>
                          doActivate(p, m.path ?? name, m.vram_gb_estimate)
                        }
                        disabled={activating === (m.path ?? name) || !!m.active}
                        className={
                          m.active
                            ? `${btn} bg-slate-800 text-slate-500 cursor-default`
                            : btnSecondary
                        }
                      >
                        {activating === (m.path ?? name)
                          ? "..."
                          : m.active
                            ? "Активна"
                            : "Активировать"}
                      </button>
                      {p === "ollama" && (
                        <button
                          onClick={() => doDeleteOllama(name)}
                          className={btnDanger}
                          title="Удалить модель из Ollama"
                        >
                          Удалить
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}
