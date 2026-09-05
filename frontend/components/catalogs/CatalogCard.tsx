"use client";

import { useState } from "react";
import type { CatalogSummary } from "@/lib/catalogs-api";

/**
 * One catalog of a supplier, with its cover and parsing progress.
 *
 * This card is what makes several catalogs usable at all: before it, two
 * catalogs of one supplier were a single undivided list of thousands of rows.
 */
export function CatalogCard({
  catalog,
  onOpen,
  onReparse,
  onDelete,
  onPauseToggle,
}: {
  catalog: CatalogSummary;
  onOpen: () => void;
  onReparse?: () => void;
  onDelete?: (mode: "data" | "file" | "all") => void;
  onPauseToggle?: (resume: boolean) => void;
}) {
  const [coverFailed, setCoverFailed] = useState(false);
  // Deleting a catalog is three different intentions; asking which one keeps a
  // person from losing prices when they only wanted the disk space back.
  const [deleteOpen, setDeleteOpen] = useState(false);

  const legacy = catalog.legacy || !catalog.document_id;
  // A paused run is resumable but not active: no spinner, no polling.
  const active =
    ["queued", "running"].includes(catalog.status) && !catalog.paused;
  // Parsing a big catalog holds the GPU for hours; a person must be able to
  // stop it from here and pick it up later at the same page.
  const unfinished =
    catalog.page_count > 0 && catalog.pages_ready < catalog.page_count;
  const percent =
    catalog.progress_total > 0
      ? Math.round((catalog.progress_done / catalog.progress_total) * 100)
      : catalog.page_count > 0
        ? Math.round((catalog.pages_ready / catalog.page_count) * 100)
        : 0;
  const imageShare =
    catalog.entries_count > 0
      ? Math.round((catalog.entries_with_image / catalog.entries_count) * 100)
      : 0;

  return (
    <div className="flex flex-col overflow-hidden rounded-lg border border-slate-700 bg-slate-800/60 transition-colors hover:border-slate-500">
      <button
        type="button"
        onClick={legacy ? undefined : onOpen}
        disabled={legacy}
        className="relative flex h-44 items-center justify-center bg-slate-900"
        title={
          legacy ? "У этих позиций нет исходного файла" : "Открыть каталог"
        }
      >
        {catalog.cover_url && !coverFailed ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={catalog.cover_url}
            alt={catalog.file_name}
            loading="lazy"
            onError={() => setCoverFailed(true)}
            className="h-full w-full object-contain"
          />
        ) : (
          <span className="text-4xl text-slate-400">PDF</span>
        )}
        {active && (
          <span className="absolute right-2 top-2 rounded bg-blue-950/80 px-2 py-0.5 text-[11px] text-blue-200">
            обрабатывается
          </span>
        )}
        {catalog.paused && (
          <span className="absolute right-2 top-2 rounded bg-amber-950/80 px-2 py-0.5 text-[11px] text-amber-200">
            разбор приостановлен
          </span>
        )}
        {!catalog.paused && catalog.waiting_for_gpu && (
          <span
            className="absolute right-2 top-2 rounded bg-sky-950/80 px-2 py-0.5 text-[11px] text-sky-200"
            title="Видеокарта занята агентом или студией. Разбор продолжится сам, ничего нажимать не нужно"
          >
            ждёт свободную видеокарту
          </span>
        )}
      </button>

      <div className="flex flex-1 flex-col gap-2 p-3">
        <button
          type="button"
          onClick={onOpen}
          className="text-left text-sm font-medium text-slate-200 hover:text-white"
        >
          {catalog.file_name}
        </button>

        <div className="text-xs text-slate-400">
          {catalog.page_count > 0
            ? `${catalog.page_count} стр.`
            : "страницы не размечены"}
          {" · "}
          {catalog.entries_count.toLocaleString("ru")} позиций
          {catalog.entries_count > 0 && (
            <>
              {" · "}
              <span title="Доля позиций с картинкой товара из каталога">
                {imageShare}% с картинкой
              </span>
            </>
          )}
        </div>

        {(active || percent < 100) && catalog.progress_total > 0 && (
          <div>
            <div className="mb-1 text-[11px] text-slate-400">
              страница {catalog.progress_done} из {catalog.progress_total}
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-slate-700">
              <div
                className={`h-full rounded-full ${catalog.error ? "bg-red-500" : "bg-emerald-500"}`}
                style={{ width: `${Math.max(2, percent)}%` }}
              />
            </div>
          </div>
        )}

        {catalog.error && (
          <p className="text-[11px] text-red-400" title={catalog.error}>
            {catalog.error.slice(0, 120)}
          </p>
        )}

        {legacy && (
          <p className="text-[11px] text-amber-300/80">
            Импортировано до постраничного разбора: без страниц и картинок.
            Загрузите файл каталога заново, чтобы получить их.
          </p>
        )}

        <div className="mt-auto flex flex-wrap gap-2 pt-1">
          {!legacy && (
            <button
              onClick={onOpen}
              className="rounded bg-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-600"
            >
              Открыть
            </button>
          )}
          {catalog.download_url && !legacy && (
            <a
              href={catalog.download_url}
              className="rounded bg-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-600"
            >
              Скачать PDF
            </a>
          )}
          {onPauseToggle && !legacy && (active || unfinished) && (
            <button
              onClick={() => onPauseToggle(!active)}
              className="rounded bg-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-600"
              title={
                active
                  ? "Остановить разбор — уже разобранные страницы сохранятся"
                  : "Продолжить разбор с той же страницы"
              }
            >
              {active ? "Приостановить" : "Продолжить разбор"}
            </button>
          )}
          {onReparse && !legacy && (
            <button
              onClick={onReparse}
              className="rounded bg-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-600"
              title="Разобрать заново — страницы и картинки будут пересчитаны"
            >
              Перечитать
            </button>
          )}
          {onDelete && !legacy && (
            <button
              onClick={() => setDeleteOpen((open) => !open)}
              className="rounded bg-slate-700 px-2 py-1 text-xs text-red-300 hover:bg-red-900/50"
            >
              Удалить…
            </button>
          )}
        </div>

        {deleteOpen && onDelete && (
          <div className="rounded border border-slate-700 bg-slate-900/80 p-2 text-xs">
            <p className="mb-2 text-slate-400">Что удалить?</p>
            <div className="flex flex-col gap-1">
              <button
                onClick={() => {
                  setDeleteOpen(false);
                  onDelete("data");
                }}
                className="rounded px-2 py-1 text-left text-slate-200 hover:bg-slate-700"
                title="Позиции, страницы и векторы. Файл останется — можно разобрать заново"
              >
                Только данные ({catalog.entries_count.toLocaleString("ru")}{" "}
                позиций)
              </button>
              <button
                onClick={() => {
                  setDeleteOpen(false);
                  onDelete("file");
                }}
                className="rounded px-2 py-1 text-left text-slate-200 hover:bg-slate-700"
                title="PDF и отрисованные страницы. Позиции и цены сохранятся"
              >
                Только файл и картинки
              </button>
              <button
                onClick={() => {
                  setDeleteOpen(false);
                  onDelete("all");
                }}
                className="rounded px-2 py-1 text-left text-red-300 hover:bg-red-900/50"
              >
                Каталог целиком
              </button>
              <button
                onClick={() => setDeleteOpen(false)}
                className="rounded px-2 py-1 text-left text-slate-400 hover:text-slate-300"
              >
                Отмена
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
