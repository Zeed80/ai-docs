"use client";

import { useState } from "react";

import type { InvoiceLine } from "@/lib/api-client";

interface LineItemsTableProps {
  lines: InvoiceLine[];
  currency?: string | null;
  /** When provided, key cells become inline-editable. */
  onLineUpdate?: (
    lineId: string,
    data: Partial<InvoiceLine>,
  ) => Promise<void> | void;
  disabled?: boolean;
}

function fmt(v: number | null | undefined, decimals = 2): string {
  if (v == null) return "—";
  return v.toLocaleString("ru-RU", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function fmtPct(v: number | null | undefined): string {
  if (v == null) return "—";
  return `${(v * 100).toFixed(0)}%`;
}

function confidenceDot(c: number | null): string {
  if (c == null) return "bg-slate-600";
  if (c >= 0.8) return "bg-green-500";
  if (c >= 0.5) return "bg-amber-400";
  return "bg-red-500";
}

type EditField = "description" | "sku" | "quantity" | "unit" | "unit_price";

interface EditableCellProps {
  value: string | number | null | undefined;
  display: string;
  type: "text" | "number";
  align?: "left" | "right";
  className?: string;
  editable: boolean;
  saving: boolean;
  onCommit: (raw: string) => void;
}

function EditableCell({
  value,
  display,
  type,
  align = "left",
  className = "",
  editable,
  saving,
  onCommit,
}: EditableCellProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");

  const alignCls = align === "right" ? "text-right" : "text-left";

  if (!editable) {
    return (
      <td className={`py-1.5 px-2 ${alignCls} ${className}`}>{display}</td>
    );
  }

  if (editing) {
    return (
      <td className={`py-1 px-1 ${alignCls}`}>
        <input
          autoFocus
          type={type}
          step={type === "number" ? "any" : undefined}
          defaultValue={value == null ? "" : String(value)}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => {
            setEditing(false);
            onCommit(draft);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              setEditing(false);
              onCommit(draft);
            } else if (e.key === "Escape") {
              setEditing(false);
            }
          }}
          className={`w-full bg-slate-950 border border-blue-500 rounded px-1 py-0.5 text-xs text-slate-100 ${alignCls}`}
        />
      </td>
    );
  }

  return (
    <td
      className={`py-1.5 px-2 ${alignCls} ${className} cursor-text hover:bg-slate-800/70 ${
        saving ? "opacity-50" : ""
      }`}
      title="Нажмите, чтобы изменить"
      onClick={() => {
        setDraft(value == null ? "" : String(value));
        setEditing(true);
      }}
    >
      {display}
    </td>
  );
}

export function LineItemsTable({
  lines,
  onLineUpdate,
  disabled = false,
}: LineItemsTableProps) {
  const [savingLine, setSavingLine] = useState<string | null>(null);
  const editable = Boolean(onLineUpdate) && !disabled;

  if (!lines?.length) {
    return (
      <div className="text-sm text-slate-500 text-center py-4">
        Позиции не извлечены
      </div>
    );
  }

  async function commit(
    lineId: string | null,
    field: EditField,
    raw: string,
    previous: string | number | null | undefined,
  ) {
    if (!onLineUpdate || !lineId) return;
    const trimmed = raw.trim();
    let value: string | number | null;
    if (field === "quantity" || field === "unit_price") {
      if (trimmed === "") {
        value = null;
      } else {
        const parsed = Number(trimmed.replace(",", "."));
        if (Number.isNaN(parsed)) return;
        value = parsed;
      }
    } else {
      value = trimmed === "" ? null : trimmed;
    }
    const prevNorm = previous == null ? null : previous;
    if (value === prevNorm) return; // no change
    setSavingLine(lineId);
    try {
      await onLineUpdate(lineId, { [field]: value } as Partial<InvoiceLine>);
    } finally {
      setSavingLine(null);
    }
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b border-slate-700 text-slate-400">
            <th className="text-left py-1.5 px-2 w-6">#</th>
            <th className="text-left py-1.5 px-2">Наименование</th>
            <th className="text-left py-1.5 px-2 w-20">Арт.</th>
            <th className="text-right py-1.5 px-2 w-16">Кол-во</th>
            <th className="text-left py-1.5 px-2 w-12">Ед.</th>
            <th className="text-right py-1.5 px-2 w-24">Цена</th>
            <th className="text-right py-1.5 px-2 w-24">Сумма</th>
            <th className="text-right py-1.5 px-2 w-16">НДС%</th>
            <th className="text-right py-1.5 px-2 w-20">НДС</th>
            <th className="w-4 px-1"></th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800">
          {lines.map((line) => {
            const saving = savingLine === line.id;
            return (
              <tr
                key={line.id}
                className="hover:bg-slate-800/50 transition-colors"
              >
                <td className="py-1.5 px-2 text-slate-500">
                  {line.line_number}
                </td>
                <EditableCell
                  value={line.description}
                  display={line.description ?? "—"}
                  type="text"
                  className="text-slate-100 max-w-[200px]"
                  editable={editable}
                  saving={saving}
                  onCommit={(raw) =>
                    commit(line.id, "description", raw, line.description)
                  }
                />
                <EditableCell
                  value={line.sku}
                  display={line.sku ?? "—"}
                  type="text"
                  className="text-slate-400 font-mono text-[11px]"
                  editable={editable}
                  saving={saving}
                  onCommit={(raw) => commit(line.id, "sku", raw, line.sku)}
                />
                <EditableCell
                  value={line.quantity}
                  display={fmt(line.quantity, 3).replace(/\.?0+$/, "")}
                  type="number"
                  align="right"
                  className="text-slate-100"
                  editable={editable}
                  saving={saving}
                  onCommit={(raw) =>
                    commit(line.id, "quantity", raw, line.quantity)
                  }
                />
                <EditableCell
                  value={line.unit}
                  display={line.unit ?? "—"}
                  type="text"
                  className="text-slate-400"
                  editable={editable}
                  saving={saving}
                  onCommit={(raw) => commit(line.id, "unit", raw, line.unit)}
                />
                <EditableCell
                  value={line.unit_price}
                  display={fmt(line.unit_price)}
                  type="number"
                  align="right"
                  className="text-slate-200 font-mono"
                  editable={editable}
                  saving={saving}
                  onCommit={(raw) =>
                    commit(line.id, "unit_price", raw, line.unit_price)
                  }
                />
                <td className="py-1.5 px-2 text-right text-slate-100 font-mono font-medium">
                  {fmt(line.amount)}
                </td>
                <td className="py-1.5 px-2 text-right text-slate-400">
                  {fmtPct(line.tax_rate)}
                </td>
                <td className="py-1.5 px-2 text-right text-slate-400 font-mono">
                  {fmt(line.tax_amount)}
                </td>
                <td className="py-1 px-1">
                  <span
                    className={`inline-block w-1.5 h-1.5 rounded-full ${confidenceDot(line.confidence)}`}
                    title={
                      line.confidence != null
                        ? `Уверенность: ${(line.confidence * 100).toFixed(0)}%`
                        : "Нет данных"
                    }
                  />
                </td>
              </tr>
            );
          })}
        </tbody>
        <tfoot className="border-t border-slate-600">
          <tr className="text-slate-300 font-medium">
            <td
              colSpan={6}
              className="py-1.5 px-2 text-right text-slate-400 text-[11px]"
            >
              Итого ({lines.length} поз.):
            </td>
            <td className="py-1.5 px-2 text-right font-mono text-slate-100">
              {fmt(lines.reduce((s, l) => s + (l.amount ?? 0), 0))}
            </td>
            <td />
            <td className="py-1.5 px-2 text-right font-mono text-slate-400">
              {fmt(lines.reduce((s, l) => s + (l.tax_amount ?? 0), 0))}
            </td>
            <td />
          </tr>
        </tfoot>
      </table>
    </div>
  );
}
