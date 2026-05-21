"use client";

import { useState, useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";

interface Drawing {
  id: string;
  drawing_number: string | null;
  filename: string;
  status: string;
  title_block: { title?: string; material?: string } | null;
  created_at: string;
}

const TP_TYPES = ["единичный", "типовой", "групповой"] as const;

export default function NewTechProcessPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const preselectedDrawingId = searchParams.get("drawing_id");

  const [step, setStep] = useState<1 | 2 | 3>(1);

  const [drawings, setDrawings] = useState<Drawing[]>([]);
  const [drawingsLoading, setDrawingsLoading] = useState(true);
  const [drawingSearch, setDrawingSearch] = useState("");
  const [selectedDrawing, setSelectedDrawing] = useState<Drawing | null>(null);

  const [tpType, setTpType] = useState<(typeof TP_TYPES)[number]>("единичный");
  const [batchSize, setBatchSize] = useState(1);

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`/api/drawings/?limit=50&q=${encodeURIComponent(drawingSearch)}`)
      .then((r) => r.json())
      .then((d) => {
        const items: Drawing[] = d.items ?? d.drawings ?? [];
        setDrawings(items);
        if (preselectedDrawingId && !selectedDrawing) {
          const found = items.find((dr) => dr.id === preselectedDrawingId);
          if (found) {
            setSelectedDrawing(found);
            setStep(2);
          }
        }
      })
      .catch(() => setDrawings([]))
      .finally(() => setDrawingsLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drawingSearch]);

  const filteredDrawings = drawings.filter((d) => {
    const q = drawingSearch.toLowerCase();
    return (
      !q ||
      d.filename.toLowerCase().includes(q) ||
      (d.drawing_number ?? "").toLowerCase().includes(q) ||
      (d.title_block?.title ?? "").toLowerCase().includes(q)
    );
  });

  const handleCreate = async () => {
    if (!selectedDrawing) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch(
        "/api/technology/process-plans/generate-from-drawing",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            drawing_id: selectedDrawing.id,
            tp_type: tpType,
            batch_size: batchSize,
            auto_normcontrol: true,
            created_by: "user",
          }),
        },
      );
      if (!res.ok) {
        const msg = await res.text();
        throw new Error(msg);
      }
      const data = await res.json();
      router.push(`/technology/${data.plan_id}/review?task_id=${data.task_id}`);
    } catch (e) {
      setError(String(e));
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100 p-6">
      <div className="max-w-2xl mx-auto">
        {/* Breadcrumb */}
        <div className="text-xs text-zinc-500 mb-4">
          <a href="/technology" className="hover:text-zinc-300">
            Техпроцессы
          </a>{" "}
          / <span className="text-zinc-300">Создать из чертежа</span>
        </div>

        <h1 className="text-xl font-semibold mb-6">
          Создать технологический процесс
        </h1>

        {/* Steps indicator */}
        <div className="flex items-center gap-2 mb-8">
          {[
            { n: 1, label: "Выбор чертежа" },
            { n: 2, label: "Параметры ТП" },
            { n: 3, label: "Запуск" },
          ].map(({ n, label }) => (
            <div key={n} className="flex items-center gap-2">
              <div
                className={`w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold
                ${step === n ? "bg-blue-600 text-white" : step > n ? "bg-emerald-600 text-white" : "bg-zinc-700 text-zinc-400"}`}
              >
                {step > n ? "✓" : n}
              </div>
              <span
                className={`text-xs ${step === n ? "text-zinc-200" : "text-zinc-500"}`}
              >
                {label}
              </span>
              {n < 3 && <span className="text-zinc-700 mx-1">→</span>}
            </div>
          ))}
        </div>

        {/* Step 1: Drawing selection */}
        {step === 1 && (
          <div className="space-y-4">
            <div>
              <label className="text-sm text-zinc-300 block mb-1">
                Поиск чертежа
              </label>
              <input
                type="text"
                value={drawingSearch}
                onChange={(e) => setDrawingSearch(e.target.value)}
                placeholder="Обозначение, название или файл…"
                className="w-full bg-zinc-800 border border-zinc-600 rounded px-3 py-2 text-sm text-zinc-100 outline-none focus:border-blue-500"
              />
            </div>

            <div className="border border-zinc-700 rounded-lg overflow-hidden max-h-80 overflow-y-auto">
              {drawingsLoading ? (
                <div className="p-6 text-center text-zinc-500 text-sm">
                  Загрузка…
                </div>
              ) : filteredDrawings.length === 0 ? (
                <div className="p-6 text-center text-zinc-500 text-sm">
                  Чертежи не найдены.{" "}
                  <a href="/drawings" className="text-blue-400 hover:underline">
                    Загрузить чертёж
                  </a>
                </div>
              ) : (
                filteredDrawings.map((d) => (
                  <div
                    key={d.id}
                    onClick={() => setSelectedDrawing(d)}
                    className={`px-4 py-3 flex items-start justify-between cursor-pointer border-b border-zinc-800 last:border-0 transition
                      ${selectedDrawing?.id === d.id ? "bg-blue-900/30 border-l-2 border-l-blue-500" : "hover:bg-zinc-800/50"}`}
                  >
                    <div>
                      <p className="text-sm text-zinc-200 font-medium">
                        {d.title_block?.title ?? d.filename}
                      </p>
                      <p className="text-xs text-zinc-400 mt-0.5">
                        {d.drawing_number && (
                          <span className="mr-2">{d.drawing_number}</span>
                        )}
                        {d.title_block?.material && (
                          <span className="text-zinc-500">
                            {d.title_block.material}
                          </span>
                        )}
                      </p>
                    </div>
                    <span
                      className={`text-xs px-1.5 py-0.5 rounded mt-0.5 flex-shrink-0
                        ${d.status === "analyzed" ? "bg-emerald-900/50 text-emerald-400" : "bg-zinc-700 text-zinc-400"}`}
                    >
                      {d.status}
                    </span>
                  </div>
                ))
              )}
            </div>

            <div className="flex justify-end">
              <button
                onClick={() => setStep(2)}
                disabled={!selectedDrawing}
                className="px-4 py-2 rounded bg-blue-600 hover:bg-blue-500 text-white text-sm disabled:opacity-40 transition"
              >
                Далее →
              </button>
            </div>
          </div>
        )}

        {/* Step 2: TP parameters */}
        {step === 2 && (
          <div className="space-y-5">
            {selectedDrawing && (
              <div className="rounded-lg border border-zinc-700 bg-zinc-800/50 p-3">
                <p className="text-xs text-zinc-500 mb-0.5">
                  Выбранный чертёж:
                </p>
                <p className="text-sm text-zinc-200 font-medium">
                  {selectedDrawing.title_block?.title ??
                    selectedDrawing.filename}
                </p>
                <p className="text-xs text-zinc-400">
                  {selectedDrawing.drawing_number}
                </p>
              </div>
            )}

            <div>
              <label className="text-sm text-zinc-300 block mb-1.5">
                Тип технологического процесса
              </label>
              <div className="flex gap-2">
                {TP_TYPES.map((t) => (
                  <button
                    key={t}
                    onClick={() => setTpType(t)}
                    className={`px-3 py-1.5 rounded text-sm transition border
                      ${tpType === t ? "border-blue-500 bg-blue-900/30 text-blue-300" : "border-zinc-600 text-zinc-400 hover:border-zinc-400"}`}
                  >
                    {t.charAt(0).toUpperCase() + t.slice(1)}
                  </button>
                ))}
              </div>
              <p className="text-xs text-zinc-500 mt-1">
                {tpType === "единичный" &&
                  "Разрабатывается для одного изделия."}
                {tpType === "типовой" &&
                  "Для группы изделий с одинаковой схемой обработки."}
                {tpType === "групповой" &&
                  "Для группы изделий с совместной обработкой."}
              </p>
            </div>

            <div>
              <label className="text-sm text-zinc-300 block mb-1">
                Размер партии
              </label>
              <input
                type="number"
                min={1}
                value={batchSize}
                onChange={(e) =>
                  setBatchSize(Math.max(1, parseInt(e.target.value) || 1))
                }
                className="w-32 bg-zinc-800 border border-zinc-600 rounded px-3 py-1.5 text-sm text-zinc-100 outline-none focus:border-blue-500"
              />
              <p className="text-xs text-zinc-500 mt-1">
                Используется для расчёта Тшт-к = Тшт + Тпз/n.
              </p>
            </div>

            <div className="flex justify-between">
              <button
                onClick={() => setStep(1)}
                className="px-4 py-2 rounded border border-zinc-600 text-zinc-400 hover:text-zinc-200 text-sm transition"
              >
                ← Назад
              </button>
              <button
                onClick={() => setStep(3)}
                className="px-4 py-2 rounded bg-blue-600 hover:bg-blue-500 text-white text-sm transition"
              >
                Далее →
              </button>
            </div>
          </div>
        )}

        {/* Step 3: Confirm and launch */}
        {step === 3 && (
          <div className="space-y-5">
            <div className="rounded-lg border border-zinc-700 bg-zinc-800/50 p-4 space-y-2 text-sm">
              <p className="font-semibold text-zinc-200 mb-2">
                Параметры генерации:
              </p>
              <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs">
                <span className="text-zinc-500">Чертёж:</span>
                <span className="text-zinc-200">
                  {selectedDrawing?.title_block?.title ??
                    selectedDrawing?.filename}
                </span>
                <span className="text-zinc-500">Обозначение:</span>
                <span className="text-zinc-200">
                  {selectedDrawing?.drawing_number ?? "—"}
                </span>
                <span className="text-zinc-500">Тип ТП:</span>
                <span className="text-zinc-200">{tpType}</span>
                <span className="text-zinc-500">Размер партии:</span>
                <span className="text-zinc-200">{batchSize} шт.</span>
                <span className="text-zinc-500">Нормоконтроль:</span>
                <span className="text-emerald-400">автоматически</span>
              </div>
            </div>

            <div className="text-xs text-zinc-500 space-y-1">
              <p>Агент Света выполнит:</p>
              <ol className="list-decimal list-inside space-y-0.5 text-zinc-400">
                <li>Анализ поверхностей чертежа</li>
                <li>Подбор заготовки (КИМ-анализ)</li>
                <li>Формирование маршрута операций</li>
                <li>Подбор оборудования из справочника</li>
                <li>Расчёт режимов резания и нормирование</li>
                <li>Проверка нормоконтролём (ГОСТ ЕСТД)</li>
              </ol>
            </div>

            {error && (
              <div className="rounded bg-red-900/30 border border-red-700 px-3 py-2 text-xs text-red-300">
                {error}
              </div>
            )}

            <div className="flex justify-between">
              <button
                onClick={() => setStep(2)}
                disabled={submitting}
                className="px-4 py-2 rounded border border-zinc-600 text-zinc-400 hover:text-zinc-200 text-sm transition disabled:opacity-40"
              >
                ← Назад
              </button>
              <button
                onClick={handleCreate}
                disabled={submitting}
                className="px-5 py-2 rounded bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-medium transition disabled:opacity-50 flex items-center gap-2"
              >
                {submitting ? (
                  <>
                    <span className="animate-spin">⟳</span> Запуск…
                  </>
                ) : (
                  "Создать ТП"
                )}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
