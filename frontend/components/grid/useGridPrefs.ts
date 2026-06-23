"use client";

// Generalised column-prefs persistence — the invoices table logic
// (frontend/lib/invoice-columns.ts) lifted to work for any grid by passing a
// storage key plus the live column catalog. localStorage-backed; SSR-safe.

import { useCallback, useEffect, useRef, useState } from "react";
import type { GridPrefs } from "./types";

export function defaultGridPrefs(
  allKeys: string[],
  defaultVisible?: string[],
): GridPrefs {
  const visible = defaultVisible ?? allKeys;
  const order = [
    ...visible.filter((k) => allKeys.includes(k)),
    ...allKeys.filter((k) => !visible.includes(k)),
  ];
  const visibility: Record<string, boolean> = {};
  for (const k of allKeys) visibility[k] = visible.includes(k);
  return { order, visibility, widths: {} };
}

// Merge stored prefs with the live catalog so added/removed columns stay sane —
// new backend columns surface (hidden) instead of being dropped.
export function reconcileGridPrefs(
  stored: Partial<GridPrefs> | null,
  allKeys: string[],
  defaultVisible?: string[],
): GridPrefs {
  const base = defaultGridPrefs(allKeys, defaultVisible);
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

function loadStored(storageKey: string): Partial<GridPrefs> | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(storageKey);
    return raw ? (JSON.parse(raw) as Partial<GridPrefs>) : null;
  } catch {
    return null;
  }
}

function saveStored(storageKey: string, prefs: GridPrefs): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(storageKey, JSON.stringify(prefs));
  } catch {
    /* ignore quota / serialization errors */
  }
}

export function visibleOrderedKeys(prefs: GridPrefs): string[] {
  return prefs.order.filter((k) => prefs.visibility[k]);
}

/**
 * Persisted column prefs for a grid. Reconciles against the live column catalog
 * once it is known. Pass `storageKey: null` to disable persistence (ephemeral
 * grids such as workspace spec-tables that should not leak prefs across blocks).
 */
export function useGridPrefs(
  storageKey: string | null,
  allKeys: string[],
  defaultVisible?: string[],
) {
  const [prefs, setPrefs] = useState<GridPrefs>(() =>
    defaultGridPrefs(allKeys, defaultVisible),
  );
  const ready = useRef(false);

  useEffect(() => {
    if (ready.current || allKeys.length === 0) return;
    ready.current = true;
    const stored = storageKey ? loadStored(storageKey) : null;
    setPrefs(reconcileGridPrefs(stored, allKeys, defaultVisible));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storageKey, allKeys.join("|")]);

  const update = useCallback(
    (next: GridPrefs | ((p: GridPrefs) => GridPrefs)) => {
      setPrefs((prev) => {
        const value = typeof next === "function" ? next(prev) : next;
        if (storageKey) saveStored(storageKey, value);
        return value;
      });
    },
    [storageKey],
  );

  return { prefs, setPrefs: update };
}
