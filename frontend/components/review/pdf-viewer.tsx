"use client";

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
  highlightedBbox: BBox | null;
  onBboxClick?: (bbox: BBox) => void;
  bboxes?: Record<string, BBox>;
  activeField?: string | null;
}

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export function PdfViewer({
  documentId,
  highlightedBbox,
  bboxes = {},
  activeField,
}: PdfViewerProps) {
  // Use backend proxy — avoids MinIO localhost URL issues across network
  const viewUrl = `${API_BASE}/api/documents/${documentId}/download?inline=true`;
  const [error, setError] = useState<string | null>(null);
  const [currentPage, setCurrentPage] = useState(1);
  const containerRef = useRef<HTMLDivElement>(null);

  // Scroll to highlighted bbox
  useEffect(() => {
    if (highlightedBbox && containerRef.current) {
      setCurrentPage(highlightedBbox.page);
    }
  }, [highlightedBbox]);

  const allBboxesOnPage = Object.entries(bboxes).filter(
    ([, b]) => b.page === currentPage,
  );

  return (
    <div className="flex flex-col h-full bg-white border border-slate-200 rounded-lg overflow-hidden">
      {/* Toolbar */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-slate-200 bg-slate-50">
        <div className="flex items-center gap-2">
          <button
            onClick={() => setCurrentPage((p) => Math.max(1, p - 1))}
            className="px-2 py-1 text-xs border rounded hover:bg-white disabled:opacity-40"
            disabled={currentPage <= 1}
          >
            &larr;
          </button>
          <span className="text-xs text-slate-500">Стр. {currentPage}</span>
          <button
            onClick={() => setCurrentPage((p) => p + 1)}
            className="px-2 py-1 text-xs border rounded hover:bg-white"
          >
            &rarr;
          </button>
        </div>
        <a
          href={`${API_BASE}/api/documents/${documentId}/download`}
          target="_blank"
          rel="noreferrer"
          className="text-xs text-blue-500 hover:underline"
        >
          Скачать оригинал
        </a>
      </div>

      {/* PDF area */}
      <div
        ref={containerRef}
        className="flex-1 relative overflow-auto bg-slate-100 p-4"
      >
        {error ? (
          <div className="flex items-center justify-center h-full text-slate-400">
            {error}
          </div>
        ) : (
          <div className="relative mx-auto" style={{ maxWidth: 800 }}>
            <iframe
              src={viewUrl}
              className="w-full border-0"
              style={{ height: "calc(100vh - 200px)" }}
              title="PDF Preview"
              onError={() => setError("PDF unavailable")}
            />
            {/* Bbox overlays */}
            {allBboxesOnPage.map(([fieldName, bbox]) => (
              <div
                key={fieldName}
                className={`absolute border-2 transition-all pointer-events-none ${
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
        )}
      </div>
    </div>
  );
}
