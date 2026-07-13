/** Pure geometry/formatting helpers of the CAD workspace — extracted from
 * the former VectorWorkspace monolith so the canvas, command line and
 * panels can share them without dragging React state around. */

import { CadIr, IrEntity, IrLineClass } from "@/lib/studio-api";

export type Tool =
  | "select"
  | "pan"
  | "line"
  | "circle"
  | "text"
  | "dim_linear"
  | "dim_diameter"
  | "dim_radial"
  | "mirror"
  | "fillet"
  | "chamfer"
  | "trim"
  | "extend"
  | "offset"
  | "split"
  | "join"
  | "pattern_linear"
  | "pattern_polar"
  | "polyline"
  | "hatch";

export const DIM_TOOL_KIND: Record<string, "linear" | "diameter" | "radial"> = {
  dim_linear: "linear",
  dim_diameter: "diameter",
  dim_radial: "radial",
};

// ГОСТ 2.307 arrow length (2.5mm); falls back to a fixed px length when the
// sheet has no known scale yet — mirrors backend/app/ai/cad_ir/dim_render.py.
export function dimArrowLenPx(scale: number | null): number {
  return scale ? 2.5 / scale : 8;
}

/** Ø/R kind prefix on the displayed label — mirrors dim_render.dimension_label. */
export function dimensionLabel(e: IrEntity): string {
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
export function dimensionArrowPolygons(
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

/** Where two segments (as drawn, not extended to infinite lines) cross —
 * null if parallel or they don't actually meet within both spans. */
export function segmentIntersection(
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
export function tangentPoints(
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

/** Snap helper: endpoints/centers/intersections of existing entities +
 * tangents from the anchor + ortho to the anchor point. `osnap`/`ortho`
 * are the status-bar toggles: osnap disables entity snapping entirely,
 * ortho FORCES axis alignment to the anchor (not just near-axis snap). */
export function snapPoint(
  ir: CadIr,
  x: number,
  y: number,
  anchor: { x: number; y: number } | null,
  tolPx: number,
  opts: { osnap?: boolean; ortho?: boolean } = {},
): { x: number; y: number } {
  const { osnap = true, ortho = false } = opts;
  if (ortho && anchor) {
    // Hard ortho: project onto the dominant axis from the anchor first,
    // then let osnap refine along that axis.
    if (Math.abs(x - anchor.x) >= Math.abs(y - anchor.y)) y = anchor.y;
    else x = anchor.x;
  }
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
  if (osnap) {
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
  }
  if (best) {
    if (ortho && anchor) {
      // Keep the ortho axis even when a snap target pulls sideways.
      const b = best as { x: number; y: number };
      return Math.abs(x - anchor.x) >= Math.abs(y - anchor.y)
        ? { x: b.x, y: anchor.y }
        : { x: anchor.x, y: b.y };
    }
    return best;
  }
  if (anchor && !ortho) {
    // Soft ortho snap: within ~4° of horizontal/vertical from the anchor.
    const dx = x - anchor.x;
    const dy = y - anchor.y;
    if (Math.abs(dx) > 8 && Math.abs(Math.atan2(dy, dx)) < 0.07)
      return { x, y: anchor.y };
    if (Math.abs(dy) > 8 && Math.abs(Math.atan2(dx, dy)) < 0.07)
      return { x: anchor.x, y };
  }
  return { x, y };
}

// ГОСТ 2.308 geometric-tolerance symbol glyphs — mirrors the backend
// cad_ir.annotations.TOLERANCE_SYMBOLS.
export const TOLERANCE_GLYPHS: Record<string, string> = {
  straightness: "—",
  flatness: "▱",
  roundness: "○",
  cylindricity: "⌭",
  profile_line: "⌒",
  parallelism: "∥",
  perpendicularity: "⊥",
  angularity: "∠",
  position: "⊕",
  concentricity: "◎",
  symmetry: "⌯",
  runout: "↗",
};

/** Canonical display string of a structured annotation — mirrors the backend
 * cad_ir.annotations.annotation_text so the canvas reads like the export. */
export function annotationText(e: IrEntity): string {
  const value = (e.value ?? "").trim();
  const symbol = (e.symbol ?? "").trim();
  const datums = e.datum_refs ?? [];
  switch (e.kind) {
    case "roughness":
      if (!value) return "Ra";
      return /^r[az]/i.test(value) ? value : `Ra ${value}`;
    case "thread":
      return value;
    case "tolerance": {
      const glyph = TOLERANCE_GLYPHS[symbol] ?? symbol ?? "?";
      return [glyph, value, ...datums].filter(Boolean).join(" ");
    }
    case "datum":
      return symbol || value || "A";
    case "weld":
      return value || symbol;
    default:
      return value;
  }
}

export function entityLabel(e: IrEntity, t: (k: string) => string): string {
  const name = t(`vector.type_${e.type}`);
  if (e.type === "text") return `${name}: ${e.text ?? ""}`;
  if (e.type === "circle") return `${name} R${Math.round(e.radius ?? 0)}`;
  return name;
}

export const ASSURANCE_COLOR: Record<string, string> = {
  observed: "text-zinc-300",
  inferred: "text-amber-300",
  constraint_validated: "text-sky-300",
  calculation_validated: "text-sky-300",
  human_approved: "text-emerald-300",
};

// Geometry stroke color reflects assurance directly (not just "flagged or
// not"): a hallucination-safe engineer needs to see at a glance what's
// merely inferred vs cross-check-validated vs human-approved. Inferred/
// observed use a readable dark grey (not the old faint zinc-400) so a
// freshly-digitized drawing — where everything is "inferred" — actually
// reads as a drawing on the white canvas instead of a faint wash.
export const ASSURANCE_STROKE: Record<string, string> = {
  observed: "#3f3f46", // zinc-700 — read but not yet cross-checked
  inferred: "#3f3f46",
  constraint_validated: "#0284c7", // sky-600
  calculation_validated: "#0284c7",
  human_approved: "#059669", // emerald-600
};

/** I4: the ЕСКД layer catalog — one row per line_class. `dxfLayer` mirrors
 * backend LINE_CLASS_LAYERS (schema.py); `linetype`/`lineweight` mirror the
 * DXF layer defs (dxf_render._LAYER_DEFS / ГОСТ 2.303). This is the single
 * source the Layers panel renders, so the editor shows exactly what the DXF
 * export will contain. */
export const LAYER_CATALOG: {
  lineClass: IrLineClass;
  dxfLayer: string;
  color: string;
  linetype: string;
  lineweightMm: number;
}[] = [
  {
    lineClass: "contour",
    dxfLayer: "OBJECT",
    color: "#f4f4f5",
    linetype: "CONTINUOUS",
    lineweightMm: 0.5,
  },
  {
    lineClass: "thin",
    dxfLayer: "OBJECT_THIN",
    color: "#a1a1aa",
    linetype: "CONTINUOUS",
    lineweightMm: 0.25,
  },
  {
    lineClass: "axis",
    dxfLayer: "CENTER",
    color: "#22d3ee",
    linetype: "CENTER",
    lineweightMm: 0.25,
  },
  {
    lineClass: "hidden",
    dxfLayer: "HIDDEN",
    color: "#a78bfa",
    linetype: "DASHED",
    lineweightMm: 0.25,
  },
  {
    lineClass: "dim",
    dxfLayer: "DIM",
    color: "#facc15",
    linetype: "CONTINUOUS",
    lineweightMm: 0.25,
  },
  {
    lineClass: "hatch",
    dxfLayer: "HATCH",
    color: "#34d399",
    linetype: "CONTINUOUS",
    lineweightMm: 0.25,
  },
];

/** Critical annotation still unresolved at the bottom of the ladder — the
 * same predicate the backend accept-vectorize gate enforces (409). Mirrored
 * here purely for a helpful pre-flight UI hint; the server is authoritative. */
export function isUnresolvedCritical(
  e: IrEntity,
  reviewPendingIds: Set<string>,
): boolean {
  return (
    (e.type === "dimension" || e.type === "text") &&
    e.assurance === "inferred" &&
    reviewPendingIds.has(e.id)
  );
}

/** AutoCAD-style coordinate entry for the command line:
 * `100,50` absolute; `@50,0` relative to the anchor; `@50<45` polar from the
 * anchor (degrees, y-down screen space so positive angles go clockwise on
 * screen but match the drawing's own angle convention). Values are in mm
 * when the sheet has a known scale (mm/px), else raw px. */
export function parseCoordinate(
  input: string,
  anchor: { x: number; y: number } | null,
  scale: number | null,
): { x: number; y: number } | null {
  const text = input.trim().replace(/\s+/g, "");
  if (!text) return null;
  const toPx = (v: number) => (scale ? v / scale : v);
  const relative = text.startsWith("@");
  const body = relative ? text.slice(1) : text;
  if (relative && !anchor) return null;

  const polar = body.match(/^(-?[\d.,]+)<(-?[\d.,]+)$/);
  if (polar) {
    if (!anchor || !relative) return null;
    const r = toPx(Number(polar[1].replace(",", ".")));
    const a = (Number(polar[2].replace(",", ".")) * Math.PI) / 180;
    if (!Number.isFinite(r) || !Number.isFinite(a)) return null;
    // Screen y grows downward; drawing angles are CCW on paper, so negate.
    return { x: anchor.x + r * Math.cos(a), y: anchor.y - r * Math.sin(a) };
  }

  const parts = body.split(/[,;]/);
  if (parts.length !== 2) return null;
  const vx = Number(parts[0].replace(",", "."));
  const vy = Number(parts[1].replace(",", "."));
  if (!Number.isFinite(vx) || !Number.isFinite(vy)) return null;
  if (relative && anchor) {
    return { x: anchor.x + toPx(vx), y: anchor.y - toPx(vy) };
  }
  return { x: toPx(vx), y: toPx(vy) };
}

/** Bounding box of an entity (for window/crossing selection). */
export function entityBBox(
  e: IrEntity,
): { x0: number; y0: number; x1: number; y1: number } | null {
  const pts: { x: number; y: number }[] = [];
  if (e.p1) pts.push(e.p1);
  if (e.p2) pts.push(e.p2);
  if (e.position) pts.push(e.position);
  if (e.points) pts.push(...e.points);
  if (e.boundary) pts.push(...e.boundary);
  if (e.center) {
    const r = e.radius ?? 4;
    pts.push(
      { x: e.center.x - r, y: e.center.y - r },
      { x: e.center.x + r, y: e.center.y + r },
    );
  }
  if (pts.length === 0) return null;
  const xs = pts.map((p) => p.x);
  const ys = pts.map((p) => p.y);
  return {
    x0: Math.min(...xs),
    y0: Math.min(...ys),
    x1: Math.max(...xs),
    y1: Math.max(...ys),
  };
}

export interface Rect {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

/** AutoCAD selection semantics: a left-to-right "window" picks entities
 * fully inside the rectangle; right-to-left "crossing" also picks anything
 * whose bbox merely intersects it. */
export function selectByRect(
  entities: IrEntity[],
  rect: Rect,
  crossing: boolean,
): string[] {
  const rx0 = Math.min(rect.x0, rect.x1);
  const ry0 = Math.min(rect.y0, rect.y1);
  const rx1 = Math.max(rect.x0, rect.x1);
  const ry1 = Math.max(rect.y0, rect.y1);
  const out: string[] = [];
  for (const e of entities) {
    const bb = entityBBox(e);
    if (!bb) continue;
    const inside = bb.x0 >= rx0 && bb.x1 <= rx1 && bb.y0 >= ry0 && bb.y1 <= ry1;
    const intersects =
      bb.x1 >= rx0 && bb.x0 <= rx1 && bb.y1 >= ry0 && bb.y0 <= ry1;
    if (inside || (crossing && intersects)) out.push(e.id);
  }
  return out;
}
