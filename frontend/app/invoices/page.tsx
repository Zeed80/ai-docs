"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { mutFetch } from "@/lib/auth";
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
  type ColumnOrderState,
  type VisibilityState,
  type Header,
} from "@tanstack/react-table";
import {
  DndContext,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  arrayMove,
  horizontalListSortingStrategy,
  useSortable,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import {
  tables,
  type TableColumn,
  type TableRow,
  type TableFilter,
  type TableSort,
  type SavedView,
} from "@/lib/api-client";
import {
  type ColumnPrefs,
  PINNED_LEFT,
  PINNED_RIGHT,
  defaultPrefs,
  loadColumnPrefs,
  reconcilePrefs,
  saveColumnPrefs,
  visibleOrderedKeys,
} from "@/lib/invoice-columns";
import { ColumnManager } from "@/components/invoices/ColumnManager";
import { FilterBuilder } from "@/components/invoices/FilterBuilder";
import { EditableNotesCell } from "@/components/invoices/EditableNotesCell";

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

// Sortable (drag-to-reorder) table header cell.
function DraggableHeader({
  header,
  children,
}: {
  header: Header<TableRow, unknown>;
  children: React.ReactNode;
}) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: header.column.id });
  return (
    <th
      ref={setNodeRef}
      style={{
        transform: CSS.Translate.toString(transform),
        transition,
        opacity: isDragging ? 0.6 : 1,
      }}
      className="px-3 py-2.5 text-left"
    >
      <span className="flex items-center gap-1">
        <button
          {...attributes}
          {...listeners}
          className="cursor-grab text-slate-600 hover:text-slate-300 active:cursor-grabbing"
          title="Перетащить столбец"
          aria-label="Перетащить столбец"
        >
          <svg className="h-3 w-3" fill="currentColor" viewBox="0 0 20 20">
            <path d="M7 4a1 1 0 110 2 1 1 0 010-2zM7 9a1 1 0 110 2 1 1 0 010-2zM7 14a1 1 0 110 2 1 1 0 010-2zM13 4a1 1 0 110 2 1 1 0 010-2zM13 9a1 1 0 110 2 1 1 0 010-2zM13 14a1 1 0 110 2 1 1 0 010-2z" />
          </svg>
        </button>
        <span
          onClick={header.column.getToggleSortingHandler()}
          className={
            header.column.getCanSort()
              ? "cursor-pointer select-none hover:text-slate-200"
              : ""
          }
        >
          {children}
          {header.column.getIsSorted() === "asc" && " ↑"}
          {header.column.getIsSorted() === "desc" && " ↓"}
        </span>
      </span>
    </th>
  );
}

export default function InvoicesPage() {
  const router = useRouter();
  const [rows, setRows] = useState<TableRow[]>([]);
  const [catalog, setCatalog] = useState<TableColumn[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [sorting, setSorting] = useState<SortingState>([]);
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({});
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [customFilters, setCustomFilters] = useState<TableFilter[]>([]);
  const [search, setSearch] = useState("");
  const [views, setViews] = useState<SavedView[]>([]);
  const [activeView, setActiveView] = useState<string | null>(null);
  const [showViewDialog, setShowViewDialog] = useState(false);
  const [viewName, setViewName] = useState("");
  const [showColumns, setShowColumns] = useState(false);
  const [showFilter, setShowFilter] = useState(false);
  const [askQuery, setAskQuery] = useState("");
  const [deleteDialog, setDeleteDialog] = useState<{ mode: DeleteMode } | null>(
    null,
  );
  const [deleting, setDeleting] = useState(false);

  // Column layout (hybrid: localStorage default + server "Views").
  const [prefs, setPrefs] = useState<ColumnPrefs>({
    order: [],
    visibility: {},
    widths: {},
  });
  const prefsReady = useRef(false);

  const searchTimeout = useRef<ReturnType<typeof setTimeout>>(undefined);
  const limit = 50;

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
  );

  // Combined filters: custom conditions + the quick status chip.
  const filters = useMemo<TableFilter[]>(() => {
    const base = [...customFilters];
    if (statusFilter && !base.some((f) => f.column === "status"))
      base.push({ column: "status", operator: "eq", value: statusFilter });
    return base;
  }, [customFilters, statusFilter]);

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
      // No `columns` passed → backend returns the FULL catalog + all data
      // fields. Visibility/order are applied client-side.
      const data = await tables.query({
        table: "invoices",
        filters,
        sort: apiSort,
        search: search || undefined,
        offset,
        limit,
      });
      setRows(data.rows);
      setCatalog(data.columns);
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

  // Initialise column prefs once the catalog is known.
  useEffect(() => {
    if (prefsReady.current || catalog.length === 0) return;
    const allKeys = catalog.map((c) => c.key);
    setPrefs(reconcilePrefs(loadColumnPrefs(), allKeys));
    prefsReady.current = true;
  }, [catalog]);

  // Persist prefs to localStorage whenever they change (after init).
  useEffect(() => {
    if (prefsReady.current && prefs.order.length) saveColumnPrefs(prefs);
  }, [prefs]);

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

  // Hand the query to the agent «AI-DOCS», scoped to invoices.
  const askAssistant = () => {
    const q = askQuery.trim();
    if (!q) return;
    window.dispatchEvent(
      new CustomEvent("aidocs:ask", { detail: { text: `[Только счета] ${q}` } }),
    );
    setAskQuery("");
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
        const chunks: string[][] = [];
        for (let i = 0; i < selectedIds.length; i += 10)
          chunks.push(selectedIds.slice(i, i + 10));
        for (const chunk of chunks) {
          await Promise.all(
            chunk.map((id) =>
              mutFetch(`${API}/api/invoices/${id}`, { method: "DELETE" }),
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
        await mutFetch(`${API}/api/invoices`, {
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

  // ── Bulk approve / reject ─────────────────────────────────────────────────

  const [bulkAction, setBulkAction] = useState<
    "approving" | "rejecting" | null
  >(null);

  const [similarFor, setSimilarFor] = useState<{
    id: string;
    label: string;
  } | null>(null);
  const [similarResults, setSimilarResults] = useState<
    Array<{ id: string; score: number; snippet?: string | null }>
  >([]);
  const [similarLoading, setSimilarLoading] = useState(false);

  const openSimilar = async (invoiceId: string, label: string) => {
    setSimilarFor({ id: invoiceId, label });
    setSimilarResults([]);
    setSimilarLoading(true);
    try {
      const r = await fetch(
        `${API}/api/search/similar/invoice/${invoiceId}?limit=5`,
      );
      const d = r.ok ? await r.json() : { results: [] };
      setSimilarResults(d.results ?? []);
    } catch {
      setSimilarResults([]);
    } finally {
      setSimilarLoading(false);
    }
  };

  const performBulkStatus = async (action: "approve" | "reject") => {
    if (!selectedIds.length) return;
    setBulkAction(action === "approve" ? "approving" : "rejecting");
    try {
      await mutFetch(`${API}/api/invoices/bulk-${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: selectedIds }),
      });
      setRowSelection({});
      await fetchData();
    } finally {
      setBulkAction(null);
    }
  };

  const updateRowNote = (invoiceId: string, notes: string) => {
    setRows((prev) =>
      prev.map((r) =>
        r.id === invoiceId ? { ...r, data: { ...r.data, notes } } : r,
      ),
    );
  };

  // ── Table columns ──────────────────────────────────────────────────────────

  const checkboxCol: ColumnDef<TableRow> = {
    id: PINNED_LEFT,
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

  const actionsCol: ColumnDef<TableRow> = {
    id: PINNED_RIGHT,
    header: "",
    cell: ({ row }) => (
      <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-all">
        <button
          onClick={(e) => {
            e.stopPropagation();
            const label =
              (row.original.data.invoice_number as string) ??
              row.original.id.slice(0, 8);
            void openSimilar(row.original.id, label);
          }}
          className="p-1 text-slate-500 hover:text-purple-400 transition-colors"
          title="Похожие счета"
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
              d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"
            />
          </svg>
        </button>
        <button
          onClick={(e) => {
            e.stopPropagation();
            setRowSelection({ [row.index]: true });
            setDeleteDialog({ mode: "selected" });
          }}
          className="p-1 text-slate-500 hover:text-red-400 transition-colors"
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
      </div>
    ),
    enableSorting: false,
    size: 64,
  };

  const renderCell = (col: TableColumn, val: unknown, row: TableRow) => {
    if (col.key === "notes") {
      return (
        <EditableNotesCell
          invoiceId={row.id}
          value={(val as string) ?? null}
          onSaved={(next) => updateRowNote(row.id, next)}
        />
      );
    }
    if (col.key === "items_list") {
      return (
        <div className="max-h-32 overflow-y-auto whitespace-pre-line text-xs leading-snug text-slate-300">
          {(val as string) ?? "—"}
        </div>
      );
    }
    if (col.key === "row_no") {
      return <span className="text-slate-500">{val as number}</span>;
    }
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
  };

  const tableColumns = useMemo<ColumnDef<TableRow>[]>(
    () => [
      checkboxCol,
      ...catalog.map((col) => ({
        id: col.key,
        header: col.label,
        accessorFn: (row: TableRow) => row.data[col.key],
        cell: ({
          getValue,
          row: r,
        }: {
          getValue: () => unknown;
          row: { original: TableRow };
        }) => renderCell(col, getValue(), r.original),
        enableSorting: col.sortable,
        size: prefs.widths[col.key],
      })),
      actionsCol,
    ],
    // checkboxCol/actionsCol/renderCell are recreated each render by design.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [catalog, prefs.widths],
  );

  // TanStack ordering/visibility derived from prefs (+ pinned service columns).
  const columnOrder = useMemo<ColumnOrderState>(
    () => [PINNED_LEFT, ...prefs.order, PINNED_RIGHT],
    [prefs.order],
  );
  const columnVisibility = useMemo<VisibilityState>(
    () => ({ ...prefs.visibility }),
    [prefs.visibility],
  );

  const table = useReactTable({
    data: rows,
    columns: tableColumns,
    state: { sorting, rowSelection, columnOrder, columnVisibility },
    onSortingChange: setSorting,
    onRowSelectionChange: setRowSelection,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    manualSorting: true,
    enableRowSelection: true,
  });

  const handleHeaderDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const a = active.id as string;
    const b = over.id as string;
    if (a === PINNED_LEFT || a === PINNED_RIGHT) return;
    setPrefs((p) => {
      const oldIndex = p.order.indexOf(a);
      const newIndex = p.order.indexOf(b);
      if (oldIndex < 0 || newIndex < 0) return p;
      return { ...p, order: arrayMove(p.order, oldIndex, newIndex) };
    });
    setActiveView(null);
  };

  // ── Export ─────────────────────────────────────────────────────────────────

  const exportColumns = useMemo(() => visibleOrderedKeys(prefs), [prefs]);

  const handleExport = async (format: string) => {
    const resp = await tables.exportUrl({
      table: "invoices",
      filters,
      sort: apiSort,
      columns: exportColumns,
      format,
    });
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
      columns: visibleOrderedKeys(prefs),
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
    setCustomFilters(view.filters.filter((f) => f.column !== "status"));
    if (view.sort.length > 0)
      setSorting(
        view.sort.map((s) => ({ id: s.column, desc: s.direction === "desc" })),
      );
    // Restore column layout (visible set + order) from the saved view.
    if (view.columns && view.columns.length) {
      setPrefs((p) => {
        const allKeys = catalog.map((c) => c.key);
        const order = [
          ...view.columns!.filter((k) => allKeys.includes(k)),
          ...allKeys.filter((k) => !view.columns!.includes(k)),
        ];
        const visibility: Record<string, boolean> = {};
        for (const k of allKeys) visibility[k] = view.columns!.includes(k);
        return { ...p, order, visibility };
      });
    }
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
  // Sortable header ids — visible data columns only (pinned cols excluded).
  const dataColumnIds = useMemo(() => visibleOrderedKeys(prefs), [prefs]);

  return (
    <div
      className="p-6 max-w-7xl mx-auto"
      onClick={() => {
        setShowColumns(false);
        setShowFilter(false);
      }}
    >
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
        <div className="flex gap-1.5 mb-3 flex-wrap items-center">
          <span className="text-xs text-slate-400 self-center mr-1">Виды:</span>
          {views.map((v) => (
            <div
              key={v.id}
              className={`flex items-center gap-0.5 rounded-full border text-xs ${activeView === v.id ? "bg-blue-900/40 border-blue-600 text-blue-400" : "bg-slate-700 border-slate-600 text-slate-300"}`}
            >
              <button
                onClick={() => applyView(v)}
                className="px-2.5 py-0.5 hover:opacity-80"
              >
                {v.name}
              </button>
              <button
                onClick={async (e) => {
                  e.stopPropagation();
                  await tables.deleteView(v.id);
                  setViews((prev) => prev.filter((x) => x.id !== v.id));
                  if (activeView === v.id) setActiveView(null);
                }}
                className="pr-2 text-slate-500 hover:text-red-400"
                title="Удалить вид"
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Ask the assistant (agent, scoped to invoices) */}
      <div className="flex items-center gap-2 mb-3">
        <input
          type="text"
          value={askQuery}
          onChange={(e) => setAskQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && askAssistant()}
          placeholder="Спросить AI-DOCS про счета: «сравни цены поставщиков за апрель»..."
          className="flex-1 px-3 py-1.5 text-sm bg-slate-800/60 border border-slate-700 text-slate-300 placeholder-slate-600 rounded outline-none focus:border-purple-500"
        />
        <button
          onClick={askAssistant}
          disabled={!askQuery.trim()}
          className="px-3 py-1.5 text-xs bg-purple-700 text-white rounded hover:bg-purple-600 disabled:opacity-40 flex items-center gap-1"
        >
          <svg className="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 20 20">
            <path d="M10 2l1.5 4.5L16 8l-4.5 1.5L10 14l-1.5-4.5L4 8l4.5-1.5L10 2z" />
          </svg>
          Спросить AI-DOCS
        </button>
      </div>

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
          placeholder="Поиск по ключевым словам..."
          onChange={(e) => handleSearchChange(e.target.value)}
          className="px-3 py-1.5 text-sm bg-slate-800 border border-slate-600 text-slate-200 placeholder-slate-500 rounded w-56 outline-none focus:border-blue-400"
        />

        {/* Filter builder */}
        <div className="relative" onClick={(e) => e.stopPropagation()}>
          <button
            onClick={() => {
              setShowFilter((s) => !s);
              setShowColumns(false);
            }}
            className={`px-2.5 py-1.5 text-xs rounded border ${
              customFilters.length
                ? "border-blue-600 bg-blue-900/30 text-blue-300"
                : "border-slate-600 text-slate-400 hover:bg-slate-700"
            }`}
          >
            Фильтр{customFilters.length ? ` (${customFilters.length})` : ""}
          </button>
          {showFilter && (
            <FilterBuilder
              catalog={catalog}
              filters={customFilters}
              onChange={(f) => {
                setCustomFilters(f);
                setOffset(0);
                setActiveView(null);
              }}
              onClose={() => setShowFilter(false)}
            />
          )}
        </div>

        {/* Column manager */}
        <div className="relative" onClick={(e) => e.stopPropagation()}>
          <button
            onClick={() => {
              setShowColumns((s) => !s);
              setShowFilter(false);
            }}
            className="px-2.5 py-1.5 text-xs text-slate-400 border border-slate-600 rounded hover:bg-slate-700"
          >
            ⚙ Столбцы
          </button>
          {showColumns && (
            <ColumnManager
              catalog={catalog}
              order={prefs.order}
              visibility={prefs.visibility}
              onChange={(order, visibility) => {
                setPrefs((p) => ({ ...p, order, visibility }));
                setActiveView(null);
              }}
              onReset={() => setPrefs(defaultPrefs(catalog.map((c) => c.key)))}
              onClose={() => setShowColumns(false)}
            />
          )}
        </div>

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
            onClick={() => void performBulkStatus("approve")}
            disabled={!!bulkAction}
            className="px-3 py-1 text-xs bg-green-800 hover:bg-green-700 disabled:opacity-50 text-white rounded"
          >
            {bulkAction === "approving" ? "…" : "✓ Утвердить"}
          </button>
          <button
            onClick={() => void performBulkStatus("reject")}
            disabled={!!bulkAction}
            className="px-3 py-1 text-xs bg-amber-800 hover:bg-amber-700 disabled:opacity-50 text-white rounded"
          >
            {bulkAction === "rejecting" ? "…" : "✕ Отклонить"}
          </button>
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
          <div className="bg-slate-800 border border-slate-700 rounded-lg overflow-x-auto">
            <DndContext
              sensors={sensors}
              collisionDetection={closestCenter}
              onDragEnd={handleHeaderDragEnd}
            >
              <table className="w-full text-sm">
                <thead className="bg-slate-700/50 text-slate-400 text-xs uppercase">
                  {table.getHeaderGroups().map((hg) => (
                    <tr key={hg.id}>
                      <SortableContext
                        items={dataColumnIds}
                        strategy={horizontalListSortingStrategy}
                      >
                        {hg.headers.map((header) => {
                          const id = header.column.id;
                          if (id === PINNED_LEFT || id === PINNED_RIGHT) {
                            return (
                              <th
                                key={header.id}
                                className="px-3 py-2.5 text-left"
                              >
                                {flexRender(
                                  header.column.columnDef.header,
                                  header.getContext(),
                                )}
                              </th>
                            );
                          }
                          return (
                            <DraggableHeader key={header.id} header={header}>
                              {flexRender(
                                header.column.columnDef.header,
                                header.getContext(),
                              )}
                            </DraggableHeader>
                          );
                        })}
                      </SortableContext>
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
                        <td
                          key={cell.id}
                          className="px-3 py-2.5 text-slate-200 align-top"
                        >
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
            </DndContext>
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

      {/* Similar invoices drawer */}
      {similarFor && (
        <div className="fixed inset-0 z-40" onClick={() => setSimilarFor(null)}>
          <div
            className="absolute right-0 top-0 h-full w-80 bg-slate-900 border-l border-slate-700 shadow-2xl flex flex-col"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between px-4 py-3 border-b border-slate-700">
              <div>
                <div className="text-xs text-slate-400 uppercase tracking-wide">
                  Похожие счета
                </div>
                <div className="text-sm font-medium text-slate-200 mt-0.5 truncate max-w-[200px]">
                  {similarFor.label}
                </div>
              </div>
              <button
                onClick={() => setSimilarFor(null)}
                className="text-slate-500 hover:text-slate-200 text-lg leading-none"
              >
                ×
              </button>
            </div>
            <div className="flex-1 overflow-y-auto p-4">
              {similarLoading ? (
                <div className="text-slate-500 text-sm text-center py-8">
                  Поиск...
                </div>
              ) : similarResults.length === 0 ? (
                <div className="text-slate-500 text-sm text-center py-8">
                  Похожих счетов не найдено
                </div>
              ) : (
                <ul className="space-y-2">
                  {similarResults.map((s) => (
                    <li key={s.id}>
                      <button
                        onClick={() => {
                          setSimilarFor(null);
                          router.push(`/invoices?highlight=${s.id}`);
                        }}
                        className="w-full text-left p-3 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded-lg transition-colors group"
                      >
                        <div className="flex items-center justify-between gap-2 mb-1">
                          <span className="text-xs text-slate-400 font-mono">
                            {s.id.slice(0, 12)}…
                          </span>
                          <span className="text-xs text-purple-400 font-medium">
                            {(s.score * 100).toFixed(0)}%
                          </span>
                        </div>
                        {s.snippet && (
                          <p className="text-sm text-slate-300 group-hover:text-white line-clamp-2">
                            {s.snippet}
                          </p>
                        )}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
