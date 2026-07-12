"use client";

export default function StatusBar({
  cursor,
  scale,
  osnap,
  ortho,
  onToggleOsnap,
  onToggleOrtho,
  selectionCount,
  toolLabel,
  t,
}: {
  cursor: { x: number; y: number } | null;
  scale: number | null;
  osnap: boolean;
  ortho: boolean;
  onToggleOsnap: () => void;
  onToggleOrtho: () => void;
  selectionCount: number;
  toolLabel: string;
  t: (k: string, v?: Record<string, string | number>) => string;
}) {
  const coords = cursor
    ? scale
      ? `${(cursor.x * scale).toFixed(1)}, ${(cursor.y * scale).toFixed(1)} мм`
      : `${Math.round(cursor.x)}, ${Math.round(cursor.y)} px`
    : "—";
  const toggle = (active: boolean) =>
    `rounded px-2 py-0.5 ${active ? "bg-sky-600 text-white" : "bg-white/5 text-zinc-400 hover:bg-white/10"}`;
  return (
    <div className="flex flex-wrap items-center gap-2 rounded border border-white/10 bg-zinc-900/60 px-2 py-1 text-[11px] text-zinc-400">
      <span className="min-w-[150px] font-mono text-zinc-300">{coords}</span>
      <span className="border-l border-white/10 pl-2">{toolLabel}</span>
      {selectionCount > 0 && (
        <span className="text-sky-300">
          {t("vector.status_selected", { n: selectionCount })}
        </span>
      )}
      <span className="flex-1" />
      <button type="button" onClick={onToggleOsnap} className={toggle(osnap)}>
        OSNAP
      </button>
      <button type="button" onClick={onToggleOrtho} className={toggle(ortho)}>
        ORTHO
      </button>
    </div>
  );
}
