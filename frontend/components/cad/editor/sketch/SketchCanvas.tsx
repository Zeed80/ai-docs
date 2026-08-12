"use client";

import { useRef, useState } from "react";

import SketchToolbar, {
  type SketchTool,
} from "@/components/cad/editor/sketch/SketchToolbar";
import { useSketchSolver } from "@/components/cad/editor/sketch/useSketchSolver";
import {
  exportSketchProfile,
  solveSketch,
  type SketchProfileSegment,
} from "@/lib/cad-sketch-api";
import {
  availableConstraints,
  nearestRefs,
  CONSTRAINT_NEEDS_VALUE,
  type ConstraintKind,
} from "@/lib/sketch-geometry";
import type { IrConstraint, IrEntity } from "@/lib/studio-api";

const CONSTRAINT_LABEL: Record<ConstraintKind, string> = {
  horizontal: "Горизонталь",
  vertical: "Вертикаль",
  radius: "Радиус",
  diameter: "Диаметр",
  parallel: "Параллельность",
  perpendicular: "Перпендикулярность",
  angle: "Угол",
  equal: "Равенство",
  concentric: "Соосность",
  distance: "Расстояние",
  coincident: "Совпадение",
};

const HIT_TOLERANCE_MM = 2.5;
const SNAP_TOLERANCE_MM = 3;
const VIEW = { minX: -20, minY: -20, size: 240 }; // mm, y-down (same convention CadIR itself uses)

function newId(): string {
  return `sketch_${crypto.randomUUID().slice(0, 12)}`;
}

function distToSegment(
  p: { x: number; y: number },
  a: { x: number; y: number },
  b: { x: number; y: number },
): number {
  const l2 = (b.x - a.x) ** 2 + (b.y - a.y) ** 2;
  if (l2 === 0) return Math.hypot(p.x - a.x, p.y - a.y);
  let t = ((p.x - a.x) * (b.x - a.x) + (p.y - a.y) * (b.y - a.y)) / l2;
  t = Math.max(0, Math.min(1, t));
  const proj = { x: a.x + t * (b.x - a.x), y: a.y + t * (b.y - a.y) };
  return Math.hypot(p.x - proj.x, p.y - proj.y);
}

function nearestEntity(
  point: { x: number; y: number },
  entities: IrEntity[],
): string | null {
  let best: string | null = null;
  let bestD = HIT_TOLERANCE_MM;
  for (const e of entities) {
    let d = Infinity;
    if (e.type === "segment" && e.p1 && e.p2) {
      d = distToSegment(point, e.p1, e.p2);
    } else if (e.type === "circle" && e.center && e.radius) {
      d = Math.abs(
        Math.hypot(point.x - e.center.x, point.y - e.center.y) - e.radius,
      );
    }
    if (d < bestD) {
      bestD = d;
      best = e.id;
    }
  }
  return best;
}

/** Ф4 нового CAD-редактора: a scoped, ephemeral sketch session — draw a
 * closed profile (line/rectangle/circle), constrain it, solve, export as
 * a boss/pocket's sketch_profile. Nothing here is persisted server-side
 * until onExported fires; closing/cancelling this component discards
 * everything, which is correct — a sketch-in-progress was never a
 * commitment (see the module's own note in the plan). */
export default function SketchCanvas({
  onCancel,
  onExported,
  onError,
}: {
  onCancel: () => void;
  onExported: (profile: SketchProfileSegment[]) => void;
  onError: (message: string) => void;
}) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [entities, setEntities] = useState<IrEntity[]>([]);
  const [constraints, setConstraints] = useState<IrConstraint[]>([]);
  const [tool, setTool] = useState<SketchTool>("line");
  const [chain, setChain] = useState<{
    start: { x: number; y: number };
    current: { x: number; y: number };
  } | null>(null);
  const [pendingFirst, setPendingFirst] = useState<{
    x: number;
    y: number;
  } | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [constraintValue, setConstraintValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [solveMessage, setSolveMessage] = useState<string | null>(null);

  const { checks, dof } = useSketchSolver(entities, constraints);
  const violatedEntityIds = new Set(
    checks.filter((c) => !c.ok).flatMap((c) => c.entity_ids),
  );

  function svgPoint(event: React.MouseEvent): { x: number; y: number } | null {
    const svg = svgRef.current;
    if (!svg) return null;
    const rect = svg.getBoundingClientRect();
    const fx = (event.clientX - rect.left) / rect.width;
    const fy = (event.clientY - rect.top) / rect.height;
    return { x: VIEW.minX + fx * VIEW.size, y: VIEW.minY + fy * VIEW.size };
  }

  function addEntity(entity: IrEntity) {
    setEntities((current) => [...current, entity]);
  }

  function handleCanvasClick(event: React.MouseEvent) {
    const point = svgPoint(event);
    if (!point) return;

    if (tool === "select") {
      const hit = nearestEntity(point, entities);
      if (!hit) {
        setSelected([]);
        return;
      }
      setSelected((current) => {
        if (current.includes(hit)) return current.filter((id) => id !== hit);
        if (current.length >= 2) return [hit];
        return [...current, hit];
      });
      return;
    }

    if (tool === "line") {
      if (!chain) {
        setChain({ start: point, current: point });
        return;
      }
      const closing =
        Math.hypot(point.x - chain.start.x, point.y - chain.start.y) <=
        SNAP_TOLERANCE_MM;
      const to = closing ? chain.start : point;
      addEntity({
        id: newId(),
        type: "segment",
        line_class: "contour",
        width_class: "main",
        confidence: 1,
        origin: "human",
        assurance: "human_approved",
        p1: chain.current,
        p2: to,
      } as IrEntity);
      if (closing) {
        setChain(null);
        setTool("select");
      } else {
        setChain({ start: chain.start, current: to });
      }
      return;
    }

    if (tool === "rectangle") {
      if (!pendingFirst) {
        setPendingFirst(point);
        return;
      }
      const a = pendingFirst;
      const c = point;
      const corners = [
        { x: a.x, y: a.y },
        { x: c.x, y: a.y },
        { x: c.x, y: c.y },
        { x: a.x, y: c.y },
      ];
      for (let i = 0; i < 4; i++) {
        addEntity({
          id: newId(),
          type: "segment",
          line_class: "contour",
          width_class: "main",
          confidence: 1,
          origin: "human",
          assurance: "human_approved",
          p1: corners[i],
          p2: corners[(i + 1) % 4],
        } as IrEntity);
      }
      setPendingFirst(null);
      setTool("select");
      return;
    }

    if (tool === "circle") {
      if (!pendingFirst) {
        setPendingFirst(point);
        return;
      }
      const radius = Math.hypot(
        point.x - pendingFirst.x,
        point.y - pendingFirst.y,
      );
      if (radius > 0.1) {
        addEntity({
          id: newId(),
          type: "circle",
          line_class: "contour",
          width_class: "main",
          confidence: 1,
          origin: "human",
          assurance: "human_approved",
          center: pendingFirst,
          radius,
        } as IrEntity);
      }
      setPendingFirst(null);
      setTool("select");
    }
  }

  const selectedEntities = entities.filter((e) => selected.includes(e.id));
  const availableKinds = availableConstraints(selectedEntities);

  function addConstraint(kind: ConstraintKind) {
    let value: number | null = CONSTRAINT_NEEDS_VALUE.has(kind)
      ? Number(constraintValue.replace(",", "."))
      : null;
    if (
      CONSTRAINT_NEEDS_VALUE.has(kind) &&
      (!Number.isFinite(value) || (value ?? 0) < 0)
    ) {
      onError("Укажите числовое значение ограничения.");
      return;
    }
    const base: IrConstraint = {
      id: newId(),
      kind,
      refs: [],
      entity_ids: [],
      value: null,
      parameter: null,
      tolerance: 0.001,
      enabled: true,
    };
    if (kind === "coincident" || kind === "distance") {
      const pair = nearestRefs(selectedEntities[0], selectedEntities[1]);
      if (!pair) {
        onError("Не удалось определить точки для этого ограничения.");
        return;
      }
      base.refs = pair;
      if (kind === "distance" && value === null) value = 0;
    } else {
      base.entity_ids = selectedEntities.map((e) => e.id);
    }
    if (value !== null) base.value = value;
    setConstraints((current) => [...current, base]);
    setConstraintValue("");
  }

  async function handleSolve() {
    setBusy(true);
    setSolveMessage(null);
    try {
      const result = await solveSketch(entities, constraints);
      setEntities(result.entities);
      setSolveMessage(
        result.converged
          ? "Решено — ограничения согласованы"
          : `Не удалось согласовать: ${result.message}`,
      );
      if (!result.converged) onError(`Решатель: ${result.message}`);
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy(false);
    }
  }

  async function handleExport() {
    setBusy(true);
    try {
      const result = await exportSketchProfile(entities);
      onExported(result.sketch_profile);
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy(false);
    }
  }

  function undoLast() {
    setEntities((current) => current.slice(0, -1));
    setChain(null);
  }

  function clearAll() {
    setEntities([]);
    setConstraints([]);
    setSelected([]);
    setChain(null);
    setPendingFirst(null);
  }

  const gridLines = [];
  for (
    let x = Math.ceil(VIEW.minX / 10) * 10;
    x <= VIEW.minX + VIEW.size;
    x += 10
  ) {
    gridLines.push(
      <line
        key={`gx${x}`}
        x1={x}
        y1={VIEW.minY}
        x2={x}
        y2={VIEW.minY + VIEW.size}
        stroke="#27272a"
        strokeWidth={0.3}
      />,
    );
  }
  for (
    let y = Math.ceil(VIEW.minY / 10) * 10;
    y <= VIEW.minY + VIEW.size;
    y += 10
  ) {
    gridLines.push(
      <line
        key={`gy${y}`}
        x1={VIEW.minX}
        y1={y}
        x2={VIEW.minX + VIEW.size}
        y2={y}
        stroke="#27272a"
        strokeWidth={0.3}
      />,
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <SketchToolbar
        active={tool}
        onChange={(next) => {
          setTool(next);
          setChain(null);
          setPendingFirst(null);
        }}
      />
      <div className="flex min-h-0 flex-1">
        <div className="relative min-h-0 flex-1 bg-zinc-950">
          <svg
            ref={svgRef}
            viewBox={`${VIEW.minX} ${VIEW.minY} ${VIEW.size} ${VIEW.size}`}
            className="h-full w-full cursor-crosshair"
            onClick={handleCanvasClick}
          >
            {gridLines}
            <line
              x1={VIEW.minX}
              y1={0}
              x2={VIEW.minX + VIEW.size}
              y2={0}
              stroke="#3f3f46"
              strokeWidth={0.6}
            />
            <line
              x1={0}
              y1={VIEW.minY}
              x2={0}
              y2={VIEW.minY + VIEW.size}
              stroke="#3f3f46"
              strokeWidth={0.6}
            />
            {entities.map((e) => {
              const isSelected = selected.includes(e.id);
              const isViolated = violatedEntityIds.has(e.id);
              const stroke = isSelected
                ? "#38bdf8"
                : isViolated
                  ? "#f59e0b"
                  : "#e4e4e7";
              if (e.type === "segment" && e.p1 && e.p2) {
                return (
                  <line
                    key={e.id}
                    x1={e.p1.x}
                    y1={e.p1.y}
                    x2={e.p2.x}
                    y2={e.p2.y}
                    stroke={stroke}
                    strokeWidth={isSelected ? 1.4 : 1}
                  />
                );
              }
              if (e.type === "circle" && e.center && e.radius) {
                return (
                  <circle
                    key={e.id}
                    cx={e.center.x}
                    cy={e.center.y}
                    r={e.radius}
                    fill="none"
                    stroke={stroke}
                    strokeWidth={isSelected ? 1.4 : 1}
                  />
                );
              }
              return null;
            })}
            {chain && (
              <circle
                cx={chain.current.x}
                cy={chain.current.y}
                r={1.5}
                fill="#38bdf8"
              />
            )}
            {pendingFirst && (
              <circle
                cx={pendingFirst.x}
                cy={pendingFirst.y}
                r={1.5}
                fill="#38bdf8"
              />
            )}
          </svg>
          <div className="pointer-events-none absolute left-2 top-2 rounded bg-zinc-950/80 px-2 py-1 text-[10px] text-zinc-400">
            {tool === "line" &&
              (chain
                ? "Кликните следующую точку контура (рядом с началом — замкнёт контур)"
                : "Кликните первую точку контура")}
            {tool === "rectangle" &&
              (pendingFirst
                ? "Кликните противоположный угол"
                : "Кликните первый угол")}
            {tool === "circle" &&
              (pendingFirst
                ? "Кликните точку на окружности (радиус)"
                : "Кликните центр")}
            {tool === "select" && "Кликните элемент (до 2 для ограничения)"}
          </div>
        </div>

        <aside className="w-64 shrink-0 overflow-y-auto border-l border-white/10 bg-zinc-900/60 p-2 text-xs">
          <div className="mb-2 flex flex-wrap gap-1.5">
            <button
              type="button"
              onClick={undoLast}
              disabled={entities.length === 0}
              className="rounded border border-white/10 px-2 py-1 text-[11px] text-zinc-300 hover:bg-white/5 disabled:opacity-40"
            >
              ↶ Отменить
            </button>
            <button
              type="button"
              onClick={clearAll}
              disabled={entities.length === 0}
              className="rounded border border-white/10 px-2 py-1 text-[11px] text-zinc-300 hover:bg-white/5 disabled:opacity-40"
            >
              Очистить
            </button>
          </div>

          {selectedEntities.length > 0 && (
            <div className="mb-2 space-y-1.5 border-t border-white/10 pt-2">
              <p className="text-[11px] text-zinc-500">
                Выбрано: {selectedEntities.length}
              </p>
              {availableKinds.some((k) => CONSTRAINT_NEEDS_VALUE.has(k)) && (
                <input
                  value={constraintValue}
                  onChange={(event) => setConstraintValue(event.target.value)}
                  placeholder="значение, мм"
                  inputMode="decimal"
                  className="w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-zinc-100"
                />
              )}
              <div className="flex flex-wrap gap-1">
                {availableKinds.map((k) => (
                  <button
                    key={k}
                    type="button"
                    onClick={() => addConstraint(k)}
                    className="rounded bg-white/5 px-2 py-0.5 text-[11px] text-zinc-200 hover:bg-white/15"
                  >
                    {CONSTRAINT_LABEL[k]}
                  </button>
                ))}
              </div>
            </div>
          )}

          {constraints.length > 0 && (
            <div className="mb-2 space-y-1 border-t border-white/10 pt-2">
              <p className="text-[11px] text-zinc-500">
                Ограничения: {constraints.length}
                {dof && (
                  <span
                    className={`ml-1 ${
                      dof.state === "well_constrained"
                        ? "text-emerald-400"
                        : dof.state === "over_constrained"
                          ? "text-amber-300"
                          : "text-sky-300"
                    }`}
                  >
                    · {dof.state}
                    {dof.dof > 0 ? ` (${dof.dof} своб.)` : ""}
                  </span>
                )}
              </p>
              <ul className="space-y-0.5">
                {constraints.map((c) => {
                  const chk = checks.find(
                    (item) => item.constraint_id === c.id,
                  );
                  return (
                    <li key={c.id} className="flex items-center gap-1.5">
                      <span
                        className={
                          chk
                            ? chk.ok
                              ? "text-emerald-400"
                              : "text-amber-300"
                            : "text-zinc-500"
                        }
                      >
                        {chk ? (chk.ok ? "✓" : "✗") : "•"}
                      </span>
                      <span className="flex-1 truncate text-zinc-300">
                        {CONSTRAINT_LABEL[c.kind as ConstraintKind] ?? c.kind}
                        {c.value !== null ? ` = ${c.value}` : ""}
                      </span>
                      <button
                        type="button"
                        onClick={() =>
                          setConstraints((current) =>
                            current.filter((item) => item.id !== c.id),
                          )
                        }
                        className="text-zinc-500 hover:text-red-300"
                      >
                        ✕
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}

          <div className="space-y-1.5 border-t border-white/10 pt-2">
            <button
              type="button"
              disabled={busy || constraints.length === 0}
              onClick={() => void handleSolve()}
              className="w-full rounded border border-sky-400/30 bg-sky-500/10 px-2 py-1.5 text-[11px] text-sky-200 hover:bg-sky-500/20 disabled:opacity-40"
            >
              Решить
            </button>
            {solveMessage && (
              <p className="text-[10px] text-zinc-400">{solveMessage}</p>
            )}
            <button
              type="button"
              disabled={busy || entities.length === 0}
              onClick={() => void handleExport()}
              className="w-full rounded bg-emerald-500/20 px-2 py-1.5 text-[11px] text-emerald-200 hover:bg-emerald-500/30 disabled:opacity-40"
            >
              Использовать этот контур
            </button>
            <button
              type="button"
              onClick={onCancel}
              className="w-full rounded border border-white/10 px-2 py-1.5 text-[11px] text-zinc-400 hover:bg-white/5"
            >
              Отмена
            </button>
          </div>
        </aside>
      </div>
    </div>
  );
}
