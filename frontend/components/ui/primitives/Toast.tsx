"use client";

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

type Tone = "ok" | "error" | "info";

interface Toast {
  id: number;
  tone: Tone;
  text: string;
  /** Подробность (код ответа, detail сервера) — под основной строкой, мельче. */
  detail?: string;
}

interface ToastApi {
  show: (
    text: string,
    opts?: { tone?: Tone; detail?: string; ms?: number },
  ) => void;
  ok: (text: string, detail?: string) => void;
  error: (text: string, detail?: string) => void;
}

const Ctx = createContext<ToastApi | null>(null);

const TONE: Record<Tone, string> = {
  ok: "border-emerald-500/40 bg-emerald-500/10 text-emerald-200",
  error: "border-red-500/40 bg-red-500/10 text-red-200",
  info: "border-slate-600 bg-slate-800 text-slate-200",
};

/**
 * Сообщения вместо `alert()`/`confirm()`.
 *
 * На экране моделей их было четырнадцать: модальное окно браузера блокирует
 * страницу, не поддаётся стилям и не показывает, к какому именно действию
 * относится. Ошибки при этом выводились тремя разными способами — где
 * `alert(JSON.stringify(detail))`, где эфемерная строка, где вообще молча.
 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => {
    setItems((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const show = useCallback<ToastApi["show"]>(
    (text, opts) => {
      const id = nextId.current++;
      const tone = opts?.tone ?? "info";
      setItems((prev) => [...prev, { id, tone, text, detail: opts?.detail }]);
      // Ошибку держим дольше: её обычно нужно прочитать и осмыслить.
      const ms = opts?.ms ?? (tone === "error" ? 8000 : 3500);
      window.setTimeout(() => dismiss(id), ms);
    },
    [dismiss],
  );

  const api = useMemo<ToastApi>(
    () => ({
      show,
      ok: (text, detail) => show(text, { tone: "ok", detail }),
      error: (text, detail) => show(text, { tone: "error", detail }),
    }),
    [show],
  );

  return (
    <Ctx.Provider value={api}>
      {children}
      <div
        aria-live="polite"
        aria-atomic="false"
        className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2"
      >
        {items.map((t) => (
          <div
            key={t.id}
            role={t.tone === "error" ? "alert" : "status"}
            className={`pointer-events-auto rounded border px-3 py-2 text-sm shadow-lg ${TONE[t.tone]}`}
          >
            <div className="flex items-start justify-between gap-2">
              <span>{t.text}</span>
              <button
                type="button"
                onClick={() => dismiss(t.id)}
                aria-label="Скрыть сообщение"
                className="shrink-0 opacity-60 hover:opacity-100"
              >
                ✕
              </button>
            </div>
            {t.detail && (
              <p className="mt-1 break-words font-mono text-[11px] opacity-70">
                {t.detail}
              </p>
            )}
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}

/**
 * Вне провайдера возвращает заглушку, а не бросает: сообщение — не та вещь,
 * из-за которой должен падать экран.
 */
export function useToast(): ToastApi {
  const ctx = useContext(Ctx);
  return (
    ctx ?? {
      show: () => {},
      ok: () => {},
      error: () => {},
    }
  );
}
