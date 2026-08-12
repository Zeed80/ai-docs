"use client";

export type SketchTool = "select" | "line" | "rectangle" | "circle";

const TOOLS: { id: SketchTool; icon: string; label: string }[] = [
  { id: "select", icon: "⛶", label: "Выбор" },
  { id: "line", icon: "╱", label: "Линия" },
  { id: "rectangle", icon: "▭", label: "Прямоугольник" },
  { id: "circle", icon: "○", label: "Окружность" },
];

/** Ф4: drawing-tool picker for SketchCanvas. Дуга (freehand arc) is
 * deliberately not offered here — constraints.py's solver has no variable
 * extraction for Arc entities (only Segment/Circle), so an arc placed on
 * the canvas could never be moved by the constraint solver, only drawn
 * once and left fixed. sketch_export.py still supports Arc entities in
 * its OUTPUT (a manually-built or future-tool-drawn arc profile still
 * exports correctly) — only the DRAWING tool is deferred, not the export
 * path. */
export default function SketchToolbar({
  active,
  onChange,
}: {
  active: SketchTool;
  onChange: (tool: SketchTool) => void;
}) {
  return (
    <div className="flex items-center gap-1 border-b border-white/10 bg-zinc-950 px-2 py-1.5">
      {TOOLS.map((tool) => (
        <button
          key={tool.id}
          type="button"
          onClick={() => onChange(tool.id)}
          className={`flex items-center gap-1.5 rounded px-2.5 py-1 text-[11px] ${
            active === tool.id
              ? "bg-sky-500/20 text-sky-200"
              : "text-zinc-300 hover:bg-white/5"
          }`}
        >
          <span className="text-sm leading-none">{tool.icon}</span>
          {tool.label}
        </button>
      ))}
    </div>
  );
}
