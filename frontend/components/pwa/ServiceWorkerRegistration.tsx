"use client";

import { useEffect, useState } from "react";

export function ServiceWorkerRegistration() {
  const [updateAvailable, setUpdateAvailable] = useState(false);

  useEffect(() => {
    if (!("serviceWorker" in navigator)) return;

    navigator.serviceWorker
      .register("/sw.js", { scope: "/" })
      .then((reg) => {
        reg.addEventListener("updatefound", () => {
          const worker = reg.installing;
          if (!worker) return;
          worker.addEventListener("statechange", () => {
            if (
              worker.state === "installed" &&
              navigator.serviceWorker.controller
            ) {
              setUpdateAvailable(true);
            }
          });
        });
      })
      .catch(() => {});
  }, []);

  if (!updateAvailable) return null;

  return (
    <div className="fixed bottom-4 left-1/2 -translate-x-1/2 z-50 flex items-center gap-3 bg-slate-800 border border-blue-600/50 rounded-xl shadow-2xl px-4 py-3 text-sm text-slate-200">
      <span>Доступно обновление приложения</span>
      <button
        onClick={() => window.location.reload()}
        className="px-3 py-1 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-xs font-medium transition-colors"
      >
        Обновить
      </button>
    </div>
  );
}

export function InstallPrompt() {
  const [prompt, setPrompt] = useState<Event | null>(null);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    if (dismissed) return;
    const handler = (e: Event) => {
      e.preventDefault();
      setPrompt(e);
    };
    window.addEventListener("beforeinstallprompt", handler);
    return () => window.removeEventListener("beforeinstallprompt", handler);
  }, [dismissed]);

  if (!prompt || dismissed) return null;

  const install = async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await (prompt as any).prompt();
    setPrompt(null);
  };

  return (
    <div className="fixed bottom-4 right-4 z-50 flex items-center gap-3 bg-slate-800 border border-slate-600 rounded-xl shadow-2xl px-4 py-3 text-sm text-slate-200 max-w-xs">
      <div className="flex-1">
        <div className="font-medium text-white text-xs mb-0.5">
          Установить приложение
        </div>
        <div className="text-xs text-slate-400">
          Добавить AI Docs на рабочий стол
        </div>
      </div>
      <div className="flex gap-1.5 shrink-0">
        <button
          onClick={() => setDismissed(true)}
          className="px-2 py-1 text-slate-500 hover:text-slate-300 text-xs"
        >
          Нет
        </button>
        <button
          onClick={() => void install()}
          className="px-3 py-1 bg-blue-600 hover:bg-blue-500 text-white rounded text-xs font-medium transition-colors"
        >
          Да
        </button>
      </div>
    </div>
  );
}
