"use client";

import { useCallback, useEffect, useState } from "react";

import {
  evaluateConstraints,
  type CadIr,
  type ConstraintCheck,
  type IrEntity,
  type IrPatchOp,
} from "@/lib/studio-api";

type Kind =
  | "horizontal"
  | "vertical"
  | "radius"
  | "diameter"
  | "parallel"
  | "perpendicular"
  | "angle"
  | "equal"
  | "concentric"
  | "distance"
  | "coincident";

type PointRef = { entity_id: string; point: "p1" | "p2" | "center" };

const NEEDS_VALUE: Set<Kind> = new Set([
  "radius",
  "diameter",
  "angle",
  "distance",
]);

/** Endpoints/centre a constraint can reference on a given entity. */
function refPoints(e: IrEntity): PointRef[] {
  if (e.type === "segment")
    return [
      { entity_id: e.id, point: "p1" },
      { entity_id: e.id, point: "p2" },
    ];
  if (e.type === "circle" || e.type === "arc")
    return [{ entity_id: e.id, point: "center" }];
  return [];
}

function coord(e: IrEntity, ref: PointRef): { x: number; y: number } | null {
  if (ref.point === "center" && e.center) return e.center;
  if (ref.point === "p1" && e.p1) return e.p1;
  if (ref.point === "p2" && e.p2) return e.p2;
  return null;
}

/** The closest pair of reference points between two entities — what a user
 * means by "make these coincident / this far apart" when they pick two lines. */
function nearestRefs(a: IrEntity, b: IrEntity): [PointRef, PointRef] | null {
  let best: [PointRef, PointRef] | null = null;
  let bestD = Infinity;
  for (const ra of refPoints(a)) {
    const pa = coord(a, ra);
    if (!pa) continue;
    for (const rb of refPoints(b)) {
      const pb = coord(b, rb);
      if (!pb) continue;
      const d = Math.hypot(pa.x - pb.x, pa.y - pb.y);
      if (d < bestD) {
        bestD = d;
        best = [ra, rb];
      }
    }
  }
  return best;
}

/** Which constraints the current selection admits (arity + entity types). */
function available(sel: IrEntity[]): Kind[] {
  if (sel.length === 1) {
    const e = sel[0];
    if (e.type === "segment") return ["horizontal", "vertical"];
    if (e.type === "circle" || e.type === "arc") return ["radius", "diameter"];
    return [];
  }
  if (sel.length === 2) {
    const segs = sel.filter((e) => e.type === "segment").length;
    const circ = sel.filter(
      (e) => e.type === "circle" || e.type === "arc",
    ).length;
    const kinds: Kind[] = ["coincident", "distance"];
    if (segs === 2) kinds.push("parallel", "perpendicular", "angle", "equal");
    if (circ === 2) kinds.push("concentric", "equal");
    return kinds;
  }
  return [];
}

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

  const refreshChecks = useCallback(async () => {
    if (!ir.constraints.length) {
      setChecks({});
      return;
    }
    try {
      const res = await evaluateConstraints(genId);
      const map: Record<string, ConstraintCheck> = {};
      for (const c of res.checks) map[c.constraint_id] = c;
      setChecks(map);
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
                          ? "text-zinc-600"
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
                      className="flex-1 truncate text-left text-zinc-300 hover:text-sky-300"
                    >
                      {t(`vector.constraint_${c.kind}`)}
                      {c.value !== null ? ` = ${c.value}` : ""}
                    </button>
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
