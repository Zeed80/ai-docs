"use client";

import { useState } from "react";

export interface BlankSpec {
  id?: string;
  blank_type: string;
  material_grade: string;
  standard_gost: string | null;
  dimensions: Record<string, number> | null;
  mass_blank_kg: number | null;
  mass_part_kg: number | null;
  utilization_factor: number | null;
  confidence: number;
  reasoning: string | null;
}

interface Props {
  spec: BlankSpec | null;
  onEdit?: (spec: BlankSpec) => void;
}

const KIM_COLOR = (kim: number | null) => {
  if (!kim) return "text-zinc-400";
  if (kim >= 0.7) return "text-emerald-400";
  if (kim >= 0.5) return "text-yellow-400";
  return "text-red-400";
};

const BLANK_TYPE_LABELS: Record<string, string> = {
  прокат: "Прокат",
  поковка: "Поковка",
  штамповка: "Штамповка",
  литье: "Литьё",
  "сварная конструкция": "Сварная конструкция",
};

export default function BlankSpecCard({ spec, onEdit }: Props) {
  const [expanded, setExpanded] = useState(false);

  if (!spec) {
    return (
      <div className="rounded-lg border border-zinc-700 bg-zinc-800/50 p-3">
        <p className="text-xs text-zinc-500">Заготовка не задана.</p>
      </div>
    );
  }

  const kim = spec.utilization_factor;

  return (
    <div className="rounded-lg border border-zinc-700 bg-zinc-800/50 p-3 space-y-2">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-xs font-semibold text-zinc-200">Заготовка</span>
          <span className="text-xs bg-zinc-700 px-1.5 py-0.5 rounded text-zinc-300">
            {BLANK_TYPE_LABELS[spec.blank_type] ?? spec.blank_type}
          </span>
        </div>
        <div className="flex items-center gap-2">
          {kim !== null && (
            <span
              className={`text-xs font-mono font-semibold ${KIM_COLOR(kim)}`}
            >
              КИМ={kim.toFixed(2)}
            </span>
          )}
          {onEdit && (
            <button
              onClick={() => onEdit(spec)}
              className="text-xs text-zinc-400 hover:text-zinc-200 transition"
            >
              ✏
            </button>
          )}
          <button
            onClick={() => setExpanded(!expanded)}
            className="text-xs text-zinc-500 hover:text-zinc-300"
          >
            {expanded ? "▲" : "▼"}
          </button>
        </div>
      </div>

      {/* Main info */}
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
        <div>
          <span className="text-zinc-500">Материал:</span>{" "}
          <span className="text-zinc-200">{spec.material_grade}</span>
        </div>
        {spec.standard_gost && (
          <div>
            <span className="text-zinc-500">ГОСТ:</span>{" "}
            <span className="text-zinc-200">{spec.standard_gost}</span>
          </div>
        )}
        {spec.mass_blank_kg !== null && (
          <div>
            <span className="text-zinc-500">Масса заготовки:</span>{" "}
            <span className="text-zinc-200">{spec.mass_blank_kg} кг</span>
          </div>
        )}
        {spec.mass_part_kg !== null && (
          <div>
            <span className="text-zinc-500">Масса детали:</span>{" "}
            <span className="text-zinc-200">{spec.mass_part_kg} кг</span>
          </div>
        )}
        {spec.dimensions && (
          <div className="col-span-2">
            <span className="text-zinc-500">Размеры:</span>{" "}
            <span className="text-zinc-200 font-mono text-xs">
              {Object.entries(spec.dimensions)
                .map(([k, v]) => `${k}=${v}`)
                .join(", ")}
            </span>
          </div>
        )}
      </div>

      {/* Expanded: reasoning + confidence */}
      {expanded && spec.reasoning && (
        <div className="pt-1 border-t border-zinc-700">
          <p className="text-xs text-zinc-400 italic">{spec.reasoning}</p>
          <p className="text-xs text-zinc-600 mt-1">
            Достоверность: {Math.round(spec.confidence * 100)}%
          </p>
        </div>
      )}
    </div>
  );
}
