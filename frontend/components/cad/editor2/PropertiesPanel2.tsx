"use client";

import { useState } from "react";

import PropertiesPanel from "@/components/cad/editor/PropertiesPanel";
import { engineeringApi, type AddedNativeFeature } from "@/lib/engineering-api";
import type { EmgFeatureNode, EmgOperationNode } from "@/lib/emg-tree";

export type AddFeatureDraftKind = "boss" | "pocket";

const KIND_LABEL: Record<AddFeatureDraftKind, string> = {
  boss: "Бобышка",
  pocket: "Карман",
};

/** Ф2 нового CAD-редактора: PropertiesPanel (коррекция, БЕЗ ИЗМЕНЕНИЙ) плюс
 * новая форма «добавить фичу», которая появляется, когда лента (вкладка
 * Фичи) начинает добавление. Пока — только circle/rectangle boss/pocket;
 * fillet/chamfer/shell/thread (Ф3) используют ТОТ ЖЕ backend-эндпоинт,
 * нужна только своя форма — клик по ребру вместо edge_key текстом. */
export default function PropertiesPanel2({
  generationId,
  operation,
  features,
  addFeatureDraft,
  onAddFeatureDraftChange,
  onSaved,
  onRebuildQueued,
  onError,
}: {
  generationId: string;
  operation: EmgOperationNode | null;
  features: EmgFeatureNode[];
  addFeatureDraft: AddFeatureDraftKind | null;
  onAddFeatureDraftChange: (draft: AddFeatureDraftKind | null) => void;
  onSaved: () => void;
  onRebuildQueued: (taskId: string) => void;
  onError: (message: string) => void;
}) {
  if (addFeatureDraft) {
    return (
      <AddFeatureForm
        generationId={generationId}
        kind={addFeatureDraft}
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

function AddFeatureForm({
  generationId,
  kind,
  onCancel,
  onAdded,
  onSaved,
  onError,
}: {
  generationId: string;
  kind: AddFeatureDraftKind;
  onCancel: () => void;
  onAdded: (taskId: string) => void;
  onSaved: () => void;
  onError: (message: string) => void;
}) {
  const [profile, setProfile] = useState<"circle" | "rectangle">("circle");
  const [centerX, setCenterX] = useState("0");
  const [centerY, setCenterY] = useState("0");
  const [depth, setDepth] = useState("5");
  const [diameter, setDiameter] = useState("10");
  const [width, setWidth] = useState("10");
  const [height, setHeight] = useState("10");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    if (!note.trim()) {
      onError("Опишите инженерное обоснование добавления фичи.");
      return;
    }
    const num = (raw: string) => Number(raw.replace(",", "."));
    const feature: AddedNativeFeature = {
      kind,
      profile,
      center_x_mm: num(centerX),
      center_y_mm: num(centerY),
      depth_mm: num(depth),
      ...(profile === "circle"
        ? { diameter_mm: num(diameter) }
        : { width_mm: num(width), height_mm: num(height) }),
    };
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

      <label className="block space-y-1">
        <span className="text-[11px] text-zinc-400">Профиль</span>
        <div className="flex gap-2">
          {(["circle", "rectangle"] as const).map((value) => (
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
              {value === "circle" ? "Круг" : "Прямоугольник"}
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
        {profile === "circle" ? (
          <NumberField
            label="diameter_mm"
            value={diameter}
            onChange={setDiameter}
          />
        ) : (
          <>
            <NumberField label="width_mm" value={width} onChange={setWidth} />
            <NumberField
              label="height_mm"
              value={height}
              onChange={setHeight}
            />
          </>
        )}
      </div>

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
