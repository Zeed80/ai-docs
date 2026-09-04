"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { focusRing, input } from "./tokens";

export interface ComboboxItem<T> {
  /** Стабильный ключ; он же возвращается в onChange. */
  key: string;
  /** Заголовок группы, в которую попадёт строка. */
  group: string;
  /** По этой строке идёт поиск — собирается вызывающим. */
  search: string;
  /** Выбор запрещён: строка видна и объяснена, но кликом не выбирается. */
  disabled?: boolean;
  value: T;
}

/**
 * Выпадающий список с поиском и группами.
 *
 * Заменяет каскад из двух нативных `<select>` (провайдер → модель): у
 * OpenRouter это сотни опций без единого поля поиска, и найти нужную модель
 * было возможно только прокруткой. Непригодные варианты здесь не прячутся, а
 * показываются с объяснением — иначе человек ищет модель, которой не видит, и
 * не понимает, почему её нет.
 */
export function Combobox<T>({
  items,
  value,
  onChange,
  renderItem,
  placeholder = "Поиск…",
  emptyText = "Ничего не найдено",
  buttonLabel,
  disabled = false,
  groupOrder,
}: {
  items: ComboboxItem<T>[];
  value: string | null;
  onChange: (key: string) => void;
  renderItem: (item: ComboboxItem<T>, selected: boolean) => ReactNode;
  placeholder?: string;
  emptyText?: string;
  /** Что показать на кнопке, когда список закрыт. */
  buttonLabel: ReactNode;
  disabled?: boolean;
  /** Порядок групп; группы вне списка идут после, в порядке появления. */
  groupOrder?: string[];
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const close = useCallback(() => {
    setOpen(false);
    setQuery("");
  }, []);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    inputRef.current?.focus();
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, close]);

  const grouped = useMemo(() => {
    const q = query.trim().toLowerCase();
    const matched = q
      ? items.filter((i) => i.search.toLowerCase().includes(q))
      : items;

    const byGroup = new Map<string, ComboboxItem<T>[]>();
    for (const item of matched) {
      const list = byGroup.get(item.group);
      if (list) list.push(item);
      else byGroup.set(item.group, [item]);
    }

    const order = groupOrder ?? [];
    return [...byGroup.entries()].sort(([a], [b]) => {
      const ia = order.indexOf(a);
      const ib = order.indexOf(b);
      if (ia === -1 && ib === -1) return 0;
      if (ia === -1) return 1;
      if (ib === -1) return -1;
      return ia - ib;
    });
  }, [items, query, groupOrder]);

  const total = grouped.reduce((n, [, list]) => n + list.length, 0);

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className={`${input} ${focusRing} flex items-center justify-between text-left disabled:cursor-not-allowed`}
      >
        <span className="min-w-0 flex-1 truncate">{buttonLabel}</span>
        <span aria-hidden="true" className="ml-2 shrink-0 text-slate-500">
          ▾
        </span>
      </button>

      {open && (
        <div className="absolute z-30 mt-1 w-full rounded-md border border-slate-600 bg-slate-900 shadow-xl">
          <div className="border-b border-slate-700 p-2">
            <input
              ref={inputRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={placeholder}
              aria-label={placeholder}
              className={`${input} py-1.5`}
            />
          </div>

          <ul role="listbox" className="max-h-80 overflow-y-auto py-1">
            {total === 0 && (
              <li className="px-3 py-4 text-center text-xs text-slate-500">
                {emptyText}
              </li>
            )}
            {grouped.map(([group, list]) => (
              <li key={group}>
                <p className="px-3 pb-1 pt-2 text-[10px] font-medium uppercase tracking-wider text-slate-500">
                  {group}
                </p>
                <ul>
                  {list.map((item) => {
                    const selected = item.key === value;
                    return (
                      <li key={item.key}>
                        <button
                          type="button"
                          role="option"
                          aria-selected={selected}
                          disabled={item.disabled}
                          onClick={() => {
                            if (item.disabled) return;
                            onChange(item.key);
                            close();
                          }}
                          className={`w-full px-3 py-1.5 text-left transition-colors disabled:cursor-not-allowed ${
                            selected ? "bg-blue-600/20" : "hover:bg-slate-800"
                          } ${item.disabled ? "opacity-60" : ""}`}
                        >
                          {renderItem(item, selected)}
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
