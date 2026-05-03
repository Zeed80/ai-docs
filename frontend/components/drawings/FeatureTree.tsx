"use client";

import { useState } from "react";
import clsx from "clsx";
import type { DrawingFeature, DrawingFeatureType } from "@/lib/drawings-api";

const FEATURE_TYPE_LABELS: Record<DrawingFeatureType, string> = {
  hole: "Отверстия",
  pocket: "Карманы",
  surface: "Поверхности",
  boss: "Бобышки",
  groove: "Канавки",
  thread: "Резьбы",
  chamfer: "Фаски",
  radius: "Радиусы",
  slot: "Пазы",
  contour: "Контуры",
  other: "Прочее",
};

const FEATURE_TYPE_ICONS: Record<DrawingFeatureType, string> = {
  hole: "○",
  pocket: "□",
  surface: "▬",
  boss: "⬡",
  groove: "⊏",
  thread: "⊘",
  chamfer: "◸",
  radius: "⌒",
  slot: "▭",
  contour: "⬟",
  other: "◇",
};

interface FeatureTreeProps {
  features: DrawingFeature[];
  selectedFeatureId: string | null;
  hoveredFeatureId: string | null;
  onSelect: (featureId: string) => void;
  onHover: (featureId: string | null) => void;
  editMode: boolean;
  onEditFeature?: (feature: DrawingFeature) => void;
}

export function FeatureTree({
  features,
  selectedFeatureId,
  hoveredFeatureId,
  onSelect,
  onHover,
  editMode,
  onEditFeature,
}: FeatureTreeProps) {
  const [expandedTypes, setExpandedTypes] = useState<Set<string>>(
    new Set(Object.keys(FEATURE_TYPE_LABELS)),
  );

  const grouped = features.reduce(
    (acc, f) => {
      const key = f.feature_type;
      if (!acc[key]) acc[key] = [];
      acc[key].push(f);
      return acc;
    },
    {} as Record<string, DrawingFeature[]>,
  );

  const toggleType = (type: string) => {
    setExpandedTypes((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  };

  const getDimensionLabel = (feature: DrawingFeature): string => {
    if (feature.dimensions.length === 0) return "";
    const d = feature.dimensions[0];
    const tol =
      d.upper_tol !== undefined && d.lower_tol !== undefined
        ? `+${d.upper_tol}/${d.lower_tol}`
        : "";
    const prefix =
      d.dim_type === "diameter" ? "Ø" : d.dim_type === "radius" ? "R" : "";
    return (
      d.label ||
      `${prefix}${d.nominal}${d.fit_system ? " " + d.fit_system : ""}${tol}`
    );
  };

  const getRoughnessLabel = (feature: DrawingFeature): string => {
    if (feature.surfaces.length === 0) return "";
    const s = feature.surfaces[0];
    return `${s.roughness_type}${s.value}`;
  };

  const hasToolBinding = (feature: DrawingFeature): boolean =>
    !!feature.tool_binding;

  const isReviewed = (feature: DrawingFeature): boolean =>
    !!feature.reviewed_at;

  return (
    <div className="flex flex-col h-full overflow-y-auto text-sm">
      {Object.entries(FEATURE_TYPE_LABELS).map(([type, label]) => {
        const typeFeatures = grouped[type];
        if (!typeFeatures || typeFeatures.length === 0) return null;
        const isExpanded = expandedTypes.has(type);

        return (
          <div key={type} className="border-b border-white/10 last:border-0">
            <button
              onClick={() => toggleType(type)}
              className="w-full flex items-center gap-2 px-3 py-2 text-left text-white/60 hover:text-white hover:bg-white/5 transition-colors font-medium text-xs uppercase tracking-wider"
            >
              <span className="text-base">
                {FEATURE_TYPE_ICONS[type as DrawingFeatureType]}
              </span>
              <span>{label}</span>
              <span className="ml-auto bg-white/10 rounded-full px-2 py-0.5 text-xs">
                {typeFeatures.length}
              </span>
              <span className="text-white/40">{isExpanded ? "▾" : "▸"}</span>
            </button>

            {isExpanded && (
              <div className="pl-2">
                {typeFeatures.map((feature) => {
                  const dimLabel = getDimensionLabel(feature);
                  const roughnessLabel = getRoughnessLabel(feature);
                  const isSelected = feature.id === selectedFeatureId;
                  const isHovered = feature.id === hoveredFeatureId;

                  return (
                    <div
                      key={feature.id}
                      data-feature-id={feature.id}
                      className={clsx(
                        "group flex items-start gap-2 px-3 py-2 rounded-lg mx-1 mb-1 cursor-pointer transition-all",
                        isSelected
                          ? "bg-blue-600/20 border border-blue-500/50 text-white"
                          : isHovered
                            ? "bg-white/10 border border-white/20 text-white"
                            : "hover:bg-white/5 border border-transparent text-white/80 hover:text-white",
                      )}
                      onClick={() => onSelect(feature.id)}
                      onMouseEnter={() => onHover(feature.id)}
                      onMouseLeave={() => onHover(null)}
                    >
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-1.5 flex-wrap">
                          <span className="font-medium truncate">
                            {feature.name}
                          </span>

                          {/* Confidence indicator */}
                          {feature.confidence < 0.7 && (
                            <span
                              className="text-yellow-400 text-xs"
                              title={`Уверенность AI: ${Math.round(feature.confidence * 100)}%`}
                            >
                              ⚠
                            </span>
                          )}

                          {/* Reviewed badge */}
                          {isReviewed(feature) && (
                            <span
                              className="text-green-400 text-xs"
                              title="Проверено"
                            >
                              ✓
                            </span>
                          )}

                          {/* Tool binding indicator */}
                          {hasToolBinding(feature) && (
                            <span
                              className="text-cyan-400 text-xs"
                              title="Инструмент привязан"
                            >
                              🔧
                            </span>
                          )}
                        </div>

                        {/* Dimension & roughness badges */}
                        <div className="flex items-center gap-1.5 mt-0.5 flex-wrap">
                          {dimLabel && (
                            <span className="text-xs text-blue-300 bg-blue-500/15 px-1.5 py-0.5 rounded font-mono">
                              {dimLabel}
                            </span>
                          )}
                          {roughnessLabel && (
                            <span className="text-xs text-orange-300 bg-orange-500/15 px-1.5 py-0.5 rounded font-mono">
                              {roughnessLabel}
                            </span>
                          )}
                          {feature.gdt_annotations.length > 0 && (
                            <span className="text-xs text-purple-300 bg-purple-500/15 px-1.5 py-0.5 rounded">
                              GD&T
                            </span>
                          )}
                        </div>
                      </div>

                      {/* Edit button in edit mode */}
                      {editMode && onEditFeature && (
                        <button
                          className="opacity-0 group-hover:opacity-100 transition-opacity text-white/50 hover:text-white p-0.5 rounded"
                          onClick={(e) => {
                            e.stopPropagation();
                            onEditFeature(feature);
                          }}
                          title="Редактировать контур"
                        >
                          ✏
                        </button>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}

      {features.length === 0 && (
        <div className="flex flex-col items-center justify-center flex-1 py-12 text-white/40 gap-2">
          <span className="text-3xl">📐</span>
          <span className="text-sm">Элементы не найдены</span>
          <span className="text-xs text-center px-4">
            Запустите AI-анализ или добавьте элементы вручную
          </span>
        </div>
      )}
    </div>
  );
}
