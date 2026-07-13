"use client";

import { IrEntity } from "@/lib/studio-api";

import {
  annotationText,
  ASSURANCE_STROKE,
  dimensionArrowPolygons,
  dimensionLabel,
  Tool,
} from "@/components/cad/geometry";

export default function EntityShape({
  e,
  selected,
  flagged,
  onClick,
  arrowLen,
  tool,
  locked = false,
}: {
  e: IrEntity;
  selected: boolean;
  flagged: boolean;
  onClick: (id: string, ev: React.MouseEvent) => void;
  arrowLen: number;
  tool: Tool;
  locked?: boolean;
}) {
  const construction = e.construction === true;
  const stroke = selected
    ? "#38bdf8"
    : flagged
      ? "#f59e0b"
      : construction
        ? "#22d3ee" // A2: auxiliary geometry — faint cyan, canvas-only
        : (ASSURANCE_STROKE[e.assurance] ?? "#a1a1aa");
  const strokeWidth = e.width_class === "main" ? 2.5 : 1.5;
  const dash = construction
    ? "4 4"
    : e.line_class === "axis"
      ? "12 3 3 3"
      : e.line_class === "hidden"
        ? "8 4"
        : undefined;
  const common = {
    stroke,
    strokeWidth: selected ? strokeWidth + 1 : construction ? 1 : strokeWidth,
    strokeDasharray: dash,
    strokeOpacity: construction && !selected ? 0.6 : undefined,
    fill: "none" as const,
    // I4: a locked layer is visible reference geometry — click-through so the
    // user can still snap to it while drawing, but it can't be picked/edited.
    style: locked
      ? ({ cursor: "default", pointerEvents: "none" } as const)
      : ({ cursor: "pointer" } as const),
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
      onClick(e.id, ev);
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
  if (e.type === "annotation" && e.position) {
    const label = annotationText(e);
    const h = e.height ?? 12;
    const boxed = e.kind === "tolerance" || e.kind === "datum";
    return (
      <g
        style={
          locked
            ? { cursor: "default", pointerEvents: "none" }
            : { cursor: "pointer" }
        }
        onClick={(ev) => {
          ev.stopPropagation();
          onClick(e.id, ev);
        }}
      >
        {e.leader && (
          <line
            x1={e.position.x}
            y1={e.position.y}
            x2={e.leader.x}
            y2={e.leader.y}
            stroke={stroke}
            strokeWidth={1}
          />
        )}
        {boxed && (
          <rect
            x={e.position.x - h * 0.3}
            y={e.position.y - h}
            width={Math.max(label.length * h * 0.62, h * 1.6)}
            height={h * 1.4}
            fill="none"
            stroke={stroke}
            strokeWidth={1}
          />
        )}
        <text
          x={e.position.x}
          y={e.position.y}
          fontSize={h}
          fill={stroke}
          stroke="none"
        >
          {label}
        </text>
      </g>
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
          onClick(e.id, ev);
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
