// Column layout config for the invoices table — hybrid persistence:
// localStorage by default, with named server-side "Views" (SavedView) layered
// on top (see invoices/page.tsx applyView/handleSaveView).

export interface ColumnPrefs {
  order: string[];
  visibility: Record<string, boolean>;
  widths: Record<string, number>;
}

const STORAGE_KEY = "invoices.columns.v1";

// Default visible columns, in display order. Mirrors the product spec:
// № позиции, номер счёта, дата счёта, поставщик, перечень товаров, сумма (с НДС),
// примечание.
export const DEFAULT_ORDER: string[] = [
  "row_no",
  "invoice_number",
  "invoice_date",
  "supplier_name",
  "items_list",
  "total_amount",
  "notes",
];

// Service columns rendered outside the reorderable area (pinned).
export const PINNED_LEFT = "__select";
export const PINNED_RIGHT = "__actions";

export function defaultPrefs(allKeys: string[]): ColumnPrefs {
  // Visible defaults first (in spec order), then any remaining catalog columns
  // appended (hidden) so new backend columns surface in the manager.
  const order = [
    ...DEFAULT_ORDER.filter((k) => allKeys.includes(k)),
    ...allKeys.filter((k) => !DEFAULT_ORDER.includes(k)),
  ];
  const visibility: Record<string, boolean> = {};
  for (const k of allKeys) visibility[k] = DEFAULT_ORDER.includes(k);
  return { order, visibility, widths: {} };
}

// Merge stored prefs with the live catalog so added/removed columns stay sane.
export function reconcilePrefs(
  stored: Partial<ColumnPrefs> | null,
  allKeys: string[],
): ColumnPrefs {
  const base = defaultPrefs(allKeys);
  if (!stored) return base;
  const order = [
    ...(stored.order ?? []).filter((k) => allKeys.includes(k)),
    ...allKeys.filter((k) => !(stored.order ?? []).includes(k)),
  ];
  const visibility: Record<string, boolean> = {};
  for (const k of allKeys) {
    visibility[k] = stored.visibility?.[k] ?? base.visibility[k] ?? false;
  }
  return { order, visibility, widths: stored.widths ?? {} };
}

export function loadColumnPrefs(): Partial<ColumnPrefs> | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Partial<ColumnPrefs>) : null;
  } catch {
    return null;
  }
}

export function saveColumnPrefs(prefs: ColumnPrefs): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    /* ignore quota / serialization errors */
  }
}

// Ordered list of visible data-column keys (excludes pinned service columns).
export function visibleOrderedKeys(prefs: ColumnPrefs): string[] {
  return prefs.order.filter((k) => prefs.visibility[k]);
}
