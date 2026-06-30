"use client";

import { Generation, resultUrl } from "@/lib/studio-api";

const STATUS_LABEL: Record<string, string> = {
  queued: "В очереди",
  running: "Генерация…",
  done: "Готово",
  failed: "Ошибка",
};

const STATUS_COLOR: Record<string, string> = {
  queued: "text-amber-400",
  running: "text-sky-400",
  done: "text-emerald-400",
  failed: "text-red-400",
};

interface Props {
  items: Generation[];
  selectedId: string | null;
  onSelect: (g: Generation) => void;
}

export default function GenerationGallery({
  items,
  selectedId,
  onSelect,
}: Props) {
  if (items.length === 0) {
    return (
      <div className="text-sm text-zinc-500 px-2 py-6 text-center">
        Пока ничего не сгенерировано. Опишите задачу слева и нажмите
        «Сгенерировать».
      </div>
    );
  }
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
      {items.map((g) => {
        const active = g.id === selectedId;
        return (
          <button
            key={g.id}
            onClick={() => onSelect(g)}
            className={`group relative aspect-square rounded-lg overflow-hidden border text-left transition ${
              active
                ? "border-sky-500 ring-1 ring-sky-500"
                : "border-white/10 hover:border-white/30"
            }`}
          >
            {g.has_result ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={resultUrl(g.id, true)}
                alt={g.prompt ?? "результат"}
                className="w-full h-full object-cover bg-zinc-900"
              />
            ) : (
              <div className="w-full h-full flex items-center justify-center bg-zinc-900">
                <span className={`text-xs ${STATUS_COLOR[g.status]}`}>
                  {STATUS_LABEL[g.status] ?? g.status}
                </span>
              </div>
            )}
            <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/80 to-transparent p-1.5">
              <div className={`text-[10px] ${STATUS_COLOR[g.status]}`}>
                {STATUS_LABEL[g.status] ?? g.status}
                {g.accepted ? " · принято" : ""}
              </div>
              <div className="text-[11px] text-zinc-300 line-clamp-1">
                {g.prompt || g.operation}
              </div>
            </div>
          </button>
        );
      })}
    </div>
  );
}
