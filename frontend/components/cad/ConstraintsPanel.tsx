"use client";

import { useCallback, useEffect, useState } from "react";

import {
  evaluateConstraints,
  type CadIr,
  type ConstraintCheck,
  type DofReport,
  type IrEntity,
  type IrPatchOp,
} from "@/lib/studio-api";
import {
  availableConstraints,
  nearestRefs,
  CONSTRAINT_NEEDS_VALUE,
  type ConstraintKind as Kind,
  type PointRef,
} from "@/lib/sketch-geometry";

const NEEDS_VALUE = CONSTRAINT_NEEDS_VALUE;
const available = availableConstraints;

/** A1: full geometric-constraint editor — pick 1-2 entities, add any solver-
 * backed constraint, then review the live constraint list (satisfied/violated
 * status, enable, delete, click-to-highlight). Complements ValidationPanel's
 * Rebuild/parameters. */
export default function ConstraintsPanel({
  ir,
  genId,
  selected,
  busy,
  onApply,
  onSolve,
  onFocus,
  onError,
  t,
}: {
  ir: CadIr;
  genId: string;
  selected: IrEntity[];
  busy: boolean;
  onApply: (ops: IrPatchOp[]) => void;
  onSolve: () => void;
  onFocus: (entityId: string) => void;
  onError: (msg: string) => void;
  t: (k: string, v?: Record<string, string | number>) => string;
}) {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const [checks, setChecks] = useState<Record<string, ConstraintCheck>>({});
  const [dof, setDof] = useState<DofReport | null>(null);

  const refreshChecks = useCallback(async () => {
    if (!ir.constraints.length) {
      setChecks({});
      setDof(null);
      return;
    }
    try {
      const res = await evaluateConstraints(genId);
      const map: Record<string, ConstraintCheck> = {};
      for (const c of res.checks) map[c.constraint_id] = c;
      setChecks(map);
      setDof(res.dof ?? null);
    } catch {
      // status is a nicety — a failed probe must not break the panel
    }
  }, [genId, ir.constraints.length]);

  useEffect(() => {
    if (open) void refreshChecks();
  }, [open, refreshChecks, ir]);

  const kinds = available(selected);

  function add(kind: Kind) {
    const base = {
      id: `constraint_${crypto.randomUUID()}`,
      kind,
      refs: [] as PointRef[],
      entity_ids: [] as string[],
      value: null as number | null,
      parameter: null,
      tolerance: 0.001,
      enabled: true,
    };
    let target = NEEDS_VALUE.has(kind) ? Number(value.replace(",", ".")) : null;
    if (
      NEEDS_VALUE.has(kind) &&
      (!Number.isFinite(target) || (target ?? 0) < 0)
    ) {
      onError(t("vector.constraint_value_required"));
      return;
    }
    if (kind === "coincident" || kind === "distance") {
      const pair = nearestRefs(selected[0], selected[1]);
      if (!pair) {
        onError(t("vector.constraint_no_refs"));
        return;
      }
      base.refs = pair;
      // distance between two centres/endpoints: seed with the current gap so
      // "add then rebuild" doesn't yank geometry unless the user changed it.
      if (kind === "distance" && target === null) target = 0;
    } else {
      base.entity_ids = selected.map((e) => e.id);
    }
    if (target !== null) base.value = target;
    onApply([
      { op: "set_constraints", constraints: [...ir.constraints, base] },
    ]);
    setValue("");
  }

  function toggle(id: string, enabled: boolean) {
    onApply([
      {
        op: "set_constraints",
        constraints: ir.constraints.map((c) =>
          c.id === id ? { ...c, enabled } : c,
        ),
      },
    ]);
  }

  function remove(id: string) {
    onApply([
      {
        op: "set_constraints",
        constraints: ir.constraints.filter((c) => c.id !== id),
      },
    ]);
  }

  function toggleDriven(id: string) {
    onApply([
      {
        op: "set_constraints",
        constraints: ir.constraints.map((c) =>
          c.id === id ? { ...c, driven: !c.driven } : c,
        ),
      },
    ]);
  }

  const violated = Object.values(checks).filter((c) => !c.ok).length;

  return (
    <div className="space-y-2 rounded border border-white/10 bg-zinc-900/60 p-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between text-xs text-zinc-300"
      >
        <span>
          {t("vector.constraints_panel")}
          {ir.constraints.length > 0 && (
            <span
              className={`ml-1 ${violated ? "text-amber-300" : "text-emerald-400"}`}
            >
              ({ir.constraints.length}
              {violated
                ? ` · ${t("vector.constraints_violated", { n: violated })}`
                : ""}
              )
            </span>
          )}
        </span>
        <span className="text-zinc-500">{open ? "−" : "+"}</span>
      </button>
      {open && (
        <div className="space-y-2">
          {/* palette */}
          {selected.length === 0 || kinds.length === 0 ? (
            <p className="text-[11px] text-zinc-500">
              {t("vector.constraints_select_hint")}
            </p>
          ) : (
            <div className="space-y-1.5">
              {kinds.some((k) => NEEDS_VALUE.has(k)) && (
                <input
                  value={value}
                  onChange={(e) => setValue(e.target.value)}
                  placeholder={t("vector.constraint_value_placeholder")}
                  inputMode="decimal"
                  className="w-full rounded border border-white/10 bg-zinc-950 px-2 py-1 text-xs text-zinc-100"
                />
              )}
              <div className="flex flex-wrap gap-1">
                {kinds.map((k) => (
                  <button
                    key={k}
                    type="button"
                    disabled={busy}
                    onClick={() => add(k)}
                    title={t(`vector.constraint_${k}`)}
                    className="rounded bg-white/5 px-2 py-0.5 text-[11px] text-zinc-200 hover:bg-white/15 disabled:opacity-40"
                  >
                    {t(`vector.constraint_${k}`)}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* DOF / conflict report */}
          {dof && ir.constraints.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5 border-t border-white/10 pt-2 text-[11px]">
              <span
                className={`rounded px-1.5 py-0.5 ${
                  dof.state === "well_constrained"
                    ? "bg-emerald-500/15 text-emerald-300"
                    : dof.state === "over_constrained"
                      ? "bg-amber-500/15 text-amber-300"
                      : "bg-sky-500/15 text-sky-300"
                }`}
                title={`${dof.unknowns} ${t("vector.dof_unknowns")} − ${dof.rank} ${t("vector.dof_rank")}`}
              >
                {t(`vector.dof_${dof.state}`)}
                {dof.dof > 0
                  ? ` · ${t("vector.dof_count", { n: dof.dof })}`
                  : ""}
              </span>
              {dof.redundant && (
                <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-amber-300">
                  {t("vector.dof_redundant")}
                </span>
              )}
              {dof.conflict && (
                <span className="rounded bg-red-500/15 px-1.5 py-0.5 text-red-300">
                  {t("vector.dof_conflict")}
                </span>
              )}
            </div>
          )}

          {/* constraint list */}
          {ir.constraints.length > 0 && (
            <ul className="space-y-1 border-t border-white/10 pt-2">
              {ir.constraints.map((c) => {
                const chk = checks[c.id];
                const ids = c.entity_ids.length
                  ? c.entity_ids
                  : c.refs.map((r) => r.entity_id);
                return (
                  <li
                    key={c.id}
                    className="flex items-center gap-1.5 text-[11px]"
                  >
                    <span
                      title={chk?.message}
                      className={
                        !c.enabled
                          ? "text-zinc-400"
                          : chk
                            ? chk.ok
                              ? "text-emerald-400"
                              : "text-amber-300"
                            : "text-zinc-500"
                      }
                    >
                      {!c.enabled ? "○" : chk ? (chk.ok ? "✓" : "✗") : "•"}
                    </span>
                    <button
                      type="button"
                      onClick={() => ids[0] && onFocus(ids[0])}
                      className={`flex-1 truncate text-left hover:text-sky-300 ${
                        c.driven ? "text-zinc-500 italic" : "text-zinc-300"
                      }`}
                    >
                      {t(`vector.constraint_${c.kind}`)}
                      {c.value !== null
                        ? ` ${c.driven ? "≈" : "="} ${c.value}`
                        : ""}
                    </button>
                    {c.value !== null && (
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => toggleDriven(c.id)}
                        title={t("vector.constraint_driven_hint")}
                        className={`rounded px-1 text-[10px] disabled:opacity-40 ${
                          c.driven
                            ? "bg-zinc-500/20 text-zinc-300"
                            : "bg-sky-500/15 text-sky-300"
                        }`}
                      >
                        {c.driven
                          ? t("vector.constraint_driven")
                          : t("vector.constraint_driving")}
                      </button>
                    )}
                    <label
                      className="flex items-center"
                      title={t("vector.constraint_enabled")}
                    >
                      <input
                        type="checkbox"
                        checked={c.enabled}
                        disabled={busy}
                        onChange={(e) => toggle(c.id, e.target.checked)}
                      />
                    </label>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => remove(c.id)}
                      title={t("vector.constraint_delete")}
                      className="rounded px-1 text-zinc-500 hover:bg-white/10 hover:text-red-300 disabled:opacity-40"
                    >
                      ✕
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          {ir.constraints.length > 0 && (
            <button
              type="button"
              disabled={busy}
              onClick={onSolve}
              className="w-full rounded bg-sky-600 px-2 py-1 text-[11px] text-white hover:bg-sky-500 disabled:opacity-50"
            >
              {t("vector.rebuild")}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
