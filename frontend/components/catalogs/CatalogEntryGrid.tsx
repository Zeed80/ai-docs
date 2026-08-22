"use client";

import { useState } from "react";
import type { CatalogEntry } from "@/lib/catalogs-api";
import { toolTypeLabel } from "@/lib/tool-types";


function EntryThumb({ entry }: { entry: CatalogEntry }) {
  const [failed, setFailed] = useState(false);
  if (!entry.thumb_url || failed) {
    return (
      <div className="flex h-full w-full items-center justify-center bg-slate-900 text-xs text-slate-600">
        нет изображения
      </div>
    );
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={entry.thumb_url}
      alt={entry.name}
      loading="lazy"
      onError={() => setFailed(true)}
      className="h-full w-full object-contain"
    />
  );
}

/**
 * Positions as cards with pictures.
 *
 * `image_kind === "page"` gets an explicit badge: that image is a preview of
 * the catalog page, not a photo of this item, and pretending otherwise is
 * exactly the kind of quiet lie this project avoids.
 */
export function CatalogEntryGrid({
  entries,
  onOpenPage,
}: {
  entries: CatalogEntry[];
  onOpenPage?: (entry: CatalogEntry) => void;
}) {
  if (!entries.length) {
    return (
      <div className="rounded border border-slate-700 bg-slate-800/40 p-6 text-center text-sm text-slate-400">
        Ничего не найдено
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {entries.map((entry) => (
        <div
          key={entry.id}
          className="flex flex-col overflow-hidden rounded-lg border border-slate-700 bg-slate-800/60 transition-colors hover:border-slate-500"
        >
          <div className="relative h-40 bg-slate-900">
            <EntryThumb entry={entry} />
            {entry.image_kind === "page" && (
              <span
                className="absolute left-2 top-2 rounded bg-slate-950/80 px-1.5 py-0.5 text-[10px] text-slate-300"
                title="Отдельной картинки товара в каталоге не нашлось — показана страница"
              >
                страница
              </span>
            )}
            {entry.page_number && (
              <button
                type="button"
                onClick={() => onOpenPage?.(entry)}
                className="absolute bottom-2 right-2 rounded bg-slate-950/80 px-1.5 py-0.5 text-[10px] text-slate-300 hover:text-white"
                title="Открыть страницу каталога"
              >
                стр. {entry.page_number}
              </button>
            )}
          </div>

          <div className="flex flex-1 flex-col gap-1 p-3">
            {entry.part_number && (
              <span className="font-mono text-xs text-slate-400">
                {entry.part_number}
              </span>
            )}
            <span className="text-sm text-slate-200" title={entry.name}>
              {entry.name.length > 90
                ? `${entry.name.slice(0, 90)}…`
                : entry.name}
            </span>
            <span className="text-xs text-slate-500">
              {toolTypeLabel(entry.tool_type)}
              {entry.diameter_mm ? ` · Ø${entry.diameter_mm}` : ""}
              {entry.material ? ` · ${entry.material}` : ""}
            </span>
            <div className="mt-auto flex items-center justify-between pt-2">
              <span className="text-sm text-slate-200">
                {entry.price_value
                  ? `${entry.price_value.toLocaleString("ru")} ${entry.price_currency}`
                  : "цена не указана"}
              </span>
              {entry.catalog_name && (
                <span
                  className="max-w-[45%] truncate text-[11px] text-slate-500"
                  title={entry.catalog_name}
                >
                  {entry.catalog_name}
                </span>
              )}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
