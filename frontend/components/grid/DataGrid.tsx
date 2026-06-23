"use client";

// DataGrid — the single Excel-like grid every table consumer shares
// (workspace spec-tables, ad-hoc sheets, agent output; the invoices page can
// migrate onto it later). Built on @tanstack/react-table (already a dep):
// sticky header, zebra, column resize, optional dnd reorder, inline editing,
// link/download/delete action cells, money/number/date formatting.

import {
  closestCenter,
  DndContext,
  type DragEndEvent,
  PointerSensor,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import {
  arrayMove,
  horizontalListSortingStrategy,
  SortableContext,
  useSortable,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import {
  type ColumnDef,
  type ColumnSizingState,
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  type Header,
  type SortingState,
  useReactTable,
} from "@tanstack/react-table";
import { useMemo, useState } from "react";
import { ActionCell } from "./ActionCell";
import { displayValue, isNumericColumn } from "./format";
import { EditableCell } from "./EditableCell";
import {
  FALLBACK_GRID_WIDTH,
  type GridCellEdit,
  type GridColumn,
  type GridRow,
  MIN_GRID_COLUMN_WIDTH,
} from "./types";
import { useGridPrefs, visibleOrderedKeys } from "./useGridPrefs";

const ACTION_TYPES = new Set(["link", "download", "delete"]);

const GUTTER_WIDTH = 44;

// Excel column label by position: A, B, … Z, AA, AB, … (matches the formula
// engine, which addresses columns by their position letter).
function colLetter(index: number): string {
  let n = index;
  let label = "";
  do {
    label = String.fromCharCode(65 + (n % 26)) + label;
    n = Math.floor(n / 26) - 1;
  } while (n >= 0);
  return label;
}

export interface DataGridProps {
  columns: GridColumn[];
  rows: GridRow[];
  /** localStorage key for column order/visibility/widths. null = ephemeral. */
  storageKey?: string | null;
  /** Default visible column keys (others surface hidden in the catalog). */
  defaultVisible?: string[];
  /** Enable drag-to-reorder headers (needs a non-null storageKey to persist). */
  enableReorder?: boolean;
  enableResize?: boolean;
  enableSort?: boolean;
  /** External (server) sorting — when provided the grid won't sort client-side. */
  sorting?: SortingState;
  onSortingChange?: (s: SortingState) => void;
  /** Client-side quick filter value (controlled by parent toolbar). */
  globalFilter?: string;
  /** Commit an edited cell. Presence + column.editable enables inline editing. */
  onCellCommit?: (edit: GridCellEdit) => void | Promise<void>;
  /** Stable per-row primary key for writeback edits. */
  getRowPk?: (row: GridRow, index: number) => string | number | undefined;
  /** Seed value shown while editing a cell (e.g. raw formula vs computed value). */
  getEditValue?: (rowIndex: number, key: string) => unknown;
  /** Excel chrome: column-letter row (A,B,C) + left row-number gutter (1,2,3). */
  spreadsheetMode?: boolean;
  /** Rename a column header (double-click the header to edit). */
  onRenameColumn?: (key: string, header: string) => void | Promise<void>;
  fill?: boolean;
}

function SortableHeader({
  header,
  enableReorder,
  enableResize,
  enableSort,
  onRename,
}: {
  header: Header<GridRow, unknown>;
  enableReorder: boolean;
  enableResize: boolean;
  enableSort: boolean;
  onRename?: (header: string) => void | Promise<void>;
}) {
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState("");
  const { attributes, listeners, setNodeRef, transform, isDragging } =
    useSortable({ id: header.column.id, disabled: !enableReorder });

  const sortDir = header.column.getIsSorted();
  return (
    <th
      ref={setNodeRef}
      colSpan={header.colSpan}
      style={{
        width: header.getSize(),
        transform: CSS.Translate.toString(transform),
        opacity: isDragging ? 0.6 : 1,
      }}
      className="relative select-none whitespace-nowrap border border-slate-600 bg-slate-800 px-3 py-2 text-left font-semibold text-slate-100"
    >
      <div className="flex items-center gap-1">
        {renaming ? (
          <input
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onClick={(e) => e.stopPropagation()}
            onBlur={() => {
              setRenaming(false);
              const v = draft.trim();
              if (v && v !== String(header.column.columnDef.header ?? "")) {
                void onRename?.(v);
              }
            }}
            onKeyDown={(e) => {
              e.stopPropagation();
              if (e.key === "Enter") {
                e.preventDefault();
                (e.target as HTMLInputElement).blur();
              } else if (e.key === "Escape") {
                setRenaming(false);
              }
            }}
            className="w-full min-w-[60px] rounded border border-blue-500 bg-slate-900 px-1 py-0.5 text-xs font-normal text-slate-100 outline-none"
          />
        ) : (
          <span
            {...(enableReorder ? { ...attributes, ...listeners } : {})}
            onClick={
              enableSort ? header.column.getToggleSortingHandler() : undefined
            }
            onDoubleClick={
              onRename
                ? (e) => {
                    e.stopPropagation();
                    setDraft(String(header.column.columnDef.header ?? ""));
                    setRenaming(true);
                  }
                : undefined
            }
            className={`flex-1 truncate ${
              enableSort ? "cursor-pointer hover:text-white" : ""
            } ${enableReorder ? "cursor-grab active:cursor-grabbing" : ""}`}
            title={
              onRename
                ? "Двойной клик — переименовать"
                : String(header.column.columnDef.header ?? "")
            }
          >
            {flexRender(header.column.columnDef.header, header.getContext())}
            {sortDir === "asc" && <span className="ml-1 opacity-60">↑</span>}
            {sortDir === "desc" && <span className="ml-1 opacity-60">↓</span>}
          </span>
        )}
      </div>
      {enableResize && header.column.getCanResize() && (
        <span
          onMouseDown={header.getResizeHandler()}
          onTouchStart={header.getResizeHandler()}
          onClick={(e) => e.stopPropagation()}
          className="absolute right-0 top-0 h-full w-1 cursor-col-resize select-none touch-none bg-slate-600/0 hover:bg-blue-500/60"
        />
      )}
    </th>
  );
}

export function DataGrid({
  columns,
  rows,
  storageKey = null,
  defaultVisible,
  enableReorder = false,
  enableResize = true,
  enableSort = true,
  sorting: externalSorting,
  onSortingChange,
  globalFilter,
  onCellCommit,
  getRowPk,
  getEditValue,
  spreadsheetMode = false,
  onRenameColumn,
  fill = false,
}: DataGridProps) {
  const allKeys = useMemo(() => columns.map((c) => c.key), [columns]);
  const { prefs, setPrefs } = useGridPrefs(storageKey, allKeys, defaultVisible);
  const colByKey = useMemo(
    () => new Map(columns.map((c) => [c.key, c])),
    [columns],
  );

  const [internalSorting, setInternalSorting] = useState<SortingState>([]);
  const sorting = externalSorting ?? internalSorting;
  const setSorting = onSortingChange ?? setInternalSorting;
  // Live resize state; persisted widths apply through columnDef.size below.
  const [sizing, setSizing] = useState<ColumnSizingState>({});

  const orderedVisible = useMemo(
    () => visibleOrderedKeys(prefs).filter((k) => colByKey.has(k)),
    [prefs, colByKey],
  );

  const tableColumns = useMemo<ColumnDef<GridRow>[]>(
    () =>
      orderedVisible.map((key) => {
        const col = colByKey.get(key)!;
        const numeric = isNumericColumn(col);
        return {
          id: key,
          accessorKey: key,
          header: col.header,
          size: col.width ?? prefs.widths[key] ?? FALLBACK_GRID_WIDTH,
          minSize: MIN_GRID_COLUMN_WIDTH,
          enableSorting: enableSort && !ACTION_TYPES.has(col.type ?? ""),
          enableResizing: enableResize,
          cell: (info) => {
            const v = info.getValue();
            if (ACTION_TYPES.has(col.type ?? "")) {
              return <ActionCell value={v} type={col.type} />;
            }
            const editable = Boolean(onCellCommit && col.editable);
            if (editable) {
              return (
                <EditableCell
                  value={v}
                  editValue={getEditValue?.(info.row.index, key)}
                  type={col.type}
                  options={col.options}
                  display={(val) => displayValue(val, col)}
                  onCommit={async (next) => {
                    await onCellCommit!({
                      rowIndex: info.row.index,
                      rowPk: getRowPk?.(info.row.original, info.row.index),
                      field: key,
                      value: next,
                      previous: v,
                    });
                  }}
                />
              );
            }
            if (v == null) return <span className="text-slate-500">—</span>;
            return (
              <span
                className={`whitespace-pre-line ${
                  numeric ? "tabular-nums text-slate-100" : ""
                }`}
              >
                {displayValue(v, col)}
              </span>
            );
          },
        };
      }),
    [
      orderedVisible,
      colByKey,
      prefs.widths,
      enableSort,
      enableResize,
      onCellCommit,
      getRowPk,
      getEditValue,
    ],
  );

  const table = useReactTable({
    data: rows,
    columns: tableColumns,
    state: { sorting, globalFilter, columnSizing: sizing },
    manualSorting: Boolean(externalSorting),
    onSortingChange: (updater) => {
      const next = typeof updater === "function" ? updater(sorting) : updater;
      setSorting(next);
    },
    onColumnSizingChange: (updater) => {
      setSizing((prev) => {
        const next = typeof updater === "function" ? updater(prev) : updater;
        if (storageKey) {
          setPrefs((p) => ({ ...p, widths: { ...p.widths, ...next } }));
        }
        return next;
      });
    },
    columnResizeMode: "onChange",
    enableColumnResizing: enableResize,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
  });

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
  );

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    setPrefs((p) => {
      const oldIndex = p.order.indexOf(String(active.id));
      const newIndex = p.order.indexOf(String(over.id));
      if (oldIndex < 0 || newIndex < 0) return p;
      return { ...p, order: arrayMove(p.order, oldIndex, newIndex) };
    });
  };

  const headerGroup = table.getHeaderGroups()[0];

  const totalWidth = enableResize
    ? table.getTotalSize() + (spreadsheetMode ? GUTTER_WIDTH : 0)
    : undefined;

  const cornerCls =
    "sticky left-0 z-20 border border-slate-600 bg-slate-800 text-center text-slate-400";

  const tableEl = (
    <table
      className="border-collapse text-left text-xs"
      style={{
        width: totalWidth ?? "100%",
        tableLayout: enableResize ? "fixed" : "auto",
      }}
    >
      <thead className="sticky top-0 z-10 bg-slate-800">
        {spreadsheetMode && (
          <tr>
            <th
              className={`${cornerCls} w-[44px]`}
              style={{ width: GUTTER_WIDTH, minWidth: GUTTER_WIDTH }}
            />
            {headerGroup.headers.map((header, i) => (
              <th
                key={`L-${header.id}`}
                style={{ width: header.getSize() }}
                className="select-none border border-slate-600 bg-slate-800 px-1 py-0.5 text-center text-[10px] font-mono font-normal text-slate-400"
                title={String(header.column.columnDef.header ?? "")}
              >
                {colLetter(i)}
              </th>
            ))}
          </tr>
        )}
        <tr>
          {spreadsheetMode && (
            <th
              className={`${cornerCls} font-semibold`}
              style={{ width: GUTTER_WIDTH, minWidth: GUTTER_WIDTH }}
            />
          )}
          {headerGroup.headers.map((header) => (
            <SortableHeader
              key={header.id}
              header={header}
              enableReorder={enableReorder}
              enableResize={enableResize}
              enableSort={enableSort}
              onRename={
                onRenameColumn
                  ? (h) => onRenameColumn(header.column.id, h)
                  : undefined
              }
            />
          ))}
        </tr>
      </thead>
      <tbody>
        {table.getRowModel().rows.map((row, idx) => (
          <tr
            key={row.id}
            className={
              idx % 2 === 0
                ? "bg-slate-950 hover:bg-slate-900"
                : "bg-slate-900 hover:bg-slate-800"
            }
          >
            {spreadsheetMode && (
              <td
                className="sticky left-0 z-10 border border-slate-700 bg-slate-800 px-1 py-1.5 text-center text-[10px] font-mono text-slate-400"
                style={{ width: GUTTER_WIDTH, minWidth: GUTTER_WIDTH }}
              >
                {idx + 1}
              </td>
            )}
            {row.getVisibleCells().map((cell) => (
              <td
                key={cell.id}
                style={{ width: cell.column.getSize() }}
                className="overflow-hidden border border-slate-700 px-3 py-1.5 align-top text-slate-200"
              >
                {flexRender(cell.column.columnDef.cell, cell.getContext())}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );

  return (
    <div
      className={`overflow-auto rounded-md border border-slate-700 bg-slate-950 ${
        fill ? "min-h-0 flex-1" : "max-h-[60vh]"
      }`}
    >
      {enableReorder ? (
        <DndContext
          sensors={sensors}
          collisionDetection={closestCenter}
          onDragEnd={handleDragEnd}
        >
          <SortableContext
            items={orderedVisible}
            strategy={horizontalListSortingStrategy}
          >
            {tableEl}
          </SortableContext>
        </DndContext>
      ) : (
        tableEl
      )}
      {table.getRowModel().rows.length === 0 && (
        <div className="py-6 text-center text-xs text-slate-500">
          Нет данных
        </div>
      )}
    </div>
  );
}
