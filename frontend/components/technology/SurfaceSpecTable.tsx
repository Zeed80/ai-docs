"use client";

export interface SurfaceSpec {
  id: string;
  surface_type: string;
  machining_method: string;
  machining_stage: string;
  nominal_mm: number | null;
  upper_tol: number | null;
  lower_tol: number | null;
  roughness_ra: number | null;
  fit_system: string | null;
  operation_id: string | null;
  drawing_feature_id: string | null;
  confidence: number;
}

interface Props {
  specs: SurfaceSpec[];
  selectedOperationId?: string | null;
  onSelectSurface?: (spec: SurfaceSpec) => void;
}

const METHOD_LABELS: Record<string, string> = {
  turning: "Токарная",
  milling: "Фрезерная",
  drilling: "Сверление",
  grinding: "Шлифование",
  boring: "Растачивание",
  reaming: "Развёртывание",
  honing: "Хонингование",
  broaching: "Протягивание",
};

const STAGE_LABELS: Record<string, string> = {
  rough: "Черновая",
  "semi-finish": "Получистовая",
  finish: "Чистовая",
};

const STAGE_COLORS: Record<string, string> = {
  rough: "text-orange-400",
  "semi-finish": "text-yellow-400",
  finish: "text-emerald-400",
};

const SURFACE_LABELS: Record<string, string> = {
  hole: "Отв.",
  external_cylindrical: "Цил.нар.",
  flat: "Плоскость",
  thread: "Резьба",
  groove: "Канавка",
  pocket: "Карман",
  contour: "Контур",
  boss: "Бобышка",
  other: "Прочее",
};

function formatTolerance(upper: number | null, lower: number | null): string {
  if (upper === null && lower === null) return "";
  const u = upper !== null ? `+${upper}` : "";
  const l = lower !== null ? `${lower}` : "";
  return `${u}/${l}`;
}

export default function SurfaceSpecTable({
  specs,
  selectedOperationId,
  onSelectSurface,
}: Props) {
  if (specs.length === 0) {
    return (
      <div className="flex items-center justify-center h-24 text-zinc-500 text-sm">
        Поверхности не проанализированы.
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs border-collapse">
        <thead>
          <tr className="bg-zinc-800 text-zinc-400">
            <th className="px-2 py-1.5 text-left font-medium">Тип</th>
            <th className="px-2 py-1.5 text-left font-medium">∅/размер</th>
            <th className="px-2 py-1.5 text-left font-medium">Допуск</th>
            <th className="px-2 py-1.5 text-left font-medium">Посадка</th>
            <th className="px-2 py-1.5 text-left font-medium">Ra</th>
            <th className="px-2 py-1.5 text-left font-medium">Метод</th>
            <th className="px-2 py-1.5 text-left font-medium">Стадия</th>
          </tr>
        </thead>
        <tbody>
          {specs.map((spec) => {
            const isLinked =
              selectedOperationId && spec.operation_id === selectedOperationId;
            return (
              <tr
                key={spec.id}
                onClick={() => onSelectSurface?.(spec)}
                className={`border-b border-zinc-800 transition cursor-pointer
                  ${isLinked ? "bg-blue-900/30" : "hover:bg-zinc-800/40"}
                `}
              >
                <td className="px-2 py-1 text-zinc-300">
                  {SURFACE_LABELS[spec.surface_type] ?? spec.surface_type}
                </td>
                <td className="px-2 py-1 font-mono text-zinc-200">
                  {spec.nominal_mm !== null ? `Ø${spec.nominal_mm}` : "—"}
                </td>
                <td className="px-2 py-1 font-mono text-zinc-400">
                  {formatTolerance(spec.upper_tol, spec.lower_tol) || "—"}
                </td>
                <td className="px-2 py-1 text-zinc-300">
                  {spec.fit_system ?? "—"}
                </td>
                <td className="px-2 py-1 font-mono text-zinc-300">
                  {spec.roughness_ra !== null ? `Ra${spec.roughness_ra}` : "—"}
                </td>
                <td className="px-2 py-1 text-zinc-300">
                  {METHOD_LABELS[spec.machining_method] ??
                    spec.machining_method}
                </td>
                <td
                  className={`px-2 py-1 ${STAGE_COLORS[spec.machining_stage] ?? "text-zinc-400"}`}
                >
                  {STAGE_LABELS[spec.machining_stage] ?? spec.machining_stage}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
