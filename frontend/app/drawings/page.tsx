"use client";

import { useState, useEffect, useRef } from "react";
import Link from "next/link";
import clsx from "clsx";
import type { Drawing, DrawingStatus } from "@/lib/drawings-api";
import { drawingsApi } from "@/lib/drawings-api";

const STATUS_LABELS: Record<DrawingStatus, string> = {
  uploaded: "Загружен",
  analyzing: "Анализ",
  analyzed: "Готов",
  needs_review: "На проверке",
  approved: "Утверждён",
  failed: "Ошибка",
};

const STATUS_COLORS: Record<DrawingStatus, string> = {
  uploaded: "text-zinc-400",
  analyzing: "text-blue-400 animate-pulse",
  analyzed: "text-green-400",
  needs_review: "text-yellow-400",
  approved: "text-emerald-400",
  failed: "text-red-400",
};

const FORMAT_ICONS: Record<string, string> = {
  dxf: "📐",
  dwg: "📐",
  pdf: "📄",
  step: "🧊",
  stp: "🧊",
  iges: "🧊",
  igs: "🧊",
  svg: "🖼️",
  png: "🖼️",
  jpg: "🖼️",
  jpeg: "🖼️",
  tiff: "🖼️",
  tif: "🖼️",
  bmp: "🖼️",
  webp: "🖼️",
  gif: "🖼️",
};

export default function DrawingsPage() {
  const [drawings, setDrawings] = useState<Drawing[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState<DrawingStatus | "">("");
  const [search, setSearch] = useState("");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [deleting, setDeleting] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const PAGE_SIZE = 20;

  const load = async () => {
    setLoading(true);
    try {
      const result = await drawingsApi.list({
        page,
        page_size: PAGE_SIZE,
        status: statusFilter || undefined,
        drawing_number: search || undefined,
      });
      setDrawings(result.items);
      setTotal(result.total);
    } catch {
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, [page, statusFilter, search]);

  const handleUpload = async (files: FileList | null) => {
    if (!files?.length) return;
    setUploading(true);
    try {
      for (const file of Array.from(files)) {
        await drawingsApi.upload(file);
      }
      await load();
    } finally {
      setUploading(false);
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    handleUpload(e.dataTransfer.files);
  };

  const toggleSelect = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleSelectAll = () => {
    if (selectedIds.size === drawings.length) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(drawings.map((d) => d.id)));
    }
  };

  const handleDeleteSelected = async () => {
    if (!selectedIds.size) return;
    setDeleting(true);
    try {
      await drawingsApi.bulkDelete(Array.from(selectedIds));
      setSelectedIds(new Set());
      setConfirmDelete(false);
      await load();
    } finally {
      setDeleting(false);
    }
  };

  const handleDeleteOne = async (id: string, e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDeleting(true);
    try {
      await drawingsApi.delete(id);
      await load();
    } finally {
      setDeleting(false);
    }
  };

  const totalPages = Math.ceil(total / PAGE_SIZE);

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-4 px-6 py-4 bg-zinc-900 border-b border-white/10">
        <h1 className="text-xl font-semibold text-white">Чертежи</h1>
        <span className="text-white/30 text-sm">{total} шт.</span>

        <div className="flex items-center gap-2 ml-auto">
          {/* Search */}
          <input
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(1);
            }}
            placeholder="Обозначение чертежа..."
            className="bg-zinc-800 border border-white/10 rounded px-3 py-1.5 text-sm text-white placeholder-white/30 focus:outline-none focus:border-blue-500/50 w-48"
          />

          {/* Status filter */}
          <select
            value={statusFilter}
            onChange={(e) => {
              setStatusFilter(e.target.value as DrawingStatus | "");
              setPage(1);
            }}
            className="bg-zinc-800 border border-white/10 rounded px-3 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500/50"
          >
            <option value="">Все статусы</option>
            {Object.entries(STATUS_LABELS).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>

          {/* Select all toggle */}
          {drawings.length > 0 && (
            <button
              onClick={toggleSelectAll}
              className="px-3 py-1.5 text-sm text-white/60 hover:text-white bg-zinc-800 hover:bg-zinc-700 border border-white/10 rounded transition-colors"
            >
              {selectedIds.size === drawings.length
                ? "Снять всё"
                : "Выбрать всё"}
            </button>
          )}

          {/* Upload button */}
          <button
            onClick={() => fileRef.current?.click()}
            disabled={uploading}
            className="flex items-center gap-2 px-4 py-1.5 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white rounded text-sm font-medium transition-colors"
          >
            {uploading ? (
              <span className="w-4 h-4 border border-white/40 border-t-white rounded-full animate-spin" />
            ) : (
              "↑"
            )}
            Загрузить
          </button>
          <input
            ref={fileRef}
            type="file"
            multiple
            accept=".dxf,.dwg,.pdf,.step,.stp,.iges,.igs,.svg,.png,.jpg,.jpeg,.tiff,.tif,.bmp,.webp,.gif"
            className="hidden"
            onChange={(e) => handleUpload(e.target.files)}
          />
        </div>
      </div>

      {/* Drop zone hint */}
      <div
        className="mx-6 mt-3 border-2 border-dashed border-white/10 rounded-lg p-3 flex items-center gap-3 text-white/30 text-sm hover:border-blue-500/30 hover:text-white/50 transition-colors cursor-pointer"
        onDragOver={(e) => e.preventDefault()}
        onDrop={handleDrop}
        onClick={() => fileRef.current?.click()}
      >
        <span className="text-2xl">📐</span>
        <span>
          DXF, DWG, PDF, STEP, SVG, PNG, JPG, TIFF, BMP, WEBP — перетащите или
          нажмите для выбора
        </span>
      </div>

      {/* Grid */}
      <div className="flex-1 overflow-y-auto px-6 py-4">
        {loading && (
          <div className="flex items-center justify-center py-12 text-white/40 gap-2">
            <div className="w-5 h-5 border border-white/30 border-t-white rounded-full animate-spin" />
            <span>Загрузка...</span>
          </div>
        )}

        {!loading && drawings.length === 0 && (
          <div className="flex flex-col items-center justify-center py-16 text-white/30 gap-3">
            <span className="text-6xl">📐</span>
            <span className="text-lg">Нет чертежей</span>
            <span className="text-sm text-center max-w-xs">
              Загрузите чертежи: DXF, DWG, PDF, SVG, PNG, JPG, TIFF и другие
              форматы
            </span>
          </div>
        )}

        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
          {drawings.map((drawing) => (
            <DrawingCard
              key={drawing.id}
              drawing={drawing}
              selected={selectedIds.has(drawing.id)}
              onToggle={() => toggleSelect(drawing.id)}
              onDelete={(e) => handleDeleteOne(drawing.id, e)}
            />
          ))}
        </div>

        {/* Pagination */}
        {totalPages > 1 && (
          <div className="flex items-center justify-center gap-2 mt-6">
            <button
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page === 1}
              className="px-3 py-1.5 bg-zinc-800 hover:bg-zinc-700 disabled:opacity-30 text-white/70 rounded text-sm"
            >
              ←
            </button>
            <span className="text-white/50 text-sm">
              {page} / {totalPages}
            </span>
            <button
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              disabled={page === totalPages}
              className="px-3 py-1.5 bg-zinc-800 hover:bg-zinc-700 disabled:opacity-30 text-white/70 rounded text-sm"
            >
              →
            </button>
          </div>
        )}
      </div>

      {/* Bulk action bar */}
      {selectedIds.size > 0 && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 flex items-center gap-3 bg-zinc-800 border border-white/20 rounded-xl px-5 py-3 shadow-2xl">
          <span className="text-white/70 text-sm">
            Выбрано:{" "}
            <span className="text-white font-semibold">{selectedIds.size}</span>
          </span>
          <button
            onClick={() => setSelectedIds(new Set())}
            className="text-white/40 hover:text-white text-sm px-2 transition-colors"
          >
            Снять
          </button>
          {confirmDelete ? (
            <>
              <span className="text-red-400 text-sm">
                Удалить {selectedIds.size} чертежей?
              </span>
              <button
                onClick={handleDeleteSelected}
                disabled={deleting}
                className="px-4 py-1.5 bg-red-600 hover:bg-red-500 disabled:opacity-50 text-white rounded text-sm font-medium transition-colors"
              >
                {deleting ? "Удаление..." : "Да, удалить"}
              </button>
              <button
                onClick={() => setConfirmDelete(false)}
                className="px-3 py-1.5 bg-zinc-700 hover:bg-zinc-600 text-white/70 rounded text-sm transition-colors"
              >
                Отмена
              </button>
            </>
          ) : (
            <button
              onClick={() => setConfirmDelete(true)}
              className="px-4 py-1.5 bg-red-600/80 hover:bg-red-600 text-white rounded text-sm font-medium transition-colors"
            >
              Удалить выбранные
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function DrawingCard({
  drawing,
  selected,
  onToggle,
  onDelete,
}: {
  drawing: Drawing;
  selected: boolean;
  onToggle: () => void;
  onDelete: (e: React.MouseEvent) => void;
}) {
  const fmt = drawing.format.toLowerCase();
  const icon = FORMAT_ICONS[fmt] || "📐";
  const titleBlock = drawing.title_block as Record<string, string> | null;

  return (
    <div
      className={clsx(
        "group relative flex flex-col bg-zinc-900 border rounded-xl overflow-hidden transition-all",
        selected
          ? "border-blue-500/60 ring-1 ring-blue-500/40"
          : "border-white/10 hover:border-white/20 hover:bg-zinc-800",
      )}
    >
      {/* Checkbox overlay */}
      <button
        onClick={(e) => {
          e.preventDefault();
          onToggle();
        }}
        className="absolute top-2 left-2 z-10 w-5 h-5 rounded border flex items-center justify-center transition-colors"
        style={{
          background: selected ? "rgb(37 99 235)" : "rgba(24,24,27,0.8)",
          borderColor: selected ? "rgb(37 99 235)" : "rgba(255,255,255,0.3)",
        }}
        title={selected ? "Снять выбор" : "Выбрать"}
      >
        {selected && (
          <svg className="w-3 h-3 text-white" fill="none" viewBox="0 0 12 12">
            <path
              d="M2 6l3 3 5-5"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        )}
      </button>

      {/* Delete button */}
      <button
        onClick={onDelete}
        className="absolute top-2 right-8 z-10 w-6 h-6 rounded flex items-center justify-center bg-zinc-900/80 hover:bg-red-600 text-white/40 hover:text-white transition-colors opacity-0 group-hover:opacity-100"
        title="Удалить"
      >
        <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 14 14">
          <path
            d="M2 3.5h10M5.5 3.5V2.5h3v1M4.5 3.5l.5 8h4l.5-8"
            stroke="currentColor"
            strokeWidth="1.2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>

      <Link href={`/drawings/${drawing.id}`} className="flex flex-col flex-1">
        {/* Thumbnail or placeholder */}
        <div className="h-32 bg-zinc-800 flex items-center justify-center border-b border-white/5 relative overflow-hidden">
          {drawing.thumbnail_path ? (
            <img
              src={`/api/drawings/${drawing.id}/thumbnail`}
              alt={drawing.filename}
              className="w-full h-full object-contain p-2"
            />
          ) : (
            <div className="flex flex-col items-center gap-1 text-white/20">
              <span className="text-4xl">{icon}</span>
              <span className="text-xs uppercase font-mono">
                {drawing.format.toUpperCase()}
              </span>
            </div>
          )}

          <div className="absolute top-2 right-2">
            <span
              className={clsx(
                "text-xs px-1.5 py-0.5 rounded font-medium bg-zinc-900/80",
                STATUS_COLORS[drawing.status as DrawingStatus],
              )}
            >
              {STATUS_LABELS[drawing.status as DrawingStatus] || drawing.status}
            </span>
          </div>
        </div>

        {/* Info */}
        <div className="p-3 flex-1">
          <div className="font-medium text-sm text-white truncate group-hover:text-blue-300 transition-colors">
            {titleBlock?.title || drawing.filename}
          </div>
          <div className="flex items-center gap-2 mt-1">
            {drawing.drawing_number && (
              <span className="text-white/40 text-xs font-mono">
                {drawing.drawing_number}
              </span>
            )}
            {drawing.revision && (
              <span className="text-amber-400/60 text-xs">
                ред. {drawing.revision}
              </span>
            )}
          </div>
          {titleBlock?.material && (
            <div className="text-white/30 text-xs mt-1 truncate">
              {titleBlock.material}
            </div>
          )}
          <div className="text-white/20 text-xs mt-1">
            {new Date(drawing.created_at).toLocaleDateString("ru")}
          </div>
        </div>
      </Link>
    </div>
  );
}
