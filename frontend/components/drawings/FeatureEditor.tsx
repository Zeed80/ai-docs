"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import clsx from "clsx";
import type {
  DrawingFeature,
  FeatureContour,
  PrimitiveType,
} from "@/lib/drawings-api";

type LineType = "solid" | "dashed" | "dotted" | "center" | "phantom";

interface EditablePrimitive {
  id: string;
  primitive_type: PrimitiveType;
  params: Record<string, unknown>;
  line_type: LineType;
  color?: string;
  sort_order: number;
}

interface FeatureEditorProps {
  feature: DrawingFeature;
  svgViewBox?: { x: number; y: number; width: number; height: number };
  onSave: (
    contours: Omit<
      FeatureContour,
      "id" | "feature_id" | "created_at" | "is_user_edited"
    >[],
  ) => Promise<void>;
  onCancel: () => void;
}

const PRIMITIVE_TOOLS: {
  type: PrimitiveType;
  label: string;
  icon: string;
  description: string;
}[] = [
  {
    type: "circle",
    label: "Окружность",
    icon: "○",
    description: "Отверстие, шейка вала",
  },
  { type: "arc", label: "Дуга", icon: "⌒", description: "Радиусный переход" },
  {
    type: "rectangle",
    label: "Прямоугольник",
    icon: "□",
    description: "Карман, паз",
  },
  {
    type: "polyline",
    label: "Полилиния",
    icon: "⌬",
    description: "Произвольный контур",
  },
  {
    type: "line",
    label: "Линия",
    icon: "⁄",
    description: "Образующая, кромка",
  },
];

const LINE_TYPES: { value: LineType; label: string; dash: string }[] = [
  { value: "solid", label: "Сплошная", dash: "" },
  { value: "dashed", label: "Штриховая", dash: "8 4" },
  { value: "dotted", label: "Пунктир", dash: "2 4" },
  { value: "center", label: "Осевая", dash: "16 4 4 4" },
  { value: "phantom", label: "Фантомная", dash: "16 4 4 4 4 4" },
];

let primitiveIdCounter = 0;
const newId = () => `p_${++primitiveIdCounter}_${Date.now()}`;

export function FeatureEditor({
  feature,
  svgViewBox,
  onSave,
  onCancel,
}: FeatureEditorProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [primitives, setPrimitives] = useState<EditablePrimitive[]>(() =>
    feature.contours.map((c, i) => ({
      id: newId(),
      primitive_type: c.primitive_type,
      params: c.params as Record<string, unknown>,
      line_type: (c.line_type as LineType) || "solid",
      color: c.color,
      sort_order: i,
    })),
  );
  const [activeTool, setActiveTool] = useState<PrimitiveType>("circle");
  const [activeLineType, setActiveLineType] = useState<LineType>("solid");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [drawing, setDrawing] = useState(false);
  const [startPoint, setStartPoint] = useState<{ x: number; y: number } | null>(
    null,
  );
  const [currentPoint, setCurrentPoint] = useState<{
    x: number;
    y: number;
  } | null>(null);
  const [polylinePoints, setPolylinePoints] = useState<
    { x: number; y: number }[]
  >([]);
  const [snapEnabled, setSnapEnabled] = useState(true);
  const [gridSize] = useState(5);
  const [saving, setSaving] = useState(false);

  const CANVAS_W = 600;
  const CANVAS_H = 400;

  const snap = useCallback(
    (val: number) =>
      snapEnabled ? Math.round(val / gridSize) * gridSize : val,
    [snapEnabled, gridSize],
  );

  const getCanvasPoint = useCallback(
    (e: React.MouseEvent<SVGElement>) => {
      const svg = e.currentTarget.closest("svg");
      if (!svg) return { x: 0, y: 0 };
      const rect = svg.getBoundingClientRect();
      const vb = svgViewBox || {
        x: 0,
        y: 0,
        width: CANVAS_W,
        height: CANVAS_H,
      };
      const scaleX = vb.width / rect.width;
      const scaleY = vb.height / rect.height;
      return {
        x: snap((e.clientX - rect.left) * scaleX + vb.x),
        y: snap((e.clientY - rect.top) * scaleY + vb.y),
      };
    },
    [snap, svgViewBox],
  );

  const handleSvgMouseDown = useCallback(
    (e: React.MouseEvent<SVGElement>) => {
      if (e.button !== 0) return;
      const pt = getCanvasPoint(e);

      if (activeTool === "polyline") {
        if (!drawing) {
          setDrawing(true);
          setPolylinePoints([pt]);
        } else {
          setPolylinePoints((prev) => [...prev, pt]);
        }
        return;
      }

      setDrawing(true);
      setStartPoint(pt);
      setCurrentPoint(pt);
    },
    [activeTool, drawing, getCanvasPoint],
  );

  const handleSvgMouseMove = useCallback(
    (e: React.MouseEvent<SVGElement>) => {
      const pt = getCanvasPoint(e);
      setCurrentPoint(pt);
    },
    [getCanvasPoint],
  );

  const handleSvgMouseUp = useCallback(
    (e: React.MouseEvent<SVGElement>) => {
      if (!drawing || activeTool === "polyline") return;
      const pt = getCanvasPoint(e);

      if (!startPoint) return;

      const dx = pt.x - startPoint.x;
      const dy = pt.y - startPoint.y;

      let newPrimitive: EditablePrimitive | null = null;

      if (activeTool === "circle") {
        const r = Math.sqrt(dx * dx + dy * dy);
        if (r < 1) {
          setDrawing(false);
          return;
        }
        newPrimitive = {
          id: newId(),
          primitive_type: "circle",
          params: { cx: startPoint.x, cy: startPoint.y, r },
          line_type: activeLineType,
          sort_order: primitives.length,
        };
      } else if (activeTool === "arc") {
        const r = Math.sqrt(dx * dx + dy * dy);
        if (r < 1) {
          setDrawing(false);
          return;
        }
        const startAngle = 0;
        const endAngle = 180;
        newPrimitive = {
          id: newId(),
          primitive_type: "arc",
          params: {
            cx: startPoint.x,
            cy: startPoint.y,
            r,
            start_angle: startAngle,
            end_angle: endAngle,
          },
          line_type: activeLineType,
          sort_order: primitives.length,
        };
      } else if (activeTool === "rectangle") {
        if (Math.abs(dx) < 1 || Math.abs(dy) < 1) {
          setDrawing(false);
          return;
        }
        newPrimitive = {
          id: newId(),
          primitive_type: "rectangle",
          params: {
            x: Math.min(startPoint.x, pt.x),
            y: Math.min(startPoint.y, pt.y),
            width: Math.abs(dx),
            height: Math.abs(dy),
            rotation: 0,
          },
          line_type: activeLineType,
          sort_order: primitives.length,
        };
      } else if (activeTool === "line") {
        if (Math.sqrt(dx * dx + dy * dy) < 1) {
          setDrawing(false);
          return;
        }
        newPrimitive = {
          id: newId(),
          primitive_type: "line",
          params: { x1: startPoint.x, y1: startPoint.y, x2: pt.x, y2: pt.y },
          line_type: activeLineType,
          sort_order: primitives.length,
        };
      }

      if (newPrimitive) {
        setPrimitives((prev) => [...prev, newPrimitive!]);
        setSelectedId(newPrimitive.id);
      }

      setDrawing(false);
      setStartPoint(null);
    },
    [
      activeTool,
      activeLineType,
      drawing,
      getCanvasPoint,
      primitives.length,
      startPoint,
    ],
  );

  const finishPolyline = useCallback(() => {
    if (polylinePoints.length < 2) {
      setDrawing(false);
      setPolylinePoints([]);
      return;
    }
    const newPrimitive: EditablePrimitive = {
      id: newId(),
      primitive_type: "polyline",
      params: { points: polylinePoints.map((p) => [p.x, p.y]), closed: false },
      line_type: activeLineType,
      sort_order: primitives.length,
    };
    setPrimitives((prev) => [...prev, newPrimitive]);
    setSelectedId(newPrimitive.id);
    setDrawing(false);
    setPolylinePoints([]);
  }, [activeLineType, polylinePoints, primitives.length]);

  const deletePrimitive = (id: string) => {
    setPrimitives((prev) => prev.filter((p) => p.id !== id));
    if (selectedId === id) setSelectedId(null);
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      await onSave(
        primitives.map((p, i) => ({
          primitive_type: p.primitive_type,
          params: p.params,
          line_type: p.line_type,
          color: p.color,
          sort_order: i,
        })),
      );
    } finally {
      setSaving(false);
    }
  };

  const getDashArray = (lt: LineType) =>
    LINE_TYPES.find((l) => l.value === lt)?.dash || "";

  // Render preview primitive while drawing
  const renderPreview = () => {
    if (!drawing || !startPoint || !currentPoint) return null;
    const dx = currentPoint.x - startPoint.x;
    const dy = currentPoint.y - startPoint.y;

    if (activeTool === "circle") {
      const r = Math.sqrt(dx * dx + dy * dy);
      return (
        <circle
          cx={startPoint.x}
          cy={startPoint.y}
          r={r}
          stroke="#60a5fa"
          strokeWidth={1}
          fill="rgba(59,130,246,0.1)"
          strokeDasharray="4 2"
        />
      );
    }
    if (activeTool === "rectangle") {
      return (
        <rect
          x={Math.min(startPoint.x, currentPoint.x)}
          y={Math.min(startPoint.y, currentPoint.y)}
          width={Math.abs(dx)}
          height={Math.abs(dy)}
          stroke="#60a5fa"
          strokeWidth={1}
          fill="rgba(59,130,246,0.1)"
          strokeDasharray="4 2"
        />
      );
    }
    if (activeTool === "line") {
      return (
        <line
          x1={startPoint.x}
          y1={startPoint.y}
          x2={currentPoint.x}
          y2={currentPoint.y}
          stroke="#60a5fa"
          strokeWidth={1}
          strokeDasharray="4 2"
        />
      );
    }
    return null;
  };

  const vb = svgViewBox || { x: 0, y: 0, width: CANVAS_W, height: CANVAS_H };
  const viewBox = `${vb.x} ${vb.y} ${vb.width} ${vb.height}`;

  return (
    <div className="flex flex-col h-full gap-3">
      {/* Toolbar */}
      <div className="flex items-center gap-2 flex-wrap">
        {/* Primitive tools */}
        <div className="flex items-center gap-1 bg-zinc-800 rounded-lg p-1">
          {PRIMITIVE_TOOLS.map((tool) => (
            <button
              key={tool.type}
              onClick={() => setActiveTool(tool.type)}
              title={`${tool.label}: ${tool.description}`}
              className={clsx(
                "w-8 h-8 rounded text-sm font-bold transition-colors",
                activeTool === tool.type
                  ? "bg-blue-600 text-white"
                  : "text-white/60 hover:text-white hover:bg-white/10",
              )}
            >
              {tool.icon}
            </button>
          ))}
        </div>

        {/* Line type */}
        <div className="flex items-center gap-1 bg-zinc-800 rounded-lg p-1">
          {LINE_TYPES.map((lt) => (
            <button
              key={lt.value}
              onClick={() => setActiveLineType(lt.value)}
              title={lt.label}
              className={clsx(
                "h-8 px-2 rounded text-xs transition-colors",
                activeLineType === lt.value
                  ? "bg-zinc-600 text-white"
                  : "text-white/50 hover:text-white hover:bg-white/10",
              )}
            >
              <svg width="24" height="8" viewBox="0 0 24 8">
                <line
                  x1="0"
                  y1="4"
                  x2="24"
                  y2="4"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeDasharray={lt.dash}
                />
              </svg>
            </button>
          ))}
        </div>

        {/* Snap toggle */}
        <button
          onClick={() => setSnapEnabled((s) => !s)}
          className={clsx(
            "h-8 px-3 rounded text-xs transition-colors",
            snapEnabled
              ? "bg-green-700/50 text-green-300 border border-green-700"
              : "bg-zinc-800 text-white/50 border border-zinc-700",
          )}
          title="Привязка к сетке"
        >
          Сетка {gridSize}мм
        </button>

        {activeTool === "polyline" && drawing && (
          <button
            onClick={finishPolyline}
            className="h-8 px-3 bg-amber-600 hover:bg-amber-500 text-white rounded text-xs"
          >
            Завершить полилинию
          </button>
        )}

        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={onCancel}
            className="h-8 px-3 bg-zinc-700 hover:bg-zinc-600 text-white/80 rounded text-xs"
          >
            Отмена
          </button>
          <button
            onClick={handleSave}
            disabled={saving}
            className="h-8 px-4 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white rounded text-xs font-medium"
          >
            {saving ? "Сохранение..." : "Сохранить контуры"}
          </button>
        </div>
      </div>

      <div className="flex gap-3 flex-1 min-h-0">
        {/* SVG canvas */}
        <div className="flex-1 bg-zinc-900 rounded-lg border border-white/10 overflow-hidden">
          <svg
            viewBox={viewBox}
            className="w-full h-full"
            style={{ cursor: "crosshair" }}
            onMouseDown={handleSvgMouseDown}
            onMouseMove={handleSvgMouseMove}
            onMouseUp={handleSvgMouseUp}
            onDoubleClick={
              activeTool === "polyline" ? finishPolyline : undefined
            }
          >
            {/* Grid */}
            {snapEnabled && (
              <defs>
                <pattern
                  id="editor-grid"
                  width={gridSize}
                  height={gridSize}
                  patternUnits="userSpaceOnUse"
                >
                  <path
                    d={`M ${gridSize} 0 L 0 0 0 ${gridSize}`}
                    fill="none"
                    stroke="rgba(255,255,255,0.06)"
                    strokeWidth="0.5"
                  />
                </pattern>
              </defs>
            )}
            {snapEnabled && (
              <rect width="100%" height="100%" fill="url(#editor-grid)" />
            )}

            {/* Existing primitives */}
            {primitives.map((p) => (
              <g
                key={p.id}
                onClick={() => setSelectedId(p.id)}
                style={{ cursor: "pointer" }}
              >
                {renderPrimitiveSvg(
                  p,
                  p.id === selectedId,
                  getDashArray(p.line_type),
                )}
              </g>
            ))}

            {/* Polyline in progress */}
            {polylinePoints.length > 0 && (
              <>
                <polyline
                  points={polylinePoints
                    .map((pt) => `${pt.x},${pt.y}`)
                    .join(" ")}
                  stroke="#60a5fa"
                  strokeWidth={1}
                  fill="none"
                  strokeDasharray="4 2"
                />
                {currentPoint && (
                  <line
                    x1={polylinePoints[polylinePoints.length - 1].x}
                    y1={polylinePoints[polylinePoints.length - 1].y}
                    x2={currentPoint.x}
                    y2={currentPoint.y}
                    stroke="#60a5fa"
                    strokeWidth={1}
                    strokeDasharray="2 2"
                    strokeOpacity={0.5}
                  />
                )}
              </>
            )}

            {/* Preview */}
            {renderPreview()}
          </svg>
        </div>

        {/* Primitives list */}
        <div className="w-48 bg-zinc-900 rounded-lg border border-white/10 p-2 overflow-y-auto">
          <div className="text-xs text-white/40 uppercase tracking-wider mb-2 px-1">
            Примитивы ({primitives.length})
          </div>
          {primitives.map((p, i) => (
            <div
              key={p.id}
              className={clsx(
                "flex items-center gap-1.5 px-2 py-1.5 rounded cursor-pointer text-xs mb-1",
                selectedId === p.id
                  ? "bg-blue-600/20 border border-blue-500/30 text-white"
                  : "hover:bg-white/5 text-white/60 hover:text-white border border-transparent",
              )}
              onClick={() => setSelectedId(p.id)}
            >
              <span className="text-sm">
                {PRIMITIVE_TOOLS.find((t) => t.type === p.primitive_type)
                  ?.icon || "◇"}
              </span>
              <span className="flex-1 truncate capitalize">
                {p.primitive_type}
              </span>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  deletePrimitive(p.id);
                }}
                className="opacity-0 group-hover:opacity-100 hover:text-red-400 transition-opacity"
                title="Удалить"
              >
                ×
              </button>
            </div>
          ))}
          {primitives.length === 0 && (
            <div className="text-xs text-white/30 text-center py-4">
              Нарисуйте примитив на холсте
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function renderPrimitiveSvg(
  p: EditablePrimitive,
  isSelected: boolean,
  dashArray: string,
): React.ReactNode {
  const stroke = isSelected ? "#3b82f6" : "#e2e8f0";
  const strokeWidth = isSelected ? 2 : 1;
  const params = p.params as Record<string, number>;
  const highlightFill = isSelected ? "rgba(59,130,246,0.1)" : "transparent";

  switch (p.primitive_type) {
    case "circle":
      return (
        <circle
          cx={params.cx}
          cy={params.cy}
          r={params.r}
          stroke={stroke}
          strokeWidth={strokeWidth}
          fill={highlightFill}
          strokeDasharray={dashArray}
        />
      );
    case "rectangle":
      return (
        <rect
          x={params.x}
          y={params.y}
          width={params.width}
          height={params.height}
          stroke={stroke}
          strokeWidth={strokeWidth}
          fill={highlightFill}
          strokeDasharray={dashArray}
          transform={params.rotation ? `rotate(${params.rotation})` : undefined}
        />
      );
    case "line":
      return (
        <>
          <line
            x1={params.x1}
            y1={params.y1}
            x2={params.x2}
            y2={params.y2}
            stroke={stroke}
            strokeWidth={strokeWidth}
            strokeDasharray={dashArray}
          />
          {isSelected && (
            <>
              <circle
                cx={params.x1}
                cy={params.y1}
                r={3}
                fill="#3b82f6"
                stroke="none"
              />
              <circle
                cx={params.x2}
                cy={params.y2}
                r={3}
                fill="#3b82f6"
                stroke="none"
              />
            </>
          )}
        </>
      );
    case "arc": {
      const cx = params.cx ?? 0;
      const cy = params.cy ?? 0;
      const r = params.r ?? 5;
      const sa = ((params.start_angle ?? 0) * Math.PI) / 180;
      const ea = ((params.end_angle ?? 180) * Math.PI) / 180;
      const x1 = cx + r * Math.cos(sa);
      const y1 = cy + r * Math.sin(sa);
      const x2 = cx + r * Math.cos(ea);
      const y2 = cy + r * Math.sin(ea);
      const large =
        Math.abs((params.end_angle ?? 180) - (params.start_angle ?? 0)) > 180
          ? 1
          : 0;
      return (
        <path
          d={`M ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2}`}
          stroke={stroke}
          strokeWidth={strokeWidth}
          fill="none"
          strokeDasharray={dashArray}
        />
      );
    }
    case "polyline": {
      const pts = (p.params.points as number[][]) ?? [];
      if (!pts.length) return null;
      const closed = p.params.closed as boolean;
      const Tag = closed ? "polygon" : "polyline";
      return (
        <Tag
          points={pts.map((pt) => `${pt[0]},${pt[1]}`).join(" ")}
          stroke={stroke}
          strokeWidth={strokeWidth}
          fill={closed ? highlightFill : "none"}
          strokeDasharray={dashArray}
        />
      );
    }
    default:
      return null;
  }
}
