"use client";

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
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

interface Position {
  left: number;
  width: number;
  maxHeight: number;
  /** Одно из двух: список раскрывается вниз или вверх. */
  top?: number;
  bottom?: number;
}

const MIN_WIDTH = 340;
const MARGIN = 8;
/** Ниже этого списка не видно ничего полезного — лучше раскрыть вверх. */
const MIN_USABLE_HEIGHT = 220;

/**
 * Выпадающий список с поиском и группами.
 *
 * Заменяет каскад из двух нативных `<select>` (провайдер → модель): у
 * OpenRouter это сотни опций без единого поля поиска, и найти нужную модель
 * было возможно только прокруткой. Непригодные варианты здесь не прячутся, а
 * показываются с объяснением — иначе человек ищет модель, которой не видит, и
 * не понимает, почему её нет.
 *
 * Список рендерится порталом в `body` с фиксированным позиционированием.
 * Абсолютное позиционирование внутри карточки не работало: карточка секции
 * объявлена `overflow-hidden`, и список обрезался её границей — в невысокой
 * секции от него оставалась одна строка, которую ещё и нельзя было
 * прокрутить.
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
  filters,
  footer,
  onOpen,
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
  /** Фильтры под строкой поиска: сужают `items` на стороне вызывающего. */
  filters?: ReactNode;
  /** Подпись под списком — например «показано 12 из 431». */
  footer?: ReactNode;
  /**
   * Список раскрыли. Для ленивой подгрузки того, что нужно только при выборе:
   * считать это на каждый отрисованный список значило бы делать работу за все
   * четырнадцать слотов ради одного, который откроют.
   */
  onOpen?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [pos, setPos] = useState<Position | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const popupRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const close = useCallback(() => {
    setOpen(false);
    setQuery("");
  }, []);

  const updatePosition = useCallback(() => {
    const btn = buttonRef.current;
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    const below = window.innerHeight - r.bottom - MARGIN;
    const above = r.top - MARGIN;
    const dropUp = below < MIN_USABLE_HEIGHT && above > below;
    const width = Math.min(
      Math.max(r.width, MIN_WIDTH),
      window.innerWidth - 2 * MARGIN,
    );
    const left = Math.min(
      Math.max(MARGIN, r.left),
      window.innerWidth - width - MARGIN,
    );
    setPos({
      left,
      width,
      maxHeight: Math.max(
        MIN_USABLE_HEIGHT,
        Math.min(460, dropUp ? above : below),
      ),
      top: dropUp ? undefined : r.bottom + 4,
      bottom: dropUp ? window.innerHeight - r.top + 4 : undefined,
    });
  }, []);

  useLayoutEffect(() => {
    if (open) updatePosition();
  }, [open, updatePosition]);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      const target = e.target as Node;
      if (buttonRef.current?.contains(target)) return;
      if (popupRef.current?.contains(target)) return;
      close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        close();
        buttonRef.current?.focus();
      }
    };
    // capture: прокрутка идёт во внутреннем контейнере, а не в окне, и без
    // capture список оставался висеть там, где кнопка уже не находится.
    const onScroll = () => updatePosition();
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onScroll);
    inputRef.current?.focus();
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onScroll);
    };
  }, [open, close, updatePosition]);

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

  const popup = open && pos && (
    <div
      ref={popupRef}
      style={{
        position: "fixed",
        left: pos.left,
        width: pos.width,
        top: pos.top,
        bottom: pos.bottom,
        maxHeight: pos.maxHeight,
      }}
      className="z-50 flex flex-col overflow-hidden rounded-md border border-slate-600 bg-slate-900 shadow-2xl"
    >
      <div className="shrink-0 border-b border-slate-700 p-2">
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={placeholder}
          aria-label={placeholder}
          className={`${input} py-1.5`}
        />
        {filters && <div className="mt-2">{filters}</div>}
      </div>

      <ul role="listbox" className="min-h-0 flex-1 overflow-y-auto py-1">
        {total === 0 && (
          <li className="px-3 py-4 text-center text-xs text-slate-400">
            {emptyText}
          </li>
        )}
        {grouped.map(([group, list]) => (
          <li key={group}>
            <p className="px-3 pb-1 pt-2 text-[10px] font-medium uppercase tracking-wider text-slate-400">
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

      {footer && (
        <div className="shrink-0 border-t border-slate-700 px-3 py-1.5 text-[11px] text-slate-400">
          {footer}
        </div>
      )}
    </div>
  );

  return (
    <div className="relative">
      <button
        ref={buttonRef}
        type="button"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() =>
          setOpen((v) => {
            if (!v) onOpen?.();
            return !v;
          })
        }
        className={`${input} ${focusRing} flex items-center justify-between text-left disabled:cursor-not-allowed`}
      >
        <span className="min-w-0 flex-1 truncate">{buttonLabel}</span>
        <span aria-hidden="true" className="ml-2 shrink-0 text-slate-400">
          ▾
        </span>
      </button>

      {/* Портал в body: см. комментарий у компонента — карточка секции режет
          абсолютно спозиционированный список своим overflow-hidden. */}
      {typeof document !== "undefined" && popup
        ? createPortal(popup, document.body)
        : null}
    </div>
  );
}
