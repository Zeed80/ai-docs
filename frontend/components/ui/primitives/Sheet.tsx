"use client";

import { useCallback, useEffect, useRef, type ReactNode } from "react";
import { focusRing } from "./tokens";

/**
 * Боковая панель для короткого потока, из которого не нужно уходить.
 *
 * Заведена под подключение облачного провайдера: ключ вводится на одной
 * вкладке, а модель выбирается на другой, поэтому «завести облако» распадалось
 * на переходы между экранами. Панель позволяет пройти ключ → проверку →
 * загрузку моделей, не теряя из виду слот, ради которого всё затевалось.
 */
export function Sheet({
  open,
  onClose,
  title,
  children,
  footer,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const restoreFocusTo = useRef<HTMLElement | null>(null);

  const handleKey = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    },
    [onClose],
  );

  useEffect(() => {
    if (!open) return;
    restoreFocusTo.current = document.activeElement as HTMLElement | null;
    document.addEventListener("keydown", handleKey);
    // Фокус внутрь панели: иначе клавиатура остаётся на странице под ней.
    panelRef.current?.focus();
    return () => {
      document.removeEventListener("keydown", handleKey);
      restoreFocusTo.current?.focus?.();
    };
  }, [open, handleKey]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <div
        className="absolute inset-0 bg-black/50"
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        className="relative flex h-full w-full max-w-md flex-col border-l border-slate-700 bg-slate-900 shadow-xl focus:outline-none"
      >
        <header className="flex items-center justify-between border-b border-slate-700 px-4 py-3">
          <h2 className="text-sm font-medium text-slate-200">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Закрыть"
            className={`${focusRing} rounded p-1 text-slate-400 hover:text-slate-200`}
          >
            ✕
          </button>
        </header>
        <div className="flex-1 overflow-y-auto px-4 py-4">{children}</div>
        {footer && (
          <footer className="border-t border-slate-700 px-4 py-3">
            {footer}
          </footer>
        )}
      </div>
    </div>
  );
}
