"use client";

export type SketchTool = "select" | "line" | "rectangle" | "circle" | "arc";

const TOOLS: { id: SketchTool; icon: string; label: string }[] = [
  { id: "select", icon: "⛶", label: "Выбор" },
  { id: "line", icon: "╱", label: "Линия" },
  { id: "rectangle", icon: "▭", label: "Прямоугольник" },
  { id: "circle", icon: "○", label: "Окружность" },
  { id: "arc", icon: "◜", label: "Дуга" },
];

/** Ф4/Ф9: drawing-tool picker for SketchCanvas. Дуга (Ф9) — 3 клика:
 * центр → начало → конец, дающие все 4 значения схемы Arc (center + два
 * радиус-вектора, из которых считаются radius/start_angle/end_angle) без
 * лишней арифметики — тот же "якорь → протяжка" паттерн, что уже приняли
 * rectangle/circle, просто на клик длиннее (у дуги на одну степень свободы
 * больше, чем у окружности). Раньше дуга была намеренно не предложена
 * здесь — constraints.py не извлекал переменные для Arc; с Ф9 solve
 * работает и с ней (center.x/center.y/radius, start/end sweep фиксирован). */
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
