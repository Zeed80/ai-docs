"use client";

import { useEffect, useState } from "react";
import { getQueuedUploads, flushQueue } from "@/lib/offline-queue";
import { getApiBaseUrl } from "@/lib/api-base";
import { mutFetch } from "@/lib/auth";

const API = getApiBaseUrl();

export function OfflineQueueWidget() {
  const [count, setCount] = useState(0);
  const [online, setOnline] = useState(true);
  const [flushing, setFlushing] = useState(false);
  const [lastResult, setLastResult] = useState<{
    flushed: number;
    failed: number;
  } | null>(null);

  useEffect(() => {
    setOnline(navigator.onLine);

    const handleOnline = () => setOnline(true);
    const handleOffline = () => setOnline(false);
    window.addEventListener("online", handleOnline);
    window.addEventListener("offline", handleOffline);
    return () => {
      window.removeEventListener("online", handleOnline);
      window.removeEventListener("offline", handleOffline);
    };
  }, []);

  useEffect(() => {
    getQueuedUploads()
      .then((items) => setCount(items.length))
      .catch(() => {});
  }, [online]);

  const flush = async () => {
    setFlushing(true);
    try {
      const result = await flushQueue(async (entry) => {
        const blob = new Blob([entry.data], { type: entry.mime });
        const fd = new FormData();
        fd.append("file", blob, entry.filename);
        const r = await mutFetch(`${API}/api/documents/ingest`, {
          method: "POST",
          body: fd,
        });
        if (!r.ok) throw new Error("upload failed");
      });
      setLastResult(result);
      setCount((c) => Math.max(0, c - result.flushed));
      setTimeout(() => setLastResult(null), 4000);
    } finally {
      setFlushing(false);
    }
  };

  if (count === 0 || !online) return null;

  return (
    <div className="fixed bottom-20 right-4 z-40 bg-slate-800 border border-amber-600/50 rounded-xl shadow-2xl px-4 py-3 text-sm text-slate-200 max-w-xs">
      <div className="flex items-center gap-3">
        <div className="flex-1">
          <div className="font-medium text-white text-xs mb-0.5">
            Очередь загрузки
          </div>
          <div className="text-xs text-slate-400">
            {count} файл{count > 1 ? "а" : ""} ожидают отправки
          </div>
          {lastResult && (
            <div className="text-xs text-green-400 mt-0.5">
              ✓ {lastResult.flushed} отправлено
              {lastResult.failed > 0 && (
                <span className="text-red-400 ml-1">
                  · {lastResult.failed} ошибок
                </span>
              )}
            </div>
          )}
        </div>
        <button
          onClick={() => void flush()}
          disabled={flushing}
          className="shrink-0 px-3 py-1.5 bg-amber-600 hover:bg-amber-500 disabled:opacity-50 text-white rounded text-xs font-medium transition-colors"
        >
          {flushing ? "…" : "Отправить"}
        </button>
      </div>
    </div>
  );
}
