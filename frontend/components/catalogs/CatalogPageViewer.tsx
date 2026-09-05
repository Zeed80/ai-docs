"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  catalogsApi,
  type CatalogEntry,
  type CatalogPageInfo,
} from "@/lib/catalogs-api";

/**
 * Catalog viewer built on rendered page images.
 *
 * Not pdf.js and not an iframe: the site CSP sets `frame-src 'none'` /
 * `object-src 'none'`, and a 44 MB / 948-page PDF is unusable in the browser
 * anyway. The pages are already rendered during ingestion, so opening one is a
 * single image request and jumping to a position's page is exact.
 */
export function CatalogPageViewer({
  documentId,
  pages,
  page,
  onPageChange,
  highlight,
}: {
  documentId: string;
  pages: CatalogPageInfo[];
  page: number;
  onPageChange: (page: number) => void;
  highlight?: CatalogEntry | null;
}) {
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [panning, setPanning] = useState(false);
  const panStart = useRef({ x: 0, y: 0, panX: 0, panY: 0 });
  const [loaded, setLoaded] = useState(false);
  const imageRef = useRef<HTMLImageElement>(null);

  const current = pages.find((item) => item.page_number === page);
  const total = pages.length;

  useEffect(() => {
    setLoaded(false);
    setZoom(1);
    setPan({ x: 0, y: 0 });
  }, [page]);

  const go = useCallback(
    (next: number) => {
      if (next >= 1 && next <= total) onPageChange(next);
    },
    [onPageChange, total],
  );

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;
      if (target && ["INPUT", "TEXTAREA"].includes(target.tagName)) return;
      if (event.key === "ArrowRight" || event.key === "PageDown") go(page + 1);
      if (event.key === "ArrowLeft" || event.key === "PageUp") go(page - 1);
      if (event.key === "+" || event.key === "=")
        setZoom((z) => Math.min(6, z * 1.25));
      if (event.key === "-") setZoom((z) => Math.max(0.5, z / 1.25));
      if (event.key === "0") {
        setZoom(1);
        setPan({ x: 0, y: 0 });
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [go, page]);

  // Highlight box: stored in pixels of the page raster, drawn in percentages so
  // it survives any display scaling.
  const box =
    highlight?.image_bbox && current?.width && current?.height
      ? {
          left: `${(highlight.image_bbox.x / current.width) * 100}%`,
          top: `${(highlight.image_bbox.y / current.height) * 100}%`,
          width: `${(highlight.image_bbox.w / current.width) * 100}%`,
          height: `${(highlight.image_bbox.h / current.height) * 100}%`,
        }
      : null;

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-slate-700 bg-slate-800/60 px-3 py-2 text-sm">
        <button
          onClick={() => go(page - 1)}
          disabled={page <= 1}
          className="rounded bg-slate-700 px-2 py-1 text-slate-200 disabled:opacity-40"
        >
          ←
        </button>
        <input
          type="number"
          value={page}
          min={1}
          max={total}
          onChange={(event) => go(Number(event.target.value))}
          className="w-16 rounded border border-slate-600 bg-slate-900 px-2 py-1 text-center text-slate-200"
        />
        <span className="text-slate-400">из {total}</span>
        <button
          onClick={() => go(page + 1)}
          disabled={page >= total}
          className="rounded bg-slate-700 px-2 py-1 text-slate-200 disabled:opacity-40"
        >
          →
        </button>
        <div className="ml-2 flex items-center gap-1">
          <button
            onClick={() => setZoom((z) => Math.max(0.5, z / 1.25))}
            className="rounded bg-slate-700 px-2 py-1 text-slate-200"
          >
            −
          </button>
          <span className="w-12 text-center text-xs text-slate-400">
            {Math.round(zoom * 100)}%
          </span>
          <button
            onClick={() => setZoom((z) => Math.min(6, z * 1.25))}
            className="rounded bg-slate-700 px-2 py-1 text-slate-200"
          >
            +
          </button>
          <button
            onClick={() => {
              setZoom(1);
              setPan({ x: 0, y: 0 });
            }}
            className="rounded bg-slate-700 px-2 py-1 text-xs text-slate-300"
          >
            сброс
          </button>
        </div>
        {current?.status === "skipped" && (
          <span className="rounded bg-slate-700 px-2 py-0.5 text-[11px] text-slate-300">
            страница без позиций
            {current.skip_reason ? ` (${current.skip_reason})` : ""}
          </span>
        )}
      </div>

      <div
        className="relative flex-1 overflow-hidden bg-slate-950"
        onMouseDown={(event) => {
          setPanning(true);
          panStart.current = {
            x: event.clientX,
            y: event.clientY,
            panX: pan.x,
            panY: pan.y,
          };
        }}
        onMouseMove={(event) => {
          if (!panning) return;
          setPan({
            x: panStart.current.panX + (event.clientX - panStart.current.x),
            y: panStart.current.panY + (event.clientY - panStart.current.y),
          });
        }}
        onMouseUp={() => setPanning(false)}
        onMouseLeave={() => setPanning(false)}
        style={{ cursor: panning ? "grabbing" : "grab" }}
      >
        {!loaded && (
          <div className="absolute inset-0 flex items-center justify-center text-sm text-slate-400">
            Загрузка страницы…
          </div>
        )}
        {/* Центрирование через flex, а не left-1/2: абсолютный блок с left:50%
            получает под себя лишь ПОЛОВИНУ ширины родителя, и страница
            съёживалась вдвое против доступного места (замерено в браузере:
            249 px при контейнере 498 px). */}
        <div className="absolute inset-0 flex items-center justify-center">
          <div
            className="relative"
            style={{
              transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
            }}
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              ref={imageRef}
              src={catalogsApi.pageImageUrl(documentId, page, "full")}
              alt={`Страница ${page}`}
              onLoad={() => setLoaded(true)}
              className="max-h-[78vh] max-w-full object-contain"
              draggable={false}
            />
            {box && (
              <div
                className="pointer-events-none absolute border-2 border-amber-400 bg-amber-400/10"
                style={box}
              />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
