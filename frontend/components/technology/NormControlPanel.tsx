"use client";

import { useState } from "react";

export interface NormControlCheck {
  id: string;
  check_code: string;
  gost_code: string;
  clause: string | null;
  severity: "error" | "warning" | "info";
  status: "open" | "resolved" | "waived";
  message: string;
  recommendation: string | null;
  auto_fixable: boolean;
  operation_id: string | null;
  form_type: string | null;
}

interface NormControlResult {
  status: "passed" | "failed" | "not_checked" | "checking";
  checks: NormControlCheck[];
  errors_count: number;
  warnings_count: number;
  total_count: number;
}

interface Props {
  planId: string;
  result: NormControlResult | null;
  onRerun: () => void;
  onResolve: (checkId: string, resolution: "fixed" | "waived") => Promise<void>;
}

const SEVERITY_ICON: Record<string, string> = {
  error: "🔴",
  warning: "🟡",
  info: "ℹ️",
};

const SEVERITY_LABEL: Record<string, string> = {
  error: "Ошибка",
  warning: "Предупреждение",
  info: "Информация",
};

const STATUS_LABEL: Record<string, string> = {
  passed: "Пройден",
  failed: "Не пройден",
  not_checked: "Не проверялся",
  checking: "Проверяется…",
};

const STATUS_COLOR: Record<string, string> = {
  passed: "text-emerald-400",
  failed: "text-red-400",
  not_checked: "text-zinc-400",
  checking: "text-blue-400",
};

export default function NormControlPanel({
  planId,
  result,
  onRerun,
  onResolve,
}: Props) {
  const [resolving, setResolving] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "open" | "error" | "warning">(
    "open",
  );

  const status = result?.status ?? "not_checked";
  const checks = result?.checks ?? [];

  const filtered = checks.filter((c) => {
    if (filter === "open") return c.status === "open";
    if (filter === "error")
      return c.severity === "error" && c.status === "open";
    if (filter === "warning")
      return c.severity === "warning" && c.status === "open";
    return true;
  });

  // Group by ГОСТ code
  const groups: Record<string, NormControlCheck[]> = {};
  for (const c of filtered) {
    if (!groups[c.gost_code]) groups[c.gost_code] = [];
    groups[c.gost_code].push(c);
  }

  const handleResolve = async (
    checkId: string,
    resolution: "fixed" | "waived",
  ) => {
    setResolving(checkId);
    try {
      await onResolve(checkId, resolution);
    } finally {
      setResolving(null);
    }
  };

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-zinc-700">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold text-zinc-200">
            Нормоконтроль
          </span>
          <span className={`text-xs font-medium ${STATUS_COLOR[status]}`}>
            {STATUS_LABEL[status]}
          </span>
          {result && (
            <span className="text-xs text-zinc-500">
              ({result.errors_count} ош. / {result.warnings_count} пред.)
            </span>
          )}
        </div>
        <button
          onClick={onRerun}
          className="text-xs px-2 py-1 rounded bg-blue-600 hover:bg-blue-500 text-white transition"
        >
          ↻ Повторить
        </button>
      </div>

      {/* Filters */}
      <div className="flex gap-1 px-4 py-2 border-b border-zinc-800">
        {(["all", "open", "error", "warning"] as const).map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`text-xs px-2 py-0.5 rounded ${
              filter === f
                ? "bg-zinc-600 text-white"
                : "text-zinc-400 hover:text-zinc-200"
            }`}
          >
            {f === "all"
              ? "Все"
              : f === "open"
                ? "Открытые"
                : f === "error"
                  ? "Ошибки"
                  : "Предупреждения"}
          </button>
        ))}
      </div>

      {/* Checks list */}
      <div className="flex-1 overflow-y-auto">
        {!result ? (
          <div className="flex flex-col items-center justify-center h-40 text-zinc-500 text-sm">
            <p>Нормоконтроль не запускался.</p>
            <button
              onClick={onRerun}
              className="mt-2 text-blue-400 hover:underline text-xs"
            >
              Запустить сейчас
            </button>
          </div>
        ) : filtered.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-40 text-zinc-500 text-sm">
            {status === "passed" ? (
              <>
                <span className="text-2xl">✅</span>
                <p className="mt-1">Замечаний нет. Нормоконтроль пройден.</p>
              </>
            ) : (
              <p>Нет замечаний по выбранному фильтру.</p>
            )}
          </div>
        ) : (
          Object.entries(groups).map(([gostCode, groupChecks]) => (
            <div
              key={gostCode}
              className="border-b border-zinc-800 last:border-0"
            >
              <div className="px-4 py-1.5 bg-zinc-800/50 text-xs font-semibold text-zinc-400">
                {gostCode}
              </div>
              {groupChecks.map((check) => (
                <div
                  key={check.id}
                  className={`px-4 py-2 hover:bg-zinc-800/30 transition ${
                    check.status !== "open" ? "opacity-50" : ""
                  }`}
                >
                  <div className="flex items-start gap-2">
                    <span className="text-sm mt-0.5 flex-shrink-0">
                      {SEVERITY_ICON[check.severity]}
                    </span>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <span className="text-xs font-mono text-zinc-400">
                          {check.check_code}
                        </span>
                        {check.clause && (
                          <span className="text-xs text-zinc-500">
                            {check.clause}
                          </span>
                        )}
                        {check.form_type && (
                          <span className="text-xs bg-zinc-700 px-1 rounded text-zinc-300">
                            {check.form_type}
                          </span>
                        )}
                        {check.status !== "open" && (
                          <span className="text-xs text-emerald-500">
                            {check.status === "resolved"
                              ? "✓ Исправлено"
                              : "≈ Снято"}
                          </span>
                        )}
                      </div>
                      <p className="text-xs text-zinc-200 mt-0.5 leading-relaxed">
                        {check.message}
                      </p>
                      {check.recommendation && (
                        <p className="text-xs text-zinc-400 mt-0.5 italic">
                          → {check.recommendation}
                        </p>
                      )}
                    </div>
                  </div>

                  {check.status === "open" && (
                    <div className="flex gap-1 mt-1.5 ml-6">
                      <button
                        onClick={() => handleResolve(check.id, "fixed")}
                        disabled={resolving === check.id}
                        className="text-xs px-2 py-0.5 rounded bg-emerald-700 hover:bg-emerald-600 text-white disabled:opacity-50 transition"
                      >
                        {resolving === check.id ? "…" : "Исправлено"}
                      </button>
                      <button
                        onClick={() => handleResolve(check.id, "waived")}
                        disabled={resolving === check.id}
                        className="text-xs px-2 py-0.5 rounded bg-zinc-700 hover:bg-zinc-600 text-zinc-200 disabled:opacity-50 transition"
                      >
                        Снять
                      </button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
