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
import { getApiBaseUrl } from "@/lib/api-base";

const API = getApiBaseUrl();

interface CanvasTableProps {
  columns: CanvasColumn[];
  rows: Record<string, unknown>[];
  title?: string;
  fill?: boolean;
  blockId?: string;
}

function isMoneyKey(key: string) {
  const normalized = key.toLowerCase();
  return (
    normalized === "amount" ||
    normalized === "total_amount" ||
    normalized === "subtotal" ||
    normalized === "subtotal_amount" ||
    normalized === "tax_amount" ||
    normalized === "unit_price" ||
    normalized === "paid_amount" ||
    normalized.endsWith("_amount") ||
    normalized.endsWith("_price")
  );
}

function formatNumberValue(value: unknown, fractionDigits = 4) {
  if (value == null || value === "") return "—";
  const text = String(value).replace(/\s/g, "").replace(",", ".");
  const number = Number(text);
  if (!Number.isFinite(number)) return String(value);
  return new Intl.NumberFormat("ru-RU", {
    minimumFractionDigits: 0,
    maximumFractionDigits: fractionDigits,
  }).format(number);
}

function formatMoneyValue(value: unknown) {
  if (value == null || value === "") return "—";
  const text = String(value).replace(/\s/g, "").replace(",", ".");
  const number = Number(text);
  if (!Number.isFinite(number)) return String(value);
  return new Intl.NumberFormat("ru-RU", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(number);
}

function displayValue(value: unknown, column: CanvasColumn) {
  if (value == null) return "—";
  if (isMoneyKey(column.key)) return formatMoneyValue(value);
  if (column.type === "number") return formatNumberValue(value);
  return String(value);
}

function exportCsv(
  columns: CanvasColumn[],
  rows: Record<string, unknown>[],
  filename: string,
) {
  const header = columns.map((c) => `"${c.header}"`).join(";");
  const body = rows
    .map((row) =>
      columns
        .map((c) => {
          const s = displayValue(row[c.key], c).replace(/^—$/, "");
          return `"${s.replace(/"/g, '""')}"`;
        })
        .join(";"),
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

function copyTsv(columns: CanvasColumn[], rows: Record<string, unknown>[]) {
  const header = columns.map((c) => c.header).join("\t");
  const body = rows
    .map((row) => columns.map((c) => displayValue(row[c.key], c)).join("\t"))
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

async function exportWorkspaceBlock(blockId: string, format: "xlsx" | "csv") {
  const res = await fetch(
    `${API}/api/workspace/blocks/${encodeURIComponent(blockId)}/export`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ format }),
    },
  );
  if (!res.ok) return;
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const disposition = res.headers.get("content-disposition") || "";
  const utfName = disposition.match(/filename\*=UTF-8''([^;]+)/)?.[1];
  const asciiName = disposition.match(/filename="?([^";]+)"?/)?.[1];
  const filename = utfName
    ? decodeURIComponent(utfName)
    : asciiName || `${blockId}.${format}`;
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export function CanvasTable({
  columns,
  rows,
  title,
  fill = false,
  blockId,
}: CanvasTableProps) {
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
          return (
            <span
              className={`whitespace-pre-line ${
                isMoneyKey(col.key) || col.type === "number"
                  ? "tabular-nums text-slate-100"
                  : ""
              }`}
            >
              {displayValue(v, col)}
            </span>
          );
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
    <div className={`space-y-2 ${fill ? "flex h-full min-h-0 flex-col" : ""}`}>
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
          onClick={() =>
            blockId ? exportWorkspaceBlock(blockId, "csv") : exportCsv(columns, rows, filename)
          }
          className="px-2 py-1 text-xs bg-slate-700 hover:bg-slate-600 rounded text-slate-300"
        >
          CSV
        </button>
        <button
          onClick={() => blockId && exportWorkspaceBlock(blockId, "xlsx")}
          disabled={!blockId}
          className="px-2 py-1 text-xs bg-emerald-700 hover:bg-emerald-600 rounded text-white"
        >
          Excel
        </button>
      </div>

      <div
        className={`overflow-auto rounded-md border border-slate-700 bg-slate-950 ${
          fill ? "min-h-0 flex-1" : "max-h-[60vh]"
        }`}
      >
        <table className="w-full border-collapse text-left text-xs">
          <thead className="sticky top-0 z-10 bg-slate-800">
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id}>
                {hg.headers.map((h) => (
                  <th
                    key={h.id}
                    onClick={h.column.getToggleSortingHandler()}
                    className="cursor-pointer select-none whitespace-nowrap border border-slate-600 px-3 py-2 font-semibold text-slate-100 hover:bg-slate-700"
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
                    ? "bg-slate-950 hover:bg-slate-900"
                    : "bg-slate-900 hover:bg-slate-800"
                }
              >
                {row.getVisibleCells().map((cell) => (
                  <td
                    key={cell.id}
                    className="border border-slate-700 px-3 py-2 align-top text-slate-200"
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
