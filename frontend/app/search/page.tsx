"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

const API = getApiBaseUrl();

interface SearchResult {
  id: string;
  file_name: string;
  doc_type: string | null;
  status: string;
  created_at: string;
  score: number | null;
  snippet: string | null;
}

interface NLFilter {
  doc_type?: string;
  status?: string;
  search_text?: string;
  supplier_name?: string;
}

const DOC_TYPE_LABELS: Record<string, string> = {
  invoice: "Счёт",
  contract: "Договор",
  act: "Акт",
  specification: "Спецификация",
  drawing: "Чертёж",
  other: "Прочее",
};

const STATUS_STYLES: Record<string, string> = {
  needs_review: "bg-amber-900/40 text-amber-400",
  approved: "bg-green-900/40 text-green-400",
  rejected: "bg-red-900/40 text-red-400",
  processing: "bg-blue-900/40 text-blue-400",
};

export default function SearchPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [query, setQuery] = useState(searchParams.get("q") ?? "");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [mode, setMode] = useState<"text" | "nl" | "similar">("text");
  const [parsedFilter, setParsedFilter] = useState<NLFilter | null>(null);
  const [total, setTotal] = useState<number | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const search = useCallback(
    async (q: string) => {
      if (!q.trim()) {
        setResults([]);
        setTotal(null);
        setParsedFilter(null);
        return;
      }
      setLoading(true);
      try {
        if (mode === "nl") {
          const res = await fetch(`${API}/api/search/nl`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ query: q, limit: 50 }),
          });
          if (res.ok) {
            const data = await res.json();
            setResults(data.results ?? []);
            setTotal(data.total ?? data.results?.length ?? 0);
            setParsedFilter(data.structured_filter ?? null);
          }
        } else if (mode === "similar") {
          const res = await fetch(`${API}/api/search/hybrid`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ query: q, limit: 50 }),
          });
          if (res.ok) {
            const data = await res.json();
            setResults(data.results ?? []);
            setTotal(data.total ?? data.results?.length ?? 0);
            setParsedFilter(null);
          }
        } else {
          // POST /api/search/documents?q=...
          const res = await fetch(
            `${API}/api/search/documents?q=${encodeURIComponent(q)}&limit=50`,
            { method: "POST" },
          );
          if (res.ok) {
            const data = await res.json();
            setResults(
              Array.isArray(data) ? data : (data.results ?? data.items ?? []),
            );
            setTotal(Array.isArray(data) ? data.length : (data.total ?? null));
            setParsedFilter(null);
          }
        }
      } finally {
        setLoading(false);
      }
    },
    [mode],
  );

  // Search on mount if there's an initial query
  useEffect(() => {
    if (query) search(query);
  }, []);

  // Re-search when mode changes and there's a query
  useEffect(() => {
    if (query) search(query);
  }, [mode]);

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Enter") {
      search(query);
      const params = new URLSearchParams({ q: query });
      router.replace(`/search?${params}`);
    }
  }

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  return (
    <div className="p-6 max-w-4xl mx-auto">
      <h1 className="text-xl font-bold text-slate-100 mb-4">Поиск</h1>

      {/* Search bar */}
      <div className="flex gap-2 mb-4">
        <div className="relative flex-1">
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={
              mode === "nl"
                ? "Введите запрос на русском: «счета от ACME за май»"
                : mode === "similar"
                  ? "Найти похожие документы по смыслу..."
                  : "Поиск по файлам и документам..."
            }
            className="w-full px-4 py-2.5 pr-10 text-sm bg-slate-800 border border-slate-600 text-slate-200 placeholder-slate-500 rounded-lg outline-none focus:border-blue-400"
          />
          {loading && (
            <span className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-500 text-xs animate-pulse">
              ...
            </span>
          )}
        </div>
        <button
          onClick={() => search(query)}
          disabled={loading || !query.trim()}
          className="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 shrink-0"
        >
          Найти
        </button>
      </div>

      {/* Mode tabs */}
      <div className="flex gap-1 mb-5">
        {(["text", "nl", "similar"] as const).map((m) => (
          <button
            key={m}
            onClick={() => setMode(m)}
            className={`px-3 py-1 text-xs rounded transition-colors ${
              mode === m
                ? "bg-slate-600 text-slate-100"
                : "bg-slate-800 text-slate-400 hover:text-slate-200"
            }`}
          >
            {m === "text"
              ? "По тексту"
              : m === "nl"
                ? "NL-запрос"
                : "Семантический"}
          </button>
        ))}
        <span className="ml-2 text-xs text-slate-600 self-center">
          {mode === "nl"
            ? "Фильтрует по структурированным полям из текста запроса"
            : mode === "similar"
              ? "Поиск по смысловой близости (Qdrant vector)"
              : "Полнотекстовый + ILIKE поиск по PostgreSQL"}
        </span>
      </div>

      {/* Parsed filter chips (NL mode) */}
      {parsedFilter && (
        <div className="flex flex-wrap gap-2 mb-4">
          {parsedFilter.doc_type && (
            <span className="px-2 py-0.5 text-xs bg-blue-900/30 text-blue-300 border border-blue-700/40 rounded-full">
              Тип:{" "}
              {DOC_TYPE_LABELS[parsedFilter.doc_type] ?? parsedFilter.doc_type}
            </span>
          )}
          {parsedFilter.status && (
            <span className="px-2 py-0.5 text-xs bg-blue-900/30 text-blue-300 border border-blue-700/40 rounded-full">
              Статус: {parsedFilter.status}
            </span>
          )}
          {parsedFilter.search_text && (
            <span className="px-2 py-0.5 text-xs bg-blue-900/30 text-blue-300 border border-blue-700/40 rounded-full">
              Текст: «{parsedFilter.search_text}»
            </span>
          )}
          {parsedFilter.supplier_name && (
            <span className="px-2 py-0.5 text-xs bg-blue-900/30 text-blue-300 border border-blue-700/40 rounded-full">
              Поставщик: {parsedFilter.supplier_name}
            </span>
          )}
        </div>
      )}

      {/* Results */}
      {results.length === 0 && !loading && query && (
        <div className="py-12 text-center text-slate-500 text-sm">
          Ничего не найдено по запросу «{query}»
        </div>
      )}

      {!query && !loading && (
        <div className="py-12 text-center text-slate-500 text-sm">
          <p>Введите запрос и нажмите Enter</p>
          <div className="mt-4 grid grid-cols-3 gap-3 max-w-lg mx-auto text-left">
            {[
              { label: "По тексту", ex: "ACME 2024" },
              { label: "NL-запрос", ex: "счета за апрель на проверке" },
              { label: "Семантический", ex: "договор поставки металла" },
            ].map((tip) => (
              <div
                key={tip.label}
                className="px-3 py-2 bg-slate-800 rounded border border-slate-700 cursor-pointer hover:bg-slate-700 transition-colors"
                onClick={() => {
                  setQuery(tip.ex);
                  const modeMap: Record<string, "text" | "nl" | "similar"> = {
                    "По тексту": "text",
                    "NL-запрос": "nl",
                    Семантический: "similar",
                  };
                  setMode(modeMap[tip.label] ?? "text");
                }}
              >
                <p className="text-[10px] text-slate-500 mb-1">{tip.label}</p>
                <p className="text-xs text-slate-300">«{tip.ex}»</p>
              </div>
            ))}
          </div>
        </div>
      )}

      {results.length > 0 && (
        <>
          <p className="text-xs text-slate-500 mb-3">
            {total !== null
              ? `${total} результатов`
              : `${results.length} результатов`}
          </p>
          <div className="bg-slate-800 rounded-lg border border-slate-700 divide-y divide-slate-700">
            {results.map((doc) => (
              <div
                key={doc.id}
                onClick={() => router.push(`/documents/${doc.id}`)}
                className="flex items-center gap-4 px-4 py-3 hover:bg-slate-700/50 cursor-pointer transition-colors"
              >
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-slate-200 truncate">
                    {doc.file_name}
                  </p>
                  {doc.snippet && (
                    <p className="text-xs text-slate-400 mt-0.5 truncate">
                      {doc.snippet}
                    </p>
                  )}
                  <p className="text-xs text-slate-500 mt-0.5">
                    {doc.doc_type
                      ? (DOC_TYPE_LABELS[doc.doc_type] ?? doc.doc_type)
                      : "Документ"}{" "}
                    · {new Date(doc.created_at).toLocaleDateString("ru-RU")}
                  </p>
                </div>
                {doc.status && (
                  <span
                    className={`text-[10px] px-2 py-0.5 rounded-full shrink-0 ${STATUS_STYLES[doc.status] ?? "bg-slate-700 text-slate-400"}`}
                  >
                    {doc.status}
                  </span>
                )}
                {doc.score !== null && doc.score !== undefined && (
                  <span className="text-[10px] text-slate-500 shrink-0">
                    {(doc.score * 100).toFixed(0)}%
                  </span>
                )}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
