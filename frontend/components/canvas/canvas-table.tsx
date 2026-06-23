"use client";

// Thin wrapper over the shared DataGrid — keeps the canvas toolbar
// (filter / copy / CSV / Excel export) while delegating the grid itself to the
// unified component so spec-tables get resize, formatting and (later) editing
// for free.

import { useState } from "react";
import { DataGrid } from "@/components/grid/DataGrid";
import { displayValue } from "@/components/grid/format";
import type { GridColumn } from "@/components/grid/types";
import { getApiBaseUrl } from "@/lib/api-base";
import { mutFetch } from "@/lib/auth";
import type { CanvasColumn } from "@/lib/canvas-context";

const API = getApiBaseUrl();

interface CanvasTableProps {
  columns: CanvasColumn[];
  rows: Record<string, unknown>[];
  title?: string;
  fill?: boolean;
  blockId?: string;
}

function toGridColumns(columns: CanvasColumn[]): GridColumn[] {
  return columns.map((c) => ({
    key: c.key,
    header: c.header,
    type: c.type,
    width: c.width,
    editable: c.editable,
  }));
}

function exportCsv(
  columns: GridColumn[],
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

function copyTsv(columns: GridColumn[], rows: Record<string, unknown>[]) {
  const header = columns.map((c) => c.header).join("\t");
  const body = rows
    .map((row) => columns.map((c) => displayValue(row[c.key], c)).join("\t"))
    .join("\n");
  navigator.clipboard.writeText(header + "\n" + body).catch(() => {});
}

async function exportWorkspaceBlock(blockId: string, format: "xlsx" | "csv") {
  const res = await mutFetch(
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
  const [globalFilter, setGlobalFilter] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const gridColumns = toGridColumns(columns);
  const filename = title || "таблица";

  // Editable spec-tables: a committed cell files an approval-gated edit.
  const hasEditable = Boolean(blockId) && gridColumns.some((c) => c.editable);
  const onCellCommit = hasEditable
    ? async (edit: {
        field: string;
        value: unknown;
        rowPk?: string | number;
      }) => {
        if (edit.rowPk == null) {
          setNotice("Не удалось определить строку для записи.");
          throw new Error("no row pk");
        }
        const res = await mutFetch(
          `${API}/api/workspace/agent/spec-table/cell-edit`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              canvas_id: blockId,
              row_pk: String(edit.rowPk),
              field: edit.field,
              value: edit.value,
            }),
          },
        );
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data?.status === "error") {
          setNotice(data?.message || "Ошибка записи правки.");
          throw new Error(data?.message || "cell edit failed");
        }
        setNotice(data?.message || "Правка отправлена на подтверждение.");
      }
    : undefined;

  const filteredCount = globalFilter
    ? rows.filter((row) =>
        gridColumns.some((c) =>
          String(row[c.key] ?? "")
            .toLowerCase()
            .includes(globalFilter.toLowerCase()),
        ),
      ).length
    : rows.length;

  return (
    <div className={`space-y-2 ${fill ? "flex h-full min-h-0 flex-col" : ""}`}>
      <div className="flex items-center gap-2">
        <input
          value={globalFilter}
          onChange={(e) => setGlobalFilter(e.target.value)}
          placeholder="Фильтр..."
          className="flex-1 rounded border border-slate-600 bg-slate-800 px-2 py-1 text-xs text-slate-200 placeholder-slate-500 focus:border-blue-500 focus:outline-none"
        />
        <button
          onClick={() => copyTsv(gridColumns, rows)}
          title="Копировать как таблицу"
          className="rounded bg-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-600"
        >
          Копировать
        </button>
        <button
          onClick={() =>
            blockId
              ? exportWorkspaceBlock(blockId, "csv")
              : exportCsv(gridColumns, rows, filename)
          }
          className="rounded bg-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-600"
        >
          CSV
        </button>
        <button
          onClick={() => blockId && exportWorkspaceBlock(blockId, "xlsx")}
          disabled={!blockId}
          className="rounded bg-emerald-700 px-2 py-1 text-xs text-white hover:bg-emerald-600"
        >
          Excel
        </button>
      </div>

      <DataGrid
        columns={gridColumns}
        rows={rows}
        storageKey={null}
        globalFilter={globalFilter}
        fill={fill}
        spreadsheetMode
        onCellCommit={onCellCommit}
        getRowPk={(row) => (row.__pk as string | undefined) ?? undefined}
      />

      {notice && (
        <div className="rounded bg-slate-800 px-2 py-1 text-xs text-amber-300">
          {notice}
        </div>
      )}

      <div className="text-xs text-slate-500">
        {filteredCount} из {rows.length} строк
      </div>
    </div>
  );
}
