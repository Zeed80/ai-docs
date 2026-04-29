"use client";

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";
import { documents as docsApi } from "@/lib/api-client";
import { getApiBaseUrl } from "@/lib/api-base";

const API = getApiBaseUrl();

interface DocumentItem {
  id: string;
  file_name: string;
  status: string;
  doc_type: string | null;
  source_channel: string | null;
  created_at: string;
}

export default function InboxPage() {
  const t = useTranslations("inbox");
  const tDoc = useTranslations("document");
  const tActions = useTranslations("actions");
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [filter, setFilter] = useState("all");
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const fetchDocuments = useCallback(() => {
    const params = new URLSearchParams({ limit: "50" });
    if (filter === "needs_review") params.set("status", "needs_review");

    fetch(`${API}/api/documents?${params}`)
      .then((r) => r.json())
      .then((data) => setDocuments(data.items ?? []))
      .catch(() => setDocuments([]));
  }, [filter]);

  async function handleUploadFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    setUploading(true);
    setUploadError(null);
    const errors: string[] = [];
    for (const file of Array.from(files)) {
      try {
        await docsApi.ingest(file);
      } catch (error) {
        errors.push(`${file.name}${error instanceof Error ? ` (${error.message})` : ""}`);
      }
    }
    setUploading(false);
    if (errors.length > 0) {
      setUploadError(`Ошибка загрузки: ${errors.join(", ")}`);
    }
    fetchDocuments();
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setIsDragging(false);
    handleUploadFiles(e.dataTransfer.files);
  }

  // Keyboard navigation
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (
        e.target instanceof HTMLInputElement ||
        e.target instanceof HTMLTextAreaElement
      )
        return;

      switch (e.key) {
        case "j":
          setSelectedIndex((i) => Math.min(i + 1, documents.length - 1));
          break;
        case "k":
          setSelectedIndex((i) => Math.max(i - 1, 0));
          break;
        case "Enter":
          if (documents[selectedIndex]) {
            window.location.href = `/documents/${documents[selectedIndex].id}`;
          }
          break;
        case "?":
          // TODO: show keyboard help modal
          break;
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [documents, selectedIndex]);

  useEffect(() => {
    fetchDocuments();
  }, [fetchDocuments]);

  const filters = ["all", "needs_review", "approved", "rejected"] as const;

  return (
    <div className="p-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-bold">{t("title")}</h1>
        <div className="flex gap-2">
          {filters.map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-3 py-1.5 text-sm rounded-md transition-colors ${
                filter === f
                  ? "bg-blue-500 text-white"
                  : "bg-slate-700 text-slate-300 hover:bg-slate-600 border border-slate-600"
              }`}
            >
              {t(`filters.${f}`)}
            </button>
          ))}
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
            className="px-3 py-1.5 text-sm bg-blue-500 text-white rounded-md hover:bg-blue-600 disabled:opacity-50 flex items-center gap-1.5"
          >
            {uploading ? "Загрузка..." : `+ ${tActions("upload")}`}
          </button>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept=".pdf,.jpg,.jpeg,.png,.docx,.xlsx"
            className="hidden"
            onChange={(e) => handleUploadFiles(e.target.files)}
          />
        </div>
      </div>

      {/* Upload error */}
      {uploadError && (
        <div className="mb-4 px-4 py-2 bg-red-950/40 border border-red-700 text-sm text-red-300 rounded-lg">
          {uploadError}
          <button
            onClick={() => setUploadError(null)}
            className="ml-2 underline"
          >
            Закрыть
          </button>
        </div>
      )}

      {/* Drop zone */}
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setIsDragging(true);
        }}
        onDragLeave={() => setIsDragging(false)}
        onDrop={handleDrop}
        onClick={() => fileInputRef.current?.click()}
        className={`mb-4 cursor-pointer rounded-lg border-2 border-dashed px-4 py-6 text-center text-sm transition-colors ${
          isDragging
            ? "border-blue-400 bg-blue-950/20 text-blue-400"
            : "border-slate-600 bg-slate-900/40 text-slate-400 hover:border-slate-500"
        }`}
      >
        <div className="font-medium text-slate-200">
          {isDragging ? "Отпустите для загрузки" : "Перетащите документы сюда"}
        </div>
        <div className="mt-1 text-xs text-slate-500">
          PDF, изображения, DOCX, XLSX, TXT, DXF/STEP
        </div>
      </div>

      {/* Document list */}
      {documents.length === 0 ? (
        <div className="text-center py-20 text-slate-400">
          <p className="text-lg">{t("empty")}</p>
          <p className="text-sm mt-2">
            <kbd className="px-1.5 py-0.5 bg-slate-700 rounded border border-slate-600 text-xs">
              j
            </kbd>{" "}
            /{" "}
            <kbd className="px-1.5 py-0.5 bg-slate-700 rounded border border-slate-600 text-xs">
              k
            </kbd>{" "}
            navigate
          </p>
        </div>
      ) : (
        <div className="bg-slate-800 rounded-lg border border-slate-700 divide-y divide-slate-700">
          {documents.map((doc, index) => (
            <div
              key={doc.id}
              className={`flex items-center gap-4 px-4 py-3 cursor-pointer transition-colors ${
                index === selectedIndex
                  ? "bg-blue-900/30 border-l-2 border-l-blue-500"
                  : "hover:bg-slate-700/50"
              }`}
              onClick={() => {
                setSelectedIndex(index);
                window.location.href = `/documents/${doc.id}`;
              }}
            >
              {/* Status badge */}
              <span
                className={`px-2 py-0.5 text-xs font-medium rounded-full ${
                  doc.status === "needs_review"
                    ? "bg-amber-900/40 text-amber-400"
                    : doc.status === "approved"
                      ? "bg-green-900/40 text-green-400"
                      : doc.status === "rejected"
                        ? "bg-red-900/40 text-red-400"
                        : "bg-slate-700 text-slate-400"
                }`}
              >
                {tDoc(`status.${doc.status}`)}
              </span>

              {/* File info */}
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium truncate">{doc.file_name}</p>
                <p className="text-xs text-slate-400">
                  {doc.doc_type ? tDoc(`type.${doc.doc_type}`) : ""} &middot;{" "}
                  {new Date(doc.created_at).toLocaleString("ru-RU")}
                </p>
              </div>

              {/* Source */}
              {doc.source_channel && (
                <span className="text-xs text-slate-400">
                  {doc.source_channel}
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
