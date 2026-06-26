"use client";

// Ad-hoc editable spreadsheet ("лист") rendered on the desktop. Reuses the
// shared DataGrid in editable mode; every edit goes to the sheets API, the
// backend re-evaluates formulas and republishes the block. Cells show computed
// values; editing seeds from the raw value so formulas (=A1*B1) stay editable.

import { useState } from "react";
import { DataGrid } from "@/components/grid/DataGrid";
import type { GridColumn } from "@/components/grid/types";
import { getApiBaseUrl } from "@/lib/api-base";
import { mutFetch } from "@/lib/auth";
import type { CanvasBlock, CanvasColumn } from "@/lib/canvas-context";

const API = getApiBaseUrl();

function toGridColumns(columns: CanvasColumn[]): GridColumn[] {
  return columns.map((c) => ({
    key: c.key,
    header: c.header,
    type: c.type,
    width: c.width,
    editable: true,
  }));
}

function refresh() {
  window.dispatchEvent(new Event("workspace-blocks-updated"));
}

function columnKeyByLetter(columns: GridColumn[], letters: string): string | null {
  let idx = 0;
  for (const ch of letters.toUpperCase()) {
    idx = idx * 26 + (ch.charCodeAt(0) - 64);
  }
  idx -= 1;
  return columns[idx]?.key ?? null;
}

function parseCellRef(columns: GridColumn[], value: string) {
  const m = value.trim().match(/^([A-Z]+)([1-9][0-9]*)$/i);
  if (!m) return null;
  const col = columnKeyByLetter(columns, m[1]);
  if (!col) return null;
  return { row: Number(m[2]) - 1, col };
}

export function CanvasSheet({
  block,
  fill = false,
}: {
  block: CanvasBlock;
  fill?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const sheetId = block.sheet_id;
  const columns = toGridColumns(block.columns ?? []);
  const rows = block.rows ?? [];
  const rawRows = block.raw_rows ?? rows;
  const base = `${API}/api/workspace/sheets/${sheetId}`;

  async function call(path: string, body: unknown) {
    setBusy(true);
    try {
      const res = await mutFetch(`${base}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (res.ok) refresh();
      return res.ok;
    } finally {
      setBusy(false);
    }
  }

  async function addColumn() {
    const key = window.prompt("Ключ нового столбца (латиницей, напр. price):");
    if (!key) return;
    const header = window.prompt("Заголовок столбца:", key) || key;
    const formula =
      window.prompt(
        "Формула столбца (опционально, напр. quantity*price):",
        "",
      ) || undefined;
    await call("/add-column", { key, header, formula });
  }

  async function mergeCells() {
    const range = window.prompt("Диапазон для объединения:", "A1:B1");
    const m = range?.match(/^\s*([A-Z]+[1-9][0-9]*)\s*:\s*([A-Z]+[1-9][0-9]*)\s*$/i);
    if (!m) return;
    const start = parseCellRef(columns, m[1]);
    const end = parseCellRef(columns, m[2]);
    if (!start || !end) return;
    await call("/merge-cells", {
      start_row: start.row,
      end_row: end.row,
      start_col: start.col,
      end_col: end.col,
    });
  }

  async function unmergeCells() {
    const cell = window.prompt("Ячейка внутри объединения:", "A1");
    if (!cell) return;
    const ref = parseCellRef(columns, cell);
    if (!ref) return;
    await call("/unmerge-cells", { row: ref.row, col: ref.col });
  }

  if (!sheetId) {
    return (
      <div className="text-xs text-slate-500">Лист не инициализирован.</div>
    );
  }

  return (
    <div className={`space-y-2 ${fill ? "flex h-full min-h-0 flex-col" : ""}`}>
      <div className="flex flex-wrap items-center gap-2">
        <button
          onClick={() => call("/add-row", { count: 1 })}
          disabled={busy}
          className="rounded bg-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-600 disabled:opacity-50"
        >
          + Строка
        </button>
        <button
          onClick={addColumn}
          disabled={busy}
          className="rounded bg-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-600 disabled:opacity-50"
        >
          + Столбец
        </button>
        <button
          onClick={mergeCells}
          disabled={busy}
          className="rounded bg-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-600 disabled:opacity-50"
        >
          Объединить
        </button>
        <button
          onClick={unmergeCells}
          disabled={busy}
          className="rounded bg-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-600 disabled:opacity-50"
        >
          Разъединить
        </button>
        <span className="text-xs text-slate-500">
          Формулы: =A1*B1, =SUM(A1:A10), =ROUND(quantity*price,2)
        </span>
      </div>

      <DataGrid
        columns={columns}
        rows={rows}
        storageKey={null}
        fill={fill}
        spreadsheetMode
        enableSort={false}
        merges={block.layout?.merges ?? []}
        onCellCommit={async (edit) => {
          const ok = await call("/patch-cells", {
            edits: [{ row: edit.rowIndex, col: edit.field, value: edit.value }],
          });
          if (!ok) throw new Error("patch failed");
        }}
        getEditValue={(rowIndex, key) => rawRows[rowIndex]?.[key]}
        onRenameColumn={async (key, header) => {
          await call("/rename-column", { key, header });
        }}
      />
    </div>
  );
}
