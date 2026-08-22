"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { getApiBaseUrl } from "@/lib/api-base";
import { mutFetch } from "@/lib/auth";
import { CatalogEntryGrid } from "@/components/catalogs/CatalogEntryGrid";
import {
  catalogsApi,
  type CatalogEntry,
  type CatalogSearchResult,
} from "@/lib/catalogs-api";

const API = getApiBaseUrl();

interface Supplier {
  id: string;
  name: string;
  inn: string | null;
  contact_email: string | null;
  contact_phone: string | null;
  user_rating: number | null;
}

interface SupplierCatalogCounts {
  party_id: string;
  entries_count: number;
  catalogs_count: number;
}

async function fetchAllCatalogSummaries(): Promise<Record<string, SupplierCatalogCounts>> {
  // One request for every supplier's catalogs. The previous version asked two
  // questions per supplier and the rate limiter answered 429 to most of them,
  // so suppliers with catalogs showed up as "каталог не загружен".
  const summaries: Record<string, SupplierCatalogCounts> = {};
  try {
    const data = await catalogsApi.list({});
    for (const item of data.items ?? []) {
      const key = item.party_id ?? item.supplier_id;
      if (!key) continue;
      const current = summaries[key] ?? {
        party_id: key,
        entries_count: 0,
        catalogs_count: 0,
      };
      current.entries_count += item.entries_count ?? 0;
      current.catalogs_count += 1;
      summaries[key] = current;
    }
  } catch {
    // The counters are decoration; the supplier list itself must still render.
  }
  return summaries;
}

export default function CatalogsPage() {
  const router = useRouter();
  // Two modes on one page: pick a supplier, or search positions across every
  // catalog of every supplier — the "удобный поиск" the catalogs lacked.
  const [mode, setMode] = useState<"suppliers" | "items">("suppliers");
  const [itemQuery, setItemQuery] = useState("");
  const [itemResult, setItemResult] = useState<CatalogSearchResult | null>(
    null,
  );
  const [itemLoading, setItemLoading] = useState(false);
  const [itemError, setItemError] = useState<string | null>(null);
  const [catalogFilter, setCatalogFilter] = useState<Set<string>>(new Set());
  const [onlyWithImage, setOnlyWithImage] = useState(false);
  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [summaries, setSummaries] = useState<Record<string, SupplierCatalogCounts>>(
    {},
  );

  const loadSuppliers = async (q: string) => {
    setLoading(true);
    try {
      const url = `${API}/api/suppliers?role=supplier`;
      if (q.length >= 2) {
        const resp = await mutFetch(`${API}/api/suppliers/search`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query: q }),
        });
        const data = await resp.json();
        setSuppliers(data.results ?? []);
        return;
      }
      const resp = await fetch(url, { credentials: "include" });
      const data = resp.ok ? await resp.json() : [];
      setSuppliers(data.items ?? data ?? []);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    const t = setTimeout(() => loadSuppliers(search), 300);
    return () => clearTimeout(t);
  }, [search]);

  // Load catalog counters for every supplier in one request.
  useEffect(() => {
    if (!suppliers.length) return;
    let cancelled = false;
    fetchAllCatalogSummaries().then((result) => {
      if (!cancelled) setSummaries(result);
    });
    return () => {
      cancelled = true;
    };
  }, [suppliers]);

  useEffect(() => {
    if (mode !== "items") return;
    const handle = window.setTimeout(async () => {
      setItemLoading(true);
      setItemError(null);
      try {
        const result = await catalogsApi.search({
          query: itemQuery || undefined,
          catalog_document_ids: catalogFilter.size
            ? Array.from(catalogFilter)
            : undefined,
          has_image: onlyWithImage ? true : undefined,
          page_size: 40,
        });
        setItemResult(result);
      } catch (e: unknown) {
        setItemError(e instanceof Error ? e.message : "Поиск не удался");
      } finally {
        setItemLoading(false);
      }
    }, 300);
    return () => window.clearTimeout(handle);
  }, [mode, itemQuery, catalogFilter, onlyWithImage]);

  function openEntryPage(entry: CatalogEntry) {
    if (!entry.catalog_document_id) return;
    router.push(
      `/catalogs/${entry.catalog_document_id}?page=${entry.page_number ?? 1}&entry=${entry.id}`,
    );
  }

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-4 px-6 py-4 bg-zinc-900 border-b border-white/10">
        <div>
          <h1 className="text-xl font-semibold text-white">
            Каталоги инструментов
          </h1>
          <p className="text-xs text-white/40 mt-0.5">
            Выберите поставщика для просмотра и загрузки каталогов режущего
            инструмента
          </p>
        </div>
      </div>

      <div className="flex gap-1 px-6 pt-4">
        {(
          [
            ["suppliers", "Поставщики"],
            ["items", "Поиск по позициям"],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            onClick={() => setMode(key)}
            className={`rounded px-3 py-1.5 text-sm transition-colors ${
              mode === key
                ? "bg-zinc-700 text-white"
                : "text-white/50 hover:text-white/80"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {mode === "items" && (
        <div className="flex flex-1 flex-col overflow-hidden px-6 pt-4">
          <div className="flex flex-wrap items-center gap-3">
            <input
              type="text"
              placeholder="Артикул, название, размер…"
              value={itemQuery}
              onChange={(e) => setItemQuery(e.target.value)}
              className="w-full max-w-md rounded-lg border border-white/10 bg-zinc-800 px-4 py-2 text-sm text-white placeholder-white/30 focus:border-blue-500/50 focus:outline-none"
            />
            <label className="flex items-center gap-2 text-sm text-white/60">
              <input
                type="checkbox"
                checked={onlyWithImage}
                onChange={(e) => setOnlyWithImage(e.target.checked)}
              />
              только с картинкой товара
            </label>
            {itemResult && (
              <span className="text-xs text-white/40">
                найдено: {itemResult.total.toLocaleString("ru")}
              </span>
            )}
          </div>

          {itemResult?.facets?.catalogs &&
            itemResult.facets.catalogs.length > 1 && (
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  onClick={() => setCatalogFilter(new Set())}
                  className={`rounded-full px-3 py-1 text-xs ${
                    catalogFilter.size === 0
                      ? "bg-blue-600 text-white"
                      : "bg-zinc-800 text-white/60 hover:text-white"
                  }`}
                >
                  все каталоги
                </button>
                {itemResult.facets.catalogs.map((facet) => (
                  <button
                    key={facet.key}
                    onClick={() =>
                      setCatalogFilter((prev) => {
                        // Multi-select: narrowing to two catalogs is as normal
                        // a question as narrowing to one.
                        const next = new Set(prev);
                        if (next.has(facet.key)) next.delete(facet.key);
                        else next.add(facet.key);
                        return next;
                      })
                    }
                    className={`rounded-full px-3 py-1 text-xs ${
                      catalogFilter.has(facet.key)
                        ? "bg-blue-600 text-white"
                        : "bg-zinc-800 text-white/60 hover:text-white"
                    }`}
                    title={facet.label}
                  >
                    {facet.label.length > 28
                      ? `${facet.label.slice(0, 28)}…`
                      : facet.label}{" "}
                    · {facet.count}
                  </button>
                ))}
              </div>
            )}

          {itemError && (
            <div className="mt-3 rounded border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
              {itemError}
            </div>
          )}

          <div className="mt-4 flex-1 overflow-y-auto pb-6">
            {itemLoading && !itemResult ? (
              <div className="py-12 text-sm text-white/40">Поиск…</div>
            ) : (
              <CatalogEntryGrid
                entries={itemResult?.items ?? []}
                onOpenPage={openEntryPage}
              />
            )}
          </div>
        </div>
      )}

      {/* Search */}
      <div className={`px-6 pt-4 ${mode === "items" ? "hidden" : ""}`}>
        <input
          type="text"
          placeholder="Поиск по поставщикам..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="w-full max-w-md px-4 py-2 bg-zinc-800 border border-white/10 text-white placeholder-white/30 rounded-lg text-sm focus:outline-none focus:border-blue-500/50"
        />
      </div>

      {/* List */}
      <div
        className={`flex-1 overflow-y-auto px-6 py-4 ${mode === "items" ? "hidden" : ""}`}
      >
        {loading ? (
          <div className="flex items-center gap-2 py-12 text-white/40 text-sm">
            <div className="w-4 h-4 border border-white/30 border-t-white rounded-full animate-spin" />
            Загрузка...
          </div>
        ) : suppliers.length === 0 ? (
          <div className="flex flex-col items-center py-16 text-white/30 gap-2">
            <svg
              className="w-12 h-12 mb-2"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={1.5}
                d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"
              />
            </svg>
            <span>Нет поставщиков</span>
            <button
              onClick={() => router.push("/suppliers")}
              className="mt-2 text-sm text-blue-400 hover:text-blue-300 underline"
            >
              Перейти к управлению поставщиками →
            </button>
          </div>
        ) : (
          <div className="space-y-2">
            {suppliers.map((s) => {
              const summary = summaries[s.id];
              const hasEntries = (summary?.entries_count ?? 0) > 0;
              return (
                <button
                  key={s.id}
                  onClick={() => router.push(`/suppliers/${s.id}?tab=catalog`)}
                  className="w-full flex items-center gap-4 px-4 py-3 bg-zinc-900 hover:bg-zinc-800 border border-white/10 hover:border-white/20 rounded-xl text-left transition-all group"
                >
                  {/* Icon */}
                  <div className="w-10 h-10 rounded-lg bg-zinc-800 group-hover:bg-zinc-700 border border-white/10 flex items-center justify-center shrink-0 transition-colors">
                    <svg
                      className="w-5 h-5 text-white/40"
                      fill="none"
                      stroke="currentColor"
                      viewBox="0 0 24 24"
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        strokeWidth={1.5}
                        d="M11 4a2 2 0 114 0v1a1 1 0 001 1h3a1 1 0 011 1v3a1 1 0 01-1 1h-1a2 2 0 100 4h1a1 1 0 011 1v3a1 1 0 01-1 1h-3a1 1 0 01-1-1v-1a2 2 0 10-4 0v1a1 1 0 01-1 1H7a1 1 0 01-1-1v-3a1 1 0 00-1-1H4a2 2 0 110-4h1a1 1 0 001-1V7a1 1 0 011-1h3a1 1 0 001-1V4z"
                      />
                    </svg>
                  </div>

                  {/* Info */}
                  <div className="flex-1 min-w-0">
                    <div className="font-medium text-white group-hover:text-blue-300 transition-colors truncate">
                      {s.name}
                    </div>
                    <div className="text-xs text-white/40 mt-0.5 flex items-center gap-3">
                      {s.inn && <span>ИНН {s.inn}</span>}
                      {s.contact_email && <span>{s.contact_email}</span>}
                    </div>
                  </div>

                  {/* Catalog summary */}
                  <div className="shrink-0 text-right">
                    {summary === undefined ? (
                      <div className="w-4 h-4 border border-white/20 border-t-white/60 rounded-full animate-spin ml-auto" />
                    ) : hasEntries ? (
                      <div>
                        <div className="text-sm font-semibold text-blue-400">
                          {summary.entries_count.toLocaleString("ru")}
                        </div>
                        <div className="text-xs text-white/30">
                          позиций
                          {summary.catalogs_count > 1
                            ? ` · ${summary.catalogs_count} каталога`
                            : ""}
                        </div>
                      </div>
                    ) : (
                      <span className="text-xs text-white/20 italic">
                        каталог не загружен
                      </span>
                    )}
                  </div>

                  {/* Arrow */}
                  <svg
                    className="w-4 h-4 text-white/20 group-hover:text-white/50 transition-colors shrink-0"
                    fill="none"
                    stroke="currentColor"
                    viewBox="0 0 24 24"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={2}
                      d="M9 5l7 7-7 7"
                    />
                  </svg>
                </button>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
