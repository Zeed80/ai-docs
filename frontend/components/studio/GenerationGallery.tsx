"use client";

import { useTranslations } from "next-intl";

import { Generation, resultUrl } from "@/lib/studio-api";

const STATUS_COLOR: Record<string, string> = {
  queued: "text-amber-400",
  running: "text-sky-400",
  cancelled: "text-zinc-500",
  done: "text-emerald-400",
  failed: "text-red-400",
};

interface Props {
  items: Generation[];
  selectedId: string | null;
  onSelect: (g: Generation) => void;
  onDelete?: (g: Generation) => void;
}

export default function GenerationGallery({
  items,
  selectedId,
  onSelect,
  onDelete,
}: Props) {
  const t = useTranslations("studio");
  const statusLabel: Record<string, string> = {
    queued: t("status.queued"),
    running: t("status.running"),
    cancelled: t("status.cancelled"),
    done: t("status.done"),
    failed: t("status.failed"),
  };

  if (items.length === 0) {
    return (
      <div className="text-sm text-zinc-500 px-2 py-6 text-center">
        {t("gallery.empty")}
      </div>
    );
  }
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
      {items.map((g) => {
        const active = g.id === selectedId;
        const busy = g.status === "running" || g.status === "queued";
        return (
          <div
            key={g.id}
            role="button"
            tabIndex={0}
            onClick={() => onSelect(g)}
            onKeyDown={(e) => e.key === "Enter" && onSelect(g)}
            className={`group relative aspect-square rounded-lg overflow-hidden border text-left transition cursor-pointer ${
              active
                ? "border-sky-500 ring-1 ring-sky-500"
                : "border-white/10 hover:border-white/30"
            }`}
          >
            {g.has_result ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={resultUrl(g.id, true)}
                alt={g.prompt ?? t("gallery.result_alt")}
                className="w-full h-full object-cover bg-zinc-900"
              />
            ) : (
              <div className="w-full h-full flex items-center justify-center bg-zinc-900">
                <span className={`text-xs ${STATUS_COLOR[g.status]}`}>
                  {statusLabel[g.status] ?? g.status}
                </span>
              </div>
            )}

            {/* Delete on hover (any status, incl. failed) */}
            {onDelete && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onDelete(g);
                }}
                title={t("detail.delete")}
                aria-label={t("detail.delete")}
                className="absolute top-1 right-1 z-10 w-7 h-7 sm:w-6 sm:h-6 rounded bg-black/60 text-red-300 hover:bg-red-500/40 opacity-100 sm:opacity-0 sm:group-hover:opacity-100 transition text-xs"
              >
                ✕
              </button>
            )}

            {/* Live progress bar while running */}
            {busy && (
              <div className="absolute inset-x-0 top-0 h-1 bg-black/40">
                <div
                  className={`h-full bg-sky-500 ${g.progress ? "transition-all" : "animate-pulse w-1/3"}`}
                  style={
                    g.progress ? { width: `${g.progress.pct}%` } : undefined
                  }
                />
              </div>
            )}

            <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/80 to-transparent p-1.5">
              <div className={`text-[10px] ${STATUS_COLOR[g.status]}`}>
                {statusLabel[g.status] ?? g.status}
                {busy && g.progress ? ` · ${g.progress.pct}%` : ""}
                {g.accepted ? t("status.accepted_suffix") : ""}
              </div>
              <div className="text-[11px] text-zinc-300 line-clamp-1">
                {g.prompt || g.operation}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
