"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { getApiBaseUrl } from "@/lib/api-base";
import { apiFetch, mutFetch } from "@/lib/auth";

const ADMIN = `${getApiBaseUrl()}/api/comfyui-admin`;

interface Recommended {
  filename: string;
  label: string;
  operation: string;
  vram: string;
  why: string;
  installed: boolean;
  available_to_download: boolean;
  size?: string | null;
  type?: string | null;
}

interface ModelsResponse {
  online: boolean;
  error?: string;
  installed: Record<string, string[]>;
  recommended: Recommended[];
}

interface QueueStatus {
  total_count: number;
  done_count: number;
  in_progress_count: number;
  is_processing: boolean;
}

export default function ModelsSection() {
  const [data, setData] = useState<ModelsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [queue, setQueue] = useState<QueueStatus | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await apiFetch(`${ADMIN}/models`);
      setData(await res.json());
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const pollQueue = useCallback(async () => {
    try {
      const res = await apiFetch(`${ADMIN}/models/install-status`);
      const q: QueueStatus = await res.json();
      setQueue(q);
      if (!q.is_processing) {
        if (pollRef.current) {
          clearInterval(pollRef.current);
          pollRef.current = null;
        }
        await load();
      }
    } catch {
      /* ignore transient */
    }
  }, [load]);

  async function install(filename: string) {
    setBusy(filename);
    setErr(null);
    try {
      const res = await mutFetch(`${ADMIN}/models/install`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename }),
      });
      if (!res.ok) {
        const b = await res.json().catch(() => ({}));
        throw new Error(b?.detail || `HTTP ${res.status}`);
      }
      if (!pollRef.current) {
        pollRef.current = setInterval(pollQueue, 3000);
      }
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(null);
    }
  }

  useEffect(
    () => () => {
      if (pollRef.current) clearInterval(pollRef.current);
    },
    [],
  );

  const installedCount = data
    ? Object.values(data.installed).reduce((n, arr) => n + arr.length, 0)
    : 0;

  return (
    <section className="rounded-lg border border-border p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="font-medium">Модели</h3>
        <button
          onClick={load}
          className="text-xs text-muted-foreground hover:text-foreground"
        >
          Обновить
        </button>
      </div>

      {loading ? (
        <p className="text-sm text-muted-foreground">Загрузка…</p>
      ) : !data?.online ? (
        <p className="text-sm text-amber-500">
          ComfyUI недоступен. {data?.error}
        </p>
      ) : (
        <>
          <p className="text-xs text-muted-foreground">
            Установлено моделей: {installedCount}. Рекомендуемые ниже подобраны
            под RTX 3090 24 ГБ (fp8 / lightning) и работают совместно с моделью
            агента.
          </p>

          {queue && queue.is_processing && (
            <div className="text-xs text-sky-500">
              Загрузка: {queue.done_count}/{queue.total_count}…
            </div>
          )}

          <ul className="space-y-2">
            {data.recommended.map((m) => (
              <li
                key={m.filename}
                className="flex items-start justify-between gap-3 rounded border border-border px-3 py-2"
              >
                <div className="min-w-0">
                  <div className="text-sm font-medium">{m.label}</div>
                  <div className="text-xs text-muted-foreground">{m.why}</div>
                  <div className="text-[11px] text-muted-foreground/80 mt-0.5">
                    {m.filename} · {m.vram}
                    {m.size ? ` · ${m.size}` : ""}
                  </div>
                </div>
                <div className="shrink-0">
                  {m.installed ? (
                    <span className="text-xs text-emerald-500">
                      установлено ✓
                    </span>
                  ) : m.available_to_download ? (
                    <button
                      disabled={!!busy || (queue?.is_processing ?? false)}
                      onClick={() => install(m.filename)}
                      className="px-2.5 py-1 rounded bg-primary text-primary-foreground text-xs disabled:opacity-50"
                    >
                      {busy === m.filename ? "…" : "Скачать"}
                    </button>
                  ) : (
                    <span className="text-xs text-muted-foreground">
                      нет в каталоге
                    </span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </>
      )}
      {err && <div className="text-sm text-red-500">{err}</div>}
    </section>
  );
}
