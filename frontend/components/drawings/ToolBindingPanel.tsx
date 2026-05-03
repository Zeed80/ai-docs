"use client";

import { useCallback, useEffect, useState } from "react";
import clsx from "clsx";
import type {
  DrawingFeature,
  FeatureToolBinding,
  ToolCatalogEntry,
  ToolSource,
  ToolSuggestion,
  ToolSupplier,
} from "@/lib/drawings-api";
import { drawingsApi, toolCatalogApi } from "@/lib/drawings-api";

const TOOL_TYPE_LABELS: Record<string, string> = {
  drill: "Сверло",
  endmill: "Концевая фреза",
  insert: "Режущая пластина",
  holder: "Оправка",
  tap: "Метчик",
  reamer: "Развёртка",
  boring_bar: "Расточная оправка",
  thread_mill: "Резьбофреза",
  grinder: "Шлифовальный",
  turning_tool: "Резец",
  milling_cutter: "Дисковая фреза",
  countersink: "Зенковка",
  counterbore: "Цековка",
  other: "Прочее",
};

interface ToolBindingPanelProps {
  drawingId: string;
  feature: DrawingFeature | null;
  onBindingChanged?: () => void;
}

type Tab = "suggestions" | "catalog" | "warehouse" | "manual";

export function ToolBindingPanel({
  drawingId,
  feature,
  onBindingChanged,
}: ToolBindingPanelProps) {
  const [activeTab, setActiveTab] = useState<Tab>("suggestions");
  const [suggestions, setSuggestions] = useState<ToolSuggestion[]>([]);
  const [loadingSuggestions, setLoadingSuggestions] = useState(false);
  const [catalogQuery, setCatalogQuery] = useState("");
  const [catalogResults, setCatalogResults] = useState<ToolCatalogEntry[]>([]);
  const [loadingCatalog, setLoadingCatalog] = useState(false);
  const [manualDesc, setManualDesc] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const currentBinding = feature?.tool_binding;

  // Load AI suggestions when feature changes
  useEffect(() => {
    if (!feature) return;
    setLoadingSuggestions(true);
    setSuggestions([]);
    toolCatalogApi
      .suggestForFeature(feature.id)
      .then((r) => setSuggestions(r.suggestions))
      .catch(() => setSuggestions([]))
      .finally(() => setLoadingSuggestions(false));
  }, [feature?.id]);

  const searchCatalog = useCallback(async () => {
    if (!catalogQuery.trim()) return;
    setLoadingCatalog(true);
    try {
      const result = await toolCatalogApi.search({
        query: catalogQuery,
        page_size: 20,
      });
      setCatalogResults(result.items);
    } catch {
      setCatalogResults([]);
    } finally {
      setLoadingCatalog(false);
    }
  }, [catalogQuery]);

  const bindFromCatalog = async (
    entry: ToolCatalogEntry,
    source: ToolSource = "catalog",
  ) => {
    if (!feature) return;
    setSaving(true);
    setError(null);
    try {
      await drawingsApi.bindTool(drawingId, feature.id, {
        tool_source: source,
        catalog_entry_id: entry.id,
        bound_by: "user",
      });
      onBindingChanged?.();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Ошибка привязки");
    } finally {
      setSaving(false);
    }
  };

  const bindManual = async () => {
    if (!feature || !manualDesc.trim()) return;
    setSaving(true);
    setError(null);
    try {
      await drawingsApi.bindTool(drawingId, feature.id, {
        tool_source: "manual",
        manual_description: manualDesc.trim(),
        bound_by: "user",
      });
      setManualDesc("");
      onBindingChanged?.();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Ошибка привязки");
    } finally {
      setSaving(false);
    }
  };

  const removeBind = async () => {
    if (!feature) return;
    setSaving(true);
    try {
      await drawingsApi.removeToolBinding(drawingId, feature.id);
      onBindingChanged?.();
    } catch {
    } finally {
      setSaving(false);
    }
  };

  if (!feature) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-white/30 gap-2 py-6">
        <span className="text-3xl">🔧</span>
        <span className="text-sm">Выберите элемент чертежа</span>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full text-sm">
      {/* Feature header */}
      <div className="px-3 py-2 border-b border-white/10">
        <div className="font-medium text-white text-sm truncate">
          {feature.name}
        </div>
        <div className="text-white/40 text-xs mt-0.5">Привязка инструмента</div>
      </div>

      {/* Current binding */}
      {currentBinding && (
        <div className="mx-3 mt-2 p-2 bg-green-500/10 border border-green-500/20 rounded-lg">
          <div className="flex items-start justify-between gap-2">
            <div className="flex-1 min-w-0">
              <div className="text-xs text-green-400 font-medium mb-0.5">
                Привязан:
              </div>
              {currentBinding.manual_description && (
                <div className="text-white/80 text-xs">
                  {currentBinding.manual_description}
                </div>
              )}
              {currentBinding.tool_source !== "manual" && (
                <div className="text-white/60 text-xs capitalize">
                  Источник: {currentBinding.tool_source}
                </div>
              )}
            </div>
            <button
              onClick={removeBind}
              disabled={saving}
              className="text-red-400 hover:text-red-300 text-xs px-1.5 py-0.5 rounded hover:bg-red-500/10"
              title="Удалить привязку"
            >
              ×
            </button>
          </div>
        </div>
      )}

      {/* Tabs */}
      <div className="flex border-b border-white/10 mt-2">
        {(["suggestions", "catalog", "manual"] as Tab[]).map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={clsx(
              "flex-1 py-1.5 text-xs transition-colors",
              activeTab === tab
                ? "text-blue-400 border-b-2 border-blue-500"
                : "text-white/40 hover:text-white/60",
            )}
          >
            {tab === "suggestions" && "AI-подбор"}
            {tab === "catalog" && "Каталог"}
            {tab === "manual" && "Вручную"}
          </button>
        ))}
      </div>

      {error && (
        <div className="mx-3 mt-2 p-2 bg-red-500/10 border border-red-500/20 rounded text-red-400 text-xs">
          {error}
        </div>
      )}

      {/* Tab content */}
      <div className="flex-1 overflow-y-auto">
        {activeTab === "suggestions" && (
          <div className="p-2">
            {loadingSuggestions && (
              <div className="flex items-center gap-2 text-white/40 py-4 justify-center">
                <div className="w-4 h-4 border border-blue-500/50 border-t-blue-500 rounded-full animate-spin" />
                <span className="text-xs">AI подбирает инструменты...</span>
              </div>
            )}
            {!loadingSuggestions && suggestions.length === 0 && (
              <div className="text-white/30 text-xs text-center py-4">
                Нет предложений. Загрузите каталоги поставщиков.
              </div>
            )}
            {suggestions.map((s, i) => (
              <ToolCard
                key={s.entry.id}
                entry={s.entry}
                supplier={s.supplier}
                score={s.score}
                reason={s.reason}
                warehouseAvailable={s.warehouse_available}
                warehouseQty={s.warehouse_qty}
                onBind={() => bindFromCatalog(s.entry)}
                saving={saving}
              />
            ))}
          </div>
        )}

        {activeTab === "catalog" && (
          <div className="p-2">
            <div className="flex gap-1 mb-2">
              <input
                value={catalogQuery}
                onChange={(e) => setCatalogQuery(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && searchCatalog()}
                placeholder="Сверло Ø5, HSS, метчик M6..."
                className="flex-1 bg-zinc-800 border border-white/10 rounded px-2 py-1 text-xs text-white placeholder-white/30 focus:outline-none focus:border-blue-500/50"
              />
              <button
                onClick={searchCatalog}
                disabled={loadingCatalog}
                className="px-2 py-1 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white rounded text-xs"
              >
                Найти
              </button>
            </div>

            {loadingCatalog && (
              <div className="text-white/40 text-xs text-center py-3">
                Поиск...
              </div>
            )}
            {catalogResults.map((entry) => (
              <ToolCard
                key={entry.id}
                entry={entry}
                onBind={() => bindFromCatalog(entry)}
                saving={saving}
              />
            ))}
            {!loadingCatalog && catalogQuery && catalogResults.length === 0 && (
              <div className="text-white/30 text-xs text-center py-3">
                Ничего не найдено
              </div>
            )}
          </div>
        )}

        {activeTab === "manual" && (
          <div className="p-2">
            <div className="text-xs text-white/40 mb-2">
              Укажите инструмент в произвольном формате:
            </div>
            <textarea
              value={manualDesc}
              onChange={(e) => setManualDesc(e.target.value)}
              placeholder="Сверло Ø5 HSS-Co, ГОСТ 10902-77&#10;Резец проходной 25×25&#10;Метчик М8×1.25"
              rows={3}
              className="w-full bg-zinc-800 border border-white/10 rounded px-2 py-1.5 text-xs text-white placeholder-white/30 focus:outline-none focus:border-blue-500/50 resize-none"
            />
            <button
              onClick={bindManual}
              disabled={saving || !manualDesc.trim()}
              className="mt-2 w-full py-1.5 bg-blue-600 hover:bg-blue-500 disabled:opacity-40 text-white rounded text-xs font-medium"
            >
              {saving ? "Сохранение..." : "Привязать"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

interface ToolCardProps {
  entry: ToolCatalogEntry;
  supplier?: ToolSupplier | null;
  score?: number;
  reason?: string;
  warehouseAvailable?: boolean;
  warehouseQty?: number;
  onBind: () => void;
  saving: boolean;
}

function ToolCard({
  entry,
  supplier,
  score,
  reason,
  warehouseAvailable,
  warehouseQty,
  onBind,
  saving,
}: ToolCardProps) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="mb-2 bg-zinc-800/50 border border-white/10 rounded-lg overflow-hidden">
      <div className="p-2">
        <div className="flex items-start gap-2">
          <div className="flex-1 min-w-0">
            <div className="text-white/90 text-xs font-medium truncate">
              {entry.name}
            </div>
            <div className="flex items-center gap-1.5 mt-0.5 flex-wrap">
              <span className="text-white/40 text-xs">
                {TOOL_TYPE_LABELS[entry.tool_type] || entry.tool_type}
              </span>
              {entry.diameter_mm && (
                <span className="text-blue-300 text-xs bg-blue-500/10 px-1 rounded font-mono">
                  Ø{entry.diameter_mm}
                </span>
              )}
              {entry.material && (
                <span className="text-orange-300 text-xs">
                  {entry.material}
                </span>
              )}
              {warehouseAvailable && (
                <span className="text-green-400 text-xs bg-green-500/10 px-1 rounded">
                  Склад {warehouseQty && `×${warehouseQty}`}
                </span>
              )}
            </div>
            {supplier && (
              <div className="text-white/30 text-xs mt-0.5">
                {supplier.name}
              </div>
            )}
            {score !== undefined && (
              <div className="flex items-center gap-1 mt-1">
                <div className="flex-1 h-1 bg-zinc-700 rounded">
                  <div
                    className="h-1 bg-blue-500 rounded"
                    style={{ width: `${Math.round(score * 100)}%` }}
                  />
                </div>
                <span className="text-white/30 text-xs">
                  {Math.round(score * 100)}%
                </span>
              </div>
            )}
          </div>
          <button
            onClick={onBind}
            disabled={saving}
            className="shrink-0 px-2 py-1 bg-blue-600 hover:bg-blue-500 disabled:opacity-40 text-white rounded text-xs"
          >
            Выбрать
          </button>
        </div>

        {reason && (
          <div className="mt-1.5 text-xs text-white/40 italic leading-relaxed">
            {reason}
          </div>
        )}

        {entry.description && (
          <button
            onClick={() => setExpanded((e) => !e)}
            className="text-white/30 hover:text-white/50 text-xs mt-1"
          >
            {expanded ? "▲ Скрыть" : "▼ Подробнее"}
          </button>
        )}
        {expanded && entry.description && (
          <div className="mt-1 text-xs text-white/50 leading-relaxed">
            {entry.description}
          </div>
        )}
      </div>

      {entry.price_value && (
        <div className="px-2 py-1 bg-zinc-900/50 border-t border-white/5 text-xs text-white/30">
          {entry.price_value.toLocaleString("ru")} {entry.price_currency}
          {entry.part_number && ` · Арт. ${entry.part_number}`}
        </div>
      )}
    </div>
  );
}
