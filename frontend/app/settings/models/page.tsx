"use client";

/**
 * Раздел «Модели и провайдеры» — оболочка с вкладками.
 *
 * От исходных 3647 строк здесь осталась только навигация и общее состояние
 * локальных серверов: сами вкладки живут в components/models — назначения,
 * инфраструктура, каталог. Каждая отвечает на свой вопрос, и править одну
 * теперь можно, не открывая остальные.
 */

import { useCallback, useEffect, useState } from "react";
import { AssignmentBoard } from "@/components/models/assignment/AssignmentBoard";
import { LibraryPanel } from "@/components/models/catalog/LibraryPanel";
import { ParametersPanel } from "@/components/models/catalog/ParametersPanel";
import { GpuPanel } from "@/components/models/infra/GpuPanel";
import { InfraPanel } from "@/components/models/infra/InfraPanel";
import { getApiBaseUrl } from "@/lib/api-base";
import { useCurrentUser } from "@/lib/auth-context";
import { hasRole } from "@/lib/rbac";
import type { AllStatus, ModelsTab as Tab } from "@/lib/models/types";

const API = getApiBaseUrl();

// Дефолтная вкладка — «Назначение»: это ежедневный сценарий, а экран
// открывался на «Провайдерах», куда заходят при первичной настройке.
const TABS: { id: Tab; label: string }[] = [
  { id: "assignment", label: "Назначение" },
  { id: "overview", label: "Провайдеры" },
  { id: "library", label: "Библиотека" },
  { id: "parameters", label: "Параметры" },
  { id: "gpu", label: "GPU Бюджет" },
];

export default function ModelsPage() {
  const currentUser = useCurrentUser();
  const isAdmin = !!currentUser && hasRole(currentUser.roles, "admin");
  const [tab, setTab] = useState<Tab>("assignment");
  const [status, setStatus] = useState<AllStatus | null>(null);
  const [loading, setLoading] = useState(true);

  const loadStatus = useCallback(async () => {
    try {
      const r = await fetch(`${API}/api/local-models/status`, {
        credentials: "include",
      });
      if (r.ok) setStatus(await r.json());
    } catch {
      // Состояние серверов — вспомогательная строка вверху экрана; без неё
      // раздел работать не перестаёт, а вкладки грузят данные сами.
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    loadStatus();
    const t = setInterval(loadStatus, 30000);
    return () => clearInterval(t);
  }, [loadStatus]);

  // Каждый эндпоинт раздела требует роли admin, а гарда на странице не было:
  // не-админ видел пустые списки и ошибки запросов вместо объяснения.
  if (currentUser && !isAdmin) {
    return (
      <div className="min-h-screen bg-slate-950 text-slate-100">
        <div className="mx-auto max-w-2xl px-4 py-16 text-center">
          <h1 className="text-xl font-semibold text-slate-100">
            Раздел доступен администраторам
          </h1>
          <p className="mt-2 text-sm text-slate-400">
            Здесь настраиваются узлы провайдеров, ключи доступа и то, какая
            модель отвечает за какую задачу. Если настройка нужна — попросите
            администратора.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <div className="max-w-5xl mx-auto px-4 py-8 space-y-6">
        <div>
          <h1 className="text-2xl font-bold text-slate-100">
            Модели и провайдеры
          </h1>
          <p className="text-sm text-slate-400 mt-1">
            Ollama, llama.cpp, vLLM и облачные провайдеры · библиотека,
            маршрутизация задач, GPU-бюджет
          </p>
        </div>

        <div className="flex gap-1 border-b border-slate-700">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => setTab(t.id)}
              aria-current={tab === t.id ? "page" : undefined}
              className={`px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
                tab === t.id
                  ? "border-blue-500 text-blue-400"
                  : "border-transparent text-slate-400 hover:text-slate-200"
              }`}
            >
              {t.label}
            </button>
          ))}
          <div className="flex-1" />
          <button
            type="button"
            onClick={loadStatus}
            className="px-3 py-2 text-xs text-slate-500 hover:text-slate-300 transition-colors"
            title="Обновить состояние серверов"
            aria-label="Обновить состояние серверов"
          >
            {loading ? "..." : "↺"}
          </button>
        </div>

        <div>
          {tab === "assignment" && <AssignmentBoard />}
          {tab === "overview" && (
            <InfraPanel status={status} onTabChange={setTab} />
          )}
          {tab === "library" && <LibraryPanel />}
          {tab === "parameters" && <ParametersPanel />}
          {tab === "gpu" && <GpuPanel status={status} />}
        </div>
      </div>
    </div>
  );
}
