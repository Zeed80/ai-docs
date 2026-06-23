// Shared value formatting for grids — lifted from canvas-table so every grid
// renders money / numbers / dates identically.

import type { GridColumn } from "./types";

export function isMoneyKey(key: string): boolean {
  const k = key.toLowerCase();
  return (
    k === "amount" ||
    k === "total_amount" ||
    k === "subtotal" ||
    k === "subtotal_amount" ||
    k === "tax_amount" ||
    k === "unit_price" ||
    k === "paid_amount" ||
    k.endsWith("_amount") ||
    k.endsWith("_price")
  );
}

export function formatNumberValue(value: unknown, fractionDigits = 4): string {
  if (value == null || value === "") return "—";
  const text = String(value).replace(/\s/g, "").replace(",", ".");
  const number = Number(text);
  if (!Number.isFinite(number)) return String(value);
  return new Intl.NumberFormat("ru-RU", {
    minimumFractionDigits: 0,
    maximumFractionDigits: fractionDigits,
  }).format(number);
}

export function formatMoneyValue(value: unknown): string {
  if (value == null || value === "") return "—";
  const text = String(value).replace(/\s/g, "").replace(",", ".");
  const number = Number(text);
  if (!Number.isFinite(number)) return String(value);
  return new Intl.NumberFormat("ru-RU", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(number);
}

export function isNumericColumn(col: GridColumn): boolean {
  return col.type === "number" || col.type === "money" || isMoneyKey(col.key);
}

export function displayValue(value: unknown, column: GridColumn): string {
  if (value == null) return "—";
  if (column.type === "money" || isMoneyKey(column.key)) {
    return formatMoneyValue(value);
  }
  if (column.type === "number") return formatNumberValue(value);
  return String(value);
}
