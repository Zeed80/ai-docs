"use client";

import {
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  SortingState,
  useReactTable,
} from "@tanstack/react-table";
import { useMemo, useState } from "react";
import type { CanvasColumn } from "@/lib/canvas-context";

interface CanvasTableProps {
  columns: CanvasColumn[];
  rows: Record<string, unknown>[];
  title?: string;
}

function exportCsv(
  columns: CanvasColumn[],
  rows: Record<string, unknown>[],
  filename: string,
) {
  const header = columns.map((c) => `"${c.header}"`).join(",");
  const body = rows
    .map((row) =>
      columns
        .map((c) => {
          const v = row[c.key];
          const s = v == null ? "" : String(v);
          return `"${s.replace(/"/g, '""')}"`;
        })
        .join(","),
    )
    .join("\n");
  const blob = new Blob(["﻿" + header + "\n" + body], {
    type: "text/csv;charset=utf-8;",
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename + ".csv";
  a.click();
  URL.revokeObjectURL(url);
}

function exportXlsx(
  columns: CanvasColumn[],
  rows: Record<string, unknown>[],
  filename: string,
) {
  import("xlsx").then((XLSX) => {
    const data = [
      columns.map((c) => c.header),
      ...rows.map((row) => columns.map((c) => row[c.key] ?? "")),
    ];
    const ws = XLSX.utils.aoa_to_sheet(data);
    // Bold header row
    const range = XLSX.utils.decode_range(ws["!ref"] || "A1");
    for (let col = range.s.c; col <= range.e.c; col++) {
      const cell = ws[XLSX.utils.encode_cell({ r: 0, c: col })];
      if (cell) cell.s = { font: { bold: true } };
    }
    // Auto-width columns
    ws["!cols"] = columns.map((c) => ({
      wch: Math.max(c.header.length + 2, 10),
    }));
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, "Данные");
    XLSX.writeFile(wb, filename + ".xlsx");
  });
}

function copyTsv(columns: CanvasColumn[], rows: Record<string, unknown>[]) {
  const header = columns.map((c) => c.header).join("\t");
  const body = rows
    .map((row) => columns.map((c) => row[c.key] ?? "").join("\t"))
    .join("\n");
  navigator.clipboard.writeText(header + "\n" + body).catch(() => {});
}

function getAction(value: unknown): {
  href?: string;
  label?: string;
  confirm?: string;
  method?: string;
} {
  if (typeof value === "string") return { href: value };
  if (!value || typeof value !== "object") return {};
  const record = value as Record<string, unknown>;
  return {
    href: typeof record.href === "string" ? record.href : undefined,
    label: typeof record.label === "string" ? record.label : undefined,
    confirm: typeof record.confirm === "string" ? record.confirm : undefined,
    method: typeof record.method === "string" ? record.method : undefined,
  };
}

function ActionCell({
  value,
  type,
}: {
  value: unknown;
  type: CanvasColumn["type"];
}) {
  const [status, setStatus] = useState<"idle" | "pending" | "done" | "error">(
    "idle",
  );
  const action = getAction(value);
  if (!action.href) return <span className="text-slate-500">—</span>;

  if (type === "delete") {
    async function runDelete() {
      if (action.confirm && !window.confirm(action.confirm)) return;
      setStatus("pending");
      try {
        const res = await fetch(action.href!, {
          method: action.method || "DELETE",
        });
        setStatus(res.ok ? "done" : "error");
      } catch {
        setStatus("error");
      }
    }

    return (
      <button
        onClick={runDelete}
        disabled={status === "pending" || status === "done"}
        className="text-red-300 hover:text-red-200 disabled:text-slate-500 underline"
      >
        {status === "pending"
          ? "Удаляю..."
          : status === "done"
            ? "Удалено"
            : status === "error"
              ? "Ошибка"
              : action.label || "Удалить"}
      </button>
    );
  }

  return (
    <a
      href={action.href}
      download={type === "download" ? true : undefined}
      target={type === "link" ? "_blank" : undefined}
      rel={type === "link" ? "noopener" : undefined}
      className="text-blue-300 hover:text-blue-200 underline"
    >
      {action.label || (type === "download" ? "Скачать" : action.href)}
    </a>
  );
}

export function CanvasTable({ columns, rows, title }: CanvasTableProps) {
  const [sorting, setSorting] = useState<SortingState>([]);
  const [globalFilter, setGlobalFilter] = useState("");

  const tableColumns = useMemo(
    () =>
      columns.map((col) => ({
        id: col.key,
        accessorKey: col.key,
        header: col.header,
        size: col.width,
        cell: (info: { getValue: () => unknown }) => {
          const v = info.getValue();
          if (["link", "download", "delete"].includes(col.type || "")) {
            return <ActionCell value={v} type={col.type} />;
          }
          if (v == null) return <span className="text-slate-500">—</span>;
          return <span>{String(v)}</span>;
        },
      })),
    [columns],
  );

  const table = useReactTable({
    data: rows,
    columns: tableColumns,
    state: { sorting, globalFilter },
    onSortingChange: setSorting,
    onGlobalFilterChange: setGlobalFilter,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
  });

  const filename = title || "таблица";

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <input
          value={globalFilter}
          onChange={(e) => setGlobalFilter(e.target.value)}
          placeholder="Фильтр..."
          className="flex-1 bg-slate-800 border border-slate-600 rounded px-2 py-1 text-xs text-slate-200 placeholder-slate-500 focus:outline-none focus:border-blue-500"
        />
        <button
          onClick={() => copyTsv(columns, rows)}
          title="Копировать как таблицу"
          className="px-2 py-1 text-xs bg-slate-700 hover:bg-slate-600 rounded text-slate-300"
        >
          Копировать
        </button>
        <button
          onClick={() => exportCsv(columns, rows, filename)}
          className="px-2 py-1 text-xs bg-slate-700 hover:bg-slate-600 rounded text-slate-300"
        >
          CSV
        </button>
        <button
          onClick={() => exportXlsx(columns, rows, filename)}
          className="px-2 py-1 text-xs bg-emerald-700 hover:bg-emerald-600 rounded text-white"
        >
          Excel
        </button>
      </div>

      <div className="overflow-auto rounded border border-slate-700 max-h-[60vh]">
        <table className="w-full text-xs text-left border-collapse">
          <thead className="sticky top-0 bg-slate-800 z-10">
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id}>
                {hg.headers.map((h) => (
                  <th
                    key={h.id}
                    onClick={h.column.getToggleSortingHandler()}
                    className="px-3 py-2 font-semibold text-slate-300 border-b border-slate-700 select-none cursor-pointer hover:bg-slate-700 whitespace-nowrap"
                    style={{ width: h.column.columnDef.size }}
                  >
                    {flexRender(h.column.columnDef.header, h.getContext())}
                    {h.column.getIsSorted() === "asc" && (
                      <span className="ml-1 opacity-60">↑</span>
                    )}
                    {h.column.getIsSorted() === "desc" && (
                      <span className="ml-1 opacity-60">↓</span>
                    )}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map((row, idx) => (
              <tr
                key={row.id}
                className={
                  idx % 2 === 0
                    ? "bg-slate-900 hover:bg-slate-800"
                    : "bg-slate-850 hover:bg-slate-800"
                }
              >
                {row.getVisibleCells().map((cell) => (
                  <td
                    key={cell.id}
                    className="px-3 py-1.5 border-b border-slate-800 text-slate-200 align-top"
                  >
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        {table.getRowModel().rows.length === 0 && (
          <div className="text-center py-6 text-slate-500 text-xs">
            Нет данных
          </div>
        )}
      </div>

      <div className="text-xs text-slate-500">
        {table.getFilteredRowModel().rows.length} из {rows.length} строк
      </div>
    </div>
  );
}
