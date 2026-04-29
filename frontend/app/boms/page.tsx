"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

interface BOMLine {
  id: string;
  line_number: number;
  description: string;
  quantity: number;
  unit: string;
  canonical_item_id?: string;
}

interface BOM {
  id: string;
  product_name: string;
  product_code?: string;
  version: string;
  status: string;
  approved_by?: string;
  approved_at?: string;
  notes?: string;
  lines: BOMLine[];
  created_at: string;
}

const STATUS_LABELS: Record<string, string> = {
  draft: "Черновик",
  approved: "Утверждена",
  obsolete: "Устарела",
};

const STATUS_COLORS: Record<string, string> = {
  draft: "bg-slate-700 text-slate-300",
  approved: "bg-green-900 text-green-200",
  obsolete: "bg-slate-800 text-slate-500",
};

interface CreateBOMModalProps {
  onClose: () => void;
  onCreated: (id: string) => void;
}

function CreateBOMModal({ onClose, onCreated }: CreateBOMModalProps) {
  const [productName, setProductName] = useState("");
  const [productCode, setProductCode] = useState("");
  const [version, setVersion] = useState("1.0");
  const [notes, setNotes] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!productName.trim()) {
      setError("Введите наименование изделия");
      return;
    }
    setLoading(true);
    try {
      const res = await fetch(`${API}/api/boms`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          product_name: productName,
          product_code: productCode || null,
          version,
          notes: notes || null,
          lines: [],
        }),
      });
      if (!res.ok) {
        const d = await res.json();
        setError(d.detail ?? "Ошибка");
        return;
      }
      const data = await res.json();
      onCreated(data.id);
    } catch {
      setError("Ошибка сети");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={onClose}
    >
      <div
        className="bg-slate-800 rounded-lg p-6 w-full max-w-md"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-semibold text-slate-100 mb-4">
          Новая спецификация
        </h2>
        {error && <p className="text-red-400 text-sm mb-3">{error}</p>}
        <form onSubmit={handleSubmit} className="space-y-3">
          <div>
            <label className="block text-xs text-slate-400 mb-1">
              Наименование изделия *
            </label>
            <input
              value={productName}
              onChange={(e) => setProductName(e.target.value)}
              className="w-full px-3 py-2 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
              placeholder="Редуктор РМ-500"
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-slate-400 mb-1">
                Код изделия
              </label>
              <input
                value={productCode}
                onChange={(e) => setProductCode(e.target.value)}
                className="w-full px-3 py-2 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
                placeholder="РМ-500"
              />
            </div>
            <div>
              <label className="block text-xs text-slate-400 mb-1">
                Версия
              </label>
              <input
                value={version}
                onChange={(e) => setVersion(e.target.value)}
                className="w-full px-3 py-2 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
              />
            </div>
          </div>
          <div>
            <label className="block text-xs text-slate-400 mb-1">
              Примечание
            </label>
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={2}
              className="w-full px-3 py-2 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
            />
          </div>
          <div className="flex gap-3 pt-1">
            <button
              type="submit"
              disabled={loading}
              className="flex-1 py-2 bg-indigo-600 text-white rounded text-sm font-medium hover:bg-indigo-500 disabled:opacity-50"
            >
              {loading ? "Создание..." : "Создать и открыть"}
            </button>
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-slate-400 hover:text-slate-200 text-sm"
            >
              Отмена
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default function BomsPage() {
  const router = useRouter();
  const [boms, setBoms] = useState<BOM[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [showCreate, setShowCreate] = useState(false);

  async function loadBoms() {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (statusFilter) params.set("status", statusFilter);
      const res = await fetch(`${API}/api/boms?${params}`);
      const data = await res.json();
      setBoms(data.items ?? []);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadBoms();
  }, [statusFilter]);

  function formatDate(d: string) {
    return new Date(d).toLocaleDateString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
    });
  }

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-xl font-semibold text-slate-100">
          Спецификации (BOM)
        </h1>
        <button
          onClick={() => setShowCreate(true)}
          className="px-4 py-2 bg-indigo-600 text-white rounded-md text-sm font-medium hover:bg-indigo-500"
        >
          + Создать спецификацию
        </button>
      </div>

      {/* Filters */}
      <div className="flex gap-2 mb-4">
        {(
          [
            { key: "", label: "Все" },
            { key: "draft", label: "Черновики" },
            { key: "approved", label: "Утверждённые" },
            { key: "obsolete", label: "Устаревшие" },
          ] as const
        ).map(({ key, label }) => (
          <button
            key={key}
            onClick={() => setStatusFilter(key)}
            className={`px-3 py-1.5 rounded text-xs font-medium transition-colors ${
              statusFilter === key
                ? "bg-indigo-600 text-white"
                : "bg-slate-800 text-slate-400 hover:text-slate-200"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {loading ? (
        <p className="text-slate-400 text-sm">Загрузка...</p>
      ) : boms.length === 0 ? (
        <div className="text-center py-16 text-slate-500">
          <p className="text-4xl mb-3">📋</p>
          <p className="text-sm">Спецификаций нет</p>
          <button
            onClick={() => setShowCreate(true)}
            className="mt-4 px-4 py-2 bg-indigo-600 text-white rounded-md text-sm hover:bg-indigo-500"
          >
            Создать первую спецификацию
          </button>
        </div>
      ) : (
        <div className="space-y-3">
          {boms.map((bom) => (
            <Link
              key={bom.id}
              href={`/boms/${bom.id}`}
              className="block bg-slate-800 rounded-lg p-4 hover:bg-slate-750 transition-colors"
            >
              <div className="flex items-start justify-between gap-4">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-3 mb-1">
                    <span
                      className={`px-2 py-0.5 rounded text-xs font-medium ${STATUS_COLORS[bom.status] ?? "bg-slate-700 text-slate-300"}`}
                    >
                      {STATUS_LABELS[bom.status] ?? bom.status}
                    </span>
                    <h3 className="text-sm font-medium text-slate-100 truncate">
                      {bom.product_name}
                    </h3>
                    <span className="text-xs text-slate-500">
                      v{bom.version}
                    </span>
                    {bom.product_code && (
                      <span className="text-xs text-slate-500 font-mono">
                        {bom.product_code}
                      </span>
                    )}
                  </div>
                  <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500">
                    <span>{bom.lines.length} позиций</span>
                    <span>Создана {formatDate(bom.created_at)}</span>
                    {bom.approved_by && (
                      <span>Утверждена: {bom.approved_by}</span>
                    )}
                  </div>
                </div>
                <svg
                  className="w-4 h-4 text-slate-600 shrink-0 mt-1"
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M9 5l7 7-7 7"
                  />
                </svg>
              </div>
            </Link>
          ))}
        </div>
      )}

      {showCreate && (
        <CreateBOMModal
          onClose={() => setShowCreate(false)}
          onCreated={(id) => router.push(`/boms/${id}`)}
        />
      )}
    </div>
  );
}
