"use client";

import { useState } from "react";
import { mutFetch } from "@/lib/auth";

interface Props {
  planId: string;
  productCode?: string | null;
  version?: string;
  normcontrolStatus?: string;
}

type FormKey = "МК" | "ОК" | "КЭ";

const FORM_LABELS: Record<FormKey, string> = {
  МК: "МК (ГОСТ 3.1118)",
  ОК: "ОК (ГОСТ 3.1404)",
  КЭ: "КЭ (ГОСТ 3.1105)",
};

export default function GostFormsExporter({
  planId,
  productCode,
  version = "1.0",
  normcontrolStatus,
}: Props) {
  const [loading, setLoading] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<FormKey>>(new Set(["МК", "ОК"]));

  const toggleForm = (key: FormKey) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  };

  const download = async (forms: FormKey[]) => {
    if (forms.length === 0) return;
    const key = forms.join("+");
    setLoading(key);
    try {
      const res = await mutFetch(
        `/api/technology/process-plans/${planId}/export-gost`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ forms, format: "xlsx" }),
        },
      );
      if (!res.ok) throw new Error(await res.text());
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `TP_${productCode ?? planId}_${version}.xlsx`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      alert(`Ошибка экспорта: ${e}`);
    } finally {
      setLoading(null);
    }
  };

  const normBlocked = normcontrolStatus === "failed";

  return (
    <div className="rounded-lg border border-zinc-700 bg-zinc-800/50 p-3 space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold text-zinc-200">
          Экспорт форм ГОСТ ЕСТД
        </span>
        {normBlocked && (
          <span className="text-xs text-red-400">
            ⚠ Нормоконтроль не пройден
          </span>
        )}
      </div>

      {/* Form checkboxes */}
      <div className="flex gap-2 flex-wrap">
        {(["МК", "ОК"] as FormKey[]).map((key) => (
          <label key={key} className="flex items-center gap-1.5 cursor-pointer">
            <input
              type="checkbox"
              checked={selected.has(key)}
              onChange={() => toggleForm(key)}
              className="accent-blue-500"
            />
            <span className="text-xs text-zinc-300">{FORM_LABELS[key]}</span>
          </label>
        ))}
        <label
          className="flex items-center gap-1.5 cursor-pointer opacity-50"
          title="Скоро"
        >
          <input type="checkbox" disabled className="accent-blue-500" />
          <span className="text-xs text-zinc-400">
            {FORM_LABELS["КЭ"]} (скоро)
          </span>
        </label>
      </div>

      {/* Export buttons */}
      <div className="flex gap-2 flex-wrap">
        <button
          onClick={() => download(Array.from(selected) as FormKey[])}
          disabled={loading !== null || selected.size === 0}
          className="text-xs px-3 py-1.5 rounded bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50 transition flex items-center gap-1"
        >
          {loading ? (
            <span className="animate-spin text-sm">⟳</span>
          ) : (
            <span>⬇</span>
          )}
          Скачать выбранные
        </button>

        <button
          onClick={() => download(["МК", "ОК"])}
          disabled={loading !== null}
          className="text-xs px-3 py-1.5 rounded bg-zinc-600 hover:bg-zinc-500 text-zinc-200 disabled:opacity-50 transition"
        >
          МК + все ОК
        </button>
      </div>

      {normBlocked && (
        <p className="text-xs text-zinc-500">
          Рекомендуется исправить замечания нормоконтроля перед экспортом.
        </p>
      )}
    </div>
  );
}
