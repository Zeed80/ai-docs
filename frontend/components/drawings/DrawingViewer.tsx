"use client";

import { useEffect, useRef, useState } from "react";
import clsx from "clsx";
import type { DrawingFeature, FeatureContour } from "@/lib/drawings-api";

interface DrawingViewerProps {
  drawingId: string;
  svgUrl: string | null;
  features: DrawingFeature[];
  selectedFeatureId: string | null;
  hoveredFeatureId: string | null;
  onFeatureClick?: (featureId: string) => void;
  onFeatureHover?: (featureId: string | null) => void;
  editMode: boolean;
  editingFeatureId?: string | null;
}

export function DrawingViewer({
  drawingId,
  svgUrl,
  features,
  selectedFeatureId,
  hoveredFeatureId,
  onFeatureClick,
  onFeatureHover,
  editMode,
  editingFeatureId,
}: DrawingViewerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const svgContainerRef = useRef<HTMLDivElement>(null);
  const [svgContent, setSvgContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [isPanning, setIsPanning] = useState(false);
  const panStart = useRef({ x: 0, y: 0, panX: 0, panY: 0 });

  // Load SVG content
  useEffect(() => {
    if (!svgUrl) return;
    setLoading(true);
    setError(null);
    fetch(svgUrl)
      .then((r) => {
        if (!r.ok) throw new Error("SVG не найден");
        return r.text();
      })
      .then((text) => {
        setSvgContent(text);
        setLoading(false);
      })
      .catch((e) => {
        setError(e.message);
        setLoading(false);
      });
  }, [svgUrl]);

  // Inject feature IDs into SVG groups based on contour data
  useEffect(() => {
    if (!svgContent || !svgContainerRef.current) return;
    const container = svgContainerRef.current;

    // After SVG is rendered, draw highlight overlays
    const svg = container.querySelector("svg");
    if (!svg) return;

    // Remove existing overlays
    container.querySelectorAll(".feature-overlay").forEach((el) => el.remove());

    // Build overlay circles/rects for each feature's contours
    features.forEach((feature) => {
      feature.contours.forEach((contour) => {
        const overlay = _buildContourOverlay(contour, feature.id, svg);
        if (overlay) {
          overlay.classList.add("feature-overlay");
          overlay.setAttribute("data-feature-id", feature.id);
          svg.appendChild(overlay);
        }
      });
    });

    // Add event listeners on overlays
    container.querySelectorAll(".feature-overlay").forEach((el) => {
      const fid = el.getAttribute("data-feature-id");
      if (!fid) return;
      el.addEventListener("click", () => onFeatureClick?.(fid));
      el.addEventListener("mouseenter", () => onFeatureHover?.(fid));
      el.addEventListener("mouseleave", () => onFeatureHover?.(null));
    });
  }, [svgContent, features]);

  // Apply highlight styles
  useEffect(() => {
    if (!svgContainerRef.current) return;
    svgContainerRef.current
      .querySelectorAll(".feature-overlay")
      .forEach((el) => {
        const fid = el.getAttribute("data-feature-id");
        const isSelected = fid === selectedFeatureId;
        const isHovered = fid === hoveredFeatureId;
        const isEditing = fid === editingFeatureId;

        // Reset
        el.setAttribute("stroke", "transparent");
        el.setAttribute("fill", "transparent");
        el.setAttribute("stroke-width", "1");
        el.setAttribute("filter", "");

        if (isEditing) {
          el.setAttribute("stroke", "#f59e0b");
          el.setAttribute("stroke-width", "2");
          el.setAttribute("fill", "rgba(245,158,11,0.15)");
        } else if (isSelected) {
          el.setAttribute("stroke", "#3b82f6");
          el.setAttribute("stroke-width", "2.5");
          el.setAttribute("fill", "rgba(59,130,246,0.15)");
          el.setAttribute(
            "filter",
            "drop-shadow(0 0 4px rgba(59,130,246,0.8))",
          );
        } else if (isHovered) {
          el.setAttribute("stroke", "#60a5fa");
          el.setAttribute("stroke-width", "2");
          el.setAttribute("fill", "rgba(96,165,250,0.10)");
          el.setAttribute(
            "filter",
            "drop-shadow(0 0 3px rgba(96,165,250,0.6))",
          );
        }
      });
  }, [selectedFeatureId, hoveredFeatureId, editingFeatureId]);

  // Zoom handlers
  const handleWheel = (e: React.WheelEvent) => {
    e.preventDefault();
    const delta = e.deltaY > 0 ? 0.9 : 1.1;
    setZoom((z) => Math.min(Math.max(z * delta, 0.1), 10));
  };

  const handleMouseDown = (e: React.MouseEvent) => {
    if (e.button === 1 || (e.button === 0 && e.altKey)) {
      setIsPanning(true);
      panStart.current = {
        x: e.clientX,
        y: e.clientY,
        panX: pan.x,
        panY: pan.y,
      };
      e.preventDefault();
    }
  };

  const handleMouseMove = (e: React.MouseEvent) => {
    if (!isPanning) return;
    setPan({
      x: panStart.current.panX + (e.clientX - panStart.current.x),
      y: panStart.current.panY + (e.clientY - panStart.current.y),
    });
  };

  const handleMouseUp = () => setIsPanning(false);

  const resetView = () => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
  };

  return (
    <div
      ref={containerRef}
      className="relative flex-1 overflow-hidden bg-zinc-900 rounded-lg border border-white/10"
      onWheel={handleWheel}
      onMouseDown={handleMouseDown}
      onMouseMove={handleMouseMove}
      onMouseUp={handleMouseUp}
      onMouseLeave={handleMouseUp}
      style={{
        cursor: isPanning ? "grabbing" : editMode ? "crosshair" : "default",
      }}
    >
      {/* Toolbar */}
      <div className="absolute top-2 right-2 z-10 flex items-center gap-1">
        <button
          onClick={() => setZoom((z) => Math.min(z * 1.2, 10))}
          className="w-7 h-7 bg-zinc-800 hover:bg-zinc-700 border border-white/20 rounded text-white/80 hover:text-white text-sm transition-colors"
          title="Увеличить"
        >
          +
        </button>
        <button
          onClick={() => setZoom((z) => Math.max(z * 0.8, 0.1))}
          className="w-7 h-7 bg-zinc-800 hover:bg-zinc-700 border border-white/20 rounded text-white/80 hover:text-white text-sm transition-colors"
          title="Уменьшить"
        >
          −
        </button>
        <button
          onClick={resetView}
          className="px-2 h-7 bg-zinc-800 hover:bg-zinc-700 border border-white/20 rounded text-white/80 hover:text-white text-xs transition-colors"
          title="Сбросить вид"
        >
          {Math.round(zoom * 100)}%
        </button>
        <button
          onClick={resetView}
          className="w-7 h-7 bg-zinc-800 hover:bg-zinc-700 border border-white/20 rounded text-white/50 hover:text-white text-xs transition-colors"
          title="По центру"
        >
          ⊞
        </button>
      </div>

      {/* Content */}
      <div
        className="w-full h-full flex items-center justify-center"
        style={{
          transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
          transformOrigin: "center center",
          transition: isPanning ? "none" : "transform 0.05s ease",
        }}
      >
        {loading && (
          <div className="flex flex-col items-center gap-3 text-white/50">
            <div className="w-8 h-8 border-2 border-blue-500/50 border-t-blue-500 rounded-full animate-spin" />
            <span className="text-sm">Загрузка чертежа...</span>
          </div>
        )}

        {error && !loading && (
          <div className="flex flex-col items-center gap-3 text-white/40">
            <span className="text-4xl">📐</span>
            <span className="text-sm">{error}</span>
            <span className="text-xs text-center max-w-xs">
              SVG-файл ещё генерируется. Подождите завершения анализа.
            </span>
          </div>
        )}

        {!loading && !error && !svgContent && !svgUrl && (
          <div className="flex flex-col items-center gap-3 text-white/30">
            <span className="text-6xl">📄</span>
            <span className="text-sm">SVG не доступен</span>
          </div>
        )}

        {!loading && !error && svgContent && (
          <div
            ref={svgContainerRef}
            className="drawing-svg-container"
            style={{ maxWidth: "100%", maxHeight: "100%" }}
            dangerouslySetInnerHTML={{ __html: _cleanSvg(svgContent) }}
          />
        )}
      </div>

      {/* Edit mode indicator */}
      {editMode && (
        <div className="absolute bottom-2 left-2 z-10 flex items-center gap-2 bg-amber-600/90 rounded-lg px-3 py-1.5 text-white text-xs font-medium">
          <span>✏</span>
          <span>Режим редактирования контуров</span>
        </div>
      )}

      {/* Hint: Alt+drag to pan */}
      <div className="absolute bottom-2 right-2 z-10 text-white/20 text-xs">
        Alt+мышь — панорамирование · колесо — масштаб
      </div>
    </div>
  );
}

function _buildContourOverlay(
  contour: FeatureContour,
  featureId: string,
  svg: SVGElement,
): SVGElement | null {
  const ns = "http://www.w3.org/2000/svg";
  const p = contour.params as Record<string, number>;

  try {
    if (contour.primitive_type === "circle") {
      const el = document.createElementNS(ns, "circle") as SVGCircleElement;
      el.setAttribute("cx", String(p.cx ?? 0));
      el.setAttribute("cy", String(p.cy ?? 0));
      el.setAttribute("r", String(p.r ?? 5));
      el.setAttribute("stroke", "transparent");
      el.setAttribute("fill", "transparent");
      el.setAttribute("stroke-width", "1");
      el.style.cursor = "pointer";
      return el;
    }

    if (contour.primitive_type === "rectangle") {
      const el = document.createElementNS(ns, "rect") as SVGRectElement;
      el.setAttribute("x", String(p.x ?? 0));
      el.setAttribute("y", String(p.y ?? 0));
      el.setAttribute("width", String(p.width ?? 10));
      el.setAttribute("height", String(p.height ?? 10));
      if (p.rotation) el.setAttribute("transform", `rotate(${p.rotation})`);
      el.setAttribute("stroke", "transparent");
      el.setAttribute("fill", "transparent");
      el.setAttribute("stroke-width", "1");
      el.style.cursor = "pointer";
      return el;
    }

    if (contour.primitive_type === "arc") {
      const el = document.createElementNS(ns, "path") as SVGPathElement;
      const startRad = ((p.start_angle ?? 0) * Math.PI) / 180;
      const endRad = ((p.end_angle ?? 180) * Math.PI) / 180;
      const cx = p.cx ?? 0;
      const cy = p.cy ?? 0;
      const r = p.r ?? 5;
      const x1 = cx + r * Math.cos(startRad);
      const y1 = cy + r * Math.sin(startRad);
      const x2 = cx + r * Math.cos(endRad);
      const y2 = cy + r * Math.sin(endRad);
      const largeArc =
        Math.abs((p.end_angle ?? 180) - (p.start_angle ?? 0)) > 180 ? 1 : 0;
      el.setAttribute(
        "d",
        `M ${x1} ${y1} A ${r} ${r} 0 ${largeArc} 1 ${x2} ${y2}`,
      );
      el.setAttribute("stroke", "transparent");
      el.setAttribute("fill", "none");
      el.setAttribute("stroke-width", "8");
      el.style.cursor = "pointer";
      return el;
    }

    if (contour.primitive_type === "line") {
      const el = document.createElementNS(ns, "line") as SVGLineElement;
      el.setAttribute("x1", String(p.x1 ?? 0));
      el.setAttribute("y1", String(p.y1 ?? 0));
      el.setAttribute("x2", String(p.x2 ?? 0));
      el.setAttribute("y2", String(p.y2 ?? 0));
      el.setAttribute("stroke", "transparent");
      el.setAttribute("stroke-width", "8");
      el.style.cursor = "pointer";
      return el;
    }

    if (contour.primitive_type === "polyline") {
      const pts = (p.points as unknown as number[][]) ?? [];
      if (!pts.length) return null;
      const el = document.createElementNS(ns, "polygon") as SVGPolygonElement;
      el.setAttribute("points", pts.map((pt) => `${pt[0]},${pt[1]}`).join(" "));
      el.setAttribute("stroke", "transparent");
      el.setAttribute("fill", "transparent");
      el.setAttribute("stroke-width", "8");
      el.style.cursor = "pointer";
      return el;
    }
  } catch {
    return null;
  }

  return null;
}

function _cleanSvg(svgContent: string): string {
  // Remove XML declaration and doctype if present
  return svgContent
    .replace(/<\?xml[^>]*\?>/g, "")
    .replace(/<!DOCTYPE[^>]*>/g, "")
    .trim();
}
