"use client";

import { getApiBaseUrl } from "@/lib/api-base";

import { useRouter } from "next/navigation";
import { useCallback, useRef, useState } from "react";

const API = getApiBaseUrl();

// ── Types ──────────────────────────────────────────────────────────────────

interface DiffChange {
  old?: unknown;
  new: unknown;
}

interface DiffRow {
  row_index: number;
  entity_id: string | null;
  action: "create" | "update" | "skip";
  changes: Record<string, DiffChange | unknown>;
}

interface DiffResponse {
  import_id: string;
  file_name: string;
  total_rows: number;
  creates: number;
  updates: number;
  skips: number;
  errors: number;
  diff: DiffRow[];
}

type Step = "upload" | "review" | "done";

const ACTION_COLORS = {
  create: "bg-green-50 border-green-200",
  update: "bg-amber-50 border-amber-200",
  skip: "bg-slate-50 border-slate-200 opacity-60",
};
const ACTION_LABELS = {
  create: "Создать",
  update: "Обновить",
  skip: "Пропустить",
};
const ACTION_BADGE = {
  create: "bg-green-100 text-green-700",
  update: "bg-amber-100 text-amber-700",
  skip: "bg-slate-100 text-slate-500",
};

// ── Component ──────────────────────────────────────────────────────────────

export default function ImportPage() {
  const router = useRouter();
  const fileRef = useRef<HTMLInputElement>(null);
  const [step, setStep] = useState<Step>("upload");
  const [diff, setDiff] = useState<DiffResponse | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [uploading, setUploading] = useState(false);
  const [applying, setApplying] = useState(false);
  const [result, setResult] = useState<{
    applied: number;
    skipped: number;
    errors: string[];
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);

  // ── Upload ──────────────────────────────────────────────────────────────

  const uploadFile = useCallback(async (file: File) => {
    setUploading(true);
    setError(null);
    try {
      const form = new FormData();
      form.append("file", file);
      const res = await fetch(`${API}/api/tables/import`, {
        method: "POST",
        body: form,
      });
      if (!res.ok) throw new Error(await res.text());
      const data: DiffResponse = await res.json();
      setDiff(data);
      // Pre-select all create/update rows
      setSelected(
        new Set(
          data.diff.filter((r) => r.action !== "skip").map((r) => r.row_index),
        ),
      );
      setStep("review");
    } catch (e) {
      setError(String(e));
    } finally {
      setUploading(false);
    }
  }, []);

  function onFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (f) uploadFile(f);
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragOver(false);
    const f = e.dataTransfer.files?.[0];
    if (f) uploadFile(f);
  }

  // ── Apply ───────────────────────────────────────────────────────────────

  async function applyDiff() {
    if (!diff) return;
    setApplying(true);
    setError(null);
    try {
      const rows = diff.diff
        .filter((r) => selected.has(r.row_index))
        .map((r) => ({
          entity_id: r.entity_id ?? null,
          action: r.action,
          changes: r.changes,
        }));

      const res = await fetch(`${API}/api/tables/apply-diff`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rows }),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      setResult(data);
      setStep("done");
    } catch (e) {
      setError(String(e));
    } finally {
      setApplying(false);
    }
  }

  function toggleRow(idx: number) {
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(idx)) next.delete(idx);
      else next.add(idx);
      return next;
    });
  }

  function toggleAll(rows: DiffRow[]) {
    const actionable = rows
      .filter((r) => r.action !== "skip")
      .map((r) => r.row_index);
    const allSelected = actionable.every((i) => selected.has(i));
    setSelected((s) => {
      const next = new Set(s);
      if (allSelected) actionable.forEach((i) => next.delete(i));
      else actionable.forEach((i) => next.add(i));
      return next;
    });
  }

  // ── Render ──────────────────────────────────────────────────────────────

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="px-6 py-4 border-b border-slate-200 flex items-center gap-3 shrink-0">
        <button
          onClick={() => router.push("/invoices")}
          className="text-slate-400 hover:text-slate-600"
        >
          <svg
            className="w-5 h-5"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M10 19l-7-7m0 0l7-7m-7 7h18"
            />
          </svg>
        </button>
        <div>
          <h1 className="text-xl font-semibold">Импорт Excel</h1>
          <p className="text-xs text-slate-400">
            {step === "upload" && "Загрузите файл для сравнения"}
            {step === "review" &&
              diff &&
              `${diff.file_name} — ${diff.total_rows} строк`}
            {step === "done" && "Импорт завершён"}
          </p>
        </div>
        {/* Steps indicator */}
        <div className="ml-auto flex items-center gap-2 text-xs">
          {(["upload", "review", "done"] as Step[]).map((s, i) => (
            <span key={s} className="flex items-center gap-1">
              <span
                className={`w-5 h-5 rounded-full flex items-center justify-center font-bold ${
                  step === s
                    ? "bg-blue-600 text-white"
                    : ["review", "done"].indexOf(step) >
                        ["upload", "review", "done"].indexOf(s)
                      ? "bg-green-500 text-white"
                      : "bg-slate-200 text-slate-400"
                }`}
              >
                {i + 1}
              </span>
              <span
                className={
                  step === s ? "text-slate-700 font-medium" : "text-slate-400"
                }
              >
                {s === "upload"
                  ? "Загрузка"
                  : s === "review"
                    ? "Проверка"
                    : "Готово"}
              </span>
              {i < 2 && <span className="text-slate-300 mx-1">→</span>}
            </span>
          ))}
        </div>
      </div>

      {/* Error banner */}
      {error && (
        <div className="mx-6 mt-3 px-4 py-2 rounded-lg text-sm bg-red-50 text-red-700 border border-red-200 shrink-0">
          {error}
        </div>
      )}

      {/* Step: Upload */}
      {step === "upload" && (
        <div className="flex-1 flex items-center justify-center p-8">
          <div className="w-full max-w-md">
            <div
              className={`border-2 border-dashed rounded-xl p-12 text-center transition-colors cursor-pointer ${
                dragOver
                  ? "border-blue-400 bg-blue-50"
                  : "border-slate-300 hover:border-blue-400 hover:bg-slate-50"
              }`}
              onDragOver={(e) => {
                e.preventDefault();
                setDragOver(true);
              }}
              onDragLeave={() => setDragOver(false)}
              onDrop={onDrop}
              onClick={() => fileRef.current?.click()}
            >
              <svg
                className="w-12 h-12 mx-auto text-slate-400 mb-4"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={1.5}
                  d="M9 17v-2m3 2v-4m3 4v-6m2 10H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
                />
              </svg>
              {uploading ? (
                <p className="text-sm text-blue-600 animate-pulse">
                  Загрузка и анализ...
                </p>
              ) : (
                <>
                  <p className="text-sm font-medium text-slate-700">
                    Перетащите Excel-файл или нажмите
                  </p>
                  <p className="text-xs text-slate-400 mt-1">.xlsx, .xls</p>
                  <button className="mt-4 px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700">
                    Выбрать файл
                  </button>
                </>
              )}
            </div>
            <input
              ref={fileRef}
              type="file"
              accept=".xlsx,.xls"
              className="hidden"
              onChange={onFileChange}
            />
            <p className="text-xs text-slate-400 text-center mt-4">
              Экспортируйте таблицу счетов и загрузите обратно для обновления
              данных
            </p>
          </div>
        </div>
      )}

      {/* Step: Review */}
      {step === "review" && diff && (
        <div className="flex-1 flex flex-col overflow-hidden">
          {/* Summary bar */}
          <div className="px-6 py-3 bg-slate-50 border-b border-slate-200 flex items-center gap-6 text-sm shrink-0">
            <span className="flex items-center gap-1.5">
              <span className="w-2 h-2 rounded-full bg-green-500" />
              <strong>{diff.creates}</strong> создать
            </span>
            <span className="flex items-center gap-1.5">
              <span className="w-2 h-2 rounded-full bg-amber-500" />
              <strong>{diff.updates}</strong> обновить
            </span>
            <span className="flex items-center gap-1.5">
              <span className="w-2 h-2 rounded-full bg-slate-400" />
              <strong>{diff.skips}</strong> без изменений
            </span>
            <span className="ml-auto text-slate-400 text-xs">
              {selected.size} выбрано из{" "}
              {diff.diff.filter((r) => r.action !== "skip").length}
            </span>
          </div>

          {/* Diff table */}
          <div className="flex-1 overflow-y-auto px-6 py-4">
            <div className="flex items-center justify-between mb-3">
              <button
                onClick={() => toggleAll(diff.diff)}
                className="text-xs text-blue-600 hover:underline"
              >
                {diff.diff
                  .filter((r) => r.action !== "skip")
                  .every((r) => selected.has(r.row_index))
                  ? "Снять всё"
                  : "Выбрать всё"}
              </button>
            </div>

            <div className="space-y-2">
              {diff.diff.map((row) => (
                <DiffRowCard
                  key={row.row_index}
                  row={row}
                  checked={selected.has(row.row_index)}
                  onToggle={() => toggleRow(row.row_index)}
                />
              ))}
            </div>
          </div>

          {/* Footer actions */}
          <div className="px-6 py-4 border-t border-slate-200 flex items-center gap-3 shrink-0 bg-white">
            <button
              onClick={() => {
                setStep("upload");
                setDiff(null);
              }}
              className="px-4 py-2 text-sm text-slate-600 hover:text-slate-800"
            >
              Загрузить другой файл
            </button>
            <div className="flex-1" />
            <button
              onClick={applyDiff}
              disabled={applying || selected.size === 0}
              className="px-6 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 disabled:opacity-50"
            >
              {applying ? "Применяется..." : `Применить (${selected.size})`}
            </button>
          </div>
        </div>
      )}

      {/* Step: Done */}
      {step === "done" && result && (
        <div className="flex-1 flex items-center justify-center">
          <div className="text-center max-w-sm">
            <div className="w-16 h-16 bg-green-100 rounded-full flex items-center justify-center mx-auto mb-4">
              <svg
                className="w-8 h-8 text-green-600"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M5 13l4 4L19 7"
                />
              </svg>
            </div>
            <h2 className="text-xl font-semibold">Импорт завершён</h2>
            <div className="mt-4 text-sm text-slate-600 space-y-1">
              <p>
                Применено записей: <strong>{result.applied}</strong>
              </p>
              <p>
                Пропущено: <strong>{result.skipped}</strong>
              </p>
              {result.errors.length > 0 && (
                <p className="text-red-600">Ошибок: {result.errors.length}</p>
              )}
            </div>
            {result.errors.length > 0 && (
              <div className="mt-3 text-xs text-red-600 bg-red-50 rounded p-2 text-left">
                {result.errors.slice(0, 5).map((e, i) => (
                  <div key={i}>{e}</div>
                ))}
              </div>
            )}
            <button
              onClick={() => router.push("/invoices")}
              className="mt-6 px-6 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700"
            >
              К таблице счетов
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Diff row card ──────────────────────────────────────────────────────────

function DiffRowCard({
  row,
  checked,
  onToggle,
}: {
  row: DiffRow;
  checked: boolean;
  onToggle: () => void;
}) {
  const [expanded, setExpanded] = useState(row.action === "update");
  const hasChanges = Object.keys(row.changes).length > 0;

  return (
    <div
      className={`rounded-lg border ${ACTION_COLORS[row.action]} transition-opacity`}
    >
      <div className="flex items-center px-4 py-2.5 gap-3">
        {row.action !== "skip" ? (
          <input
            type="checkbox"
            checked={checked}
            onChange={onToggle}
            className="w-4 h-4 rounded accent-blue-600"
          />
        ) : (
          <div className="w-4 h-4" />
        )}

        <span
          className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${ACTION_BADGE[row.action]}`}
        >
          {ACTION_LABELS[row.action]}
        </span>

        <span className="text-xs text-slate-500">
          Строка {row.row_index + 1}
          {row.entity_id && (
            <span className="ml-1 font-mono text-[10px] text-slate-400">
              {row.entity_id.slice(0, 8)}…
            </span>
          )}
        </span>

        {hasChanges && row.action !== "skip" && (
          <button
            onClick={() => setExpanded((e) => !e)}
            className="ml-auto text-xs text-slate-400 hover:text-slate-600 flex items-center gap-1"
          >
            {Object.keys(row.changes).length} поля
            <svg
              className={`w-3.5 h-3.5 transition-transform ${expanded ? "rotate-180" : ""}`}
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M19 9l-7 7-7-7"
              />
            </svg>
          </button>
        )}
      </div>

      {expanded && hasChanges && (
        <div className="px-4 pb-3 border-t border-current border-opacity-10">
          <div className="mt-2 space-y-1">
            {Object.entries(row.changes).map(([field, change]) => {
              const isObj =
                typeof change === "object" &&
                change !== null &&
                "new" in (change as object);
              const oldVal = isObj
                ? String((change as DiffChange).old ?? "—")
                : "—";
              const newVal = isObj
                ? String((change as DiffChange).new ?? "—")
                : String(change);
              return (
                <div key={field} className="flex items-center gap-2 text-xs">
                  <span className="w-32 text-slate-500 shrink-0">{field}</span>
                  {isObj && (
                    <>
                      <span className="line-through text-red-500">
                        {oldVal}
                      </span>
                      <span className="text-slate-400">→</span>
                    </>
                  )}
                  <span className="font-medium text-slate-800">{newVal}</span>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
