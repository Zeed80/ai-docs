"use client";

import type { TableColumn, TableFilter } from "@/lib/api-client";
import { useState } from "react";

interface FilterBuilderProps {
  catalog: TableColumn[];
  filters: TableFilter[];
  onChange: (filters: TableFilter[]) => void;
  onClose: () => void;
}

const OPERATOR_LABELS: Record<string, string> = {
  eq: "=",
  gt: ">",
  lt: "<",
  gte: "≥",
  lte: "≤",
  contains: "содержит",
};

const STATUS_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "needs_review", label: "На проверку" },
  { value: "approved", label: "Утверждён" },
  { value: "rejected", label: "Отклонён" },
  { value: "draft", label: "Черновик" },
  { value: "paid", label: "Оплачен" },
];

function operatorsFor(col: TableColumn): string[] {
  if (col.data_type === "number" || col.data_type === "date")
    return ["eq", "gte", "lte", "gt", "lt"];
  if (col.data_type === "enum") return ["eq"];
  return ["contains", "eq"];
}

export function FilterBuilder({
  catalog,
  filters,
  onChange,
  onClose,
}: FilterBuilderProps) {
  // Only real, filterable columns (exclude aggregates / synthetic).
  const cols = catalog.filter((c) => c.filterable && c.key !== "row_no");
  const [column, setColumn] = useState(cols[0]?.key ?? "");
  const [operator, setOperator] = useState("contains");
  const [value, setValue] = useState("");

  const selected = cols.find((c) => c.key === column);
  const ops = selected ? operatorsFor(selected) : ["contains"];

  const labelFor = (key: string) =>
    catalog.find((c) => c.key === key)?.label ?? key;

  const addFilter = () => {
    if (!column || value === "") return;
    const col = cols.find((c) => c.key === column);
    const numeric = col?.data_type === "number";
    const next: TableFilter = {
      column,
      operator,
      value: numeric ? Number(value) : value,
    };
    onChange([...filters.filter((f) => !(f.column === column)), next]);
    setValue("");
  };

  const removeFilter = (idx: number) =>
    onChange(filters.filter((_, i) => i !== idx));

  return (
    <div
      className="absolute z-30 mt-2 w-96 rounded-lg border border-slate-700 bg-slate-800 p-3 shadow-2xl"
      onClick={(e) => e.stopPropagation()}
    >
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-slate-200">Фильтр</h3>
        <button
          onClick={onClose}
          className="text-lg leading-none text-slate-500 hover:text-slate-200"
        >
          ×
        </button>
      </div>

      {/* Active conditions */}
      {filters.length > 0 && (
        <div className="mb-3 flex flex-wrap gap-1.5">
          {filters.map((f, i) => (
            <span
              key={`${f.column}-${i}`}
              className="flex items-center gap-1 rounded-full border border-blue-700/50 bg-blue-900/30 px-2 py-0.5 text-xs text-blue-200"
            >
              {labelFor(f.column)} {OPERATOR_LABELS[f.operator] ?? f.operator}{" "}
              {String(f.value)}
              <button
                onClick={() => removeFilter(i)}
                className="text-blue-400 hover:text-white"
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}

      {/* Add condition */}
      <div className="flex flex-wrap items-center gap-2">
        <select
          value={column}
          onChange={(e) => {
            setColumn(e.target.value);
            const c = cols.find((x) => x.key === e.target.value);
            setOperator(c ? operatorsFor(c)[0] : "contains");
            setValue("");
          }}
          className="rounded border border-slate-600 bg-slate-900 px-2 py-1 text-xs text-slate-200"
        >
          {cols.map((c) => (
            <option key={c.key} value={c.key}>
              {c.label}
            </option>
          ))}
        </select>

        <select
          value={operator}
          onChange={(e) => setOperator(e.target.value)}
          className="rounded border border-slate-600 bg-slate-900 px-2 py-1 text-xs text-slate-200"
        >
          {ops.map((op) => (
            <option key={op} value={op}>
              {OPERATOR_LABELS[op] ?? op}
            </option>
          ))}
        </select>

        {selected?.data_type === "enum" ? (
          <select
            value={value}
            onChange={(e) => setValue(e.target.value)}
            className="flex-1 rounded border border-slate-600 bg-slate-900 px-2 py-1 text-xs text-slate-200"
          >
            <option value="">—</option>
            {STATUS_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        ) : (
          <input
            type={
              selected?.data_type === "number"
                ? "number"
                : selected?.data_type === "date"
                  ? "date"
                  : "text"
            }
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && addFilter()}
            placeholder="значение"
            className="w-28 flex-1 rounded border border-slate-600 bg-slate-900 px-2 py-1 text-xs text-slate-200 outline-none focus:border-blue-400"
          />
        )}

        <button
          onClick={addFilter}
          disabled={value === ""}
          className="rounded bg-blue-700 px-3 py-1 text-xs text-white hover:bg-blue-600 disabled:opacity-40"
        >
          Добавить
        </button>
      </div>

      {filters.length > 0 && (
        <button
          onClick={() => onChange([])}
          className="mt-3 text-xs text-slate-400 hover:text-slate-200"
        >
          Очистить все
        </button>
      )}
    </div>
  );
}
