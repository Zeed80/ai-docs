"use client";

import { useCallback, useEffect, useState } from "react";
import clsx from "clsx";
import type {
  ToolCatalogEntry,
  ToolCatalogListResponse,
  ToolType,
} from "@/lib/drawings-api";
import { toolCatalogApi } from "@/lib/drawings-api";

const TOOL_TYPE_LABELS: Record<string, string> = {
  drill: "Свёрла",
  endmill: "Концевые фрезы",
  insert: "Пластины",
  holder: "Оправки",
  tap: "Метчики",
  reamer: "Развёртки",
  boring_bar: "Расточные",
  thread_mill: "Резьбофрезы",
  grinder: "Шлифовальные",
  turning_tool: "Резцы",
  milling_cutter: "Дисковые фрезы",
  countersink: "Зенковки",
  counterbore: "Цековки",
  other: "Прочее",
};

interface CatalogBrowserProps {
  supplierId?: string;
  onSelectEntry?: (entry: ToolCatalogEntry) => void;
  selectionMode?: boolean;
}

export function CatalogBrowser({
  supplierId,
  onSelectEntry,
  selectionMode,
}: CatalogBrowserProps) {
  const [query, setQuery] = useState("");
  const [toolTypeFilter, setToolTypeFilter] = useState<ToolType | "">("");
  const [diameterMin, setDiameterMin] = useState("");
  const [diameterMax, setDiameterMax] = useState("");
  const [result, setResult] = useState<ToolCatalogListResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [selectedEntry, setSelectedEntry] = useState<ToolCatalogEntry | null>(
    null,
  );
  const PAGE_SIZE = 30;

  const search = useCallback(async () => {
    setLoading(true);
    try {
      const params: Parameters<typeof toolCatalogApi.search>[0] = {
        page,
        page_size: PAGE_SIZE,
        semantic: !!query,
      };
      if (query) params.query = query;
      if (toolTypeFilter) params.tool_type = toolTypeFilter;
      if (supplierId) params.supplier_id = supplierId;
      if (diameterMin) params.diameter_min = parseFloat(diameterMin);
      if (diameterMax) params.diameter_max = parseFloat(diameterMax);
      const r = await toolCatalogApi.search(params);
      setResult(r);
    } finally {
      setLoading(false);
    }
  }, [query, toolTypeFilter, supplierId, diameterMin, diameterMax, page]);

  useEffect(() => {
    search();
  }, [search]);

  const totalPages = result ? Math.ceil(result.total / PAGE_SIZE) : 0;

  return (
    <div className="flex flex-col h-full">
      {/* Filters */}
      <div className="flex items-center gap-2 flex-wrap p-3 bg-zinc-900 border-b border-white/10">
        <input
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setPage(1);
          }}
          onKeyDown={(e) => e.key === "Enter" && search()}
          placeholder="Название, артикул, параметры..."
          className="flex-1 min-w-40 bg-zinc-800 border border-white/10 rounded px-3 py-1.5 text-sm text-white placeholder-white/30 focus:outline-none focus:border-blue-500/50"
        />

        <select
          value={toolTypeFilter}
          onChange={(e) => {
            setToolTypeFilter(e.target.value as ToolType | "");
            setPage(1);
          }}
          className="bg-zinc-800 border border-white/10 rounded px-2 py-1.5 text-sm text-white focus:outline-none"
        >
          <option value="">Все типы</option>
          {Object.entries(TOOL_TYPE_LABELS).map(([k, v]) => (
            <option key={k} value={k}>
              {v}
            </option>
          ))}
        </select>

        <div className="flex items-center gap-1 text-white/40 text-sm">
          <span>Ø</span>
          <input
            value={diameterMin}
            onChange={(e) => setDiameterMin(e.target.value)}
            placeholder="от"
            className="w-16 bg-zinc-800 border border-white/10 rounded px-2 py-1.5 text-sm text-white text-center focus:outline-none"
          />
          <span>–</span>
          <input
            value={diameterMax}
            onChange={(e) => setDiameterMax(e.target.value)}
            placeholder="до"
            className="w-16 bg-zinc-800 border border-white/10 rounded px-2 py-1.5 text-sm text-white text-center focus:outline-none"
          />
          <span>мм</span>
        </div>

        {result && (
          <span className="text-white/30 text-xs ml-auto">
            {result.total} шт.
          </span>
        )}
      </div>

      {/* Table */}
      <div className="flex-1 overflow-auto">
        {loading && (
          <div className="flex items-center justify-center py-8 text-white/40 gap-2">
            <div className="w-4 h-4 border border-white/30 border-t-white rounded-full animate-spin" />
            <span className="text-sm">Загрузка...</span>
          </div>
        )}

        {!loading && result?.items.length === 0 && (
          <div className="flex flex-col items-center justify-center py-12 text-white/30 gap-2">
            <span className="text-3xl">🔍</span>
            <span className="text-sm">Ничего не найдено</span>
          </div>
        )}

        {!loading && result && result.items.length > 0 && (
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-zinc-900 border-b border-white/10">
              <tr>
                <th className="text-left text-xs text-white/40 px-3 py-2 font-medium">
                  Наименование
                </th>
                <th className="text-left text-xs text-white/40 px-3 py-2 font-medium">
                  Тип
                </th>
                <th className="text-center text-xs text-white/40 px-3 py-2 font-medium">
                  Ø мм
                </th>
                <th className="text-left text-xs text-white/40 px-3 py-2 font-medium">
                  Материал
                </th>
                <th className="text-left text-xs text-white/40 px-3 py-2 font-medium">
                  Покрытие
                </th>
                <th className="text-right text-xs text-white/40 px-3 py-2 font-medium">
                  Цена
                </th>
                {selectionMode && <th className="w-16 px-3 py-2" />}
              </tr>
            </thead>
            <tbody>
              {result.items.map((entry) => (
                <tr
                  key={entry.id}
                  className={clsx(
                    "border-b border-white/5 transition-colors cursor-pointer",
                    selectedEntry?.id === entry.id
                      ? "bg-blue-600/15"
                      : "hover:bg-white/5",
                  )}
                  onClick={() => {
                    setSelectedEntry(entry);
                    if (selectionMode && onSelectEntry) onSelectEntry(entry);
                  }}
                >
                  <td className="px-3 py-2">
                    <div className="text-white/90 truncate max-w-xs">
                      {entry.name}
                    </div>
                    {entry.part_number && (
                      <div className="text-white/30 text-xs font-mono">
                        {entry.part_number}
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-2 text-white/50 text-xs whitespace-nowrap">
                    {TOOL_TYPE_LABELS[entry.tool_type] || entry.tool_type}
                  </td>
                  <td className="px-3 py-2 text-center">
                    {entry.diameter_mm != null ? (
                      <span className="text-blue-300 font-mono text-xs">
                        {entry.diameter_mm}
                      </span>
                    ) : (
                      <span className="text-white/20">—</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-white/50 text-xs">
                    {entry.material || <span className="text-white/20">—</span>}
                  </td>
                  <td className="px-3 py-2 text-white/50 text-xs">
                    {entry.coating || <span className="text-white/20">—</span>}
                  </td>
                  <td className="px-3 py-2 text-right text-xs">
                    {entry.price_value != null ? (
                      <span className="text-white/60">
                        {entry.price_value.toLocaleString("ru")}{" "}
                        {entry.price_currency}
                      </span>
                    ) : (
                      <span className="text-white/20">—</span>
                    )}
                  </td>
                  {selectionMode && (
                    <td className="px-3 py-2 text-right">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          onSelectEntry?.(entry);
                        }}
                        className="text-xs px-2 py-0.5 bg-blue-600 hover:bg-blue-500 text-white rounded"
                      >
                        Выбрать
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-2 p-3 border-t border-white/10">
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page === 1}
            className="px-3 py-1 bg-zinc-800 hover:bg-zinc-700 disabled:opacity-30 text-white/70 rounded text-xs"
          >
            ←
          </button>
          <span className="text-white/40 text-xs">
            {page} / {totalPages}
          </span>
          <button
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page === totalPages}
            className="px-3 py-1 bg-zinc-800 hover:bg-zinc-700 disabled:opacity-30 text-white/70 rounded text-xs"
          >
            →
          </button>
        </div>
      )}
    </div>
  );
}
