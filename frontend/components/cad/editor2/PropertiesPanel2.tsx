"use client";

import { useEffect, useState } from "react";

import PropertiesPanel from "@/components/cad/editor/PropertiesPanel";
import { engineeringApi, type AddedNativeFeature } from "@/lib/engineering-api";
import type { EmgFeatureNode, EmgOperationNode } from "@/lib/emg-tree";
import type { SketchProfileSegment } from "@/lib/cad-sketch-api";

export type AddFeatureDraftKind =
  "boss" | "pocket" | "fillet" | "chamfer" | "shell" | "thread";

const KIND_LABEL: Record<AddFeatureDraftKind, string> = {
  boss: "Бобышка",
  pocket: "Карман",
  fillet: "Скругление",
  chamfer: "Фаска",
  shell: "Оболочка",
  thread: "Резьба",
};

export interface KernelEdgeDescriptor {
  key: string;
  index: number;
  curve: string;
  length_mm: number;
  vertices: Array<{ x: number; y: number; z: number }>;
}

/** Ф2-Ф3 нового CAD-редактора: PropertiesPanel (коррекция, БЕЗ ИЗМЕНЕНИЙ)
 * плюс форма «добавить фичу» для всех 6 видов, что уже принимает
 * AddNativeFeatureRequest на бэкенде. Выбор ребра для fillet/chamfer —
 * пока выпадающий список (те же report.edges, что раньше показывал
 * Cad3dPanel), а не клик по 3D: для честного клик-выбора ребра нужен
 * отдельный edge-topology сайдкар в ядре (аналог существующего
 * face-topology для граней), намеренно отложено отдельной задачей —
 * не выдавать выпадающий список за то, чем он не является. */
export default function PropertiesPanel2({
  generationId,
  operation,
  features,
  edges,
  addFeatureDraft,
  onAddFeatureDraftChange,
  sketchModeActive,
  onStartSketch,
  exportedSketchProfile,
  onSketchProfileConsumed,
  onSaved,
  onRebuildQueued,
  onError,
}: {
  generationId: string;
  operation: EmgOperationNode | null;
  features: EmgFeatureNode[];
  edges: KernelEdgeDescriptor[];
  addFeatureDraft: AddFeatureDraftKind | null;
  onAddFeatureDraftChange: (draft: AddFeatureDraftKind | null) => void;
  sketchModeActive: boolean;
  onStartSketch: () => void;
  exportedSketchProfile: SketchProfileSegment[] | null;
  onSketchProfileConsumed: () => void;
  onSaved: () => void;
  onRebuildQueued: (taskId: string) => void;
  onError: (message: string) => void;
}) {
  if (addFeatureDraft) {
    if (sketchModeActive) {
      return (
        <p className="p-3 text-[11px] text-zinc-500">
          Эскиз открыт в центральной области. Нарисуйте контур и нажмите
          «Использовать этот контур», чтобы вернуться сюда.
        </p>
      );
    }
    return (
      <AddFeatureForm
        generationId={generationId}
        kind={addFeatureDraft}
        edges={edges}
        onStartSketch={onStartSketch}
        exportedSketchProfile={exportedSketchProfile}
        onSketchProfileConsumed={onSketchProfileConsumed}
        onCancel={() => onAddFeatureDraftChange(null)}
        onAdded={(taskId) => {
          onAddFeatureDraftChange(null);
          onRebuildQueued(taskId);
        }}
        onSaved={onSaved}
        onError={onError}
      />
    );
  }
  return (
    <PropertiesPanel
      generationId={generationId}
      operation={operation}
      features={features}
      onSaved={onSaved}
      onRebuildQueued={onRebuildQueued}
      onError={onError}
    />
  );
}

function edgeLabel(edge: KernelEdgeDescriptor): string {
  const from = edge.vertices[0];
  const to = edge.vertices[edge.vertices.length - 1];
  const point = (p?: { x: number; y: number; z: number }) =>
    p ? `${p.x.toFixed(1)},${p.y.toFixed(1)},${p.z.toFixed(1)}` : "?";
  return `${edge.curve} #${edge.index} — ${edge.length_mm.toFixed(1)} мм (${point(from)} → ${point(to)})`;
}

function AddFeatureForm({
  generationId,
  kind,
  edges,
  onStartSketch,
  exportedSketchProfile,
  onSketchProfileConsumed,
  onCancel,
  onAdded,
  onSaved,
  onError,
}: {
  generationId: string;
  kind: AddFeatureDraftKind;
  edges: KernelEdgeDescriptor[];
  onStartSketch: () => void;
  exportedSketchProfile: SketchProfileSegment[] | null;
  onSketchProfileConsumed: () => void;
  onCancel: () => void;
  onAdded: (taskId: string) => void;
  onSaved: () => void;
  onError: (message: string) => void;
}) {
  const isProfileKind = kind === "boss" || kind === "pocket";
  const isEdgeKind = kind === "fillet" || kind === "chamfer";

  const [profile, setProfile] = useState<"circle" | "rectangle" | "sketch">(
    "circle",
  );
  const [sketchProfile, setSketchProfile] = useState<
    SketchProfileSegment[] | null
  >(null);
  const [centerX, setCenterX] = useState("0");
  const [centerY, setCenterY] = useState("0");
  const [depth, setDepth] = useState("5");
  const [diameter, setDiameter] = useState("10");
  const [width, setWidth] = useState("10");
  const [height, setHeight] = useState("10");
  const [edgeKey, setEdgeKey] = useState(edges[0]?.key ?? "");
  const [sizeMm, setSizeMm] = useState("2");
  const [thicknessMm, setThicknessMm] = useState("3");
  const [threadSpec, setThreadSpec] = useState("M10");
  const [pitchMm, setPitchMm] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (exportedSketchProfile) {
      setSketchProfile(exportedSketchProfile);
      setProfile("sketch");
      onSketchProfileConsumed();
    }
    // Only react to a NEW handoff arriving — onSketchProfileConsumed is
    // stable-ish and re-running this on its identity would re-consume.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [exportedSketchProfile]);

  const num = (raw: string) => Number(raw.replace(",", "."));

  function buildFeature(): AddedNativeFeature | null {
    if (isProfileKind) {
      if (profile === "sketch") {
        if (!sketchProfile) {
          onError("Сначала нарисуйте и используйте контур эскиза.");
          return null;
        }
        return {
          kind,
          profile: "sketch",
          center_x_mm: num(centerX),
          center_y_mm: num(centerY),
          depth_mm: num(depth),
          sketch_profile: sketchProfile,
        };
      }
      return {
        kind,
        profile,
        center_x_mm: num(centerX),
        center_y_mm: num(centerY),
        depth_mm: num(depth),
        ...(profile === "circle"
          ? { diameter_mm: num(diameter) }
          : { width_mm: num(width), height_mm: num(height) }),
      };
    }
    if (isEdgeKind) {
      if (!edgeKey) {
        onError("Выберите ребро.");
        return null;
      }
      return { kind, edge_key: edgeKey, size_mm: num(sizeMm) };
    }
    if (kind === "shell") {
      return { kind, thickness_mm: num(thicknessMm) };
    }
    // thread
    return {
      kind,
      diameter_mm: num(diameter),
      spec: threadSpec.trim(),
      ...(pitchMm.trim() ? { pitch_mm: num(pitchMm) } : {}),
    };
  }

  async function submit() {
    if (!note.trim()) {
      onError("Опишите инженерное обоснование добавления фичи.");
      return;
    }
    const feature = buildFeature();
    if (!feature) return;
    setBusy(true);
    try {
      const result = await engineeringApi.addGenerationModelGraphFeature(
        generationId,
        {
          feature,
          note: note.trim(),
          idempotency_key: `add-feature:${crypto.randomUUID()}`,
        },
      );
      onAdded(result.rebuild_task_id);
      onSaved();
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3 p-3 text-xs">
      <div className="flex items-center justify-between">
        <p className="text-sm font-medium text-zinc-100">
          Добавить: {KIND_LABEL[kind]}
        </p>
        <button
          type="button"
          onClick={onCancel}
          className="text-[11px] text-zinc-500 hover:text-zinc-300"
        >
          ✕ Отмена
        </button>
      </div>

      {isProfileKind && (
        <>
          <label className="block space-y-1">
            <span className="text-[11px] text-zinc-400">Профиль</span>
            <div className="flex gap-2">
              {(["circle", "rectangle", "sketch"] as const).map((value) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => setProfile(value)}
                  className={`rounded border px-2.5 py-1 text-[11px] ${
                    profile === value
                      ? "border-sky-400/60 bg-sky-500/15 text-sky-200"
                      : "border-white/10 text-zinc-400 hover:bg-white/5"
                  }`}
                >
                  {value === "circle"
                    ? "Круг"
                    : value === "rectangle"
                      ? "Прямоугольник"
                      : "Эскиз"}
                </button>
              ))}
            </div>
          </label>
          <div className="grid grid-cols-2 gap-2">
            <NumberField
              label="center_x_mm"
              value={centerX}
              onChange={setCenterX}
            />
            <NumberField
              label="center_y_mm"
              value={centerY}
              onChange={setCenterY}
            />
            <NumberField label="depth_mm" value={depth} onChange={setDepth} />
            {profile === "circle" && (
              <NumberField
                label="diameter_mm"
                value={diameter}
                onChange={setDiameter}
              />
            )}
            {profile === "rectangle" && (
              <>
                <NumberField
                  label="width_mm"
                  value={width}
                  onChange={setWidth}
                />
                <NumberField
                  label="height_mm"
                  value={height}
                  onChange={setHeight}
                />
              </>
            )}
          </div>
          {profile === "sketch" && (
            <div className="rounded border border-white/10 bg-black/20 p-2">
              {sketchProfile ? (
                <p className="flex items-center justify-between text-[11px]">
                  <span className="text-emerald-300">
                    ✓ Контур готов: {sketchProfile.length} элемент(ов)
                  </span>
                  <button
                    type="button"
                    onClick={onStartSketch}
                    className="text-sky-300 hover:text-sky-200"
                  >
                    Перерисовать
                  </button>
                </p>
              ) : (
                <button
                  type="button"
                  onClick={onStartSketch}
                  className="w-full rounded border border-sky-400/30 bg-sky-500/10 px-2 py-1.5 text-[11px] text-sky-200 hover:bg-sky-500/20"
                >
                  ✎ Открыть эскизный редактор
                </button>
              )}
            </div>
          )}
        </>
      )}

      {isEdgeKind && (
        <>
          <label className="block space-y-1">
            <span className="text-[11px] text-zinc-400">
              Ребро{" "}
              {edges.length === 0 && "(нет данных — сначала пересоберите)"}
            </span>
            <select
              value={edgeKey}
              onChange={(event) => setEdgeKey(event.target.value)}
              className="w-full rounded border border-white/10 bg-black/30 px-2 py-1.5 text-[11px] text-zinc-100 outline-none focus:border-sky-400/60"
            >
              {edges.map((edge) => (
                <option key={edge.key} value={edge.key}>
                  {edgeLabel(edge)}
                </option>
              ))}
            </select>
          </label>
          <NumberField
            label={
              kind === "fillet" ? "size_mm (радиус)" : "size_mm (катет фаски)"
            }
            value={sizeMm}
            onChange={setSizeMm}
          />
        </>
      )}

      {kind === "shell" && (
        <NumberField
          label="thickness_mm"
          value={thicknessMm}
          onChange={setThicknessMm}
        />
      )}

      {kind === "thread" && (
        <div className="grid grid-cols-2 gap-2">
          <NumberField
            label="diameter_mm"
            value={diameter}
            onChange={setDiameter}
          />
          <label className="block space-y-1">
            <span className="font-mono text-[10px] text-zinc-500">spec</span>
            <input
              value={threadSpec}
              onChange={(event) => setThreadSpec(event.target.value)}
              className="w-full rounded border border-white/10 bg-black/30 px-2 py-1.5 font-mono text-[12px] text-zinc-100 outline-none focus:border-sky-400/60"
            />
          </label>
          <NumberField
            label="pitch_mm (опц.)"
            value={pitchMm}
            onChange={setPitchMm}
          />
        </div>
      )}

      <label className="block space-y-1">
        <span className="text-[11px] text-zinc-400">
          Инженерное обоснование
        </span>
        <textarea
          value={note}
          onChange={(event) => setNote(event.target.value)}
          placeholder="Например: добавлена крепёжная бобышка по требованию сборки"
          className="h-16 w-full rounded border border-white/10 bg-black/30 p-2 text-[11px] text-zinc-100 outline-none focus:border-sky-400/60"
        />
      </label>

      <button
        type="button"
        disabled={busy}
        onClick={() => void submit()}
        className="w-full rounded bg-emerald-500/20 px-3 py-1.5 text-emerald-200 hover:bg-emerald-500/30 disabled:opacity-40"
      >
        Добавить и пересобрать
      </button>
    </div>
  );
}

function NumberField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="block space-y-1">
      <span className="font-mono text-[10px] text-zinc-500">{label}</span>
      <input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        inputMode="decimal"
        className="w-full rounded border border-white/10 bg-black/30 px-2 py-1.5 font-mono text-[12px] text-zinc-100 outline-none focus:border-sky-400/60"
      />
    </label>
  );
}
