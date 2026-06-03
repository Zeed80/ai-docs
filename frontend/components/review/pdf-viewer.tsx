"use client";

import { getApiBaseUrl } from "@/lib/api-base";

import { useEffect, useRef, useState } from "react";

export interface BBox {
  page: number;
  x: number;
  y: number;
  w: number;
  h: number;
}

interface PdfViewerProps {
  documentId: string;
  mimeType?: string | null;
  highlightedBbox?: BBox | null;
  onBboxClick?: (bbox: BBox) => void;
  bboxes?: Record<string, BBox>;
  activeField?: string | null;
}

const API_BASE = getApiBaseUrl();

/**
 * Renders PDFs to <canvas> with PDF.js instead of an <iframe>/<object>.
 *
 * Why: a client-side antivirus (e.g. Kaspersky) injects a strict CSP
 * (`frame-src 'none'`, `object-src 'none'`) that blocks every iframe/object —
 * including blob: and same-origin. Canvas drawing isn't governed by those
 * directives, and PDF.js fetches the bytes over `connect-src 'self'` (allowed),
 * so this works regardless of the injected CSP. The worker is served same-origin
 * from /pdf.worker.min.mjs (covered by `script-src 'self'`).
 */
export function PdfViewer({
  documentId,
  mimeType,
  bboxes = {},
  activeField,
}: PdfViewerProps) {
  const isImage = mimeType?.startsWith("image/") ?? false;
  const inlineUrl = `${API_BASE}/api/documents/${documentId}/download?inline=true`;
  const downloadUrl = `${API_BASE}/api/documents/${documentId}/download`;

  const [loading, setLoading] = useState(!isImage);
  const [error, setError] = useState<string | null>(null);
  const pagesRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (isImage) return;
    let cancelled = false;

    (async () => {
      setLoading(true);
      setError(null);
      try {
        const res = await fetch(inlineUrl, { credentials: "include" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.arrayBuffer();
        if (cancelled) return;

        const pdfjs = await import("pdfjs-dist");
        pdfjs.GlobalWorkerOptions.workerSrc = "/pdf.worker.min.mjs";

        const pdf = await pdfjs.getDocument({ data }).promise;
        if (cancelled) return;

        const host = pagesRef.current;
        if (!host) return;
        host.innerHTML = "";

        for (let n = 1; n <= pdf.numPages; n++) {
          const page = await pdf.getPage(n);
          if (cancelled) return;
          const scale = (window.devicePixelRatio || 1) * 1.4;
          const viewport = page.getViewport({ scale });
          const canvas = document.createElement("canvas");
          canvas.width = viewport.width;
          canvas.height = viewport.height;
          canvas.style.width = "100%";
          canvas.style.maxWidth = `${viewport.width / (window.devicePixelRatio || 1)}px`;
          canvas.className = "mx-auto mb-3 block rounded bg-white shadow";
          host.appendChild(canvas);
          const ctx = canvas.getContext("2d");
          if (!ctx) continue;
          await page.render({ canvasContext: ctx, viewport }).promise;
          if (cancelled) return;
        }
        setLoading(false);
      } catch (e) {
        if (!cancelled) {
          setError(
            `Не удалось отобразить PDF (${String((e as Error)?.message ?? e)})`,
          );
          setLoading(false);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [inlineUrl, isImage]);

  const overlays = Object.entries(bboxes);

  return (
    <div className="flex h-full flex-col overflow-hidden rounded-lg border border-slate-700 bg-slate-900">
      {/* Toolbar */}
      <div className="flex items-center justify-between border-b border-slate-700 bg-slate-800 px-3 py-2">
        <span className="text-xs text-slate-400">
          {isImage ? "Изображение" : "PDF"}
        </span>
        <a
          href={downloadUrl}
          target="_blank"
          rel="noreferrer"
          className="text-xs text-blue-400 hover:underline"
        >
          Открыть оригинал ↗
        </a>
      </div>

      {/* Document area — dark canvas so the (white) page stands out */}
      <div className="relative flex-1 overflow-auto bg-slate-800 p-4">
        {isImage ? (
          <div className="relative mx-auto" style={{ maxWidth: 1100 }}>
            <img
              src={inlineUrl}
              alt="Документ"
              className="w-full rounded bg-white object-contain"
            />
            {overlays.map(([fieldName, bbox]) => (
              <div
                key={fieldName}
                className={`pointer-events-none absolute border-2 transition-all ${
                  activeField === fieldName
                    ? "border-blue-500 bg-blue-500/10 ring-2 ring-blue-300"
                    : "border-amber-400 bg-amber-400/5"
                }`}
                style={{
                  left: `${bbox.x}%`,
                  top: `${bbox.y}%`,
                  width: `${bbox.w}%`,
                  height: `${bbox.h}%`,
                }}
                title={fieldName}
              />
            ))}
          </div>
        ) : (
          <>
            {loading && (
              <div className="flex h-full items-center justify-center text-sm text-slate-400">
                Загрузка документа…
              </div>
            )}
            {error && (
              <div className="flex h-full flex-col items-center justify-center gap-2 text-sm text-slate-400">
                <span>{error}</span>
                <a
                  href={downloadUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="text-blue-400 hover:underline"
                >
                  Открыть в новой вкладке ↗
                </a>
              </div>
            )}
            <div
              ref={pagesRef}
              className={loading || error ? "hidden" : "mx-auto max-w-3xl"}
            />
          </>
        )}
      </div>
    </div>
  );
}
