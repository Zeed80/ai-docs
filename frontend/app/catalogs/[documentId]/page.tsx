"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { CatalogPageViewer } from "@/components/catalogs/CatalogPageViewer";
import {
  catalogsApi,
  type CatalogEntry,
  type CatalogPageInfo,
  type CatalogSummary,
} from "@/lib/catalogs-api";

export default function CatalogViewerPage() {
  const params = useParams<{ documentId: string }>();
  const router = useRouter();
  const search = useSearchParams();
  const documentId = params.documentId;

  const [catalog, setCatalog] = useState<CatalogSummary | null>(null);
  const [pages, setPages] = useState<CatalogPageInfo[]>([]);
  const [entries, setEntries] = useState<CatalogEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(Number(search.get("page") || 1));
  // The strip must never render every page at once: a 948-page catalog fired
  // 948 thumbnail requests on open and the rate limiter answered 429 to most of
  // them (found in the browser — the API tests could not see this).
  const STRIP_WINDOW = 40;
  const [stripEnd, setStripEnd] = useState(STRIP_WINDOW);
  const highlightId = search.get("entry");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [summary, pageList] = await Promise.all([
          catalogsApi.get(documentId),
          catalogsApi.pages(documentId),
        ]);
        if (cancelled) return;
        setCatalog(summary);
        setPages(pageList.items);
      } catch (e: unknown) {
        if (cancelled) return;
        // /catalogs/{id} used to mean a SUPPLIER id; keep those links working
        // instead of showing "каталог не найден" to anyone with a bookmark.
        const message = e instanceof Error ? e.message : "Ошибка загрузки";
        if (
          message.includes("404") ||
          message.toLowerCase().includes("не найден")
        ) {
          router.replace(`/suppliers/${documentId}?tab=catalog`);
          return;
        }
        setError(message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [documentId]);

  const loadEntries = useCallback(async () => {
    try {
      const result = await catalogsApi.search({
        catalog_document_id: documentId,
        page_number: page,
        page_size: 100,
        include_facets: false,
      });
      setEntries(result.items);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Ошибка загрузки позиций");
    }
  }, [documentId, page]);

  useEffect(() => {
    loadEntries();
  }, [loadEntries]);

  // Keep the URL in step so a position's page can be linked to directly.
  useEffect(() => {
    const next = new URLSearchParams(Array.from(search.entries()));
    next.set("page", String(page));
    router.replace(`/catalogs/${documentId}?${next}`, { scroll: false });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page]);

  // Keep the current page inside the rendered window when navigating.
  useEffect(() => {
    setStripEnd((end) => (page + 10 > end ? page + 10 : end));
  }, [page]);

  const visiblePages = useMemo(
    () => pages.slice(0, Math.min(stripEnd, pages.length)),
    [pages, stripEnd],
  );

  const highlight = useMemo(
    () => entries.find((entry) => entry.id === highlightId) ?? null,
    [entries, highlightId],
  );

  const parsing = catalog && ["queued", "running"].includes(catalog.status);

  return (
    <div className="flex h-[calc(100vh-4rem)] flex-col gap-2 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <h1 className="truncate text-lg text-slate-100">
            {catalog?.file_name ?? "Каталог"}
          </h1>
          <p className="text-xs text-slate-400">
            {catalog?.supplier_name ? `${catalog.supplier_name} · ` : ""}
            {catalog?.page_count ?? 0} стр. ·{" "}
            {(catalog?.entries_count ?? 0).toLocaleString("ru")} позиций
            {parsing && catalog?.progress_total
              ? ` · разбор: страница ${catalog.progress_done} из ${catalog.progress_total}`
              : ""}
          </p>
        </div>
        <div className="flex gap-2">
          {catalog?.download_url && (
            <a
              href={catalog.download_url}
              className="rounded bg-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-600"
            >
              Скачать PDF
            </a>
          )}
          {catalog?.supplier_name && (
            <Link
              href="/catalogs"
              className="rounded bg-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-600"
            >
              Все каталоги
            </Link>
          )}
        </div>
      </div>

      {error && (
        <div className="rounded border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      )}

      <div className="flex min-h-0 flex-1 gap-3">
        <aside className="w-28 shrink-0 overflow-y-auto rounded border border-slate-700 bg-slate-800/40 p-1">
          {visiblePages.map((item) => (
            <button
              key={item.page_number}
              onClick={() => setPage(item.page_number)}
              className={`mb-1 w-full rounded border p-1 ${
                item.page_number === page
                  ? "border-blue-500 bg-blue-950/40"
                  : "border-transparent hover:border-slate-600"
              }`}
              title={
                item.status === "skipped"
                  ? `Страница без позиций (${item.skip_reason ?? ""})`
                  : `${item.entries_count} позиций`
              }
            >
              {item.thumb_url ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={item.thumb_url}
                  alt={`Страница ${item.page_number}`}
                  loading="lazy"
                  className="w-full"
                />
              ) : (
                <div className="flex h-24 items-center justify-center bg-slate-900 text-[10px] text-slate-600">
                  {item.page_number}
                </div>
              )}
              <div className="mt-0.5 text-center text-[10px] text-slate-500">
                {item.page_number}
                {item.entries_count > 0 ? ` · ${item.entries_count}` : ""}
              </div>
            </button>
          ))}
          {visiblePages.length < pages.length && (
            <button
              onClick={() => setStripEnd((end) => end + STRIP_WINDOW)}
              className="mb-1 w-full rounded border border-slate-700 py-2 text-[11px] text-slate-400 hover:border-slate-500 hover:text-slate-200"
            >
              ещё {Math.min(STRIP_WINDOW, pages.length - visiblePages.length)} из{" "}
              {pages.length - visiblePages.length}
            </button>
          )}
        </aside>

        <main className="min-w-0 flex-1 overflow-hidden rounded border border-slate-700">
          {pages.length > 0 ? (
            <CatalogPageViewer
              documentId={documentId}
              pages={pages}
              page={page}
              onPageChange={setPage}
              highlight={highlight}
            />
          ) : (
            <div className="flex h-full items-center justify-center text-sm text-slate-500">
              {parsing
                ? "Страницы ещё рендерятся…"
                : "Страницы каталога не размечены"}
            </div>
          )}
        </main>

        <aside className="w-80 shrink-0 overflow-y-auto rounded border border-slate-700 bg-slate-800/40 p-2">
          <h2 className="mb-2 text-sm text-slate-300">
            Позиции страницы {page}
            <span className="ml-1 text-slate-500">({entries.length})</span>
          </h2>
          {entries.length === 0 && (
            <p className="text-xs text-slate-500">
              На этой странице позиций нет
            </p>
          )}
          {entries.map((entry) => (
            <div
              key={entry.id}
              className={`mb-2 rounded border p-2 ${
                entry.id === highlightId
                  ? "border-amber-500 bg-amber-950/20"
                  : "border-slate-700 bg-slate-900/60"
              }`}
            >
              <div className="flex gap-2">
                {entry.thumb_url && (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={entry.thumb_url}
                    alt={entry.name}
                    loading="lazy"
                    className="h-14 w-14 shrink-0 rounded bg-slate-950 object-contain"
                  />
                )}
                <div className="min-w-0">
                  {entry.part_number && (
                    <div className="truncate font-mono text-[11px] text-slate-400">
                      {entry.part_number}
                    </div>
                  )}
                  <div className="text-xs text-slate-200">{entry.name}</div>
                  <div className="text-[11px] text-slate-500">
                    {entry.price_value
                      ? `${entry.price_value.toLocaleString("ru")} ${entry.price_currency}`
                      : "цена не указана"}
                    {entry.image_kind === "page" ? " · картинка: страница" : ""}
                  </div>
                </div>
              </div>
            </div>
          ))}
        </aside>
      </div>
    </div>
  );
}
