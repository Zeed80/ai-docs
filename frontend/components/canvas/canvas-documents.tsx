"use client";

import { useState } from "react";
import type { CanvasDocumentItem } from "@/lib/canvas-context";

interface CanvasDocumentsProps {
  documents: CanvasDocumentItem[];
}

function formatBytes(bytes?: number): string {
  if (!bytes) return "";
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1048576) return `${Math.round(bytes / 1024)} КБ`;
  return `${(bytes / 1048576).toFixed(1)} МБ`;
}

function DocumentRow({ doc }: { doc: CanvasDocumentItem }) {
  const [deleteState, setDeleteState] = useState<
    "idle" | "pending" | "done" | "error"
  >("idle");

  async function deleteDocument() {
    if (!doc.delete_url) return;
    if (!window.confirm(`Удалить документ «${doc.title}»?`)) return;
    setDeleteState("pending");
    try {
      const res = await fetch(doc.delete_url, { method: "DELETE" });
      setDeleteState(res.ok ? "done" : "error");
    } catch {
      setDeleteState("error");
    }
  }

  return (
    <div className="flex items-center gap-3 rounded border border-slate-700 bg-slate-900 px-3 py-2">
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium text-slate-200">
          {doc.title}
        </div>
        <div className="mt-0.5 flex flex-wrap gap-x-2 gap-y-1 text-[11px] text-slate-500">
          {doc.filename && <span>{doc.filename}</span>}
          {doc.mime_type && <span>{doc.mime_type}</span>}
          {doc.size_bytes ? <span>{formatBytes(doc.size_bytes)}</span> : null}
        </div>
      </div>
      {doc.download_url && (
        <a
          href={doc.download_url}
          download
          className="shrink-0 rounded bg-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-600"
        >
          Скачать
        </a>
      )}
      {doc.delete_url && (
        <button
          onClick={deleteDocument}
          disabled={deleteState === "pending" || deleteState === "done"}
          className="shrink-0 rounded bg-red-900/70 px-2 py-1 text-xs text-red-100 hover:bg-red-800 disabled:bg-slate-800 disabled:text-slate-500"
        >
          {deleteState === "pending"
            ? "Удаляю..."
            : deleteState === "done"
              ? "Удалено"
              : deleteState === "error"
                ? "Ошибка"
                : "Удалить"}
        </button>
      )}
    </div>
  );
}

export function CanvasDocuments({ documents }: CanvasDocumentsProps) {
  if (documents.length === 0) {
    return <div className="text-sm text-slate-500">Документы не найдены</div>;
  }

  return (
    <div className="space-y-2">
      {documents.map((doc) => (
        <DocumentRow key={doc.id} doc={doc} />
      ))}
    </div>
  );
}
