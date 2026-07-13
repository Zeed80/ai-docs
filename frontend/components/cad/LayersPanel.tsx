"use client";

import { useState } from "react";

import { LAYER_CATALOG } from "@/components/cad/geometry";
import type { IrLineClass } from "@/lib/studio-api";

/** I4: AutoCAD-style layer manager over the fixed ЕСКД line classes. Shows
 * each layer's DXF name / linetype / lineweight (exactly what the DXF export
 * writes) and its three independent states: visible, locked (drawn but inert)
 * and frozen (neither drawn nor selectable). State lives in the parent so the
 * canvas render/selection honour it. */
export default function LayersPanel({
  counts,
  visible,
  locked,
  frozen,
  onToggleVisible,
  onToggleLocked,
  onToggleFrozen,
  t,
}: {
  counts: Record<string, number>;
  visible: Set<IrLineClass>;
  locked: Set<IrLineClass>;
  frozen: Set<IrLineClass>;
  onToggleVisible: (layer: IrLineClass) => void;
  onToggleLocked: (layer: IrLineClass) => void;
  onToggleFrozen: (layer: IrLineClass) => void;
  t: (k: string, v?: Record<string, string | number>) => string;
}) {
  const [open, setOpen] = useState(false);

  return (
    <div className="rounded border border-white/10 bg-zinc-900/60 p-2 space-y-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between text-xs text-zinc-300"
      >
        <span>{t("vector.layers")}</span>
        <span className="text-zinc-500">{open ? "−" : "+"}</span>
      </button>
      {open && (
        <div className="space-y-1">
          <div className="grid grid-cols-[1fr_auto_auto_auto] items-center gap-x-2 gap-y-1 text-[11px]">
            {LAYER_CATALOG.map((layer) => {
              const isFrozen = frozen.has(layer.lineClass);
              const isLocked = locked.has(layer.lineClass);
              const isVisible = visible.has(layer.lineClass);
              const n = counts[layer.lineClass] ?? 0;
              return (
                <div key={layer.lineClass} className="contents">
                  <div
                    className={`flex items-center gap-1.5 truncate ${
                      isFrozen ? "opacity-40" : ""
                    }`}
                    title={`${layer.dxfLayer} · ${layer.linetype} · ${layer.lineweightMm} мм`}
                  >
                    <span
                      className="h-2.5 w-2.5 shrink-0 rounded-sm"
                      style={{ backgroundColor: layer.color }}
                    />
                    <span className="truncate text-zinc-200">
                      {t(`vector.line_${layer.lineClass}`)}
                    </span>
                    <span className="text-zinc-500">{n}</span>
                  </div>
                  <button
                    type="button"
                    title={t("vector.layer_visible")}
                    onClick={() => onToggleVisible(layer.lineClass)}
                    disabled={isFrozen}
                    className={`h-5 w-5 rounded ${
                      isVisible && !isFrozen
                        ? "bg-white/10 text-zinc-100"
                        : "text-zinc-600"
                    } disabled:opacity-30`}
                  >
                    {isVisible ? "👁" : "—"}
                  </button>
                  <button
                    type="button"
                    title={t("vector.layer_lock")}
                    onClick={() => onToggleLocked(layer.lineClass)}
                    className={`h-5 w-5 rounded ${
                      isLocked
                        ? "bg-amber-500/20 text-amber-300"
                        : "text-zinc-500"
                    }`}
                  >
                    {isLocked ? "🔒" : "🔓"}
                  </button>
                  <button
                    type="button"
                    title={t("vector.layer_freeze")}
                    onClick={() => onToggleFrozen(layer.lineClass)}
                    className={`h-5 w-5 rounded ${
                      isFrozen ? "bg-sky-500/20 text-sky-300" : "text-zinc-500"
                    }`}
                  >
                    {isFrozen ? "❄" : "○"}
                  </button>
                </div>
              );
            })}
          </div>
          <p className="pt-1 text-[10px] leading-tight text-zinc-500">
            {t("vector.layers_hint")}
          </p>
        </div>
      )}
    </div>
  );
}
