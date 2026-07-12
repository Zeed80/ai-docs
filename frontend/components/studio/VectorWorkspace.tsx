"use client";

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  AddedCadEdgeFeature,
  AddedCadFeature,
  CadIr,
  FeatureParameterOverride,
  FeatureTreeCandidate,
  Generation,
  IrEntity,
  IrLineClass,
  IrPatchOp,
  acceptVectorize,
  artifactUrl,
  compileFeatureTreeCandidate,
  getFeatureTreeCandidates,
  getIr,
  patchIr,
  revertIr,
  runFullCheck,
  solveIr,
  sourceUrl,
} from "@/lib/studio-api";
import CadModelViewer from "@/components/studio/CadModelViewer";

type Tool =
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
  tool,
}: {
  e: IrEntity;
  selected: boolean;
  flagged: boolean;
  onClick: (id: string) => void;
  arrowLen: number;
  tool: Tool;
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
      // select/fillet/chamfer need EXCLUSIVE entity-click handling (pick
      // this entity, nothing else). Every other tool (line/circle/dim/
      // mirror/polyline/hatch) is drawing something NEW — a click that
      // visually lands on existing geometry should still register as a
      // normal canvas point (snapPoint already prefers snapping to nearby
      // entity endpoints/intersections), not be silently swallowed just
      // because the cursor happened to be over a stroke.
      if (tool === "select" || tool === "fillet" || tool === "chamfer") {
        ev.stopPropagation();
      }
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
  if (e.type === "hatch" && e.boundary) {
    // <polygon> can't represent holes; a <path> with fill-rule="evenodd"
    // and one "M...Z" subpath per loop (outer + holes) renders holes as
    // actual gaps instead of painting over them.
    const loops = [e.boundary, ...(e.holes ?? [])];
    const d = loops
      .map((loop) => "M " + loop.map((p) => `${p.x} ${p.y}`).join(" L ") + " Z")
      .join(" ");
    return <path d={d} {...common} fillOpacity={0.1} fillRule="evenodd" />;
  }
  if (e.type === "polyline" && e.points) {
    const pts = e.points.map((p) => `${p.x},${p.y}`).join(" ");
    return e.closed ? (
      <polygon points={pts} {...common} fillOpacity={0} />
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
  // Separate from `busy`: full-check (LLM/VLM review levels) can run up to
  // ~180s server-side, unlike every other apply() here which is a fast
  // deterministic PATCH — the button needs its own elapsed-time readout so
  // a long wait doesn't look identical to a hang.
  const [fullCheckRunning, setFullCheckRunning] = useState(false);
  const [fullCheckElapsed, setFullCheckElapsed] = useState(0);
  const [fullCheckedRevision, setFullCheckedRevision] = useState<number | null>(
    typeof gen.params?.full_check_revision === "number"
      ? gen.params.full_check_revision
      : null,
  );
  const [featureCandidates, setFeatureCandidates] = useState<FeatureTreeCandidate[]>([]);
  const [selectedCandidateIndex, setSelectedCandidateIndex] = useState(
    typeof gen.params?.cad_candidate_index === "number" ? gen.params.cad_candidate_index : 0,
  );
  const [cadFeatureOverrides, setCadFeatureOverrides] = useState<FeatureParameterOverride[]>(
    () => Array.isArray(gen.params?.cad_feature_overrides)
      ? gen.params.cad_feature_overrides as FeatureParameterOverride[]
      : [],
  );
  const [cadParametersDirty, setCadParametersDirty] = useState(false);
  const [cadAddedFeatures, setCadAddedFeatures] = useState<AddedCadFeature[]>(
    () => Array.isArray(gen.params?.cad_added_features)
      ? (gen.params.cad_added_features as (AddedCadFeature | AddedCadEdgeFeature)[])
        .filter((feature): feature is AddedCadFeature => feature.kind === "boss" || feature.kind === "pocket")
      : [],
  );
  const [cadEdgeFeatures, setCadEdgeFeatures] = useState<AddedCadEdgeFeature[]>(
    () => Array.isArray(gen.params?.cad_added_features)
      ? (gen.params.cad_added_features as (AddedCadFeature | AddedCadEdgeFeature)[])
        .filter((feature): feature is AddedCadEdgeFeature => feature.kind === "fillet" || feature.kind === "chamfer")
      : [],
  );
  const [cadBuiltCandidateIndex, setCadBuiltCandidateIndex] = useState<number | null>(
    typeof gen.params?.cad_candidate_index === "number" ? gen.params.cad_candidate_index : null,
  );
  const [cadCandidatesLoading, setCadCandidatesLoading] = useState(false);
  const [cadBuilding, setCadBuilding] = useState(false);
  const [cadPreviewVersion, setCadPreviewVersion] = useState(0);
  const [cadReadyRevision, setCadReadyRevision] = useState<number | null>(
    typeof gen.params?.cad_artifact_revision === "number"
      ? gen.params.cad_artifact_revision
      : null,
  );
  const [err, setErr] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [tool, setTool] = useState<Tool>("select");
  const [showSource, setShowSource] = useState(true);
  const hasNormalizedSource = Boolean(gen.params?.normalized_source_path);
  const [sourceVariant, setSourceVariant] = useState<"original" | "normalized">(
    hasNormalizedSource ? "normalized" : "original",
  );
  const [viewBox, setViewBox] = useState<{
    x: number;
    y: number;
    width: number;
    height: number;
  } | null>(null);
  const [panStart, setPanStart] = useState<{
    clientX: number;
    clientY: number;
    viewBox: { x: number; y: number; width: number; height: number };
  } | null>(null);
  const [draftStart, setDraftStart] = useState<{ x: number; y: number } | null>(
    null,
  );
  const [cursor, setCursor] = useState<{ x: number; y: number } | null>(null);
  const [scaleInput, setScaleInput] = useState("");
  const [textInput, setTextInput] = useState("");
  const [parameterName, setParameterName] = useState("");
  const [parameterValue, setParameterValue] = useState("");
  const [constraintKind, setConstraintKind] = useState<"horizontal" | "vertical">("horizontal");
  const [reviewFilter, setReviewFilter] = useState<string>("all");
  const [reviewPage, setReviewPage] = useState(0);
  const [visibleLayers, setVisibleLayers] = useState<Set<IrLineClass>>(
    () => new Set(["contour", "thin", "axis", "hidden", "dim", "hatch"]),
  );
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
      setFullCheckedRevision(
        typeof gen.params?.full_check_revision === "number"
          ? gen.params.full_check_revision
          : null,
      );
      setCadReadyRevision(
        typeof gen.params?.cad_artifact_revision === "number"
          ? gen.params.cad_artifact_revision
          : null,
      );
      setViewBox((current) => current ?? {
        x: 0,
        y: 0,
        width: env.ir.source.image_width,
        height: env.ir.source.image_height,
      });
      setErr(null);
      historyRef.current = [env.revision];
      setHistoryIndex(0);
    } catch (e) {
      setErr(String((e as Error).message || e));
    }
  }, [gen.id, gen.params?.cad_artifact_revision, gen.params?.full_check_revision]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    let cancelled = false;
    if (!gen.accepted) {
      setFeatureCandidates([]);
      return;
    }
    setCadCandidatesLoading(true);
    void getFeatureTreeCandidates(gen.id)
      .then((items) => {
        if (!cancelled) {
          setFeatureCandidates(items);
          setSelectedCandidateIndex((current) => Math.min(current, Math.max(0, items.length - 1)));
        }
      })
      .catch((e) => !cancelled && setErr(String((e as Error).message || e)))
      .finally(() => !cancelled && setCadCandidatesLoading(false));
    return () => {
      cancelled = true;
    };
  }, [gen.accepted, gen.id, revision]);

  async function undo() {
    if (!canUndo) return;
    const target = historyRef.current[historyIndex - 1];
    setBusy(true);
    setErr(null);
    try {
      const env = await revertIr(gen.id, target);
      setIr(env.ir);
      setRevision(env.revision);
      setFullCheckedRevision(null);
      setCadReadyRevision(null);
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
      setFullCheckedRevision(null);
      setCadReadyRevision(null);
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
  const filteredPending = useMemo(() => {
    const filtered = reviewFilter === "all"
      ? pending
      : pending.filter((r) => r.reason === reviewFilter);
    const entityById = new Map((ir?.entities ?? []).map((entity) => [entity.id, entity]));
    return [...filtered].sort((a, b) => {
      const priority = (item: typeof a) => {
        const entity = entityById.get(item.entity_id);
        if (entity && (entity.type === "dimension" || entity.type === "text")) return 0;
        if (item.reason === "validation_error") return 1;
        if (item.reason === "unresolved_hypothesis") return 2;
        return 3;
      };
      return priority(a) - priority(b);
    });
  }, [pending, reviewFilter, ir]);
  const flaggedIds = useMemo(
    () => new Set(pending.map((r) => r.entity_id)),
    [pending],
  );
  const pendingIds = flaggedIds;
  const issues = useMemo(() => ir?.validation.issues ?? [], [ir]);
  const blocking = issues.filter((i) => i.severity === "error");
  const fullCheckCurrent = fullCheckedRevision === revision;
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
  const selectedCandidate = featureCandidates[selectedCandidateIndex] ?? null;
  const unresolvedCadAssumptions = useMemo(() => {
    if (!selectedCandidate) return [];
    const resolvedMarkers = new Set<string>();
    selectedCandidate.features.forEach((feature, index) => {
      const override = cadFeatureOverrides.find((item) => item.feature_index === index);
      if (feature.kind === "extrude" && override?.depth_mm != null) {
        resolvedMarkers.add("extrude-depth");
      }
      if (feature.kind === "hole" && override?.through != null) {
        resolvedMarkers.add(`hole-${Number(feature.params.diameter_mm ?? 0).toFixed(6)}`);
      }
    });
    return selectedCandidate.missing_data.filter((item) => {
      if (resolvedMarkers.has("extrude-depth") && (item.includes("бокового вида") || item.includes("глубина выдавливания"))) {
        return false;
      }
      for (const marker of resolvedMarkers) {
        if (!marker.startsWith("hole-")) continue;
        const diameter = Number(marker.slice(5));
        const match = item.match(/глубина отверстия ([\d.,]+)мм/);
        if (match && Math.abs(Number(match[1].replace(",", ".")) - diameter) < 1e-6) return false;
      }
      return true;
    });
  }, [selectedCandidate, cadFeatureOverrides]);
  const cadArtifactCurrent = cadReadyRevision === revision
    && cadBuiltCandidateIndex === selectedCandidateIndex
    && !cadParametersDirty;
  const cadReport = (gen.params?.cad_report ?? null) as {
    volume_mm3?: number;
    solid_count?: number;
    bounds_mm?: { x?: number; y?: number; z?: number };
    warnings?: string[];
    edges?: Array<{
      key: string;
      index: number;
      curve: string;
      length_mm: number;
      vertices: Array<{ x: number; y: number; z: number }>;
    }>;
  } | null;

  function focusEntity(entityId: string) {
    if (!ir) return;
    const entity = ir.entities.find((item) => item.id === entityId);
    if (!entity) return;
    const points: { x: number; y: number }[] = [];
    if (entity.p1) points.push(entity.p1);
    if (entity.p2) points.push(entity.p2);
    if (entity.position) points.push(entity.position);
    if (entity.points) points.push(...entity.points);
    if (entity.boundary) points.push(...entity.boundary);
    if (entity.center) {
      const radius = entity.radius ?? 8;
      points.push(
        { x: entity.center.x - radius, y: entity.center.y - radius },
        { x: entity.center.x + radius, y: entity.center.y + radius },
      );
    }
    setSelectedId(entityId);
    if (points.length === 0) return;
    const xs = points.map((point) => point.x);
    const ys = points.map((point) => point.y);
    const padding = Math.max(20, ir.source.image_width / 50);
    const width = Math.min(
      ir.source.image_width,
      Math.max(Math.max(...xs) - Math.min(...xs) + padding * 2, ir.source.image_width / 8),
    );
    const height = width * (ir.source.image_height / ir.source.image_width);
    const centerX = (Math.min(...xs) + Math.max(...xs)) / 2;
    const centerY = (Math.min(...ys) + Math.max(...ys)) / 2;
    setViewBox({
      x: Math.max(0, Math.min(ir.source.image_width - width, centerX - width / 2)),
      y: Math.max(0, Math.min(ir.source.image_height - height, centerY - height / 2)),
      width,
      height,
    });
  }

  async function apply(ops: IrPatchOp[]) {
    setBusy(true);
    setErr(null);
    try {
      const env = await patchIr(gen.id, ops);
      setIr(env.ir);
      setRevision(env.revision);
      setFullCheckedRevision(null);
      setCadReadyRevision(null);
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

  function addParameter() {
    if (!ir || !parameterName.trim()) return;
    const value = Number(parameterValue);
    if (!Number.isFinite(value)) {
      setErr("Введите числовое значение параметра");
      return;
    }
    const name = parameterName.trim();
    const next = [
      ...ir.parameters.filter((item) => item.name !== name),
      { name, value, unit: "mm" as const, expression: null },
    ];
    void apply([{ op: "set_parameters", parameters: next }]);
    setParameterName("");
    setParameterValue("");
  }

  function addSelectedConstraint() {
    if (!ir || !selected || selected.type !== "segment") return;
    void apply([{
      op: "set_constraints",
      constraints: [...ir.constraints, {
        id: `constraint_${crypto.randomUUID()}`,
        kind: constraintKind,
        refs: [],
        entity_ids: [selected.id],
        value: null,
        parameter: null,
        tolerance: 0.001,
        enabled: true,
      }],
    }]);
  }

  async function buildCadModel() {
    if (!selectedCandidate) return;
    const hasAssumptions = unresolvedCadAssumptions.length > 0;
    if (
      hasAssumptions
      && !window.confirm(t("vector.cad_confirm_assumptions", { n: selectedCandidate.missing_data.length }))
    ) {
      return;
    }
    setCadBuilding(true);
    setErr(null);
    try {
      await compileFeatureTreeCandidate(
        gen.id,
        selectedCandidateIndex,
        hasAssumptions,
        cadFeatureOverrides,
        [...cadAddedFeatures, ...cadEdgeFeatures],
      );
      setCadReadyRevision(revision);
      setCadBuiltCandidateIndex(selectedCandidateIndex);
      setCadParametersDirty(false);
      setCadPreviewVersion((value) => value + 1);
      onChanged();
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setCadBuilding(false);
    }
  }

  function featureOverride(index: number): FeatureParameterOverride | undefined {
    return cadFeatureOverrides.find((item) => item.feature_index === index);
  }

  function updateFeatureOverride(index: number, patch: Omit<FeatureParameterOverride, "feature_index">) {
    setCadFeatureOverrides((current) => {
      const existing = current.find((item) => item.feature_index === index) ?? { feature_index: index };
      return [
        ...current.filter((item) => item.feature_index !== index),
        { ...existing, ...patch },
      ].sort((a, b) => a.feature_index - b.feature_index);
    });
    setCadParametersDirty(true);
  }

  function addCadFeature(kind: "boss" | "pocket") {
    if (!selectedCandidate) return;
    const base = selectedCandidate.features.find((feature) => feature.kind === "extrude");
    const width = Number(base?.params.width_mm ?? 100);
    const height = Number(base?.params.height_mm ?? 100);
    const depth = featureOverride(selectedCandidate.features.indexOf(base!))?.depth_mm
      ?? Number(base?.params.depth_mm ?? 10);
    setCadAddedFeatures((current) => [
      ...current,
      {
        kind,
        profile: "circle",
        center_x_mm: width / 2,
        center_y_mm: height / 2,
        depth_mm: Math.max(0.1, Math.min(depth / 4, kind === "pocket" ? depth - 0.1 : depth)),
        diameter_mm: Math.max(0.1, Math.min(width, height) / 4),
      },
    ]);
    setCadParametersDirty(true);
  }

  function updateAddedCadFeature(index: number, patch: Partial<AddedCadFeature>) {
    setCadAddedFeatures((current) => current.map((feature, itemIndex) => {
      if (itemIndex !== index) return feature;
      const updated = { ...feature, ...patch };
      if (patch.profile === "circle") {
        delete updated.width_mm;
        delete updated.height_mm;
        updated.diameter_mm ??= 10;
      } else if (patch.profile === "rectangle") {
        delete updated.diameter_mm;
        updated.width_mm ??= 10;
        updated.height_mm ??= 10;
      }
      return updated;
    }));
    setCadParametersDirty(true);
  }

  function removeAddedCadFeature(index: number) {
    setCadAddedFeatures((current) => current.filter((_, itemIndex) => itemIndex !== index));
    setCadParametersDirty(true);
  }

  function addCadEdgeFeature(kind: "fillet" | "chamfer") {
    const edge = cadReport?.edges?.[0];
    if (!edge) return;
    setCadEdgeFeatures((current) => [
      ...current,
      { kind, edge_key: edge.key, size_mm: 1 },
    ]);
    setCadParametersDirty(true);
  }

  function updateCadEdgeFeature(index: number, patch: Partial<AddedCadEdgeFeature>) {
    setCadEdgeFeatures((current) => current.map((feature, itemIndex) => (
      itemIndex === index ? { ...feature, ...patch } : feature
    )));
    setCadParametersDirty(true);
  }

  function removeCadEdgeFeature(index: number) {
    setCadEdgeFeatures((current) => current.filter((_, itemIndex) => itemIndex !== index));
    setCadParametersDirty(true);
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
    // Same in-flight guard as canvasClick — dblclick is a distinct handler
    // from the click chain that builds polylinePoints, so it isn't covered
    // by the guard added there.
    if (busy) return;
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
    if (!svg || !ir || !viewBox) return null;
    const rect = svg.getBoundingClientRect();
    const x = viewBox.x + ((ev.clientX - rect.left) / rect.width) * viewBox.width;
    const y = viewBox.y + ((ev.clientY - rect.top) / rect.height) * viewBox.height;
    return { x, y };
  }

  function canvasClick(ev: React.MouseEvent) {
    if (!ir) return;
    // A previous apply() (PATCH /ir) is still in flight — a rapid double-
    // click (or clicking again before the network round-trip finishes)
    // would otherwise fire a second concurrent PATCH for the same
    // generation. Toolbar buttons already have this via disabled={busy};
    // the canvas itself didn't.
    if (busy) return;
    const raw = svgPoint(ev);
    if (!raw) return;
    if (tool === "pan") return;
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

  const activeViewBox = viewBox ?? {
    x: 0,
    y: 0,
    width: ir.source.image_width,
    height: ir.source.image_height,
  };
  const vb = `${activeViewBox.x} ${activeViewBox.y} ${activeViewBox.width} ${activeViewBox.height}`;
  const toolButtons: { key: Tool; label: string }[] = [
    { key: "select", label: t("vector.tool_select") },
    { key: "pan", label: t("vector.tool_pan") },
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
  const layerClasses: IrLineClass[] = ["contour", "thin", "axis", "hidden", "dim", "hatch"];
  const layerColors: Record<IrLineClass, string> = {
    contour: "#f4f4f5",
    thin: "#a1a1aa",
    axis: "#22d3ee",
    hidden: "#a78bfa",
    dim: "#facc15",
    hatch: "#34d399",
  };
  const arrowLen = dimArrowLenPx(ir.scale);
  const reviewPageSize = 100;
  const reviewPageCount = Math.max(1, Math.ceil(filteredPending.length / reviewPageSize));
  const safeReviewPage = Math.min(reviewPage, reviewPageCount - 1);
  const visibleReview = filteredPending.slice(
    safeReviewPage * reviewPageSize,
    (safeReviewPage + 1) * reviewPageSize,
  );
  const imageWidth = ir.source.image_width;
  const imageHeight = ir.source.image_height;

  function zoomAt(factor: number, centerX?: number, centerY?: number) {
    setViewBox((current) => {
      if (!current) return current;
      const cx = centerX ?? current.x + current.width / 2;
      const cy = centerY ?? current.y + current.height / 2;
      const width = Math.min(imageWidth, Math.max(imageWidth / 50, current.width * factor));
      const height = width * (imageHeight / imageWidth);
      const rx = (cx - current.x) / current.width;
      const ry = (cy - current.y) / current.height;
      return {
        x: Math.max(0, Math.min(imageWidth - width, cx - width * rx)),
        y: Math.max(0, Math.min(imageHeight - height, cy - height * ry)),
        width,
        height,
      };
    });
  }

  return (
    <div className="flex flex-col gap-3 pb-24 sm:pb-0">
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
        <button
          type="button"
          onClick={() => zoomAt(0.75)}
          title={t("vector.zoom_in")}
          className="px-2 py-1 rounded text-xs bg-white/5 text-zinc-300 hover:bg-white/10"
        >
          +
        </button>
        <button
          type="button"
          onClick={() => zoomAt(1.25)}
          title={t("vector.zoom_out")}
          className="px-2 py-1 rounded text-xs bg-white/5 text-zinc-300 hover:bg-white/10"
        >
          −
        </button>
        <button
          type="button"
          onClick={() => setViewBox({
            x: 0,
            y: 0,
            width: ir.source.image_width,
            height: ir.source.image_height,
          })}
          title={t("vector.zoom_fit")}
          className="px-2 py-1 rounded text-xs bg-white/5 text-zinc-300 hover:bg-white/10"
        >
          ⛶
        </button>
        <div className="flex items-center gap-1 border-l border-white/10 pl-2">
          {layerClasses.map((layer) => {
            const visible = visibleLayers.has(layer);
            return (
              <button
                key={layer}
                type="button"
                title={`${visible ? t("vector.layer_hide") : t("vector.layer_show")}: ${t(`vector.line_${layer}`)}`}
                onClick={() => setVisibleLayers((current) => {
                  const next = new Set(current);
                  if (next.has(layer)) next.delete(layer);
                  else next.add(layer);
                  return next;
                })}
                className={`h-6 w-6 rounded border ${visible ? "border-white/30 bg-white/10" : "border-white/10 opacity-35"}`}
              >
                <span
                  className="mx-auto block h-2.5 w-2.5 rounded-sm"
                  style={{ backgroundColor: layerColors[layer] }}
                />
              </button>
            );
          })}
        </div>
        {hasSource && (
          <div className="ml-auto flex items-center gap-2 text-[11px] text-zinc-400">
            {hasNormalizedSource && showSource && (
              <select
                value={sourceVariant}
                onChange={(e) => setSourceVariant(e.target.value as "original" | "normalized")}
                className="rounded bg-zinc-900 border border-white/10 px-1.5 py-1 text-zinc-200"
              >
                <option value="normalized">{t("vector.source_normalized")}</option>
                <option value="original">{t("vector.source_original")}</option>
              </select>
            )}
            <label className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={showSource}
                onChange={(e) => setShowSource(e.target.checked)}
              />
              {t("vector.show_source")}
            </label>
          </div>
        )}
      </div>

      {/* Canvas: source photo + entity overlay */}
      <div
        className="relative w-full rounded border border-white/10 bg-white overflow-hidden"
        style={{
          aspectRatio: `${ir.source.image_width} / ${ir.source.image_height}`,
        }}
      >
        <svg
          ref={svgRef}
          viewBox={vb}
          className={`absolute inset-0 h-full w-full ${tool === "pan" ? "cursor-grab" : ""} ${panStart ? "cursor-grabbing" : ""}`}
          onClick={canvasClick}
          onMouseDown={(ev) => {
            if (tool !== "pan" || !viewBox) return;
            ev.preventDefault();
            setPanStart({ clientX: ev.clientX, clientY: ev.clientY, viewBox });
          }}
          onMouseUp={() => setPanStart(null)}
          onMouseLeave={() => setPanStart(null)}
          onDoubleClick={() => {
            if (tool === "polyline") finishPolyline();
          }}
          onWheel={(ev) => {
            ev.preventDefault();
            const point = svgPoint(ev);
            zoomAt(ev.deltaY < 0 ? 0.8 : 1.25, point?.x, point?.y);
          }}
          onMouseMove={(ev) => {
            if (panStart && svgRef.current) {
              const rect = svgRef.current.getBoundingClientRect();
              const dx = ((ev.clientX - panStart.clientX) / rect.width) * panStart.viewBox.width;
              const dy = ((ev.clientY - panStart.clientY) / rect.height) * panStart.viewBox.height;
              setViewBox({
                ...panStart.viewBox,
                x: Math.max(0, Math.min(ir.source.image_width - panStart.viewBox.width, panStart.viewBox.x - dx)),
                y: Math.max(0, Math.min(ir.source.image_height - panStart.viewBox.height, panStart.viewBox.y - dy)),
              });
              return;
            }
            setCursor(svgPoint(ev));
          }}
        >
          {hasSource && showSource && (
            <image
              href={sourceUrl(gen.id, 0, sourceVariant)}
              x={0}
              y={0}
              width={ir.source.image_width}
              height={ir.source.image_height}
              opacity={0.45}
              preserveAspectRatio="none"
            />
          )}
          {ir.entities.filter((e) => visibleLayers.has(e.line_class)).map((e) => (
            <EntityShape
              key={e.id}
              e={e}
              selected={e.id === selectedId || e.id === pickedSegmentId}
              flagged={flaggedIds.has(e.id)}
              arrowLen={arrowLen}
              tool={tool}
              onClick={(id) => {
                // Same in-flight guard as canvasClick — this is a separate
                // handler (EntityShape's onClick, not the SVG's onClick) so
                // it needs its own check before the fillet/chamfer second
                // pick can fire apply().
                if (busy) return;
                if (tool === "select") {
                  setSelectedId(id === selectedId ? null : id);
                  return;
                }
                if (tool === "fillet" || tool === "chamfer") {
                  const clicked = ir.entities.find((x) => x.id === id);
                  if (!clicked || clicked.type !== "segment") {
                    // Previously silent no-op — a click that visibly does
                    // nothing reads as a broken tool, not "wrong pick, try
                    // again". Surface it in the same error banner apply()
                    // failures use, without cancelling an already-picked
                    // first segment.
                    setErr(t("vector.fillet_chamfer_not_a_segment"));
                    return;
                  }
                  setErr(null);
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
                onChange={(e) => {
                  setReviewFilter(e.target.value);
                  setReviewPage(0);
                }}
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
            <div className="flex items-center gap-1">
              <button
                type="button"
                disabled={busy || visibleReview.length === 0}
                onClick={() => apply(visibleReview.map((item) => ({
                  op: "confirm" as const,
                  entity_id: item.entity_id,
                })))}
                className="rounded bg-emerald-600/70 px-2 py-0.5 text-[11px] text-white hover:bg-emerald-500 disabled:opacity-40"
              >
                {t("vector.review_confirm_page")}
              </button>
              <button
                type="button"
                disabled={busy || visibleReview.length === 0}
                onClick={() => {
                  if (window.confirm(t("vector.review_delete_page_confirm", { n: visibleReview.length }))) {
                    void apply(visibleReview.map((item) => ({
                      op: "delete" as const,
                      entity_id: item.entity_id,
                    })));
                  }
                }}
                className="rounded bg-red-600/60 px-2 py-0.5 text-[11px] text-white hover:bg-red-500 disabled:opacity-40"
              >
                {t("vector.review_delete_page")}
              </button>
            </div>
          </div>
          <div className="max-h-48 overflow-y-auto space-y-1">
            {visibleReview.map((r) => {
              const e = ir.entities.find((x) => x.id === r.entity_id);
              if (!e) return null;
              return (
                <div
                  key={r.entity_id}
                  className="flex items-center gap-2 text-[11px]"
                >
                  <button
                    onClick={() => focusEntity(r.entity_id)}
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
          {reviewPageCount > 1 && (
            <div className="flex items-center justify-between text-[11px] text-zinc-400">
              <button
                type="button"
                disabled={safeReviewPage === 0}
                onClick={() => setReviewPage((page) => Math.max(0, page - 1))}
                className="rounded bg-white/5 px-2 py-0.5 hover:bg-white/10 disabled:opacity-30"
              >
                {t("vector.review_prev")}
              </button>
              <span>{t("vector.review_page", { page: safeReviewPage + 1, pages: reviewPageCount, n: filteredPending.length })}</span>
              <button
                type="button"
                disabled={safeReviewPage >= reviewPageCount - 1}
                onClick={() => setReviewPage((page) => Math.min(reviewPageCount - 1, page + 1))}
                className="rounded bg-white/5 px-2 py-0.5 hover:bg-white/10 disabled:opacity-30"
              >
                {t("vector.review_next")}
              </button>
            </div>
          )}
        </div>
      )}

      {/* Validation report — grouped by assurance-pipeline level (Ф7.2) */}
      <div className="rounded border border-white/10 bg-zinc-900/60 p-2 space-y-2">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-xs text-zinc-300">
                {t("vector.validation_title")}
              </div>
              <div className={`text-[10px] ${fullCheckCurrent ? "text-emerald-400" : "text-amber-300"}`}>
                {fullCheckCurrent
                  ? t("vector.full_check_current")
                  : t("vector.full_check_required")}
              </div>
            </div>
            <button
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                setErr(null);
                setFullCheckRunning(true);
                setFullCheckElapsed(0);
                const timer = window.setInterval(
                  () => setFullCheckElapsed((s) => s + 1),
                  1000,
                );
                try {
                  const env = await runFullCheck(gen.id);
                  setIr(env.ir);
                  setRevision(env.revision);
                  setFullCheckedRevision(env.revision);
                  onChanged();
                } catch (e) {
                  setErr(String((e as Error).message || e));
                } finally {
                  window.clearInterval(timer);
                  setFullCheckRunning(false);
                  setBusy(false);
                }
              }}
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
                Ограничения: {ir.constraints.length}; параметры: {ir.parameters.length}
              </div>
              <button
                type="button"
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  setErr(null);
                  try {
                    const env = await solveIr(gen.id);
                    setIr(env.ir);
                    setRevision(env.revision);
                    setFullCheckedRevision(null);
                    onChanged();
                  } catch (e) {
                    setErr(String((e as Error).message || e));
                  } finally {
                    setBusy(false);
                  }
                }}
                className="rounded bg-sky-600 px-2 py-1 text-[11px] text-white hover:bg-sky-500 disabled:opacity-50"
              >
                Перестроить
              </button>
            </div>
          )}
          <div className="space-y-2 border-t border-white/10 pt-2">
            <div className="text-[11px] text-zinc-400">Параметры эскиза</div>
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
              <input value={parameterName} onChange={(event) => setParameterName(event.target.value)} placeholder="Имя" className="min-w-0 rounded border border-white/10 bg-zinc-950 px-2 py-1 text-[11px] text-zinc-100" />
              <input value={parameterValue} onChange={(event) => setParameterValue(event.target.value)} inputMode="decimal" placeholder="мм" className="min-w-0 rounded border border-white/10 bg-zinc-950 px-2 py-1 text-[11px] text-zinc-100" />
              <button type="button" disabled={busy} onClick={addParameter} className="rounded bg-white/10 px-2 py-1 text-[11px] text-zinc-200 hover:bg-white/20 disabled:opacity-50">Сохранить</button>
            </div>
          </div>
          {selected?.type === "segment" && (
            <div className="flex items-center justify-between gap-2 border-t border-white/10 pt-2">
              <select value={constraintKind} onChange={(event) => setConstraintKind(event.target.value as "horizontal" | "vertical")} disabled={busy} className="rounded border border-white/10 bg-zinc-950 px-2 py-1 text-[11px] text-zinc-200">
                <option value="horizontal">Горизонтальность</option>
                <option value="vertical">Вертикальность</option>
              </select>
              <button type="button" disabled={busy} onClick={addSelectedConstraint} className="rounded bg-white/10 px-2 py-1 text-[11px] text-zinc-200 hover:bg-white/20 disabled:opacity-50">Ограничить отрезок</button>
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
                      onClick={() => focusEntity(issue.entity_ids[0])}
                      className="ml-1 text-sky-400 hover:text-sky-300"
                    >
                      {t("vector.show_entity")}
                    </button>
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

      {!gen.accepted && criticalUnresolved.length > 0 && (
        <div className="rounded border border-amber-500/20 bg-amber-500/5 p-2 text-[11px] text-amber-300">
          {t("vector.accept_blocked_critical", {
            n: criticalUnresolved.length,
          })}
        </div>
      )}
      {!gen.accepted && pending.length > 0 && criticalUnresolved.length === 0 && (
        <div className="rounded border border-amber-500/20 bg-amber-500/5 p-2 text-[11px] text-amber-300">
          {t("vector.accept_blocked_review", { n: pending.length })}
        </div>
      )}
      {!gen.accepted && blocking.length === 0 && pending.length === 0 && !fullCheckCurrent && (
        <div className="rounded border border-amber-500/20 bg-amber-500/5 p-2 text-[11px] text-amber-300">
          {t("vector.accept_blocked_full_check")}
        </div>
      )}

      {gen.accepted && (
        <section className="space-y-3 border-t border-white/10 pt-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <div className="text-sm font-medium text-zinc-100">{t("vector.cad_title")}</div>
              <div className="text-[11px] text-zinc-500">{t("vector.cad_revision", { revision })}</div>
            </div>
            {featureCandidates.length > 0 && (
              <div className="flex min-w-0 items-center gap-2">
                <select
                  value={selectedCandidateIndex}
                  onChange={(event) => {
                    setSelectedCandidateIndex(Number(event.target.value));
                    setCadFeatureOverrides([]);
                    setCadAddedFeatures([]);
                    setCadEdgeFeatures([]);
                    setCadParametersDirty(true);
                  }}
                  disabled={cadBuilding}
                  aria-label={t("vector.cad_candidate")}
                  className="min-w-0 max-w-[420px] rounded border border-white/10 bg-zinc-900 px-2 py-1 text-xs text-zinc-200"
                >
                  {featureCandidates.map((candidate, index) => (
                    <option key={`${candidate.label}-${index}`} value={index}>
                      {index + 1}. {candidate.label} ({Math.round(candidate.score * 100)}%)
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  disabled={cadBuilding || !selectedCandidate}
                  onClick={() => void buildCadModel()}
                  className="shrink-0 rounded bg-sky-600 px-3 py-1.5 text-xs text-white hover:bg-sky-500 disabled:opacity-50"
                >
                  {cadBuilding ? t("vector.cad_building") : t("vector.cad_build")}
                </button>
              </div>
            )}
          </div>

          {cadCandidatesLoading && (
            <div className="text-xs text-zinc-500">{t("vector.cad_candidates_loading")}</div>
          )}
          {!cadCandidatesLoading && featureCandidates.length === 0 && (
            <div className="text-xs text-amber-300">{t("vector.cad_no_candidates")}</div>
          )}
          {selectedCandidate && unresolvedCadAssumptions.length > 0 && (
            <div className="border-l-2 border-amber-400/60 pl-3">
              <div className="text-[11px] font-medium text-amber-300">
                {t("vector.cad_missing_title", { n: unresolvedCadAssumptions.length })}
              </div>
              <ul className="mt-1 space-y-0.5 text-[11px] text-zinc-400">
                {unresolvedCadAssumptions.map((item, index) => (
                  <li key={`${item}-${index}`}>{item}</li>
                ))}
              </ul>
            </div>
          )}

          {selectedCandidate && (
            <div data-testid="cad-feature-tree" className="border-y border-white/10 py-2">
              <div className="mb-2 text-[11px] font-medium uppercase text-zinc-400">
                {t("vector.cad_tree_title")}
              </div>
              <div className="space-y-2">
                {selectedCandidate.features.map((feature, index) => {
                  const override = featureOverride(index);
                  if (feature.kind === "extrude") {
                    const depth = override?.depth_mm ?? Number(feature.params.depth_mm ?? 0);
                    return (
                      <div key={`feature-${index}`} className="grid grid-cols-[minmax(110px,1fr)_120px] items-center gap-3 text-xs">
                        <div className="min-w-0">
                          <div className="text-zinc-200">{index + 1}. {t("vector.cad_extrude")}</div>
                          <div className="truncate text-[11px] text-zinc-500">
                            {Number(feature.params.width_mm ?? 0).toFixed(2)} × {Number(feature.params.height_mm ?? 0).toFixed(2)} mm
                          </div>
                        </div>
                        <label className="grid grid-cols-[1fr_auto] items-center gap-1 text-[11px] text-zinc-400">
                          <input
                            type="number"
                            min="0.01"
                            step="0.1"
                            value={depth}
                            aria-label={t("vector.cad_depth")}
                            onChange={(event) => updateFeatureOverride(index, { depth_mm: Number(event.target.value) })}
                            className="min-w-0 rounded border border-white/10 bg-zinc-900 px-2 py-1 text-right text-xs text-zinc-100"
                          />
                          mm
                        </label>
                      </div>
                    );
                  }
                  if (feature.kind === "hole") {
                    const hasThroughOverride = override && Object.prototype.hasOwnProperty.call(override, "through");
                    const through = hasThroughOverride ? override?.through : feature.params.through as boolean | null | undefined;
                    const base = selectedCandidate.features.find((item) => item.kind === "extrude");
                    const baseIndex = selectedCandidate.features.findIndex((item) => item.kind === "extrude");
                    const baseDepth = featureOverride(baseIndex)?.depth_mm ?? Number(base?.params.depth_mm ?? 10);
                    const blindDepth = override?.depth_mm ?? Math.max(0.1, Math.min(baseDepth / 2, baseDepth - 0.1));
                    return (
                      <div key={`feature-${index}`} className="grid grid-cols-[minmax(110px,1fr)_minmax(150px,220px)] items-center gap-3 text-xs">
                        <div className="min-w-0">
                          <div className="text-zinc-200">
                            {index + 1}. {t("vector.cad_hole")} ⌀{Number(feature.params.diameter_mm ?? 0).toFixed(2)} mm
                          </div>
                          <div className="truncate text-[11px] text-zinc-500">
                            {t("vector.cad_hole_position", {
                              x: Number(feature.params.center_x_mm ?? 0).toFixed(2),
                              y: Number(feature.params.center_y_mm ?? 0).toFixed(2),
                            })}
                          </div>
                        </div>
                        <div className="flex min-w-0 items-center gap-2">
                          <select
                            value={through === true ? "through" : through === false ? "blind" : "unknown"}
                            aria-label={t("vector.cad_hole_type")}
                            onChange={(event) => {
                              if (event.target.value === "through") updateFeatureOverride(index, { through: true, depth_mm: undefined });
                              else if (event.target.value === "blind") updateFeatureOverride(index, { through: false, depth_mm: blindDepth });
                              else updateFeatureOverride(index, { through: null, depth_mm: undefined });
                            }}
                            className="min-w-0 flex-1 rounded border border-white/10 bg-zinc-900 px-2 py-1 text-xs text-zinc-200"
                          >
                            <option value="through">{t("vector.cad_through")}</option>
                            <option value="blind">{t("vector.cad_blind")}</option>
                            <option value="unknown">{t("vector.cad_unknown")}</option>
                          </select>
                          {through === false && (
                            <label className="flex w-[92px] items-center gap-1 text-[11px] text-zinc-400">
                              <input
                                type="number"
                                min="0.01"
                                max={Math.max(0.01, baseDepth - 0.01)}
                                step="0.1"
                                value={blindDepth}
                                aria-label={t("vector.cad_blind_depth")}
                                onChange={(event) => updateFeatureOverride(index, { through: false, depth_mm: Number(event.target.value) })}
                                className="min-w-0 rounded border border-white/10 bg-zinc-900 px-2 py-1 text-right text-xs text-zinc-100"
                              />
                              mm
                            </label>
                          )}
                        </div>
                      </div>
                    );
                  }
                  return null;
                })}
                {cadAddedFeatures.map((feature, index) => (
                  <div key={`added-feature-${index}`} className="border-t border-white/10 pt-2">
                    <div className="mb-2 flex items-center justify-between gap-2">
                      <div className="text-xs text-zinc-200">
                        {selectedCandidate.features.length + index + 1}. {t(feature.kind === "boss" ? "vector.cad_boss" : "vector.cad_pocket")}
                      </div>
                      <button
                        type="button"
                        onClick={() => removeAddedCadFeature(index)}
                        aria-label={t("vector.cad_remove_feature")}
                        title={t("vector.cad_remove_feature")}
                        className="grid h-7 w-7 place-items-center text-lg text-zinc-500 hover:text-red-400"
                      >
                        ×
                      </button>
                    </div>
                    <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                      <label className="text-[10px] text-zinc-500">
                        {t("vector.cad_profile")}
                        <select
                          value={feature.profile}
                          onChange={(event) => updateAddedCadFeature(index, { profile: event.target.value as AddedCadFeature["profile"] })}
                          className="mt-0.5 w-full rounded border border-white/10 bg-zinc-900 px-2 py-1 text-xs text-zinc-200"
                        >
                          <option value="circle">{t("vector.cad_profile_circle")}</option>
                          <option value="rectangle">{t("vector.cad_profile_rectangle")}</option>
                        </select>
                      </label>
                      {([
                        ["center_x_mm", "vector.cad_center_x"],
                        ["center_y_mm", "vector.cad_center_y"],
                        ["depth_mm", "vector.cad_operation_depth"],
                      ] as const).map(([field, label]) => (
                        <label key={field} className="text-[10px] text-zinc-500">
                          {t(label)}
                          <input
                            type="number"
                            min={field === "depth_mm" ? "0.01" : "0"}
                            step="0.1"
                            value={feature[field]}
                            onChange={(event) => updateAddedCadFeature(index, { [field]: Number(event.target.value) })}
                            className="mt-0.5 w-full rounded border border-white/10 bg-zinc-900 px-2 py-1 text-right text-xs text-zinc-100"
                          />
                        </label>
                      ))}
                    </div>
                    <div className="mt-2 grid grid-cols-2 gap-2 sm:max-w-[50%]">
                      {feature.profile === "circle" ? (
                        <label className="text-[10px] text-zinc-500">
                          {t("vector.cad_diameter")}
                          <input
                            type="number"
                            min="0.01"
                            step="0.1"
                            value={feature.diameter_mm ?? 0}
                            onChange={(event) => updateAddedCadFeature(index, { diameter_mm: Number(event.target.value) })}
                            className="mt-0.5 w-full rounded border border-white/10 bg-zinc-900 px-2 py-1 text-right text-xs text-zinc-100"
                          />
                        </label>
                      ) : (
                        <>
                          <label className="text-[10px] text-zinc-500">
                            {t("vector.cad_width")}
                            <input
                              type="number"
                              min="0.01"
                              step="0.1"
                              value={feature.width_mm ?? 0}
                              onChange={(event) => updateAddedCadFeature(index, { width_mm: Number(event.target.value) })}
                              className="mt-0.5 w-full rounded border border-white/10 bg-zinc-900 px-2 py-1 text-right text-xs text-zinc-100"
                            />
                          </label>
                          <label className="text-[10px] text-zinc-500">
                            {t("vector.cad_height")}
                            <input
                              type="number"
                              min="0.01"
                              step="0.1"
                              value={feature.height_mm ?? 0}
                              onChange={(event) => updateAddedCadFeature(index, { height_mm: Number(event.target.value) })}
                              className="mt-0.5 w-full rounded border border-white/10 bg-zinc-900 px-2 py-1 text-right text-xs text-zinc-100"
                            />
                          </label>
                        </>
                      )}
                    </div>
                  </div>
                ))}
                {cadEdgeFeatures.map((feature, index) => (
                  <div key={`edge-feature-${index}`} className="border-t border-white/10 pt-2">
                    <div className="mb-2 flex items-center justify-between gap-2">
                      <div className="text-xs text-zinc-200">
                        {selectedCandidate.features.length + cadAddedFeatures.length + index + 1}. {t(feature.kind === "fillet" ? "vector.cad_fillet" : "vector.cad_chamfer")}
                      </div>
                      <button
                        type="button"
                        onClick={() => removeCadEdgeFeature(index)}
                        aria-label={t("vector.cad_remove_feature")}
                        title={t("vector.cad_remove_feature")}
                        className="grid h-7 w-7 place-items-center text-lg text-zinc-500 hover:text-red-400"
                      >
                        ×
                      </button>
                    </div>
                    <div className="grid grid-cols-[minmax(0,1fr)_110px] gap-2">
                      <label className="text-[10px] text-zinc-500">
                        {t("vector.cad_edge")}
                        <select
                          value={feature.edge_key}
                          onChange={(event) => updateCadEdgeFeature(index, { edge_key: event.target.value })}
                          className="mt-0.5 w-full rounded border border-white/10 bg-zinc-900 px-2 py-1 text-xs text-zinc-200"
                        >
                          {(cadReport?.edges ?? []).map((edge) => (
                            <option key={edge.key} value={edge.key}>
                              #{edge.index} · {edge.curve} · {edge.length_mm.toFixed(2)} mm
                            </option>
                          ))}
                        </select>
                      </label>
                      <label className="text-[10px] text-zinc-500">
                        {t(feature.kind === "fillet" ? "vector.cad_radius" : "vector.cad_chamfer_size")}
                        <input
                          type="number"
                          min="0.01"
                          step="0.1"
                          value={feature.size_mm}
                          onChange={(event) => updateCadEdgeFeature(index, { size_mm: Number(event.target.value) })}
                          className="mt-0.5 w-full rounded border border-white/10 bg-zinc-900 px-2 py-1 text-right text-xs text-zinc-100"
                        />
                      </label>
                    </div>
                  </div>
                ))}
                <div className="flex flex-wrap gap-2 border-t border-white/10 pt-2">
                  <button
                    type="button"
                    onClick={() => addCadFeature("boss")}
                    className="rounded border border-white/10 px-2 py-1 text-xs text-zinc-300 hover:bg-white/5"
                  >
                    + {t("vector.cad_add_boss")}
                  </button>
                  <button
                    type="button"
                    onClick={() => addCadFeature("pocket")}
                    className="rounded border border-white/10 px-2 py-1 text-xs text-zinc-300 hover:bg-white/5"
                  >
                    + {t("vector.cad_add_pocket")}
                  </button>
                  {(cadReport?.edges?.length ?? 0) > 0 && (
                    <>
                      <button
                        type="button"
                        onClick={() => addCadEdgeFeature("fillet")}
                        className="rounded border border-white/10 px-2 py-1 text-xs text-zinc-300 hover:bg-white/5"
                      >
                        + {t("vector.cad_add_fillet")}
                      </button>
                      <button
                        type="button"
                        onClick={() => addCadEdgeFeature("chamfer")}
                        className="rounded border border-white/10 px-2 py-1 text-xs text-zinc-300 hover:bg-white/5"
                      >
                        + {t("vector.cad_add_chamfer")}
                      </button>
                    </>
                  )}
                </div>
              </div>
              {cadParametersDirty && (
                <div className="mt-2 text-[11px] text-amber-300">{t("vector.cad_rebuild_required")}</div>
              )}
            </div>
          )}

          {cadArtifactCurrent && (
            <>
              <CadModelViewer
                url={`${artifactUrl(gen.id, "stl")}&v=${cadPreviewVersion}`}
                loadingLabel={t("vector.cad_preview_loading")}
                errorLabel={t("vector.cad_preview_error")}
              />
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-zinc-400">
                {cadReport?.bounds_mm && (
                  <span>
                    {t("vector.cad_bounds", {
                      x: Number(cadReport.bounds_mm.x ?? 0).toFixed(2),
                      y: Number(cadReport.bounds_mm.y ?? 0).toFixed(2),
                      z: Number(cadReport.bounds_mm.z ?? 0).toFixed(2),
                    })}
                  </span>
                )}
                {typeof cadReport?.volume_mm3 === "number" && (
                  <span>{t("vector.cad_volume", { value: cadReport.volume_mm3.toFixed(2) })}</span>
                )}
                <a
                  href={artifactUrl(gen.id, "step")}
                  download={`studio-${gen.id}.step`}
                  className="text-sky-400 hover:text-sky-300"
                >
                  STEP
                </a>
                <a
                  href={artifactUrl(gen.id, "fcstd")}
                  download={`studio-${gen.id}.FCStd`}
                  className="text-sky-400 hover:text-sky-300"
                >
                  FCStd
                </a>
              </div>
              {(cadReport?.warnings?.length ?? 0) > 0 && (
                <div className="text-[11px] text-amber-300">
                  {cadReport?.warnings?.join("; ")}
                </div>
              )}
            </>
          )}
        </section>
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
              busy || blocking.length > 0 || pending.length > 0 || !fullCheckCurrent
            }
            title={
              blocking.length > 0
                ? t("vector.accept_blocked")
                : pending.length > 0
                  ? t("vector.accept_blocked_review", {
                      n: pending.length,
                    })
                  : !fullCheckCurrent
                    ? t("vector.accept_blocked_full_check")
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
