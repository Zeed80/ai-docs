"use client";

import { getApiBaseUrl } from "@/lib/api-base";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  flexRender,
  type ColumnDef,
  type SortingState,
  type RowSelectionState,
} from "@tanstack/react-table";
import {
  tables,
  type TableColumn,
  type TableRow,
  type TableFilter,
  type TableSort,
  type SavedView,
} from "@/lib/api-client";

const API = getApiBaseUrl();

const statusBadge: Record<string, string> = {
  draft: "bg-slate-700 text-slate-400",
  needs_review: "bg-amber-900/40 text-amber-400",
  approved: "bg-green-900/40 text-green-400",
  rejected: "bg-red-900/40 text-red-400",
  paid: "bg-blue-900/40 text-blue-400",
};

const statusLabel: Record<string, string> = {
  draft: "Черновик",
  needs_review: "На проверку",
  approved: "Утверждён",
  rejected: "Отклонён",
  paid: "Оплачен",
};

type DeleteMode = "selected" | "filtered" | "all";

interface DeleteDialogProps {
  mode: DeleteMode;
  count: number;
  statusFilter: string;
  onConfirm: () => void;
  onCancel: () => void;
}

function DeleteDialog({
  mode,
  count,
  statusFilter,
  onConfirm,
  onCancel,
}: DeleteDialogProps) {
  const label =
    mode === "selected"
      ? `${count} выбранных счетов`
      : mode === "filtered"
        ? `всех счетов${statusFilter ? ` со статусом "${statusLabel[statusFilter]}"` : ""} (${count} шт.)`
        : `ВСЕХ счетов в системе (${count} шт.)`;

  return (
    <div
      className="fixed inset-0 bg-black/60 flex items-center justify-center z-50"
      onClick={onCancel}
    >
      <div
        className="bg-slate-800 border border-red-800/60 rounded-xl shadow-2xl p-6 w-96 text-slate-200"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 mb-4">
          <div className="w-10 h-10 rounded-full bg-red-900/50 flex items-center justify-center flex-shrink-0">
            <svg
              className="w-5 h-5 text-red-400"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"
              />
            </svg>
          </div>
          <div>
            <h3 className="font-semibold text-red-400">Подтвердите удаление</h3>
            <p className="text-xs text-slate-400 mt-0.5">
              Это действие нельзя отменить
            </p>
          </div>
        </div>
        <p className="text-sm mb-5 text-slate-300">
          Будут удалены{" "}
          <span className="font-semibold text-white">{label}</span>. Исходные
          документы останутся нетронутыми.
        </p>
        <div className="flex gap-2 justify-end">
          <button
            onClick={onCancel}
            className="px-4 py-1.5 text-sm text-slate-400 hover:text-slate-200"
          >
            Отмена
          </button>
          <button
            onClick={onConfirm}
            className="px-4 py-1.5 text-sm bg-red-700 hover:bg-red-600 text-white rounded-lg font-medium"
          >
            Удалить
          </button>
        </div>
      </div>
    </div>
  );
}

export default function InvoicesPage() {
  const router = useRouter();
  const [rows, setRows] = useState<TableRow[]>([]);
  const [columns, setColumns] = useState<TableColumn[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [sorting, setSorting] = useState<SortingState>([]);
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({});
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [search, setSearch] = useState("");
  const [views, setViews] = useState<SavedView[]>([]);
  const [activeView, setActiveView] = useState<string | null>(null);
  const [showViewDialog, setShowViewDialog] = useState(false);
  const [viewName, setViewName] = useState("");
  const [deleteDialog, setDeleteDialog] = useState<{ mode: DeleteMode } | null>(
    null,
  );
  const [deleting, setDeleting] = useState(false);
  const searchTimeout = useRef<ReturnType<typeof setTimeout>>(undefined);
  const limit = 50;

  const filters = useMemo<TableFilter[]>(() => {
    const f: TableFilter[] = [];
    if (statusFilter)
      f.push({ column: "status", operator: "eq", value: statusFilter });
    return f;
  }, [statusFilter]);

  const apiSort = useMemo<TableSort[]>(
    () =>
      sorting.map((s) => ({
        column: s.id,
        direction: s.desc ? "desc" : "asc",
      })),
    [sorting],
  );

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const data = await tables.query({
        table: "invoices",
        filters,
        sort: apiSort,
        search: search || undefined,
        offset,
        limit,
      });
      setRows(data.rows);
      setColumns(data.columns);
      setTotal(data.total);
    } catch {
      setRows([]);
    } finally {
      setLoading(false);
    }
  }, [filters, apiSort, search, offset]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  useEffect(() => {
    tables
      .listViews("invoices")
      .then(setViews)
      .catch(() => {});
  }, []);

  // Clear selection when data refreshes
  useEffect(() => {
    setRowSelection({});
  }, [rows]);

  const handleSearchChange = (val: string) => {
    if (searchTimeout.current) clearTimeout(searchTimeout.current);
    searchTimeout.current = setTimeout(() => {
      setSearch(val);
      setOffset(0);
    }, 300);
  };

  const selectedIds = useMemo(
    () =>
      Object.keys(rowSelection)
        .filter((k) => rowSelection[k])
        .map((idx) => rows[Number(idx)]?.id)
        .filter(Boolean) as string[],
    [rowSelection, rows],
  );

  // ── Delete handlers ────────────────────────────────────────────────────────

  const performDelete = async (mode: DeleteMode) => {
    setDeleting(true);
    try {
      if (mode === "selected") {
        // Delete one by one (parallel, max 10 at a time)
        const chunks: string[][] = [];
        for (let i = 0; i < selectedIds.length; i += 10)
          chunks.push(selectedIds.slice(i, i + 10));
        for (const chunk of chunks) {
          await Promise.all(
            chunk.map((id) =>
              fetch(`${API}/api/invoices/${id}`, { method: "DELETE" }),
            ),
          );
        }
      } else {
        const body: Record<string, unknown> =
          mode === "all"
            ? { delete_all: true }
            : {
                ...(statusFilter ? { status: statusFilter } : {}),
                delete_all: !statusFilter,
              };
        await fetch(`${API}/api/invoices`, {
          method: "DELETE",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      }
      setDeleteDialog(null);
      setRowSelection({});
      await fetchData();
    } finally {
      setDeleting(false);
    }
  };

  // ── Table columns ──────────────────────────────────────────────────────────

  const checkboxCol: ColumnDef<TableRow> = {
    id: "__select",
    header: ({ table }) => (
      <input
        type="checkbox"
        checked={table.getIsAllRowsSelected()}
        ref={(el) => {
          if (el) el.indeterminate = table.getIsSomeRowsSelected();
        }}
        onChange={table.getToggleAllRowsSelectedHandler()}
        className="w-3.5 h-3.5 accent-blue-500 cursor-pointer"
        onClick={(e) => e.stopPropagation()}
      />
    ),
    cell: ({ row }) => (
      <input
        type="checkbox"
        checked={row.getIsSelected()}
        onChange={row.getToggleSelectedHandler()}
        className="w-3.5 h-3.5 accent-blue-500 cursor-pointer"
        onClick={(e) => e.stopPropagation()}
      />
    ),
    enableSorting: false,
    size: 40,
  };

  const deleteCol: ColumnDef<TableRow> = {
    id: "__delete",
    header: "",
    cell: ({ row }) => (
      <button
        onClick={(e) => {
          e.stopPropagation();
          setRowSelection({ [row.index]: true });
          setDeleteDialog({ mode: "selected" });
        }}
        className="opacity-0 group-hover:opacity-100 p-1 text-slate-500 hover:text-red-400 transition-all"
        title="Удалить"
      >
        <svg
          className="w-3.5 h-3.5"
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"
          />
        </svg>
      </button>
    ),
    enableSorting: false,
    size: 36,
  };

  const tableColumns = useMemo<ColumnDef<TableRow>[]>(
    () => [
      checkboxCol,
      ...columns.map((col) => ({
        id: col.key,
        header: col.label,
        accessorFn: (row: TableRow) => row.data[col.key],
        cell: ({
          getValue,
          row: r,
        }: {
          getValue: () => unknown;
          row: { original: TableRow };
        }) => {
          const val = getValue();
          if (col.key === "status") {
            const s = val as string;
            return (
              <span
                className={`px-2 py-0.5 text-xs rounded-full ${statusBadge[s] ?? "bg-slate-700"}`}
              >
                {statusLabel[s] ?? s}
              </span>
            );
          }
          if (["total_amount", "tax_amount", "subtotal"].includes(col.key)) {
            const n = val as number | null;
            return n != null
              ? n.toLocaleString("ru-RU", { minimumFractionDigits: 2 })
              : "—";
          }
          if (col.key === "overall_confidence") {
            const n = val as number | null;
            if (n == null) return "—";
            return (
              <span
                className={`text-xs ${n >= 0.8 ? "text-green-400" : n >= 0.5 ? "text-amber-400" : "text-red-400"}`}
              >
                {(n * 100).toFixed(0)}%
              </span>
            );
          }
          if (col.data_type === "date" && val)
            return new Date(val as string).toLocaleDateString("ru-RU");
          return (val as string) ?? "—";
        },
        enableSorting: col.sortable,
      })),
      deleteCol,
      // eslint-disable-next-line react-hooks/exhaustive-deps
    ],
    [columns],
  );

  const table = useReactTable({
    data: rows,
    columns: tableColumns,
    state: { sorting, rowSelection },
    onSortingChange: setSorting,
    onRowSelectionChange: setRowSelection,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    manualSorting: true,
    enableRowSelection: true,
  });

  // ── Export ─────────────────────────────────────────────────────────────────

  const handleExport = async (format: string) => {
    const resp = await tables.exportUrl({ table: "invoices", filters, format });
    if (!resp.ok) return;
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download =
      resp.headers.get("Content-Disposition")?.match(/filename="(.+)"/)?.[1] ??
      `export.${format}`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleExport1C = async () => {
    const ids = rows.map((r) => r.id);
    const resp = await tables.export1cUrl({ invoice_ids: ids });
    if (!resp.ok) return;
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download =
      resp.headers.get("Content-Disposition")?.match(/filename="(.+)"/)?.[1] ??
      "export_1c.xml";
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleSaveView = async () => {
    if (!viewName.trim()) return;
    const view = await tables.createView({
      name: viewName.trim(),
      table: "invoices",
      filters,
      sort: apiSort,
    });
    setViews((v) => [view, ...v]);
    setShowViewDialog(false);
    setViewName("");
  };

  const applyView = (view: SavedView) => {
    setActiveView(view.id);
    const sf = view.filters.find((f) => f.column === "status");
    setStatusFilter(sf ? (sf.value as string) : "");
    if (view.sort.length > 0)
      setSorting(
        view.sort.map((s) => ({ id: s.column, desc: s.direction === "desc" })),
      );
    setOffset(0);
  };

  const filterOptions = ["", "needs_review", "approved", "rejected", "draft"];
  const filterLabels: Record<string, string> = {
    "": "Все",
    needs_review: "На проверку",
    approved: "Утверждённые",
    rejected: "Отклонённые",
    draft: "Черновики",
  };
  const totalPages = Math.ceil(total / limit);
  const currentPage = Math.floor(offset / limit) + 1;

  return (
    <div className="p-6 max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-xl font-bold text-slate-100">Счета</h1>
        <div className="flex gap-2">
          <button
            onClick={() => handleExport("xlsx")}
            className="px-3 py-1.5 text-xs bg-green-700 text-white rounded hover:bg-green-600"
          >
            Excel
          </button>
          <button
            onClick={() => handleExport("csv")}
            className="px-3 py-1.5 text-xs bg-slate-600 text-white rounded hover:bg-slate-500"
          >
            CSV
          </button>
          <button
            onClick={handleExport1C}
            className="px-3 py-1.5 text-xs bg-amber-700 text-white rounded hover:bg-amber-600"
          >
            1C
          </button>
          <button
            onClick={() => router.push("/invoices/import")}
            className="px-3 py-1.5 text-xs bg-blue-700 text-white rounded hover:bg-blue-600 flex items-center gap-1"
          >
            <svg
              className="w-3 h-3"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12"
              />
            </svg>
            Импорт
          </button>
        </div>
      </div>

      {/* Saved views */}
      {views.length > 0 && (
        <div className="flex gap-1.5 mb-3 flex-wrap">
          <span className="text-xs text-slate-400 self-center mr-1">Виды:</span>
          {views.map((v) => (
            <button
              key={v.id}
              onClick={() => applyView(v)}
              className={`px-2.5 py-0.5 text-xs rounded-full border ${activeView === v.id ? "bg-blue-900/40 border-blue-600 text-blue-400" : "bg-slate-700 border-slate-600 text-slate-300 hover:bg-slate-600"}`}
            >
              {v.name}
            </button>
          ))}
        </div>
      )}

      {/* Toolbar */}
      <div className="flex items-center gap-2 mb-4 flex-wrap">
        <div className="flex gap-1.5">
          {filterOptions.map((f) => (
            <button
              key={f}
              onClick={() => {
                setStatusFilter(f);
                setOffset(0);
                setActiveView(null);
              }}
              className={`px-3 py-1 text-xs rounded-full border transition-colors ${statusFilter === f ? "bg-slate-600 text-white border-slate-500" : "bg-slate-700 text-slate-300 border-slate-600 hover:bg-slate-600"}`}
            >
              {filterLabels[f]}
            </button>
          ))}
        </div>

        <div className="flex-1" />

        <input
          type="text"
          placeholder="Поиск по номеру..."
          onChange={(e) => handleSearchChange(e.target.value)}
          className="px-3 py-1.5 text-sm bg-slate-800 border border-slate-600 text-slate-200 placeholder-slate-500 rounded w-48 outline-none focus:border-blue-400"
        />

        <button
          onClick={() => setShowViewDialog(true)}
          className="px-2.5 py-1.5 text-xs text-slate-400 border border-slate-600 rounded hover:bg-slate-700"
        >
          + Вид
        </button>
      </div>

      {/* Bulk action bar — shown when rows are selected */}
      {selectedIds.length > 0 && (
        <div className="flex items-center gap-3 mb-3 px-3 py-2 bg-blue-950/40 border border-blue-700/50 rounded-lg text-sm">
          <span className="text-blue-300 font-medium">
            Выбрано: {selectedIds.length}
          </span>
          <button
            onClick={() => {
              setRowSelection({});
            }}
            className="text-xs text-slate-400 hover:text-slate-200"
          >
            Сбросить
          </button>
          <div className="flex-1" />
          <button
            onClick={() => setDeleteDialog({ mode: "selected" })}
            className="px-3 py-1 text-xs bg-red-800 hover:bg-red-700 text-white rounded flex items-center gap-1.5"
          >
            <svg
              className="w-3.5 h-3.5"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"
              />
            </svg>
            Удалить выбранные
          </button>
        </div>
      )}

      {/* Table */}
      {loading && rows.length === 0 ? (
        <div className="text-slate-400 py-12 text-center">Загрузка...</div>
      ) : rows.length === 0 ? (
        <div className="text-slate-400 py-12 text-center">Нет счетов</div>
      ) : (
        <>
          <div className="bg-slate-800 border border-slate-700 rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-slate-700/50 text-slate-400 text-xs uppercase">
                {table.getHeaderGroups().map((hg) => (
                  <tr key={hg.id}>
                    {hg.headers.map((header) => (
                      <th
                        key={header.id}
                        onClick={header.column.getToggleSortingHandler()}
                        className={`px-3 py-2.5 text-left ${header.column.getCanSort() ? "cursor-pointer select-none hover:text-slate-200" : ""}`}
                      >
                        <span className="flex items-center gap-1">
                          {flexRender(
                            header.column.columnDef.header,
                            header.getContext(),
                          )}
                          {header.column.getIsSorted() === "asc" && " ↑"}
                          {header.column.getIsSorted() === "desc" && " ↓"}
                        </span>
                      </th>
                    ))}
                  </tr>
                ))}
              </thead>
              <tbody className="divide-y divide-slate-700">
                {table.getRowModel().rows.map((row) => (
                  <tr
                    key={row.id}
                    className={`group cursor-pointer transition-colors ${row.getIsSelected() ? "bg-blue-950/30" : "hover:bg-slate-700/50"}`}
                    onClick={() => {
                      const docId = row.original.data.document_id as
                        | string
                        | null;
                      if (docId) router.push(`/documents/${docId}/review`);
                    }}
                  >
                    {row.getVisibleCells().map((cell) => (
                      <td key={cell.id} className="px-3 py-2.5 text-slate-200">
                        {flexRender(
                          cell.column.columnDef.cell,
                          cell.getContext(),
                        )}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Pagination + bulk delete buttons */}
          <div className="mt-3 flex items-center justify-between text-xs text-slate-500">
            <div className="flex items-center gap-2">
              <span>{total} всего</span>
              <span className="text-slate-600">·</span>
              <button
                onClick={() => setDeleteDialog({ mode: "filtered" })}
                className="text-red-500 hover:text-red-400"
                title={
                  statusFilter
                    ? `Удалить все "${filterLabels[statusFilter]}"`
                    : "Удалить все в текущем фильтре"
                }
              >
                {statusFilter
                  ? `Удалить "${filterLabels[statusFilter]}"`
                  : "Удалить все"}
              </button>
            </div>
            <div className="flex items-center gap-2">
              <button
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - limit))}
                className="px-2 py-1 border border-slate-600 text-slate-400 rounded disabled:opacity-30 hover:bg-slate-700"
              >
                ←
              </button>
              <span>
                {currentPage} / {totalPages || 1}
              </span>
              <button
                disabled={offset + limit >= total}
                onClick={() => setOffset(offset + limit)}
                className="px-2 py-1 border border-slate-600 text-slate-400 rounded disabled:opacity-30 hover:bg-slate-700"
              >
                →
              </button>
            </div>
          </div>
        </>
      )}

      {/* Save View Dialog */}
      {showViewDialog && (
        <div
          className="fixed inset-0 bg-black/40 flex items-center justify-center z-50"
          onClick={() => setShowViewDialog(false)}
        >
          <div
            className="bg-slate-800 border border-slate-700 rounded-lg shadow-xl p-5 w-80 text-slate-200"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-sm font-bold mb-3">Сохранить вид</h3>
            <input
              type="text"
              placeholder="Название..."
              value={viewName}
              onChange={(e) => setViewName(e.target.value)}
              className="w-full px-3 py-2 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded mb-3 outline-none focus:border-blue-400"
              autoFocus
              onKeyDown={(e) => e.key === "Enter" && handleSaveView()}
            />
            <div className="flex gap-2 justify-end">
              <button
                onClick={() => setShowViewDialog(false)}
                className="px-3 py-1.5 text-xs text-slate-500"
              >
                Отмена
              </button>
              <button
                onClick={handleSaveView}
                className="px-3 py-1.5 text-xs bg-blue-600 text-white rounded hover:bg-blue-700"
              >
                Сохранить
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Delete Confirmation Dialog */}
      {deleteDialog && !deleting && (
        <DeleteDialog
          mode={deleteDialog.mode}
          count={deleteDialog.mode === "selected" ? selectedIds.length : total}
          statusFilter={statusFilter}
          onConfirm={() => performDelete(deleteDialog.mode)}
          onCancel={() => setDeleteDialog(null)}
        />
      )}

      {deleting && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
          <div className="bg-slate-800 border border-slate-700 rounded-xl p-6 text-slate-200 flex items-center gap-3">
            <svg
              className="w-5 h-5 animate-spin text-red-400"
              fill="none"
              viewBox="0 0 24 24"
            >
              <circle
                className="opacity-25"
                cx="12"
                cy="12"
                r="10"
                stroke="currentColor"
                strokeWidth="4"
              />
              <path
                className="opacity-75"
                fill="currentColor"
                d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
              />
            </svg>
            Удаление...
          </div>
        </div>
      )}
    </div>
  );
}
