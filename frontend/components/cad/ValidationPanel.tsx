"use client";

import { useMemo, useState } from "react";

import { CadIr, IrEntity, IrPatchOp } from "@/lib/studio-api";

/** Validation report grouped by assurance level + the sketch parameter/
 * constraint editors and the long-running full check. Parameter input state
 * lives here; every mutation still flows through the parent's apply()/API
 * callbacks so revision history stays in one place. */
export default function ValidationPanel({
  ir,
  busy,
  selected,
  fullCheckCurrent,
  fullCheckRunning,
  fullCheckElapsed,
  onRunFullCheck,
  onSolve,
  onApply,
  onFocus,
  onError,
  t,
}: {
  ir: CadIr;
  busy: boolean;
  selected: IrEntity | null;
  fullCheckCurrent: boolean;
  fullCheckRunning: boolean;
  fullCheckElapsed: number;
  onRunFullCheck: () => void;
  onSolve: () => void;
  onApply: (ops: IrPatchOp[]) => void;
  onFocus: (entityId: string) => void;
  onError: (message: string) => void;
  t: (k: string, v?: Record<string, string | number>) => string;
}) {
  const [parameterName, setParameterName] = useState("");
  const [parameterValue, setParameterValue] = useState("");
  const [constraintKind, setConstraintKind] = useState<
    "horizontal" | "vertical"
  >("horizontal");

  const issues = useMemo(() => ir.validation.issues ?? [], [ir]);
  const levelGroups = useMemo(() => {
    const groups = new Map<number, typeof issues>();
    for (const issue of issues) {
      const list = groups.get(issue.level) ?? [];
      list.push(issue);
      groups.set(issue.level, list);
    }
    return Array.from(groups.entries()).sort((a, b) => a[0] - b[0]);
  }, [issues]);

  function addParameter() {
    if (!parameterName.trim()) return;
    const value = Number(parameterValue);
    if (!Number.isFinite(value)) {
      onError(t("vector.parameter_value_invalid"));
      return;
    }
    const name = parameterName.trim();
    const next = [
      ...ir.parameters.filter((item) => item.name !== name),
      { name, value, unit: "mm" as const, expression: null },
    ];
    onApply([{ op: "set_parameters", parameters: next }]);
    setParameterName("");
    setParameterValue("");
  }

  function addSelectedConstraint() {
    if (!selected || selected.type !== "segment") return;
    onApply([
      {
        op: "set_constraints",
        constraints: [
          ...ir.constraints,
          {
            id: `constraint_${crypto.randomUUID()}`,
            kind: constraintKind,
            refs: [],
            entity_ids: [selected.id],
            value: null,
            parameter: null,
            tolerance: 0.001,
            enabled: true,
          },
        ],
      },
    ]);
  }

  return (
    <div className="rounded border border-white/10 bg-zinc-900/60 p-2 space-y-2">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-xs text-zinc-300">
            {t("vector.validation_title")}
          </div>
          <div
            className={`text-[10px] ${fullCheckCurrent ? "text-emerald-400" : "text-amber-300"}`}
          >
            {fullCheckCurrent
              ? t("vector.full_check_current")
              : t("vector.full_check_required")}
          </div>
        </div>
        <button
          disabled={busy}
          onClick={onRunFullCheck}
          className={`text-[11px] px-2 py-0.5 rounded bg-white/10 hover:bg-white/20 text-zinc-200 disabled:opacity-50 ${
            fullCheckRunning ? "animate-pulse" : ""
          }`}
        >
          {fullCheckRunning
            ? t("vector.full_check_running", { s: fullCheckElapsed })
            : t("vector.run_full_check")}
        </button>
      </div>
      {ir.constraints.length > 0 && (
        <div className="flex items-center justify-between border-t border-white/10 pt-2">
          <div className="text-[11px] text-zinc-400">
            {t("vector.constraints_count", {
              constraints: ir.constraints.length,
              parameters: ir.parameters.length,
            })}
          </div>
          <button
            type="button"
            disabled={busy}
            onClick={onSolve}
            className="rounded bg-sky-600 px-2 py-1 text-[11px] text-white hover:bg-sky-500 disabled:opacity-50"
          >
            {t("vector.rebuild")}
          </button>
        </div>
      )}
      <div className="space-y-2 border-t border-white/10 pt-2">
        <div className="text-[11px] text-zinc-400">
          {t("vector.parameters_title")}
        </div>
        <div className="flex flex-wrap gap-1">
          {ir.parameters.map((parameter) => (
            <button
              key={parameter.name}
              type="button"
              disabled={busy}
              onClick={() => {
                setParameterName(parameter.name);
                setParameterValue(String(parameter.value));
              }}
              className="rounded border border-white/10 px-1.5 py-0.5 font-mono text-[10px] text-zinc-300 hover:bg-white/10"
            >
              {parameter.name}={parameter.value} {parameter.unit}
            </button>
          ))}
        </div>
        <div className="grid grid-cols-[minmax(0,1fr)_86px_auto] gap-1">
          <input
            value={parameterName}
            onChange={(event) => setParameterName(event.target.value)}
            placeholder={t("vector.parameter_name")}
            className="min-w-0 rounded border border-white/10 bg-zinc-950 px-2 py-1 text-[11px] text-zinc-100"
          />
          <input
            value={parameterValue}
            onChange={(event) => setParameterValue(event.target.value)}
            inputMode="decimal"
            placeholder={t("vector.parameter_mm")}
            className="min-w-0 rounded border border-white/10 bg-zinc-950 px-2 py-1 text-[11px] text-zinc-100"
          />
          <button
            type="button"
            disabled={busy}
            onClick={addParameter}
            className="rounded bg-white/10 px-2 py-1 text-[11px] text-zinc-200 hover:bg-white/20 disabled:opacity-50"
          >
            {t("vector.parameter_save")}
          </button>
        </div>
      </div>
      {selected?.type === "segment" && (
        <div className="flex items-center justify-between gap-2 border-t border-white/10 pt-2">
          <select
            value={constraintKind}
            onChange={(event) =>
              setConstraintKind(event.target.value as "horizontal" | "vertical")
            }
            disabled={busy}
            className="rounded border border-white/10 bg-zinc-950 px-2 py-1 text-[11px] text-zinc-200"
          >
            <option value="horizontal">
              {t("vector.constraint_horizontal")}
            </option>
            <option value="vertical">{t("vector.constraint_vertical")}</option>
          </select>
          <button
            type="button"
            disabled={busy}
            onClick={addSelectedConstraint}
            className="rounded bg-white/10 px-2 py-1 text-[11px] text-zinc-200 hover:bg-white/20 disabled:opacity-50"
          >
            {t("vector.constraint_segment")}
          </button>
        </div>
      )}
      {levelGroups.map(([level, levelIssues]) => (
        <div key={level} className="space-y-1">
          <div className="text-[10px] uppercase tracking-wide text-zinc-500">
            {t(`vector.level_${level}`)}
          </div>
          {levelIssues.map((issue, idx) => (
            <div
              key={`${issue.code}-${idx}`}
              className={`text-[11px] pl-1 ${
                issue.severity === "error"
                  ? "text-red-400"
                  : issue.severity === "warn"
                    ? "text-amber-300"
                    : "text-zinc-400"
              }`}
            >
              <span className="font-mono">{issue.code}</span> —{" "}
              {issue.message_ru}
              {issue.entity_ids.length > 0 && (
                <button
                  onClick={() => onFocus(issue.entity_ids[0])}
                  className="ml-1 text-sky-400 hover:text-sky-300"
                >
                  {t("vector.show_entity")}
                </button>
              )}
              {/* C2: ЕСКД citation + concrete fix path from the versioned
                  rule profile, when the issue is standard-backed. */}
              {(issue.norm_ref || issue.fix_hint) && (
                <div className="mt-0.5 pl-1 text-[10px] text-zinc-500">
                  {issue.norm_ref && (
                    <span className="text-zinc-400">{issue.norm_ref}</span>
                  )}
                  {issue.fix_hint && (
                    <span className="block text-zinc-500">
                      → {issue.fix_hint}
                    </span>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      ))}
      {issues.length === 0 && (
        <div className="text-[11px] text-zinc-500">
          {t("vector.validation_no_issues")}
        </div>
      )}
    </div>
  );
}
