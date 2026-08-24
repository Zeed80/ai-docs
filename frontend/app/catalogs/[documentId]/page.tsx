"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { CatalogPageViewer } from "@/components/catalogs/CatalogPageViewer";
import {
  catalogsApi,
  type CatalogEntry,
  type CatalogPageHit,
  type CatalogPageInfo,
  type CatalogPageSearchResult,
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
  // Поиск ПО ЭТОМУ каталогу: по тексту страниц и по позициям. Без него
  // открытый каталог на 948 страниц можно было только листать.
  const [query, setQuery] = useState("");
  const [found, setFound] = useState<CatalogPageSearchResult | null>(null);
  const [searching, setSearching] = useState(false);

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
  //
  // history.replaceState, NOT router.replace: the router treats a query change
  // as a navigation and fires an RSC request, which the next page turn aborts —
  // the browser then shows a red net::ERR_ABORTED for every keypress (user
  // report: «красным проскакивает networkerror»). The address bar is all we
  // actually need to update here.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    if (url.searchParams.get("page") === String(page)) return;
    url.searchParams.set("page", String(page));
    window.history.replaceState(window.history.state, "", url.toString());
  }, [page]);

  useEffect(() => {
    const needle = query.trim();
    if (needle.length < 2) {
      setFound(null);
      return;
    }
    let cancelled = false;
    setSearching(true);
    const handle = window.setTimeout(async () => {
      try {
        const result = await catalogsApi.searchPages(documentId, needle);
        if (!cancelled) setFound(result);
      } catch (e: unknown) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Поиск по каталогу не удался");
        }
      } finally {
        if (!cancelled) setSearching(false);
      }
    }, 350);
    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
  }, [documentId, query]);

  function openHit(hit: CatalogPageHit) {
    setPage(hit.page_number);
  }

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

  const parsing =
    catalog && ["queued", "running"].includes(catalog.status) && !catalog.paused;
  const paused = Boolean(catalog?.paused);

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
            {paused && catalog?.progress_total
              ? ` · разбор приостановлен на странице ${catalog.progress_done} из ${catalog.progress_total}`
              : ""}
            {!paused && catalog?.waiting_for_gpu
              ? " · ждёт свободную видеокарту (агент или студия заняли её) — продолжится сам"
              : ""}
          </p>
        </div>
        <div className="flex flex-1 items-center justify-end gap-2">
          <div className="relative w-full max-w-sm">
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Поиск по каталогу: слово или артикул"
              className="w-full rounded border border-slate-600 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 placeholder-slate-500 focus:border-blue-500 focus:outline-none"
            />
            {query && (
              <button
                onClick={() => setQuery("")}
                className="absolute right-2 top-1.5 text-sm text-slate-500 hover:text-slate-200"
                title="Очистить поиск"
              >
                ✕
              </button>
            )}
          </div>
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
        {found ? (
          <aside className="w-72 shrink-0 overflow-y-auto rounded border border-slate-700 bg-slate-800/40 p-2">
            <h2 className="mb-2 text-sm text-slate-300">
              Найдено страниц: {found.total}
              {searching && <span className="ml-2 text-slate-500">…</span>}
            </h2>
            {found.message && (
              <p className="mb-2 rounded border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-200">
                {found.message}
              </p>
            )}
            {found.items.length === 0 && !found.message && (
              <p className="text-xs text-slate-500">
                Ничего не нашлось. Попробуйте часть артикула или другое слово.
              </p>
            )}
            {found.items.map((hit) => (
              <button
                key={hit.page_number}
                onClick={() => openHit(hit)}
                className={`mb-2 flex w-full gap-2 rounded border p-2 text-left ${
                  hit.page_number === page
                    ? "border-blue-500 bg-blue-950/30"
                    : "border-slate-700 hover:border-slate-500"
                }`}
              >
                {hit.thumb_url && (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={hit.thumb_url}
                    alt={`Страница ${hit.page_number}`}
                    loading="lazy"
                    className="h-16 w-12 shrink-0 object-contain"
                  />
                )}
                <span className="min-w-0">
                  <span className="block text-xs text-slate-300">
                    стр. {hit.page_number}
                    {hit.matched_entries > 0 && (
                      <span className="ml-1 text-slate-500">
                        · совпало позиций: {hit.matched_entries}
                      </span>
                    )}
                  </span>
                  <span className="mt-0.5 block text-[11px] leading-snug text-slate-500">
                    {hit.snippet.length > 120
                      ? `${hit.snippet.slice(0, 120)}…`
                      : hit.snippet}
                  </span>
                </span>
              </button>
            ))}
          </aside>
        ) : (
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
        )}

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
