"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { useCallback, useEffect, useState } from "react";
import { apiFetch, mutFetch } from "@/lib/auth";

const API = getApiBaseUrl();

interface RecipeStep {
  capability: string;
  action: string;
  args_template: Record<string, unknown>;
}

interface Recipe {
  id: string;
  name: string;
  description: string | null;
  role: string;
  trigger_examples: string[];
  steps: RecipeStep[];
  param_slots: Record<string, { source: string; example: string }> | null;
  success_count: number;
  fail_count: number;
  last_used_at: string | null;
  status: "draft" | "active" | "retired";
  created_at: string;
}

const STATUS_LABELS: Record<Recipe["status"], string> = {
  draft: "Черновик",
  active: "Активен",
  retired: "Отключён",
};

const STATUS_COLORS: Record<Recipe["status"], string> = {
  draft: "bg-amber-900/30 text-amber-400 border-amber-700/40",
  active: "bg-green-900/30 text-green-400 border-green-700/40",
  retired: "bg-slate-800 text-slate-500 border-slate-700",
};

export default function RecipesSettingsPage() {
  const [recipes, setRecipes] = useState<Recipe[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await apiFetch(`${API}/api/agent/recipes`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      setRecipes((await resp.json()) as Recipe[]);
    } catch (e) {
      setError(`Не удалось загрузить рецепты: ${e}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const act = async (id: string, action: "activate" | "retire" | "delete") => {
    setBusyId(id);
    try {
      const resp =
        action === "delete"
          ? await mutFetch(`${API}/api/agent/recipes/${id}`, {
              method: "DELETE",
            })
          : await mutFetch(`${API}/api/agent/recipes/${id}/${action}`, {
              method: "POST",
            });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      await load();
    } catch (e) {
      setError(`Операция не выполнена: ${e}`);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold text-slate-100">Рецепты агента</h1>
        <p className="mt-1 text-sm text-slate-400">
          Выученные последовательности действий: успешно решённая задача
          записывается как черновик и после проверенных повторов (или вашего
          подтверждения) выполняется мгновенно, без планирования моделью.
          Рецепты состоят только из существующих инструментов — действия,
          требующие подтверждения, в рецепты не попадают.
        </p>
      </div>

      {error && (
        <div className="rounded-md border border-red-700/40 bg-red-900/20 px-3 py-2 text-sm text-red-400">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-sm text-slate-400">Загрузка…</div>
      ) : recipes.length === 0 ? (
        <div className="rounded-md border border-slate-700 bg-slate-900/50 px-4 py-6 text-sm text-slate-400">
          Рецептов пока нет. Они появятся автоматически после успешно
          выполненных многошаговых задач.
        </div>
      ) : (
        <div className="space-y-3">
          {recipes.map((recipe) => (
            <div
              key={recipe.id}
              className="rounded-lg border border-slate-700 bg-slate-900/50 p-4"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-slate-200">
                  {recipe.name}
                </span>
                <span
                  className={`rounded border px-2 py-0.5 text-xs ${STATUS_COLORS[recipe.status]}`}
                >
                  {STATUS_LABELS[recipe.status]}
                </span>
                <span className="text-xs text-slate-500">
                  роль: {recipe.role}
                </span>
                <span className="ml-auto text-xs text-slate-500">
                  ✓ {recipe.success_count} · ✗ {recipe.fail_count}
                </span>
              </div>
              {recipe.description && (
                <p className="mt-1 text-sm text-slate-400">
                  {recipe.description}
                </p>
              )}
              <div className="mt-2 text-xs text-slate-400">
                Шаги:{" "}
                <span className="font-mono text-slate-300">
                  {recipe.steps
                    .map((s) => `${s.capability}.${s.action || "call"}`)
                    .join(" → ")}
                </span>
              </div>
              {recipe.param_slots &&
                Object.keys(recipe.param_slots).length > 0 && (
                  <div className="mt-1 text-xs text-slate-500">
                    Параметры: {Object.keys(recipe.param_slots).join(", ")}
                  </div>
                )}
              <div className="mt-3 flex gap-2">
                {recipe.status !== "active" && (
                  <button
                    disabled={busyId === recipe.id}
                    onClick={() => act(recipe.id, "activate")}
                    className="rounded-md border border-green-700/40 bg-green-900/20 px-3 py-1 text-xs text-green-400 hover:bg-green-900/40 disabled:opacity-50"
                  >
                    Активировать
                  </button>
                )}
                {recipe.status !== "retired" && (
                  <button
                    disabled={busyId === recipe.id}
                    onClick={() => act(recipe.id, "retire")}
                    className="rounded-md border border-slate-600 bg-slate-800 px-3 py-1 text-xs text-slate-300 hover:bg-slate-700 disabled:opacity-50"
                  >
                    Отключить
                  </button>
                )}
                <button
                  disabled={busyId === recipe.id}
                  onClick={() => act(recipe.id, "delete")}
                  className="rounded-md border border-red-700/40 bg-red-900/20 px-3 py-1 text-xs text-red-400 hover:bg-red-900/40 disabled:opacity-50"
                >
                  Удалить
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
