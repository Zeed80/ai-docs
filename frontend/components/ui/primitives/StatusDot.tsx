"use client";

type State = "ok" | "warn" | "error" | "unknown";

const COLOR: Record<State, string> = {
  ok: "bg-emerald-400",
  warn: "bg-amber-400",
  error: "bg-red-400",
  unknown: "bg-slate-600",
};

/**
 * Точка состояния. `title` обязателен: цвет сам по себе ничего не сообщает
 * человеку, который не различает оттенки или пользуется скринридером.
 */
export function StatusDot({ state, title }: { state: State; title: string }) {
  return (
    <span
      role="img"
      aria-label={title}
      title={title}
      className={`inline-block h-2 w-2 shrink-0 rounded-full ${COLOR[state]}`}
    />
  );
}
