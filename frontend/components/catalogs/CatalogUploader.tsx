"use client";

import { useRef, useState } from "react";
import clsx from "clsx";
import type { ToolSupplier } from "@/lib/drawings-api";
import { toolCatalogApi } from "@/lib/drawings-api";

const SUPPORTED_FORMATS = [".pdf", ".xlsx", ".xls", ".csv", ".json"];
const FORMAT_DESCRIPTIONS = {
  ".pdf": "PDF с таблицами",
  ".xlsx": "Excel",
  ".xls": "Excel (старый)",
  ".csv": "CSV",
  ".json": "JSON",
};

interface CatalogUploaderProps {
  supplier: ToolSupplier;
  onUploaded: (taskId?: string) => void;
  onCancel?: () => void;
}

export function CatalogUploader({
  supplier,
  onUploaded,
  onCancel,
}: CatalogUploaderProps) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  const handleFile = (f: File | null) => {
    if (!f) return;
    const ext = "." + f.name.split(".").pop()?.toLowerCase();
    if (!SUPPORTED_FORMATS.includes(ext)) {
      setError(
        `Неподдерживаемый формат. Допустимые: ${SUPPORTED_FORMATS.join(", ")}`,
      );
      return;
    }
    setFile(f);
    setError(null);
  };

  const handleUpload = async () => {
    if (!file) return;
    setUploading(true);
    setError(null);
    try {
      const result = await toolCatalogApi.uploadCatalog(supplier.id, file);
      onUploaded(result.task_id);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Ошибка загрузки");
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="text-sm text-white/60">
        Загрузите каталог инструментов поставщика{" "}
        <span className="text-white font-medium">{supplier.name}</span>. После
        загрузки файл будет обработан автоматически: таблицы извлечены,
        нормализованы и добавлены в базу данных.
      </div>

      {/* Drop zone */}
      <div
        className={clsx(
          "border-2 border-dashed rounded-xl p-8 flex flex-col items-center gap-3 cursor-pointer transition-all",
          dragging
            ? "border-blue-500/60 bg-blue-500/5"
            : file
              ? "border-green-500/40 bg-green-500/5"
              : "border-white/15 hover:border-white/25",
        )}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          handleFile(e.dataTransfer.files[0] || null);
        }}
        onClick={() => fileRef.current?.click()}
      >
        <input
          ref={fileRef}
          type="file"
          accept={SUPPORTED_FORMATS.join(",")}
          className="hidden"
          onChange={(e) => handleFile(e.target.files?.[0] || null)}
        />

        {file ? (
          <>
            <span className="text-4xl">📂</span>
            <div className="text-center">
              <div className="text-white font-medium">{file.name}</div>
              <div className="text-white/40 text-sm mt-0.5">
                {(file.size / 1024).toFixed(1)} КБ
              </div>
            </div>
            <button
              onClick={(e) => {
                e.stopPropagation();
                setFile(null);
              }}
              className="text-white/30 hover:text-white/60 text-xs"
            >
              × Убрать файл
            </button>
          </>
        ) : (
          <>
            <span className="text-4xl text-white/20">📁</span>
            <div className="text-center">
              <div className="text-white/60 font-medium">
                Перетащите файл или нажмите
              </div>
              <div className="text-white/30 text-sm mt-1">
                {SUPPORTED_FORMATS.join(", ")}
              </div>
            </div>
          </>
        )}
      </div>

      {/* Format hints */}
      <div className="grid grid-cols-3 gap-2">
        {Object.entries(FORMAT_DESCRIPTIONS).map(([ext, desc]) => (
          <div
            key={ext}
            className="bg-zinc-800/50 border border-white/5 rounded-lg p-2 text-center"
          >
            <div className="text-xs font-mono text-blue-300">{ext}</div>
            <div className="text-xs text-white/30 mt-0.5">{desc}</div>
          </div>
        ))}
      </div>

      {error && (
        <div className="p-3 bg-red-500/10 border border-red-500/20 rounded-lg text-red-400 text-sm">
          {error}
        </div>
      )}

      <div className="flex items-center gap-3 justify-end">
        {onCancel && (
          <button
            onClick={onCancel}
            className="px-4 py-2 bg-zinc-800 hover:bg-zinc-700 text-white/70 rounded text-sm"
          >
            Отмена
          </button>
        )}
        <button
          onClick={handleUpload}
          disabled={!file || uploading}
          className="px-4 py-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-40 text-white rounded text-sm font-medium"
        >
          {uploading ? (
            <span className="flex items-center gap-2">
              <span className="w-4 h-4 border border-white/40 border-t-white rounded-full animate-spin" />
              Загрузка...
            </span>
          ) : (
            "Загрузить каталог"
          )}
        </button>
      </div>

      <div className="text-xs text-white/20 text-center">
        После загрузки задача обработки добавляется в очередь. Результаты
        появятся автоматически.
      </div>
    </div>
  );
}
