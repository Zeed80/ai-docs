"use client";

import { getApiBaseUrl } from "@/lib/api-base";

import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState, useCallback } from "react";

const API = getApiBaseUrl();

// ── Types ─────────────────────────────────────────────────────────────────────

interface SupplierFull {
  id: string;
  name: string;
  inn: string | null;
  kpp: string | null;
  ogrn: string | null;
  address: string | null;
  contact_email: string | null;
  contact_phone: string | null;
  bank_name: string | null;
  bank_bik: string | null;
  bank_account: string | null;
  corr_account: string | null;
  user_notes: string | null;
  user_rating: number | null;
  profile: {
    total_invoices: number;
    total_amount: number;
    trust_score: number | null;
    last_invoice_date: string | null;
    notes: string | null;
  } | null;
  recent_invoices_count: number;
  open_invoices_amount: number;
}

interface TrustScore {
  trust_score: number;
  breakdown: {
    factor: string;
    weight: number;
    score: number;
    detail: string | null;
  }[];
  recommendation: string | null;
}

interface PriceHistoryItem {
  description: string;
  current_price: number | null;
  avg_price: number | null;
  trend: string | null;
  points: { date: string; price: number }[];
}

interface Alert {
  id: string;
  alert_type: string;
  severity: string;
  message: string;
}

interface RequisiteCheck {
  field: string;
  status: string;
  message: string | null;
}

interface ToolCatalogEntry {
  id: string;
  supplier_id: string | null;
  part_number: string | null;
  tool_type: string;
  name: string;
  description: string | null;
  diameter_mm: number | null;
  length_mm: number | null;
  material: string | null;
  coating: string | null;
  price_currency: string;
  price_value: number | null;
  catalog_page: number | null;
  is_active: boolean;
}

const TOOL_TYPE_LABELS: Record<string, string> = {
  drill: "Сверло",
  endmill: "Концевая фреза",
  insert: "Пластина",
  holder: "Держатель",
  tap: "Метчик",
  reamer: "Развёртка",
  boring_bar: "Расточная оправка",
  saw: "Дисковая пила",
  other: "Другое",
};

// ── Sub-components ────────────────────────────────────────────────────────────

function StarRating({
  value,
  onChange,
  readonly,
}: {
  value: number;
  onChange?: (v: number) => void;
  readonly?: boolean;
}) {
  const [hover, setHover] = useState(0);
  return (
    <span className="flex gap-0.5">
      {[1, 2, 3, 4, 5].map((star) => (
        <button
          key={star}
          type="button"
          disabled={readonly}
          onClick={() => onChange?.(star === value ? 0 : star)}
          onMouseEnter={() => !readonly && setHover(star)}
          onMouseLeave={() => !readonly && setHover(0)}
          className={`text-xl leading-none transition-colors ${readonly ? "cursor-default" : "cursor-pointer"} ${
            star <= (hover || value) ? "text-yellow-400" : "text-slate-600"
          }`}
        >
          ★
        </button>
      ))}
    </span>
  );
}

function Field({
  label,
  value,
  editing,
  onChange,
  multiline,
  className,
}: {
  label: string;
  value: string;
  editing: boolean;
  onChange: (v: string) => void;
  multiline?: boolean;
  className?: string;
}) {
  if (!editing) {
    return (
      <div className={className}>
        <span className="text-xs text-slate-500">{label}</span>
        <p className="text-sm text-slate-200 mt-0.5">{value || "—"}</p>
      </div>
    );
  }
  return (
    <div className={className}>
      <label className="block text-xs text-slate-400 mb-1">{label}</label>
      {multiline ? (
        <textarea
          value={value}
          onChange={(e) => onChange(e.target.value)}
          rows={3}
          className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 rounded text-slate-200 focus:outline-none focus:border-blue-500 resize-none"
        />
      ) : (
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 rounded text-slate-200 focus:outline-none focus:border-blue-500"
        />
      )}
    </div>
  );
}

function StatCard({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="bg-slate-800 border border-slate-700 rounded-lg p-3 text-center">
      <div className="text-lg font-bold text-slate-100">{value}</div>
      <div className="text-xs text-slate-500">{label}</div>
    </div>
  );
}

// ── Catalog Tab ───────────────────────────────────────────────────────────────

function CatalogTab({
  partyId,
  partyName,
}: {
  partyId: string;
  partyName: string;
}) {
  const [entries, setEntries] = useState<ToolCatalogEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [toolType, setToolType] = useState("");
  const [page, setPage] = useState(1);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadSuccess, setUploadSuccess] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [selectedEntryIds, setSelectedEntryIds] = useState<Set<string>>(
    new Set(),
  );
  const [deletingEntries, setDeletingEntries] = useState(false);
  const [confirmDeleteEntries, setConfirmDeleteEntries] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const PAGE_SIZE = 50;

  const loadEntries = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({
        page: String(page),
        page_size: String(PAGE_SIZE),
      });
      if (search) params.set("query", search);
      if (toolType) params.set("tool_type", toolType);
      const resp = await fetch(
        `${API}/tool-catalog/by-supplier/${partyId}/entries?${params}`,
      );
      if (resp.ok) {
        const data = await resp.json();
        setEntries(data.items ?? []);
        setTotal(data.total ?? 0);
      }
    } finally {
      setLoading(false);
    }
  }, [partyId, page, search, toolType]);

  useEffect(() => {
    loadEntries();
  }, [loadEntries]);

  // Reset page on filter change
  useEffect(() => {
    setPage(1);
  }, [search, toolType]);

  async function handleFileUpload(file: File) {
    setUploading(true);
    setUploadError(null);
    setUploadSuccess(null);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const resp = await fetch(
        `${API}/tool-catalog/by-supplier/${partyId}/catalog`,
        {
          method: "POST",
          body: formData,
        },
      );
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail ?? "Ошибка загрузки");
      }
      const data = await resp.json();
      setUploadSuccess(
        `Каталог принят в обработку${data.task_id ? ` (задача ${data.task_id.slice(0, 8)}...)` : ""}. Позиции появятся после индексации.`,
      );
      setTimeout(() => loadEntries(), 3000);
    } catch (e: unknown) {
      setUploadError(e instanceof Error ? e.message : "Ошибка загрузки");
    } finally {
      setUploading(false);
    }
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    const file = e.dataTransfer.files[0];
    if (file) handleFileUpload(file);
  }

  async function handleRefreshCatalog() {
    setRefreshing(true);
    setUploadError(null);
    setUploadSuccess(null);
    try {
      const resp = await fetch(
        `${API}/api/tool-catalog/suppliers-by-party/${partyId}/refresh`,
        {
          method: "POST",
        },
      );
      if (!resp.ok) {
        // Try finding the tool_supplier id first
        const tsResp = await fetch(
          `${API}/api/tool-catalog/by-supplier/${partyId}`,
        );
        if (tsResp.ok) {
          const tsData = await tsResp.json();
          const supplierId = (tsData.items ?? [])[0]?.id;
          if (supplierId) {
            const r2 = await fetch(
              `${API}/api/tool-catalog/suppliers/${supplierId}/refresh`,
              { method: "POST" },
            );
            if (r2.ok) {
              const d2 = await r2.json();
              setUploadSuccess(
                `Обновление каталога запущено${d2.task_id ? ` (задача ${d2.task_id.slice(0, 8)}...)` : ""}. Данные появятся через несколько минут.`,
              );
              setTimeout(() => loadEntries(), 4000);
              return;
            }
          }
        }
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail ?? "Ошибка обновления");
      }
      const data = await resp.json();
      setUploadSuccess(
        `Обновление каталога запущено${data.task_id ? ` (задача ${data.task_id.slice(0, 8)}...)` : ""}. Данные появятся через несколько минут.`,
      );
      setTimeout(() => loadEntries(), 4000);
    } catch (e: unknown) {
      setUploadError(
        e instanceof Error ? e.message : "Ошибка обновления каталога",
      );
    } finally {
      setRefreshing(false);
    }
  }

  function toggleSelectEntry(id: string) {
    setSelectedEntryIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleSelectAllEntries() {
    if (selectedEntryIds.size === entries.length) {
      setSelectedEntryIds(new Set());
    } else {
      setSelectedEntryIds(new Set(entries.map((e) => e.id)));
    }
  }

  async function handleDeleteEntry(entryId: string) {
    try {
      await fetch(`${API}/api/tool-catalog/entries/${entryId}`, {
        method: "DELETE",
      });
      await loadEntries();
    } catch {}
  }

  async function handleBulkDeleteEntries() {
    if (!selectedEntryIds.size) return;
    setDeletingEntries(true);
    try {
      await fetch(`${API}/api/tool-catalog/entries/bulk-delete`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ entry_ids: Array.from(selectedEntryIds) }),
      });
      setSelectedEntryIds(new Set());
      setConfirmDeleteEntries(false);
      await loadEntries();
    } finally {
      setDeletingEntries(false);
    }
  }

  return (
    <div className="space-y-4">
      {/* Upload zone */}
      <div
        onDragOver={(e) => e.preventDefault()}
        onDrop={onDrop}
        className="border-2 border-dashed border-slate-600 hover:border-blue-500/50 rounded-lg p-5 text-center transition-colors cursor-pointer"
        onClick={() => fileInputRef.current?.click()}
      >
        <input
          ref={fileInputRef}
          type="file"
          accept=".pdf,.xlsx,.xls,.csv,.json"
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) handleFileUpload(file);
            e.target.value = "";
          }}
        />
        <div className="flex flex-col items-center gap-2">
          {uploading ? (
            <>
              <div className="w-6 h-6 border-2 border-blue-400 border-t-transparent rounded-full animate-spin" />
              <span className="text-sm text-slate-400">
                Загрузка каталога...
              </span>
            </>
          ) : (
            <>
              <svg
                className="w-8 h-8 text-slate-500"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={1.5}
                  d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"
                />
              </svg>
              <span className="text-sm text-slate-400">
                Перетащите или нажмите для загрузки каталога инструментов
              </span>
              <span className="text-xs text-slate-600">
                PDF, Excel (.xlsx), CSV, JSON
              </span>
            </>
          )}
        </div>
      </div>

      {uploadSuccess && (
        <div className="px-4 py-2 bg-green-500/10 border border-green-500/30 rounded text-green-400 text-sm">
          {uploadSuccess}
        </div>
      )}
      {uploadError && (
        <div className="px-4 py-2 bg-red-500/10 border border-red-500/30 rounded text-red-400 text-sm">
          {uploadError}
        </div>
      )}

      {/* Filters + actions row */}
      <div className="flex items-center gap-3 flex-wrap">
        <input
          type="text"
          placeholder="Поиск по каталогу..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="flex-1 min-w-40 px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 rounded text-slate-200 placeholder-slate-500 focus:outline-none focus:border-blue-500"
        />
        <select
          value={toolType}
          onChange={(e) => setToolType(e.target.value)}
          className="px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 rounded text-slate-200 focus:outline-none focus:border-blue-500"
        >
          <option value="">Все типы</option>
          {Object.entries(TOOL_TYPE_LABELS).map(([k, v]) => (
            <option key={k} value={k}>
              {v}
            </option>
          ))}
        </select>
        <span className="text-xs text-slate-500 shrink-0">
          {total.toLocaleString("ru")} позиций
        </span>
        {/* Refresh button */}
        <button
          onClick={handleRefreshCatalog}
          disabled={refreshing}
          title="Повторно проиндексировать последний загруженный файл каталога"
          className="flex items-center gap-1.5 px-3 py-1.5 text-sm bg-slate-700 hover:bg-slate-600 border border-slate-600 rounded text-slate-300 disabled:opacity-50 transition-colors"
        >
          {refreshing ? (
            <span className="w-3.5 h-3.5 border border-slate-400 border-t-white rounded-full animate-spin" />
          ) : (
            <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 16 16">
              <path
                d="M2 8a6 6 0 0110.5-3.96M14 8a6 6 0 01-10.5 3.96M12 4.5V2l2 2.5-2 2.5V4.5zM4 11.5V14l-2-2.5 2-2.5v2.5z"
                stroke="currentColor"
                strokeWidth="1.2"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          )}
          Обновить каталог
        </button>
        {/* Select all toggle */}
        {entries.length > 0 && (
          <button
            onClick={toggleSelectAllEntries}
            className="px-3 py-1.5 text-xs text-slate-400 hover:text-white bg-slate-700 hover:bg-slate-600 border border-slate-600 rounded transition-colors"
          >
            {selectedEntryIds.size === entries.length
              ? "Снять всё"
              : "Выбрать всё"}
          </button>
        )}
      </div>

      {/* Bulk delete bar */}
      {selectedEntryIds.size > 0 && (
        <div className="flex items-center gap-3 bg-slate-700/60 border border-slate-600 rounded-lg px-4 py-2">
          <span className="text-sm text-slate-300">
            Выбрано:{" "}
            <strong className="text-white">{selectedEntryIds.size}</strong>
          </span>
          <button
            onClick={() => setSelectedEntryIds(new Set())}
            className="text-xs text-slate-400 hover:text-white px-2 transition-colors"
          >
            Снять
          </button>
          {confirmDeleteEntries ? (
            <>
              <span className="text-red-400 text-sm">
                Удалить {selectedEntryIds.size} позиций?
              </span>
              <button
                onClick={handleBulkDeleteEntries}
                disabled={deletingEntries}
                className="px-3 py-1 bg-red-600 hover:bg-red-500 disabled:opacity-50 text-white rounded text-sm transition-colors"
              >
                {deletingEntries ? "Удаление..." : "Да"}
              </button>
              <button
                onClick={() => setConfirmDeleteEntries(false)}
                className="px-3 py-1 bg-slate-600 text-white rounded text-sm transition-colors"
              >
                Отмена
              </button>
            </>
          ) : (
            <button
              onClick={() => setConfirmDeleteEntries(true)}
              className="ml-auto px-3 py-1 bg-red-700/60 hover:bg-red-700 text-white rounded text-sm transition-colors"
            >
              Удалить выбранные
            </button>
          )}
        </div>
      )}

      {/* Table */}
      {loading ? (
        <div className="py-8 text-center text-slate-500 text-sm">
          Загрузка...
        </div>
      ) : entries.length === 0 ? (
        <div className="py-10 text-center text-slate-500 text-sm">
          {total === 0
            ? "Каталог пуст. Загрузите файл каталога выше."
            : "Ничего не найдено по фильтру."}
        </div>
      ) : (
        <>
          <div className="bg-slate-800 border border-slate-700 rounded-lg overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-slate-700/50 text-slate-400 text-xs uppercase">
                  <tr>
                    <th className="w-8 px-2 py-2" />
                    <th className="text-left px-3 py-2">Артикул</th>
                    <th className="text-left px-3 py-2">Наименование</th>
                    <th className="text-left px-3 py-2">Тип</th>
                    <th className="text-right px-3 py-2">Ø мм</th>
                    <th className="text-left px-3 py-2">Материал</th>
                    <th className="text-left px-3 py-2">Покрытие</th>
                    <th className="text-right px-3 py-2">Цена</th>
                    <th className="w-8 px-2 py-2" />
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-700">
                  {entries.map((e) => (
                    <tr
                      key={e.id}
                      className={`group hover:bg-slate-700/30 transition-colors ${selectedEntryIds.has(e.id) ? "bg-blue-900/20" : ""}`}
                    >
                      <td className="px-2 py-2">
                        <button
                          onClick={() => toggleSelectEntry(e.id)}
                          className="w-4 h-4 rounded border flex items-center justify-center transition-colors"
                          style={{
                            background: selectedEntryIds.has(e.id)
                              ? "rgb(37 99 235)"
                              : "rgba(51,65,85,0.8)",
                            borderColor: selectedEntryIds.has(e.id)
                              ? "rgb(37 99 235)"
                              : "rgba(100,116,139,0.5)",
                          }}
                        >
                          {selectedEntryIds.has(e.id) && (
                            <svg
                              className="w-2.5 h-2.5 text-white"
                              fill="none"
                              viewBox="0 0 10 10"
                            >
                              <path
                                d="M1.5 5l2.5 2.5 4.5-4.5"
                                stroke="currentColor"
                                strokeWidth="1.5"
                                strokeLinecap="round"
                                strokeLinejoin="round"
                              />
                            </svg>
                          )}
                        </button>
                      </td>
                      <td className="px-3 py-2 font-mono text-xs text-slate-400">
                        {e.part_number ?? "—"}
                      </td>
                      <td className="px-3 py-2 text-slate-200 max-w-xs truncate">
                        {e.name}
                      </td>
                      <td className="px-3 py-2 text-slate-400 text-xs">
                        {TOOL_TYPE_LABELS[e.tool_type] ?? e.tool_type}
                      </td>
                      <td className="px-3 py-2 text-right font-mono text-xs text-slate-300">
                        {e.diameter_mm != null ? e.diameter_mm.toFixed(2) : "—"}
                      </td>
                      <td className="px-3 py-2 text-slate-400 text-xs">
                        {e.material ?? "—"}
                      </td>
                      <td className="px-3 py-2 text-slate-400 text-xs">
                        {e.coating ?? "—"}
                      </td>
                      <td className="px-3 py-2 text-right text-xs text-slate-300">
                        {e.price_value != null
                          ? `${e.price_value.toLocaleString("ru")} ${e.price_currency}`
                          : "—"}
                      </td>
                      <td className="px-2 py-2">
                        <button
                          onClick={() => handleDeleteEntry(e.id)}
                          className="w-6 h-6 rounded flex items-center justify-center text-slate-600 hover:text-red-400 hover:bg-red-900/30 transition-colors opacity-0 group-hover:opacity-100"
                          title="Удалить"
                        >
                          <svg
                            className="w-3.5 h-3.5"
                            fill="none"
                            viewBox="0 0 14 14"
                          >
                            <path
                              d="M2 3.5h10M5.5 3.5V2.5h3v1M4.5 3.5l.5 8h4l.5-8"
                              stroke="currentColor"
                              strokeWidth="1.2"
                              strokeLinecap="round"
                              strokeLinejoin="round"
                            />
                          </svg>
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Pagination */}
          {total > PAGE_SIZE && (
            <div className="flex items-center justify-between text-xs text-slate-500">
              <span>
                Показано {(page - 1) * PAGE_SIZE + 1}–
                {Math.min(page * PAGE_SIZE, total)} из{" "}
                {total.toLocaleString("ru")}
              </span>
              <div className="flex gap-2">
                <button
                  disabled={page === 1}
                  onClick={() => setPage((p) => p - 1)}
                  className="px-3 py-1 border border-slate-600 rounded hover:bg-slate-700 disabled:opacity-40 transition-colors"
                >
                  ←
                </button>
                <button
                  disabled={page * PAGE_SIZE >= total}
                  onClick={() => setPage((p) => p + 1)}
                  className="px-3 py-1 border border-slate-600 rounded hover:bg-slate-700 disabled:opacity-40 transition-colors"
                >
                  →
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

type Tab = "overview" | "catalog";

export default function SupplierProfilePage() {
  const params = useParams();
  const id = params.id as string;
  const router = useRouter();
  const searchParams = useSearchParams();

  const initialTab = (searchParams.get("tab") as Tab) ?? "overview";
  const [activeTab, setActiveTab] = useState<Tab>(initialTab);

  const [supplier, setSupplier] = useState<SupplierFull | null>(null);
  const [trust, setTrust] = useState<TrustScore | null>(null);
  const [prices, setPrices] = useState<PriceHistoryItem[]>([]);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [requisites, setRequisites] = useState<RequisiteCheck[]>([]);
  const [loading, setLoading] = useState(true);

  const [editing, setEditing] = useState(false);
  const [editForm, setEditForm] = useState<Partial<SupplierFull>>({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  function load() {
    setLoading(true);
    Promise.all([
      fetch(`${API}/api/suppliers/${id}`).then((r) => r.json()),
      fetch(`${API}/api/suppliers/${id}/trust-score`).then((r) => r.json()),
      fetch(`${API}/api/suppliers/${id}/price-history`).then((r) => r.json()),
      fetch(`${API}/api/suppliers/${id}/alerts`).then((r) => r.json()),
      fetch(`${API}/api/suppliers/${id}/check-requisites`, {
        method: "POST",
      }).then((r) => r.json()),
    ])
      .then(([s, t, p, a, req]) => {
        setSupplier(s);
        setTrust(t);
        setPrices(p.items ?? []);
        setAlerts(a.alerts ?? []);
        setRequisites(req.checks ?? []);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    if (!id) return;
    load();
  }, [id]);

  // Sync tab with URL param
  useEffect(() => {
    const tab = searchParams.get("tab") as Tab | null;
    if (tab && tab !== activeTab) setActiveTab(tab);
  }, [searchParams]);

  function switchTab(tab: Tab) {
    setActiveTab(tab);
    const url = new URL(window.location.href);
    url.searchParams.set("tab", tab);
    router.replace(url.pathname + url.search, { scroll: false });
  }

  async function handleDeleteSupplier() {
    setDeleting(true);
    try {
      const resp = await fetch(`${API}/api/suppliers/${id}?confirm=true`, {
        method: "DELETE",
      });
      if (resp.ok) {
        router.push("/suppliers");
      }
    } finally {
      setDeleting(false);
    }
  }

  function startEdit() {
    if (!supplier) return;
    setEditForm({
      name: supplier.name,
      inn: supplier.inn ?? "",
      kpp: supplier.kpp ?? "",
      ogrn: supplier.ogrn ?? "",
      address: supplier.address ?? "",
      contact_email: supplier.contact_email ?? "",
      contact_phone: supplier.contact_phone ?? "",
      bank_name: supplier.bank_name ?? "",
      bank_bik: supplier.bank_bik ?? "",
      bank_account: supplier.bank_account ?? "",
      corr_account: supplier.corr_account ?? "",
      user_notes: supplier.user_notes ?? "",
      user_rating: supplier.user_rating ?? 0,
    });
    setSaveError("");
    setEditing(true);
  }

  async function saveEdit() {
    setSaving(true);
    setSaveError("");
    try {
      const body: Record<string, unknown> = {};
      const fields = [
        "name",
        "inn",
        "kpp",
        "ogrn",
        "address",
        "contact_email",
        "contact_phone",
        "bank_name",
        "bank_bik",
        "bank_account",
        "corr_account",
        "user_notes",
      ] as const;
      for (const f of fields) {
        const v = editForm[f as keyof typeof editForm];
        body[f] = typeof v === "string" && v.trim() === "" ? null : (v ?? null);
      }
      body.user_rating = (editForm.user_rating as number) || null;
      const resp = await fetch(`${API}/api/suppliers/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        setSaveError("Ошибка сохранения");
        return;
      }
      setEditing(false);
      load();
    } finally {
      setSaving(false);
    }
  }

  if (loading)
    return <div className="p-6 text-slate-400 text-sm">Загрузка...</div>;
  if (!supplier)
    return (
      <div className="p-6 text-slate-400 text-sm">Поставщик не найден</div>
    );

  const trustColor =
    (trust?.trust_score ?? 0) >= 0.8
      ? "text-green-400"
      : (trust?.trust_score ?? 0) >= 0.5
        ? "text-amber-400"
        : "text-red-400";

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <button
        onClick={() => router.back()}
        className="text-sm text-slate-500 hover:text-slate-300 mb-4 block"
      >
        ← Назад
      </button>

      {/* Header */}
      <div className="flex items-start justify-between mb-4">
        <div className="flex-1 min-w-0">
          {editing ? (
            <input
              type="text"
              value={(editForm.name as string) ?? ""}
              onChange={(e) =>
                setEditForm((f) => ({ ...f, name: e.target.value }))
              }
              className="text-xl font-bold bg-slate-700 border border-slate-600 rounded px-3 py-1 text-slate-100 focus:outline-none focus:border-blue-500 w-full max-w-lg"
            />
          ) : (
            <h1 className="text-xl font-bold text-slate-100">
              {supplier.name}
            </h1>
          )}
          <div className="text-sm text-slate-500 mt-1">
            ИНН {supplier.inn ?? "—"} / КПП {supplier.kpp ?? "—"}
          </div>
          {supplier.address && !editing && (
            <div className="text-sm text-slate-400 mt-0.5">
              {supplier.address}
            </div>
          )}
        </div>
        <div className="flex items-start gap-4 ml-4 shrink-0">
          {trust && !editing && (
            <div className="text-right">
              <div className={`text-2xl font-bold ${trustColor}`}>
                {(trust.trust_score * 100).toFixed(0)}%
              </div>
              <div className="text-xs text-slate-500">
                {trust.recommendation}
              </div>
            </div>
          )}
          {editing ? (
            <div className="flex gap-2">
              <button
                onClick={() => setEditing(false)}
                className="px-3 py-1.5 text-xs border border-slate-600 text-slate-400 hover:text-slate-200 rounded transition-colors"
              >
                Отмена
              </button>
              <button
                onClick={saveEdit}
                disabled={saving}
                className="px-4 py-1.5 text-xs bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white rounded transition-colors"
              >
                {saving ? "Сохраняю..." : "Сохранить"}
              </button>
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <button
                onClick={startEdit}
                className="px-3 py-1.5 text-xs border border-slate-600 text-slate-400 hover:text-slate-200 hover:border-slate-500 rounded transition-colors"
              >
                Редактировать
              </button>
              {confirmDelete ? (
                <>
                  <span className="text-red-400 text-xs">
                    Удалить поставщика?
                  </span>
                  <button
                    onClick={handleDeleteSupplier}
                    disabled={deleting}
                    className="px-3 py-1.5 text-xs bg-red-600 hover:bg-red-500 disabled:opacity-50 text-white rounded transition-colors"
                  >
                    {deleting ? "Удаление..." : "Да"}
                  </button>
                  <button
                    onClick={() => setConfirmDelete(false)}
                    className="px-3 py-1.5 text-xs bg-slate-700 text-white rounded transition-colors"
                  >
                    Отмена
                  </button>
                </>
              ) : (
                <button
                  onClick={() => setConfirmDelete(true)}
                  className="px-3 py-1.5 text-xs border border-red-600/40 text-red-400 hover:bg-red-600/10 rounded transition-colors"
                >
                  Удалить
                </button>
              )}
            </div>
          )}
        </div>
      </div>

      {saveError && <p className="text-red-400 text-xs mb-4">{saveError}</p>}

      {/* Alerts */}
      {alerts.length > 0 && !editing && (
        <div className="mb-4 space-y-2">
          {alerts.map((a) => (
            <div
              key={a.id}
              className={`px-4 py-2 rounded text-sm ${
                a.severity === "error"
                  ? "bg-red-500/10 text-red-400 border border-red-500/30"
                  : "bg-amber-500/10 text-amber-400 border border-amber-500/30"
              }`}
            >
              {a.message}
            </div>
          ))}
        </div>
      )}

      {/* Tabs */}
      {!editing && (
        <div className="flex gap-1 mb-6 border-b border-slate-700">
          {(
            [
              { key: "overview", label: "Обзор" },
              { key: "catalog", label: "Каталог инструментов" },
            ] as { key: Tab; label: string }[]
          ).map(({ key, label }) => (
            <button
              key={key}
              onClick={() => switchTab(key)}
              className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                activeTab === key
                  ? "border-blue-500 text-blue-400"
                  : "border-transparent text-slate-500 hover:text-slate-300"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      )}

      {/* ── Overview tab ── */}
      {(activeTab === "overview" || editing) && (
        <>
          {/* Edit form */}
          {editing && (
            <div className="bg-slate-800 border border-slate-700 rounded-lg p-5 mb-6 space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <Field
                  label="ИНН"
                  value={(editForm.inn as string) ?? ""}
                  editing
                  onChange={(v) => setEditForm((f) => ({ ...f, inn: v }))}
                />
                <Field
                  label="КПП"
                  value={(editForm.kpp as string) ?? ""}
                  editing
                  onChange={(v) => setEditForm((f) => ({ ...f, kpp: v }))}
                />
                <Field
                  label="ОГРН"
                  value={(editForm.ogrn as string) ?? ""}
                  editing
                  onChange={(v) => setEditForm((f) => ({ ...f, ogrn: v }))}
                />
                <Field
                  label="Email"
                  value={(editForm.contact_email as string) ?? ""}
                  editing
                  onChange={(v) =>
                    setEditForm((f) => ({ ...f, contact_email: v }))
                  }
                />
                <Field
                  label="Телефон"
                  value={(editForm.contact_phone as string) ?? ""}
                  editing
                  onChange={(v) =>
                    setEditForm((f) => ({ ...f, contact_phone: v }))
                  }
                />
                <Field
                  label="Адрес"
                  value={(editForm.address as string) ?? ""}
                  editing
                  onChange={(v) => setEditForm((f) => ({ ...f, address: v }))}
                  className="col-span-2"
                />
              </div>
              <details className="group">
                <summary className="cursor-pointer text-xs text-slate-400 hover:text-slate-200 select-none">
                  Банковские реквизиты
                </summary>
                <div className="grid grid-cols-2 gap-4 mt-3">
                  <Field
                    label="Банк"
                    value={(editForm.bank_name as string) ?? ""}
                    editing
                    onChange={(v) =>
                      setEditForm((f) => ({ ...f, bank_name: v }))
                    }
                    className="col-span-2"
                  />
                  <Field
                    label="БИК"
                    value={(editForm.bank_bik as string) ?? ""}
                    editing
                    onChange={(v) =>
                      setEditForm((f) => ({ ...f, bank_bik: v }))
                    }
                  />
                  <Field
                    label="Р/с"
                    value={(editForm.bank_account as string) ?? ""}
                    editing
                    onChange={(v) =>
                      setEditForm((f) => ({ ...f, bank_account: v }))
                    }
                  />
                  <Field
                    label="Корр. счёт"
                    value={(editForm.corr_account as string) ?? ""}
                    editing
                    onChange={(v) =>
                      setEditForm((f) => ({ ...f, corr_account: v }))
                    }
                  />
                </div>
              </details>
              <div>
                <label className="block text-xs text-slate-400 mb-1">
                  Рейтинг поставщика
                </label>
                <StarRating
                  value={(editForm.user_rating as number) ?? 0}
                  onChange={(v) =>
                    setEditForm((f) => ({ ...f, user_rating: v }))
                  }
                />
              </div>
              <Field
                label="Заметки"
                value={(editForm.user_notes as string) ?? ""}
                editing
                onChange={(v) => setEditForm((f) => ({ ...f, user_notes: v }))}
                multiline
              />
            </div>
          )}

          {/* User rating + notes (read) */}
          {!editing && (supplier.user_rating || supplier.user_notes) && (
            <div className="bg-slate-800 border border-slate-700 rounded-lg p-4 mb-6 space-y-2">
              {supplier.user_rating ? (
                <div className="flex items-center gap-3">
                  <span className="text-xs text-slate-500">Оценка:</span>
                  <StarRating value={supplier.user_rating} readonly />
                </div>
              ) : null}
              {supplier.user_notes ? (
                <div>
                  <span className="text-xs text-slate-500 block mb-1">
                    Заметки:
                  </span>
                  <p className="text-sm text-slate-300 whitespace-pre-wrap">
                    {supplier.user_notes}
                  </p>
                </div>
              ) : null}
            </div>
          )}

          {/* Stats */}
          {!editing && (
            <div className="grid grid-cols-4 gap-4 mb-6">
              <StatCard
                label="Счетов"
                value={supplier.profile?.total_invoices ?? 0}
              />
              <StatCard
                label="Общая сумма"
                value={`${((supplier.profile?.total_amount ?? 0) / 1000).toFixed(0)} K`}
              />
              <StatCard
                label="Открытых"
                value={supplier.recent_invoices_count}
              />
              <StatCard
                label="Открытая сумма"
                value={`${(supplier.open_invoices_amount / 1000).toFixed(0)} K`}
              />
            </div>
          )}

          {!editing && (
            <div className="grid grid-cols-2 gap-6">
              {trust && (
                <div className="bg-slate-800 border border-slate-700 rounded-lg p-4">
                  <h3 className="font-semibold text-sm text-slate-200 mb-3">
                    Trust Score
                  </h3>
                  <div className="space-y-2">
                    {trust.breakdown.map((b) => (
                      <div key={b.factor}>
                        <div className="flex justify-between text-xs text-slate-400">
                          <span>{b.detail}</span>
                          <span className="font-mono">
                            {(b.score * 100).toFixed(0)}%
                          </span>
                        </div>
                        <div className="h-1.5 bg-slate-700 rounded-full mt-1">
                          <div
                            className="h-1.5 bg-blue-500 rounded-full"
                            style={{ width: `${b.score * 100}%` }}
                          />
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              <div className="bg-slate-800 border border-slate-700 rounded-lg p-4">
                <h3 className="font-semibold text-sm text-slate-200 mb-3">
                  Реквизиты
                </h3>
                <div className="space-y-1.5 text-sm">
                  {requisites.map((r) => (
                    <div key={r.field} className="flex items-center gap-2">
                      <span
                        className={`w-2 h-2 rounded-full shrink-0 ${
                          r.status === "ok"
                            ? "bg-green-500"
                            : r.status === "warning"
                              ? "bg-amber-500"
                              : r.status === "error"
                                ? "bg-red-500"
                                : "bg-slate-500"
                        }`}
                      />
                      <span className="text-slate-400 text-xs">
                        {r.field}: {r.message ?? "OK"}
                      </span>
                    </div>
                  ))}
                </div>
                <div className="mt-3 text-xs text-slate-500">
                  Email: {supplier.contact_email ?? "—"} | Тел:{" "}
                  {supplier.contact_phone ?? "—"}
                </div>
              </div>
            </div>
          )}

          {/* Price History */}
          {!editing && prices.length > 0 && (
            <div className="mt-6 bg-slate-800 border border-slate-700 rounded-lg p-4">
              <h3 className="font-semibold text-sm text-slate-200 mb-3">
                История цен ({prices.length} позиций)
              </h3>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="text-xs text-slate-500 uppercase">
                    <tr>
                      <th className="text-left px-3 py-1.5">Позиция</th>
                      <th className="text-right px-3 py-1.5">Текущая</th>
                      <th className="text-right px-3 py-1.5">Средняя</th>
                      <th className="text-center px-3 py-1.5">Тренд</th>
                      <th className="text-right px-3 py-1.5">Точек</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700">
                    {prices.map((item) => (
                      <tr key={item.description}>
                        <td className="px-3 py-2 text-slate-200">
                          {item.description}
                        </td>
                        <td className="px-3 py-2 text-right font-mono text-slate-200">
                          {item.current_price?.toLocaleString("ru-RU") ?? "—"}
                        </td>
                        <td className="px-3 py-2 text-right font-mono text-slate-400">
                          {item.avg_price?.toLocaleString("ru-RU") ?? "—"}
                        </td>
                        <td className="px-3 py-2 text-center">
                          {item.trend === "up" && (
                            <span className="text-red-400 text-xs">▲ Рост</span>
                          )}
                          {item.trend === "down" && (
                            <span className="text-green-400 text-xs">
                              ▼ Снижение
                            </span>
                          )}
                          {item.trend === "stable" && (
                            <span className="text-slate-400 text-xs">
                              ▶ Стабильно
                            </span>
                          )}
                        </td>
                        <td className="px-3 py-2 text-right text-slate-500">
                          {item.points.length}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}

      {/* ── Catalog tab ── */}
      {activeTab === "catalog" && !editing && (
        <CatalogTab partyId={supplier.id} partyName={supplier.name} />
      )}
    </div>
  );
}
