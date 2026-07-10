"use client";

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  CadIr,
  Generation,
  IrEntity,
  IrPatchOp,
  acceptVectorize,
  artifactUrl,
  getIr,
  patchIr,
  revertIr,
  runFullCheck,
  sourceUrl,
} from "@/lib/studio-api";

type Tool =
  | "select"
  | "line"
  | "circle"
  | "text"
  | "dim_linear"
  | "dim_diameter"
  | "dim_radial"
  | "mirror"
  | "fillet"
  | "chamfer"
  | "polyline"
  | "hatch";

const DIM_TOOL_KIND: Record<string, "linear" | "diameter" | "radial"> = {
  dim_linear: "linear",
  dim_diameter: "diameter",
  dim_radial: "radial",
};

// ГОСТ 2.307 arrow length (2.5mm); falls back to a fixed px length when the
// sheet has no known scale yet — mirrors backend/app/ai/cad_ir/dim_render.py.
function dimArrowLenPx(scale: number | null): number {
  return scale ? 2.5 / scale : 8;
}

/** Ø/R kind prefix on the displayed label — mirrors dim_render.dimension_label. */
function dimensionLabel(e: IrEntity): string {
  const base = e.text || (e.value_mm != null ? String(e.value_mm) : "");
  if (!base) return "";
  const upper = base.toUpperCase();
  if (e.kind === "diameter" && !base.includes("⌀") && !upper.includes("Ø"))
    return `⌀${base}`;
  if (e.kind === "radial" && !upper.trimStart().startsWith("R"))
    return `R${base}`;
  return base;
}

/** Filled arrowhead triangle point-lists for a dimension line — mirrors
 * dim_render.dimension_arrows_for_points. */
function dimensionArrowPolygons(
  p1: { x: number; y: number },
  p2: { x: number; y: number },
  kind: string | undefined,
  length: number,
): string[] {
  const dx = p2.x - p1.x;
  const dy = p2.y - p1.y;
  const norm = Math.hypot(dx, dy) || 1;
  const ux = dx / norm;
  const uy = dy / norm;
  const px = -uy;
  const py = ux;
  const halfW = length * 0.28;
  const tri = (tipX: number, tipY: number, dirX: number, dirY: number) => {
    const baseX = tipX - dirX * length;
    const baseY = tipY - dirY * length;
    return `${tipX},${tipY} ${baseX + px * halfW},${baseY + py * halfW} ${baseX - px * halfW},${baseY - py * halfW}`;
  };
  if (kind === "radial") return [tri(p2.x, p2.y, ux, uy)];
  return [tri(p1.x, p1.y, -ux, -uy), tri(p2.x, p2.y, ux, uy)];
}

interface Props {
  gen: Generation;
  onChanged: () => void;
}

/** Snap helper: endpoints of existing entities + ortho to the anchor point. */
/** Where two segments (as drawn, not extended to infinite lines) cross —
 * null if parallel or they don't actually meet within both spans. */
function segmentIntersection(
  a1: { x: number; y: number },
  a2: { x: number; y: number },
  b1: { x: number; y: number },
  b2: { x: number; y: number },
): { x: number; y: number } | null {
  const d1x = a2.x - a1.x;
  const d1y = a2.y - a1.y;
  const d2x = b2.x - b1.x;
  const d2y = b2.y - b1.y;
  const denom = d1x * d2y - d1y * d2x;
  if (Math.abs(denom) < 1e-9) return null;
  const t = ((b1.x - a1.x) * d2y - (b1.y - a1.y) * d2x) / denom;
  const u = ((b1.x - a1.x) * d1y - (b1.y - a1.y) * d1x) / denom;
  if (t < -0.01 || t > 1.01 || u < -0.01 || u > 1.01) return null;
  return { x: a1.x + t * d1x, y: a1.y + t * d1y };
}

/** The (up to two) points on a circle where a line from `anchor` is
 * tangent — classic CAD snap for drawing a line/dimension leader tangent to
 * an existing circle/arc from an external point. */
function tangentPoints(
  anchor: { x: number; y: number },
  center: { x: number; y: number },
  r: number,
): { x: number; y: number }[] {
  const dx = anchor.x - center.x;
  const dy = anchor.y - center.y;
  const d = Math.hypot(dx, dy);
  if (d <= r + 1e-6) return []; // anchor inside/on the circle: no external tangent
  const alpha = Math.atan2(dy, dx);
  const phi = Math.acos(r / d);
  return [
    {
      x: center.x + r * Math.cos(alpha + phi),
      y: center.y + r * Math.sin(alpha + phi),
    },
    {
      x: center.x + r * Math.cos(alpha - phi),
      y: center.y + r * Math.sin(alpha - phi),
    },
  ];
}

function snapPoint(
  ir: CadIr,
  x: number,
  y: number,
  anchor: { x: number; y: number } | null,
  tolPx: number,
): { x: number; y: number } {
  let best: { x: number; y: number } | null = null;
  let bestD = tolPx;
  const consider = (px?: { x: number; y: number } | null) => {
    if (!px) return;
    const d = Math.hypot(px.x - x, px.y - y);
    if (d < bestD) {
      bestD = d;
      best = { x: px.x, y: px.y };
    }
  };
  const segments: {
    p1: { x: number; y: number };
    p2: { x: number; y: number };
  }[] = [];
  for (const e of ir.entities) {
    consider(e.p1);
    consider(e.p2);
    consider(e.center);
    for (const p of e.points ?? []) consider(p);
    if (e.type === "segment" && e.p1 && e.p2)
      segments.push({ p1: e.p1, p2: e.p2 });
  }
  // Intersections between existing segments (corners two contours would meet at).
  for (let i = 0; i < segments.length; i++) {
    for (let j = i + 1; j < segments.length; j++) {
      consider(
        segmentIntersection(
          segments[i].p1,
          segments[i].p2,
          segments[j].p1,
          segments[j].p2,
        ),
      );
    }
  }
  // Tangent points from the draft's anchor to nearby circles/arcs.
  if (anchor) {
    for (const e of ir.entities) {
      if ((e.type === "circle" || e.type === "arc") && e.center && e.radius) {
        for (const tp of tangentPoints(anchor, e.center, e.radius))
          consider(tp);
      }
    }
  }
  if (best) return best;
  if (anchor) {
    // Ortho snap: within ~4° of horizontal/vertical from the anchor.
    const dx = x - anchor.x;
    const dy = y - anchor.y;
    if (Math.abs(dx) > 8 && Math.abs(Math.atan2(dy, dx)) < 0.07)
      return { x, y: anchor.y };
    if (Math.abs(dy) > 8 && Math.abs(Math.atan2(dx, dy)) < 0.07)
      return { x: anchor.x, y };
  }
  return { x, y };
}

function entityLabel(e: IrEntity, t: (k: string) => string): string {
  const name = t(`vector.type_${e.type}`);
  if (e.type === "text") return `${name}: ${e.text ?? ""}`;
  if (e.type === "circle") return `${name} R${Math.round(e.radius ?? 0)}`;
  return name;
}

const ASSURANCE_COLOR: Record<string, string> = {
  observed: "text-zinc-300",
  inferred: "text-amber-300",
  constraint_validated: "text-sky-300",
  calculation_validated: "text-sky-300",
  human_approved: "text-emerald-300",
};

function AssuranceBadge({
  assurance,
  t,
}: {
  assurance: string;
  t: (k: string) => string;
}) {
  return (
    <span
      className={`px-1.5 py-0.5 rounded bg-white/5 text-[10px] ${ASSURANCE_COLOR[assurance] ?? "text-zinc-400"}`}
    >
      {t(`vector.assurance_${assurance}`)}
    </span>
  );
}

/** Critical annotation still unresolved at the bottom of the ladder — the
 * same predicate the backend accept-vectorize gate enforces (409). Mirrored
 * here purely for a helpful pre-flight UI hint; the server is authoritative. */
function isUnresolvedCritical(
  e: IrEntity,
  reviewPendingIds: Set<string>,
): boolean {
  return (
    (e.type === "dimension" || e.type === "text") &&
    e.assurance === "inferred" &&
    reviewPendingIds.has(e.id)
  );
}

// Geometry stroke color reflects assurance directly (not just "flagged or
// not"): a hallucination-safe engineer needs to see at a glance what's
// merely inferred vs cross-check-validated vs human-approved.
const ASSURANCE_STROKE: Record<string, string> = {
  observed: "#a1a1aa", // zinc-400 — read but not yet cross-checked
  inferred: "#a1a1aa",
  constraint_validated: "#38bdf8", // sky-400
  calculation_validated: "#38bdf8",
  human_approved: "#34d399", // emerald-400
};

function EntityShape({
  e,
  selected,
  flagged,
  onClick,
  arrowLen,
}: {
  e: IrEntity;
  selected: boolean;
  flagged: boolean;
  onClick: (id: string) => void;
  arrowLen: number;
}) {
  const stroke = selected
    ? "#38bdf8"
    : flagged
      ? "#f59e0b"
      : (ASSURANCE_STROKE[e.assurance] ?? "#a1a1aa");
  const strokeWidth = e.width_class === "main" ? 2.5 : 1.5;
  const dash =
    e.line_class === "axis"
      ? "12 3 3 3"
      : e.line_class === "hidden"
        ? "8 4"
        : undefined;
  const common = {
    stroke,
    strokeWidth: selected ? strokeWidth + 1 : strokeWidth,
    strokeDasharray: dash,
    fill: "none" as const,
    style: { cursor: "pointer" },
    onClick: (ev: React.MouseEvent) => {
      ev.stopPropagation();
      onClick(e.id);
    },
  };
  if (e.type === "segment" && e.p1 && e.p2) {
    return <line x1={e.p1.x} y1={e.p1.y} x2={e.p2.x} y2={e.p2.y} {...common} />;
  }
  if (e.type === "circle" && e.center) {
    return (
      <circle cx={e.center.x} cy={e.center.y} r={e.radius ?? 1} {...common} />
    );
  }
  if (e.type === "arc" && e.center && e.radius) {
    const a0 = ((e.start_angle ?? 0) * Math.PI) / 180;
    const a1 = ((e.end_angle ?? 0) * Math.PI) / 180;
    const x0 = e.center.x + e.radius * Math.cos(a0);
    const y0 = e.center.y + e.radius * Math.sin(a0);
    const x1 = e.center.x + e.radius * Math.cos(a1);
    const y1 = e.center.y + e.radius * Math.sin(a1);
    const span = Math.abs((e.end_angle ?? 0) - (e.start_angle ?? 0));
    const large = span % 360 > 180 ? 1 : 0;
    const sweep = (e.end_angle ?? 0) > (e.start_angle ?? 0) ? 1 : 0;
    return (
      <path
        d={`M ${x0} ${y0} A ${e.radius} ${e.radius} 0 ${large} ${sweep} ${x1} ${y1}`}
        {...common}
      />
    );
  }
  if (
    (e.type === "polyline" || e.type === "hatch") &&
    (e.points ?? e.boundary)
  ) {
    const pts = (e.points ?? e.boundary ?? [])
      .map((p) => `${p.x},${p.y}`)
      .join(" ");
    return e.closed || e.type === "hatch" ? (
      <polygon
        points={pts}
        {...common}
        fillOpacity={e.type === "hatch" ? 0.1 : 0}
      />
    ) : (
      <polyline points={pts} {...common} />
    );
  }
  if (e.type === "text" && e.position) {
    return (
      <text
        x={e.position.x}
        y={e.position.y}
        fontSize={e.height ?? 12}
        fill={stroke}
        stroke="none"
        style={{ cursor: "pointer" }}
        onClick={(ev) => {
          ev.stopPropagation();
          onClick(e.id);
        }}
      >
        {e.text}
      </text>
    );
  }
  if (e.type === "dimension" && e.p1 && e.p2) {
    const label = dimensionLabel(e);
    return (
      <g {...common}>
        <line x1={e.p1.x} y1={e.p1.y} x2={e.p2.x} y2={e.p2.y} />
        {dimensionArrowPolygons(e.p1, e.p2, e.kind, arrowLen).map(
          (points, i) => (
            <polygon key={i} points={points} fill={stroke} stroke="none" />
          ),
        )}
        {label && (
          <text
            x={(e.p1.x + e.p2.x) / 2}
            y={(e.p1.y + e.p2.y) / 2 - 4}
            fontSize={12}
            fill={stroke}
            stroke="none"
            textAnchor="middle"
          >
            {label}
          </text>
        )}
      </g>
    );
  }
  return null;
}

export default function VectorWorkspace({ gen, onChanged }: Props) {
  const t = useTranslations("studio");
  const [ir, setIr] = useState<CadIr | null>(null);
  const [revision, setRevision] = useState<number>(0);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [tool, setTool] = useState<Tool>("select");
  const [showSource, setShowSource] = useState(true);
  const [draftStart, setDraftStart] = useState<{ x: number; y: number } | null>(
    null,
  );
  const [cursor, setCursor] = useState<{ x: number; y: number } | null>(null);
  const [scaleInput, setScaleInput] = useState("");
  const [textInput, setTextInput] = useState("");
  const [reviewFilter, setReviewFilter] = useState<string>("all");
  const [mirrorTargetId, setMirrorTargetId] = useState<string | null>(null);
  const [pickedSegmentId, setPickedSegmentId] = useState<string | null>(null);
  const [polylinePoints, setPolylinePoints] = useState<
    { x: number; y: number }[]
  >([]);
  const svgRef = useRef<SVGSVGElement | null>(null);

  // Undo/redo (Ф5.2): a session-local pointer into the revision numbers this
  // editor has visited, in logical (not raw DB-counter) order. Every real
  // edit truncates anything past the pointer and appends; undo/redo just
  // move the pointer and ask the backend to re-surface that revision's
  // content as a NEW row (POST .../ir/revert) — history is append-only on
  // the server, this is purely a client-side navigation convenience.
  const historyRef = useRef<number[]>([]);
  const [historyIndex, setHistoryIndex] = useState(-1);
  const canUndo = historyIndex > 0;
  const canRedo =
    historyIndex >= 0 && historyIndex < historyRef.current.length - 1;

  const hasSource = (gen.source_image_paths?.length ?? 0) > 0;

  const load = useCallback(async () => {
    try {
      const env = await getIr(gen.id);
      setIr(env.ir);
      setRevision(env.revision);
      setErr(null);
      historyRef.current = [env.revision];
      setHistoryIndex(0);
    } catch (e) {
      setErr(String((e as Error).message || e));
    }
  }, [gen.id]);

  useEffect(() => {
    void load();
  }, [load]);

  async function undo() {
    if (!canUndo) return;
    const target = historyRef.current[historyIndex - 1];
    setBusy(true);
    setErr(null);
    try {
      const env = await revertIr(gen.id, target);
      setIr(env.ir);
      setRevision(env.revision);
      setHistoryIndex((i) => i - 1);
      setSelectedId(null);
      onChanged();
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

  async function redo() {
    if (!canRedo) return;
    const target = historyRef.current[historyIndex + 1];
    setBusy(true);
    setErr(null);
    try {
      const env = await revertIr(gen.id, target);
      setIr(env.ir);
      setRevision(env.revision);
      setHistoryIndex((i) => i + 1);
      setSelectedId(null);
      onChanged();
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

  const pending = useMemo(
    () => (ir?.review ?? []).filter((r) => !r.resolved),
    [ir],
  );
  const reviewReasons = useMemo(
    () => Array.from(new Set(pending.map((r) => r.reason))).sort(),
    [pending],
  );
  const filteredPending = useMemo(
    () =>
      reviewFilter === "all"
        ? pending
        : pending.filter((r) => r.reason === reviewFilter),
    [pending, reviewFilter],
  );
  const flaggedIds = useMemo(
    () => new Set(pending.map((r) => r.entity_id)),
    [pending],
  );
  const pendingIds = flaggedIds;
  const issues = useMemo(() => ir?.validation.issues ?? [], [ir]);
  const blocking = issues.filter((i) => i.severity === "error");
  const levelGroups = useMemo(() => {
    const groups = new Map<number, typeof issues>();
    for (const issue of issues) {
      const list = groups.get(issue.level) ?? [];
      list.push(issue);
      groups.set(issue.level, list);
    }
    return Array.from(groups.entries()).sort((a, b) => a[0] - b[0]);
  }, [issues]);
  const criticalUnresolved = useMemo(
    () =>
      (ir?.entities ?? []).filter((e) => isUnresolvedCritical(e, pendingIds)),
    [ir, pendingIds],
  );
  const selected = ir?.entities.find((e) => e.id === selectedId) ?? null;

  async function apply(ops: IrPatchOp[]) {
    setBusy(true);
    setErr(null);
    try {
      const env = await patchIr(gen.id, ops);
      setIr(env.ir);
      setRevision(env.revision);
      // A fresh edit after undo abandons the redo branch — standard
      // undo/redo semantics (matches browser/editor behavior).
      setHistoryIndex((i) => {
        const truncated = historyRef.current.slice(0, i + 1);
        truncated.push(env.revision);
        historyRef.current = truncated;
        return truncated.length - 1;
      });
      onChanged();
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

  // Keyboard-first UX (Ф5.3): tool switching, delete, escape, undo/redo.
  // Ignored while typing in a text field (scale/label inputs) so shortcuts
  // don't fire mid-keystroke.
  useEffect(() => {
    function onKeyDown(ev: KeyboardEvent) {
      const target = ev.target as HTMLElement | null;
      if (target && ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName))
        return;
      const mod = ev.ctrlKey || ev.metaKey;
      if (mod && ev.key.toLowerCase() === "z") {
        ev.preventDefault();
        if (ev.shiftKey) void redo();
        else void undo();
        return;
      }
      if (mod && ev.key.toLowerCase() === "y") {
        ev.preventDefault();
        void redo();
        return;
      }
      if (ev.key === "Escape") {
        setDraftStart(null);
        setSelectedId(null);
        setPickedSegmentId(null);
        setMirrorTargetId(null);
        setPolylinePoints([]);
        return;
      }
      if ((ev.key === "Delete" || ev.key === "Backspace") && selectedId) {
        ev.preventDefault();
        const id = selectedId;
        setSelectedId(null);
        void apply([{ op: "delete", entity_id: id }]);
        return;
      }
      if (
        tool === "select" &&
        selectedId &&
        ir &&
        ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(ev.key)
      ) {
        ev.preventDefault();
        const step = (ev.shiftKey ? 10 : 1) * (ir.source.image_width / 400);
        const [dx, dy] = {
          ArrowUp: [0, -step],
          ArrowDown: [0, step],
          ArrowLeft: [-step, 0],
          ArrowRight: [step, 0],
        }[ev.key] as [number, number];
        void apply([{ op: "move", entity_id: selectedId, dx, dy }]);
        return;
      }
      const toolKeys: Record<string, Tool> = {
        v: "select",
        l: "line",
        c: "circle",
        t: "text",
        d: "dim_linear",
        o: "dim_diameter",
        r: "dim_radial",
        p: "polyline",
        h: "hatch",
      };
      const next = toolKeys[ev.key.toLowerCase()];
      if (next) {
        setTool(next);
        setDraftStart(null);
        setPickedSegmentId(null);
        setMirrorTargetId(null);
        setPolylinePoints([]);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  });

  function finishPolyline() {
    let pts = polylinePoints;
    if (pts.length >= 2) {
      const last = pts[pts.length - 1];
      const secondLast = pts[pts.length - 2];
      // Double-click fires two "click"s at the same spot before dblclick —
      // drop the accidental duplicate point that leaves behind.
      if (Math.hypot(last.x - secondLast.x, last.y - secondLast.y) < 1e-6) {
        pts = pts.slice(0, -1);
      }
    }
    if (pts.length >= 2) {
      void apply([
        {
          op: "add",
          entity: {
            type: "polyline",
            points: pts,
            closed: false,
            line_class: "contour",
            width_class: "main",
          },
        },
      ]);
    }
    setPolylinePoints([]);
  }

  function svgPoint(ev: React.MouseEvent): { x: number; y: number } | null {
    const svg = svgRef.current;
    if (!svg || !ir) return null;
    const rect = svg.getBoundingClientRect();
    const x = ((ev.clientX - rect.left) / rect.width) * ir.source.image_width;
    const y = ((ev.clientY - rect.top) / rect.height) * ir.source.image_height;
    return { x, y };
  }

  function canvasClick(ev: React.MouseEvent) {
    if (!ir) return;
    const raw = svgPoint(ev);
    if (!raw) return;
    const tol = ir.source.image_width / 80;
    if (tool === "select") {
      setSelectedId(null);
      return;
    }
    if (tool === "fillet" || tool === "chamfer") {
      // Clicking blank canvas cancels an in-progress pick — the actual
      // picks happen on EntityShape's onClick (segments only).
      setPickedSegmentId(null);
      return;
    }
    if (tool === "hatch") {
      void apply([{ op: "hatch_click", click_x: raw.x, click_y: raw.y }]);
      return;
    }
    if (tool === "polyline") {
      const anchor = polylinePoints[polylinePoints.length - 1] ?? null;
      const pp = snapPoint(ir, raw.x, raw.y, anchor, tol);
      if (polylinePoints.length >= 2) {
        const first = polylinePoints[0];
        if (Math.hypot(pp.x - first.x, pp.y - first.y) < tol) {
          void apply([
            {
              op: "add",
              entity: {
                type: "polyline",
                points: polylinePoints,
                closed: true,
                line_class: "contour",
                width_class: "main",
              },
            },
          ]);
          setPolylinePoints([]);
          return;
        }
      }
      setPolylinePoints([...polylinePoints, pp]);
      return;
    }
    const p = snapPoint(ir, raw.x, raw.y, draftStart, tol);
    if (tool === "text") {
      const text = textInput.trim() || prompt(t("vector.text_prompt")) || "";
      if (!text.trim()) return;
      void apply([
        {
          op: "add",
          entity: {
            type: "text",
            position: { x: p.x, y: p.y },
            text: text.trim(),
            height: 14,
            line_class: "dim",
            width_class: "thin",
          },
        },
      ]);
      return;
    }
    if (!draftStart) {
      setDraftStart(p);
      return;
    }
    if (tool === "line") {
      void apply([
        {
          op: "add",
          entity: {
            type: "segment",
            p1: { x: draftStart.x, y: draftStart.y },
            p2: { x: p.x, y: p.y },
            line_class: "contour",
            width_class: "main",
          },
        },
      ]);
    } else if (tool === "circle") {
      const r = Math.hypot(p.x - draftStart.x, p.y - draftStart.y);
      if (r > 1) {
        void apply([
          {
            op: "add",
            entity: {
              type: "circle",
              center: { x: draftStart.x, y: draftStart.y },
              radius: r,
              line_class: "contour",
              width_class: "main",
            },
          },
        ]);
      }
    } else if (tool in DIM_TOOL_KIND) {
      const kind = DIM_TOOL_KIND[tool];
      const lengthPx = Math.hypot(p.x - draftStart.x, p.y - draftStart.y);
      const defaultVal = ir.scale ? (lengthPx * ir.scale).toFixed(1) : "";
      const typed = window.prompt(t("vector.dimension_prompt"), defaultVal);
      if (typed !== null && typed.trim()) {
        const text = typed.trim();
        const numMatch = text.replace(",", ".").match(/[\d.]+/);
        void apply([
          {
            op: "add",
            entity: {
              type: "dimension",
              kind,
              p1: { x: draftStart.x, y: draftStart.y },
              p2: { x: p.x, y: p.y },
              text,
              value_mm: numMatch ? Number(numMatch[0]) : null,
              line_class: "dim",
              width_class: "thin",
            },
          },
        ]);
      }
    } else if (tool === "mirror") {
      if (mirrorTargetId) {
        void apply([
          {
            op: "mirror",
            entity_id: mirrorTargetId,
            mirror_p1: { x: draftStart.x, y: draftStart.y },
            mirror_p2: { x: p.x, y: p.y },
          },
        ]);
      }
      setMirrorTargetId(null);
      setTool("select");
    }
    setDraftStart(null);
  }

  if (!ir) {
    return (
      <div className="text-xs text-zinc-400">
        {err ? (
          <span className="text-red-400">{err}</span>
        ) : (
          t("vector.loading")
        )}
      </div>
    );
  }

  const vb = `0 0 ${ir.source.image_width} ${ir.source.image_height}`;
  const toolButtons: { key: Tool; label: string }[] = [
    { key: "select", label: t("vector.tool_select") },
    { key: "line", label: t("vector.tool_line") },
    { key: "circle", label: t("vector.tool_circle") },
    { key: "text", label: t("vector.tool_text") },
    { key: "dim_linear", label: t("vector.tool_dim_linear") },
    { key: "dim_diameter", label: t("vector.tool_dim_diameter") },
    { key: "dim_radial", label: t("vector.tool_dim_radial") },
    { key: "fillet", label: t("vector.tool_fillet") },
    { key: "chamfer", label: t("vector.tool_chamfer") },
    { key: "polyline", label: t("vector.tool_polyline") },
    { key: "hatch", label: t("vector.tool_hatch") },
  ];
  const arrowLen = dimArrowLenPx(ir.scale);

  return (
    <div className="flex flex-col gap-3">
      {/* Header: revision, coverage, scale */}
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-zinc-400">
        <span className="px-2 py-0.5 rounded bg-white/5">
          {t("vector.revision", { n: revision })}
        </span>
        {ir.validation.coverage_recall != null && (
          <span className="px-2 py-0.5 rounded bg-white/5">
            {t("vector.coverage", {
              recall: Math.round((ir.validation.coverage_recall ?? 0) * 100),
              precision: Math.round(
                (ir.validation.coverage_precision ?? 0) * 100,
              ),
            })}
          </span>
        )}
        {ir.scale ? (
          <span className="px-2 py-0.5 rounded bg-white/5">
            {t("vector.scale_known", { scale: ir.scale.toFixed(4) })}
          </span>
        ) : (
          <span className="flex items-center gap-1 px-2 py-0.5 rounded bg-amber-500/10 text-amber-300">
            {t("vector.scale_unknown")}
            <input
              value={scaleInput}
              onChange={(e) => setScaleInput(e.target.value)}
              placeholder={t("vector.scale_placeholder")}
              className="w-20 rounded bg-zinc-900 border border-white/10 px-1 py-0.5 text-[11px] text-zinc-200"
            />
            <button
              disabled={busy || !Number(scaleInput)}
              onClick={() =>
                apply([{ op: "set_scale", scale: Number(scaleInput) }])
              }
              className="text-sky-400 hover:text-sky-300 disabled:opacity-40"
            >
              OK
            </button>
          </span>
        )}
        {pending.length > 0 && (
          <span className="px-2 py-0.5 rounded bg-amber-500/10 text-amber-300">
            {t("vector.review_pending", { n: pending.length })}
          </span>
        )}
      </div>

      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-1">
        {toolButtons.map((b) => (
          <button
            key={b.key}
            onClick={() => {
              setTool(b.key);
              setDraftStart(null);
              setPickedSegmentId(null);
              setMirrorTargetId(null);
              setPolylinePoints([]);
            }}
            className={`px-2 py-1 rounded text-xs ${
              tool === b.key
                ? "bg-sky-600 text-white"
                : "bg-white/5 text-zinc-300 hover:bg-white/10"
            }`}
          >
            {b.label}
          </button>
        ))}
        {tool === "text" && (
          <input
            value={textInput}
            onChange={(e) => setTextInput(e.target.value)}
            placeholder={t("vector.text_placeholder")}
            className="rounded bg-zinc-900 border border-white/10 px-2 py-1 text-xs text-zinc-200"
          />
        )}
        <button
          disabled={busy || !canUndo}
          onClick={() => void undo()}
          title={t("vector.undo")}
          className="px-2 py-1 rounded text-xs bg-white/5 text-zinc-300 hover:bg-white/10 disabled:opacity-30"
        >
          ↶
        </button>
        <button
          disabled={busy || !canRedo}
          onClick={() => void redo()}
          title={t("vector.redo")}
          className="px-2 py-1 rounded text-xs bg-white/5 text-zinc-300 hover:bg-white/10 disabled:opacity-30"
        >
          ↷
        </button>
        {hasSource && (
          <label className="ml-auto flex items-center gap-1 text-[11px] text-zinc-400">
            <input
              type="checkbox"
              checked={showSource}
              onChange={(e) => setShowSource(e.target.checked)}
            />
            {t("vector.show_source")}
          </label>
        )}
      </div>

      {/* Canvas: source photo + entity overlay */}
      <div
        className="relative w-full rounded border border-white/10 bg-white overflow-hidden"
        style={{
          aspectRatio: `${ir.source.image_width} / ${ir.source.image_height}`,
        }}
      >
        {hasSource && showSource && (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={sourceUrl(gen.id, 0)}
            alt={t("composer.source_alt")}
            className="absolute inset-0 h-full w-full object-fill opacity-45"
          />
        )}
        <svg
          ref={svgRef}
          viewBox={vb}
          className="absolute inset-0 h-full w-full"
          onClick={canvasClick}
          onDoubleClick={() => {
            if (tool === "polyline") finishPolyline();
          }}
          onMouseMove={(ev) => setCursor(svgPoint(ev))}
        >
          {ir.entities.map((e) => (
            <EntityShape
              key={e.id}
              e={e}
              selected={e.id === selectedId || e.id === pickedSegmentId}
              flagged={flaggedIds.has(e.id)}
              arrowLen={arrowLen}
              onClick={(id) => {
                if (tool === "select") {
                  setSelectedId(id === selectedId ? null : id);
                  return;
                }
                if (tool === "fillet" || tool === "chamfer") {
                  const clicked = ir.entities.find((x) => x.id === id);
                  if (!clicked || clicked.type !== "segment") return;
                  if (!pickedSegmentId) {
                    setPickedSegmentId(id);
                    return;
                  }
                  if (pickedSegmentId === id) return;
                  const label =
                    tool === "fillet"
                      ? t("vector.fillet_prompt")
                      : t("vector.chamfer_prompt");
                  const typed = window.prompt(label, "5");
                  const value = typed ? Number(typed.replace(",", ".")) : NaN;
                  const firstId = pickedSegmentId;
                  setPickedSegmentId(null);
                  if (typed !== null && !Number.isNaN(value) && value > 0) {
                    void apply([
                      {
                        op: tool,
                        entity_id: firstId,
                        entity_id_2: id,
                        value,
                      },
                    ]);
                  }
                }
              }}
            />
          ))}
          {draftStart && cursor && tool === "line" && (
            <line
              x1={draftStart.x}
              y1={draftStart.y}
              x2={cursor.x}
              y2={cursor.y}
              stroke="#38bdf8"
              strokeDasharray="6 4"
              strokeWidth={1.5}
            />
          )}
          {draftStart && cursor && tool === "circle" && (
            <circle
              cx={draftStart.x}
              cy={draftStart.y}
              r={Math.hypot(cursor.x - draftStart.x, cursor.y - draftStart.y)}
              stroke="#38bdf8"
              strokeDasharray="6 4"
              strokeWidth={1.5}
              fill="none"
            />
          )}
          {draftStart && cursor && tool in DIM_TOOL_KIND && (
            <g stroke="#38bdf8" strokeDasharray="6 4" strokeWidth={1.5}>
              <line
                x1={draftStart.x}
                y1={draftStart.y}
                x2={cursor.x}
                y2={cursor.y}
              />
              {dimensionArrowPolygons(
                draftStart,
                cursor,
                DIM_TOOL_KIND[tool],
                arrowLen,
              ).map((points, i) => (
                <polygon key={i} points={points} fill="#38bdf8" stroke="none" />
              ))}
            </g>
          )}
          {draftStart && cursor && tool === "mirror" && (
            <line
              x1={draftStart.x}
              y1={draftStart.y}
              x2={cursor.x}
              y2={cursor.y}
              stroke="#a78bfa"
              strokeDasharray="3 3"
              strokeWidth={1}
            />
          )}
          {tool === "polyline" && polylinePoints.length > 0 && (
            <g
              stroke="#38bdf8"
              strokeDasharray="6 4"
              strokeWidth={1.5}
              fill="none"
            >
              <polyline
                points={polylinePoints.map((p) => `${p.x},${p.y}`).join(" ")}
              />
              {cursor && (
                <line
                  x1={polylinePoints[polylinePoints.length - 1].x}
                  y1={polylinePoints[polylinePoints.length - 1].y}
                  x2={cursor.x}
                  y2={cursor.y}
                />
              )}
              {polylinePoints.map((p, i) => (
                <circle key={i} cx={p.x} cy={p.y} r={2.5} fill="#38bdf8" />
              ))}
            </g>
          )}
        </svg>
      </div>

      {err && <div className="text-xs text-red-400">{err}</div>}

      {/* Selected entity properties */}
      {selected && (
        <div className="rounded border border-white/10 bg-zinc-900/60 p-2 space-y-2 text-xs">
          <div className="flex items-center justify-between">
            <span className="flex flex-wrap items-center gap-1 text-zinc-300">
              {entityLabel(selected, t)}
              <span className="text-zinc-500">
                {Math.round(selected.confidence * 100)}% · {selected.origin}
              </span>
              <AssuranceBadge assurance={selected.assurance} t={t} />
            </span>
            <div className="flex gap-2">
              {selected.assurance !== "human_approved" && (
                <button
                  disabled={busy}
                  onClick={() =>
                    apply([{ op: "confirm", entity_id: selected.id }])
                  }
                  className="px-2 py-1 rounded bg-emerald-600/80 hover:bg-emerald-500 text-white disabled:opacity-50"
                >
                  {t("vector.confirm")}
                </button>
              )}
              <button
                disabled={busy}
                onClick={() => {
                  const typed = window.prompt(t("vector.copy_prompt"), "20,0");
                  if (!typed) return;
                  const [dxs, dys] = typed.split(",").map((s) => s.trim());
                  const dx = Number(dxs?.replace(",", "."));
                  const dy = Number(dys?.replace(",", ".") || "0");
                  if (Number.isNaN(dx) || Number.isNaN(dy)) return;
                  void apply([{ op: "copy", entity_id: selected.id, dx, dy }]);
                }}
                className="px-2 py-1 rounded bg-white/10 hover:bg-white/20 text-zinc-200 disabled:opacity-50"
              >
                {t("vector.copy")}
              </button>
              <button
                disabled={busy}
                onClick={() => {
                  setMirrorTargetId(selected.id);
                  setTool("mirror");
                  setDraftStart(null);
                }}
                className="px-2 py-1 rounded bg-white/10 hover:bg-white/20 text-zinc-200 disabled:opacity-50"
              >
                {t("vector.mirror")}
              </button>
              <button
                disabled={busy}
                onClick={() => {
                  setSelectedId(null);
                  void apply([{ op: "delete", entity_id: selected.id }]);
                }}
                className="px-2 py-1 rounded bg-red-600/70 hover:bg-red-500 text-white disabled:opacity-50"
              >
                {t("vector.delete")}
              </button>
            </div>
          </div>
          {tool === "mirror" && mirrorTargetId === selected.id && (
            <div className="text-[11px] text-sky-300">
              {t("vector.mirror_pick_line")}
            </div>
          )}
          {(selected.alternatives?.length ?? 0) > 0 && (
            <div className="rounded bg-white/5 p-1.5 space-y-1">
              <div className="text-[10px] text-zinc-500">
                {t("vector.alternatives_title")}
              </div>
              {selected.alternatives!.map((alt, i) => (
                <button
                  key={i}
                  disabled={busy || !alt.value}
                  onClick={() => {
                    if (!alt.value) return;
                    void apply([
                      {
                        op: "update",
                        entity_id: selected.id,
                        entity: { ...selected, text: alt.value },
                      },
                    ]);
                  }}
                  className="flex w-full items-center justify-between rounded px-1.5 py-1 text-left text-[11px] text-zinc-300 hover:bg-white/10 disabled:cursor-default disabled:opacity-60"
                >
                  <span>{alt.value ?? t("vector.alternative_geometry")}</span>
                  <span className="text-zinc-500">
                    {Math.round(alt.p * 100)}%
                  </span>
                </button>
              ))}
            </div>
          )}
          <div className="flex flex-wrap gap-2 items-center">
            <label className="flex items-center gap-1 text-zinc-400">
              {t("vector.line_class")}
              <select
                value={selected.line_class}
                disabled={busy}
                onChange={(e) =>
                  apply([
                    {
                      op: "update",
                      entity_id: selected.id,
                      entity: {
                        ...selected,
                        line_class: e.target.value as IrEntity["line_class"],
                        width_class: ["axis", "dim", "hatch"].includes(
                          e.target.value,
                        )
                          ? "thin"
                          : selected.width_class,
                      },
                    },
                  ])
                }
                className="rounded bg-zinc-900 border border-white/10 px-1 py-0.5 text-zinc-200"
              >
                {["contour", "thin", "axis", "hidden", "dim", "hatch"].map(
                  (lc) => (
                    <option key={lc} value={lc}>
                      {t(`vector.line_${lc}`)}
                    </option>
                  ),
                )}
              </select>
            </label>
            {selected.type === "text" && (
              <label className="flex items-center gap-1 text-zinc-400">
                {t("vector.text_label")}
                <input
                  defaultValue={selected.text ?? ""}
                  disabled={busy}
                  onBlur={(e) => {
                    if (e.target.value !== selected.text) {
                      void apply([
                        {
                          op: "update",
                          entity_id: selected.id,
                          entity: { ...selected, text: e.target.value },
                        },
                      ]);
                    }
                  }}
                  className="rounded bg-zinc-900 border border-white/10 px-1 py-0.5 text-zinc-200"
                />
              </label>
            )}
          </div>
        </div>
      )}

      {/* Review queue */}
      {pending.length > 0 && (
        <div className="rounded border border-amber-500/20 bg-amber-500/5 p-2 space-y-1">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-xs text-amber-300">
              {t("vector.review_title")}
            </div>
            {reviewReasons.length > 1 && (
              <select
                value={reviewFilter}
                onChange={(e) => setReviewFilter(e.target.value)}
                className="rounded bg-zinc-900 border border-white/10 px-1.5 py-0.5 text-[11px] text-zinc-300"
              >
                <option value="all">{t("vector.review_filter_all")}</option>
                {reviewReasons.map((reason) => (
                  <option key={reason} value={reason}>
                    {t(`vector.review_reason_${reason}`)}
                  </option>
                ))}
              </select>
            )}
          </div>
          <div className="max-h-48 overflow-y-auto space-y-1">
            {filteredPending.slice(0, 50).map((r) => {
              const e = ir.entities.find((x) => x.id === r.entity_id);
              if (!e) return null;
              return (
                <div
                  key={r.entity_id}
                  className="flex items-center gap-2 text-[11px]"
                >
                  <button
                    onClick={() => setSelectedId(r.entity_id)}
                    className="flex flex-1 items-center gap-1.5 text-left text-zinc-300 hover:text-white truncate"
                  >
                    <span className="truncate">
                      {entityLabel(e, t)} — {Math.round(e.confidence * 100)}%
                    </span>
                    <span className="shrink-0 text-zinc-500">
                      {t(`vector.review_reason_${r.reason}`)}
                    </span>
                  </button>
                  <button
                    disabled={busy}
                    onClick={() =>
                      apply([{ op: "confirm", entity_id: r.entity_id }])
                    }
                    className="text-emerald-400 hover:text-emerald-300 disabled:opacity-40"
                  >
                    ✓
                  </button>
                  <button
                    disabled={busy}
                    onClick={() =>
                      apply([{ op: "delete", entity_id: r.entity_id }])
                    }
                    className="text-red-400 hover:text-red-300 disabled:opacity-40"
                  >
                    ✕
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Validation report — grouped by assurance-pipeline level (Ф7.2) */}
      {issues.length > 0 && (
        <div className="rounded border border-white/10 bg-zinc-900/60 p-2 space-y-2">
          <div className="flex items-center justify-between">
            <div className="text-xs text-zinc-300">
              {t("vector.validation_title")}
            </div>
            <button
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                setErr(null);
                try {
                  const env = await runFullCheck(gen.id);
                  setIr(env.ir);
                  setRevision(env.revision);
                  onChanged();
                } catch (e) {
                  setErr(String((e as Error).message || e));
                } finally {
                  setBusy(false);
                }
              }}
              className="text-[11px] px-2 py-0.5 rounded bg-white/10 hover:bg-white/20 text-zinc-200 disabled:opacity-50"
            >
              {t("vector.run_full_check")}
            </button>
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
                      onClick={() => setSelectedId(issue.entity_ids[0])}
                      className="ml-1 text-sky-400 hover:text-sky-300"
                    >
                      {t("vector.show_entity")}
                    </button>
                  )}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}

      {!gen.accepted && criticalUnresolved.length > 0 && (
        <div className="rounded border border-amber-500/20 bg-amber-500/5 p-2 text-[11px] text-amber-300">
          {t("vector.accept_blocked_critical", {
            n: criticalUnresolved.length,
          })}
        </div>
      )}

      {/* Downloads + accept */}
      <div className="flex flex-wrap items-center gap-2">
        <a
          href={artifactUrl(gen.id, "dxf")}
          download={`studio-${gen.id}.dxf`}
          className="px-3 py-1.5 rounded bg-white/10 hover:bg-white/20 text-sm"
        >
          DXF
        </a>
        <a
          href={artifactUrl(gen.id, "dwg")}
          download={`studio-${gen.id}.dwg`}
          className="px-3 py-1.5 rounded bg-white/10 hover:bg-white/20 text-sm"
        >
          DWG
        </a>
        <a
          href={artifactUrl(gen.id, "svg")}
          download={`studio-${gen.id}.svg`}
          className="px-3 py-1.5 rounded bg-white/10 hover:bg-white/20 text-sm"
        >
          SVG
        </a>
        {!gen.accepted && (
          <button
            disabled={
              busy || blocking.length > 0 || criticalUnresolved.length > 0
            }
            title={
              blocking.length > 0
                ? t("vector.accept_blocked")
                : criticalUnresolved.length > 0
                  ? t("vector.accept_blocked_critical", {
                      n: criticalUnresolved.length,
                    })
                  : undefined
            }
            onClick={async () => {
              setBusy(true);
              setErr(null);
              try {
                await acceptVectorize(gen.id);
                onChanged();
              } catch (e) {
                setErr(String((e as Error).message || e));
              } finally {
                setBusy(false);
              }
            }}
            className="ml-auto px-3 py-1.5 rounded bg-emerald-600 hover:bg-emerald-500 text-white text-sm disabled:opacity-50"
          >
            {t("vector.accept")}
          </button>
        )}
      </div>
    </div>
  );
}
