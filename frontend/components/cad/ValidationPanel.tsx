"use client";

import { useMemo, useState } from "react";

import { CadIr, IrPatchOp } from "@/lib/studio-api";

/** Validation report grouped by assurance level + the sketch parameter editor
 * and the long-running full check. Geometric constraints live in
 * ConstraintsPanel; parameter input state lives here; every mutation still
 * flows through the parent's apply()/API callbacks so revision history stays
 * in one place. */
export default function ValidationPanel({
  ir,
  busy,
  fullCheckCurrent,
  fullCheckRunning,
  fullCheckElapsed,
  onRunFullCheck,
  onApply,
  onFocus,
  onError,
  t,
}: {
  ir: CadIr;
  busy: boolean;
  fullCheckCurrent: boolean;
  fullCheckRunning: boolean;
  fullCheckElapsed: number;
  onRunFullCheck: () => void;
  onApply: (ops: IrPatchOp[]) => void;
  onFocus: (entityId: string) => void;
  onError: (message: string) => void;
  t: (k: string, v?: Record<string, string | number>) => string;
}) {
  const [parameterName, setParameterName] = useState("");
  const [parameterValue, setParameterValue] = useState("");

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
    const name = parameterName.trim();
    if (!name) return;
    const raw = parameterValue.trim();
    const num = Number(raw.replace(",", "."));
    // A1: a numeric entry is a plain value; anything else (e.g. "2*height+5")
    // is stored as an expression and resolved on the backend.
    const isExpr = raw !== "" && !Number.isFinite(num);
    if (!isExpr && !Number.isFinite(num)) {
      onError(t("vector.parameter_value_invalid"));
      return;
    }
    const param = isExpr
      ? { name, value: 0, unit: "mm" as const, expression: raw }
      : { name, value: num, unit: "mm" as const, expression: null };
    const next = [...ir.parameters.filter((item) => item.name !== name), param];
    onApply([{ op: "set_parameters", parameters: next }]);
    setParameterName("");
    setParameterValue("");
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
                setParameterValue(
                  parameter.expression ?? String(parameter.value),
                );
              }}
              title={
                parameter.expression
                  ? `${parameter.expression} = ${parameter.value}`
                  : undefined
              }
              className="rounded border border-white/10 px-1.5 py-0.5 font-mono text-[10px] text-zinc-300 hover:bg-white/10"
            >
              {parameter.name}={parameter.value}
              {parameter.expression ? " ƒ" : ""} {parameter.unit}
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
            placeholder={t("vector.parameter_value_or_expr")}
            title={t("vector.parameter_expr_hint")}
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
        {/* A1: named configurations — a family of parameter sets. */}
        {ir.parameters.length > 0 && (
          <div className="space-y-1 pt-1">
            <div className="flex flex-wrap items-center gap-1">
              <span className="text-[10px] text-zinc-500">
                {t("vector.configurations")}
              </span>
              {(ir.configurations ?? []).map((cfg) => (
                <span
                  key={cfg.name}
                  className="flex items-center gap-0.5 rounded border border-white/10 bg-white/5 text-[10px]"
                >
                  <button
                    type="button"
                    disabled={busy}
                    title={t("vector.configuration_apply")}
                    onClick={() =>
                      onApply([
                        { op: "apply_configuration", config_name: cfg.name },
                      ])
                    }
                    className="px-1.5 py-0.5 text-zinc-200 hover:text-sky-300"
                  >
                    {cfg.name}
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    title={t("vector.configuration_delete")}
                    onClick={() =>
                      onApply([
                        {
                          op: "set_configurations",
                          configurations: (ir.configurations ?? []).filter(
                            (c) => c.name !== cfg.name,
                          ),
                        },
                      ])
                    }
                    className="pr-1 text-zinc-500 hover:text-red-300"
                  >
                    ✕
                  </button>
                </span>
              ))}
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  const name = window.prompt(t("vector.configuration_name"));
                  if (!name?.trim()) return;
                  const values: Record<string, number> = {};
                  for (const p of ir.parameters) {
                    if (!p.expression) values[p.name] = p.value;
                  }
                  const others = (ir.configurations ?? []).filter(
                    (c) => c.name !== name.trim(),
                  );
                  onApply([
                    {
                      op: "set_configurations",
                      configurations: [
                        ...others,
                        { name: name.trim(), values },
                      ],
                    },
                  ]);
                }}
                className="rounded bg-white/5 px-1.5 py-0.5 text-[10px] text-zinc-300 hover:bg-white/10"
              >
                {t("vector.configuration_save")}
              </button>
            </div>
          </div>
        )}
      </div>
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
