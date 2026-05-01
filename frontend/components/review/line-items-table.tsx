"use client";

import type { InvoiceLine } from "@/lib/api-client";

interface LineItemsTableProps {
  lines: InvoiceLine[];
  currency?: string | null;
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

export function LineItemsTable({ lines, currency }: LineItemsTableProps) {
  if (lines.length === 0) {
    return (
      <div className="text-sm text-slate-500 text-center py-4">
        Позиции не извлечены
      </div>
    );
  }

  const cur = currency ?? "₽";

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
          {lines.map((line) => (
            <tr
              key={line.id}
              className="hover:bg-slate-800/50 transition-colors"
            >
              <td className="py-1.5 px-2 text-slate-500">{line.line_number}</td>
              <td className="py-1.5 px-2 text-slate-100 max-w-[200px]">
                <span className="line-clamp-2 leading-tight">
                  {line.description ?? "—"}
                </span>
              </td>
              <td className="py-1.5 px-2 text-slate-400 font-mono text-[11px]">
                {line.sku ?? "—"}
              </td>
              <td className="py-1.5 px-2 text-right text-slate-100">
                {fmt(line.quantity, 3).replace(/\.?0+$/, "")}
              </td>
              <td className="py-1.5 px-2 text-slate-400">{line.unit ?? "—"}</td>
              <td className="py-1.5 px-2 text-right text-slate-200 font-mono">
                {fmt(line.unit_price)}
              </td>
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
          ))}
        </tbody>
        {lines.length > 0 && (
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
        )}
      </table>
    </div>
  );
}
