"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { getApiBaseUrl } from "@/lib/api-base";

const API = getApiBaseUrl();

interface Supplier {
  id: string;
  name: string;
  inn: string | null;
  contact_email: string | null;
  contact_phone: string | null;
  user_rating: number | null;
}

interface CatalogSummary {
  party_id: string;
  entries_count: number;
  tool_suppliers: { id: string; name: string; catalog_format: string | null }[];
}

async function fetchCatalogSummary(partyId: string): Promise<CatalogSummary> {
  const [tsResp, entriesResp] = await Promise.all([
    fetch(`${API}/tool-catalog/by-supplier/${partyId}`),
    fetch(`${API}/tool-catalog/by-supplier/${partyId}/entries?page_size=1`),
  ]);
  const ts = tsResp.ok ? await tsResp.json() : { items: [] };
  const entries = entriesResp.ok ? await entriesResp.json() : { total: 0 };
  return {
    party_id: partyId,
    entries_count: entries.total ?? 0,
    tool_suppliers: ts.items ?? [],
  };
}

export default function CatalogsPage() {
  const router = useRouter();
  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [summaries, setSummaries] = useState<Record<string, CatalogSummary>>(
    {},
  );

  const loadSuppliers = async (q: string) => {
    setLoading(true);
    try {
      const url = `${API}/api/suppliers?role=supplier`;
      if (q.length >= 2) {
        const resp = await fetch(`${API}/api/suppliers/search`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query: q }),
        });
        const data = await resp.json();
        setSuppliers(data.results ?? []);
        return;
      }
      const resp = await fetch(url);
      setSuppliers(resp.ok ? await resp.json() : []);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    const t = setTimeout(() => loadSuppliers(search), 300);
    return () => clearTimeout(t);
  }, [search]);

  // Load catalog summaries for visible suppliers
  useEffect(() => {
    if (!suppliers.length) return;
    const missing = suppliers.filter((s) => !summaries[s.id]);
    if (!missing.length) return;
    Promise.all(missing.map((s) => fetchCatalogSummary(s.id))).then(
      (results) => {
        setSummaries((prev) => {
          const next = { ...prev };
          for (const r of results) next[r.party_id] = r;
          return next;
        });
      },
    );
  }, [suppliers]);

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

      {/* Search */}
      <div className="px-6 pt-4">
        <input
          type="text"
          placeholder="Поиск по поставщикам..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="w-full max-w-md px-4 py-2 bg-zinc-800 border border-white/10 text-white placeholder-white/30 rounded-lg text-sm focus:outline-none focus:border-blue-500/50"
        />
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto px-6 py-4">
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
                        <div className="text-xs text-white/30">позиций</div>
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
