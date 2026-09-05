"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Badge } from "@/components/ui/primitives/Badge";
import { StatusDot } from "@/components/ui/primitives/StatusDot";
import { formatLatency } from "@/lib/models/format";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";

const API = getApiBaseUrl();

export interface DiffEntry {
  slot: string;
  label?: string;
  old_model: string | null;
  new_model: string | null;
  affected: string[];
}

interface Preview {
  ok: boolean;
  error?: string | null;
  provider?: string | null;
  provider_model?: string | null;
  thinking_requested?: boolean | null;
  thinking_payload_supported?: boolean;
  // Заполняется только при настоящем прогоне: в dry_run провайдер не
  // вызывается, и измерять нечего.
  latency_ms?: number | null;
  warnings?: { message: string }[];
}

/**
 * Что изменится, до применения.
 *
 * Раньше diff показывался плоским текстом «слот: старое → новое» и только
 * после нажатия «Проверить», а при любой правке селекта пропадал. Понять, во
 * что выльется набор изменений, было нельзя — оставалось применить и
 * посмотреть.
 *
 * Предпросмотр использует тот же пробный вызов, что и проверка после
 * применения, но в режиме dry_run: он резолвит каталог, политику, узел и
 * параметры рассуждения, ничего не отправляя провайдеру. Значит показать
 * результат заранее ничего не стоит.
 */
export function DiffPanel({
  diff,
  draftFor,
}: {
  diff: DiffEntry[];
  /** Параметры черновика для слота — их и надо проверять, а не текущие. */
  draftFor: (slot: string) => {
    model?: string | null;
    thinking?: boolean | null;
    thinking_level?: string | null;
  };
}) {
  const [previews, setPreviews] = useState<Record<string, Preview | "loading">>(
    {},
  );
  const timer = useRef<number | null>(null);

  const runPreview = useCallback(async () => {
    for (const entry of diff) {
      setPreviews((p) => ({ ...p, [entry.slot]: "loading" }));
    }
    // Параллельно: слотов в одном черновике единицы, ждать их по очереди
    // незачем.
    await Promise.all(
      diff.map(async (entry) => {
        try {
          const r = await fetch(
            `${API}/api/providers/slots/${entry.slot}/smoke`,
            {
              method: "POST",
              credentials: "include",
              headers: { "Content-Type": "application/json", ...csrfHeaders() },
              body: JSON.stringify({ ...draftFor(entry.slot), dry_run: true }),
            },
          );
          const data = r.ok
            ? await r.json()
            : { ok: false, error: `сервер ответил ${r.status}` };
          setPreviews((p) => ({ ...p, [entry.slot]: data }));
        } catch {
          setPreviews((p) => ({
            ...p,
            [entry.slot]: { ok: false, error: "нет связи с сервером" },
          }));
        }
      }),
    );
  }, [diff, draftFor]);

  // Дебаунс: пока человек перебирает модели, дёргать сервер на каждый клик
  // незачем.
  useEffect(() => {
    if (diff.length === 0) {
      setPreviews({});
      return;
    }
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => void runPreview(), 400);
    return () => {
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, [diff, runPreview]);

  if (diff.length === 0) return null;

  return (
    <div className="rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2">
      <p className="mb-2 text-xs font-medium text-amber-300">
        Изменится {diff.length}{" "}
        {diff.length === 1 ? "слот" : diff.length < 5 ? "слота" : "слотов"}
      </p>

      <ul className="flex flex-col gap-2">
        {diff.map((entry) => {
          const preview = previews[entry.slot];
          return (
            <li key={entry.slot} className="text-[11px]">
              <div className="flex flex-wrap items-baseline gap-x-2">
                <span className="text-slate-200">
                  {entry.label ?? entry.slot}
                </span>
                <span className="text-slate-400">
                  {entry.old_model ?? "—"} → {entry.new_model ?? "—"}
                </span>
              </div>

              {entry.affected.length > 0 && (
                <p className="text-slate-500">
                  затронет: {entry.affected.join(", ")}
                </p>
              )}

              {preview === "loading" && (
                <p className="text-slate-500">проверяем…</p>
              )}

              {preview && preview !== "loading" && (
                <p className="flex flex-wrap items-center gap-1.5">
                  <StatusDot
                    state={preview.ok ? "ok" : "error"}
                    title={
                      preview.ok ? "проверка пройдена" : "проверка не прошла"
                    }
                  />
                  {preview.ok ? (
                    <>
                      <span className="text-slate-400">
                        {preview.provider_model ?? entry.new_model}
                        {preview.provider ? ` · ${preview.provider}` : ""}
                      </span>
                      {preview.thinking_requested && (
                        <Badge
                          tone={
                            preview.thinking_payload_supported ? "info" : "warn"
                          }
                          title={
                            preview.thinking_payload_supported
                              ? "провайдер принимает параметр рассуждения"
                              : "провайдер может проигнорировать параметр рассуждения"
                          }
                        >
                          рассуждение
                        </Badge>
                      )}
                      {preview.latency_ms != null && (
                        <span className="text-slate-500">
                          {formatLatency(preview.latency_ms)}
                        </span>
                      )}
                    </>
                  ) : (
                    <span className="text-red-400">{preview.error}</span>
                  )}
                </p>
              )}

              {preview &&
                preview !== "loading" &&
                (preview.warnings ?? []).map((w, i) => (
                  <p key={i} className="text-amber-400">
                    {w.message}
                  </p>
                ))}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
