"use client";

import { useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { useToast } from "@/components/ui/primitives/Toast";
import {
  btn,
  card,
  cardHeader as cardH,
  input,
} from "@/components/ui/primitives/tokens";
import { providerBarColor, providerLabel } from "@/lib/models/labels";
import type { AllStatus } from "@/lib/models/types";

const API = getApiBaseUrl();
const btnPrimary = `${btn} bg-blue-600 hover:bg-blue-700 text-white disabled:opacity-50`;

export function GpuPanel({ status }: { status: AllStatus | null }) {
  const toast = useToast();
  const [limits, setLimits] = useState<Record<string, number>>({});
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!status) return;
    const init: Record<string, number> = {};
    for (const [p, a] of Object.entries(status.vram_allocations)) {
      init[p] = a.vram_limit_gb ?? 24;
    }
    setLimits(init);
  }, [status]);

  const saveLimits = async () => {
    setSaving(true);
    try {
      await fetch(`${API}/api/local-models/gpu-budget`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        credentials: "include",
        body: JSON.stringify(limits),
      });
    } catch (e) {
      toast.error("Лимиты не сохранены", String(e));
    }
    setSaving(false);
  };

  if (!status)
    return <div className="text-slate-400 text-sm p-6">Загрузка...</div>;
  const { gpu, vram_allocations, total_vram_gb } = status;
  const total = gpu?.total_gb ?? total_vram_gb;
  const usedByProvider = Object.entries(vram_allocations);

  return (
    <div className="space-y-4">
      {/* GPU info */}
      {gpu && (
        <div className="grid grid-cols-3 gap-3">
          {[
            { label: "Всего VRAM", value: `${gpu.total_gb.toFixed(1)} GB` },
            { label: "Используется", value: `${gpu.used_gb.toFixed(1)} GB` },
            { label: "Свободно", value: `${gpu.free_gb.toFixed(1)} GB` },
          ].map(({ label, value }) => (
            <div
              key={label}
              className="bg-slate-900 rounded p-3 border border-slate-700 text-center"
            >
              <div className="text-xs text-slate-400">{label}</div>
              <div className="text-lg font-mono text-slate-100">{value}</div>
            </div>
          ))}
        </div>
      )}

      {/* Per-provider usage */}
      <div className={card}>
        <div className={cardH}>
          <span className="text-sm font-medium text-slate-100">
            Использование VRAM по провайдерам
          </span>
        </div>
        <div className="p-4 space-y-4">
          {usedByProvider.map(([p, a]) => {
            const pct = total > 0 ? (a.vram_used_gb / total) * 100 : 0;
            const limitPct =
              limits[p] != null && total > 0 ? (limits[p] / total) * 100 : 100;
            return (
              <div key={p} className="space-y-2">
                <div className="flex justify-between text-sm">
                  <span className="text-slate-200">
                    {providerLabel(p)}
                  </span>
                  <span className="text-slate-400">
                    {a.vram_used_gb.toFixed(1)} /{" "}
                    {(limits[p] ?? total).toFixed(0)} GB
                  </span>
                </div>
                <div className="relative h-3 rounded bg-slate-800">
                  {/* Limit indicator */}
                  {limits[p] != null && (
                    <div
                      className="absolute top-0 h-full border-r-2 border-amber-400"
                      style={{ left: `${limitPct}%` }}
                      title={`Лимит: ${limits[p]} GB`}
                    />
                  )}
                  {/* Usage bar */}
                  <div
                    className={`h-full rounded ${providerBarColor(p)} transition-all`}
                    style={{ width: `${pct}%` }}
                  />
                </div>
                <div className="flex items-center gap-3">
                  <span className="text-xs text-slate-400 w-16">
                    Лимит (GB)
                  </span>
                  <input
                    type="number"
                    min={1}
                    max={total}
                    step={1}
                    value={limits[p] ?? total}
                    onChange={(e) =>
                      setLimits((prev) => ({
                        ...prev,
                        [p]: parseFloat(e.target.value),
                      }))
                    }
                    className={`${input} w-24`}
                  />
                  <span className="text-xs text-slate-400">
                    (мягкий лимит — предупреждение при превышении)
                  </span>
                </div>
                {a.models.map((m) => (
                  <div
                    key={m.name}
                    className="flex justify-between text-xs text-slate-400 pl-4"
                  >
                    <span className="truncate">{m.name}</span>
                    <span>{m.vram_gb.toFixed(1)} GB</span>
                  </div>
                ))}
              </div>
            );
          })}
        </div>
        <div className="px-4 pb-4">
          <button onClick={saveLimits} disabled={saving} className={btnPrimary}>
            {saving ? "Сохранение..." : "Сохранить лимиты"}
          </button>
        </div>
      </div>

      <div className="text-xs text-slate-400 bg-slate-900 rounded p-3 border border-slate-800">
        <b className="text-slate-400">Как работают мягкие лимиты:</b> при
        попытке активировать модель, которая превысит лимит, система показывает
        предупреждение, но не блокирует — вы сами решаете. Для жёсткого
        ограничения — используйте отдельный Docker с device-memory limit.
      </div>
    </div>
  );
}
