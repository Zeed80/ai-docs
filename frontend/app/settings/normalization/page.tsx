"use client";

import { useEffect, useState } from "react";
import {
  normalization as normApi,
  type NormRule,
  type NormRuleListResponse,
} from "@/lib/api-client";

const statusBadge: Record<string, string> = {
  proposed: "bg-amber-100 text-amber-700",
  active: "bg-green-100 text-green-700",
  disabled: "bg-slate-100 text-slate-500",
  rejected: "bg-red-100 text-red-700",
};

const statusLabel: Record<string, string> = {
  proposed: "Предложено",
  active: "Активно",
  disabled: "Отключено",
  rejected: "Отклонено",
};

export default function NormalizationSettingsPage() {
  const [data, setData] = useState<NormRuleListResponse | null>(null);
  const [filter, setFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [toast, setToast] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [suggesting, setSuggesting] = useState(false);

  // Create form state
  const [newRule, setNewRule] = useState({
    field_name: "",
    pattern: "",
    replacement: "",
    is_regex: false,
    description: "",
  });

  function showToast(msg: string) {
    setToast(msg);
    setTimeout(() => setToast(null), 3000);
  }

  async function fetchRules() {
    setLoading(true);
    try {
      const params: Record<string, string> = {};
      if (filter) params.status = filter;
      const result = await normApi.listRules(params);
      setData(result);
    } catch {
      setData(null);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchRules();
  }, [filter]);

  async function handleActivate(id: string) {
    try {
      await normApi.activateRule(id);
      showToast("Правило активировано");
      fetchRules();
    } catch {
      showToast("Ошибка активации");
    }
  }

  async function handleDisable(id: string) {
    try {
      await normApi.disableRule(id);
      showToast("Правило отключено");
      fetchRules();
    } catch {
      showToast("Ошибка отключения");
    }
  }

  async function handleCreate() {
    if (!newRule.field_name || !newRule.pattern || !newRule.replacement) return;
    try {
      await normApi.createRule(newRule);
      showToast("Правило создано");
      setShowCreate(false);
      setNewRule({
        field_name: "",
        pattern: "",
        replacement: "",
        is_regex: false,
        description: "",
      });
      fetchRules();
    } catch {
      showToast("Ошибка создания правила");
    }
  }

  async function handleSuggest() {
    setSuggesting(true);
    try {
      const result = await normApi.suggest({ min_corrections: 3 });
      if (result.suggested_rules.length > 0) {
        showToast(
          `Предложено ${result.suggested_rules.length} правил из ${result.total_corrections_analyzed} исправлений`,
        );
        fetchRules();
      } else {
        showToast(
          `Нет предложений (проанализировано ${result.total_corrections_analyzed} исправлений)`,
        );
      }
    } catch {
      showToast("Ошибка анализа");
    } finally {
      setSuggesting(false);
    }
  }

  const filters = ["", "proposed", "active", "disabled"];
  const filterLabels: Record<string, string> = {
    "": "Все",
    proposed: "Предложенные",
    active: "Активные",
    disabled: "Отключённые",
  };

  return (
    <div className="p-6 max-w-4xl mx-auto">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-xl font-bold">Правила нормализации</h1>
        <div className="flex gap-2">
          <button
            onClick={handleSuggest}
            disabled={suggesting}
            className="px-3 py-1.5 text-sm border border-slate-200 rounded-md hover:bg-slate-50 disabled:opacity-50"
          >
            {suggesting ? "Анализ..." : "Предложить правила"}
          </button>
          <button
            onClick={() => setShowCreate(!showCreate)}
            className="px-3 py-1.5 text-sm bg-blue-500 text-white rounded-md hover:bg-blue-600"
          >
            + Новое правило
          </button>
        </div>
      </div>

      {/* Create form */}
      {showCreate && (
        <div className="bg-white border border-slate-200 rounded-lg p-4 mb-4 space-y-3">
          <h3 className="text-sm font-semibold">Новое правило</h3>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-xs text-slate-500 block mb-1">Поле</label>
              <input
                value={newRule.field_name}
                onChange={(e) =>
                  setNewRule({ ...newRule, field_name: e.target.value })
                }
                placeholder="supplier_name"
                className="w-full text-sm border rounded px-3 py-1.5 focus:outline-none focus:ring-1 focus:ring-blue-400"
              />
            </div>
            <div>
              <label className="text-xs text-slate-500 block mb-1">
                Описание
              </label>
              <input
                value={newRule.description}
                onChange={(e) =>
                  setNewRule({ ...newRule, description: e.target.value })
                }
                placeholder="Нормализация названия поставщика"
                className="w-full text-sm border rounded px-3 py-1.5 focus:outline-none focus:ring-1 focus:ring-blue-400"
              />
            </div>
            <div>
              <label className="text-xs text-slate-500 block mb-1">
                Паттерн
              </label>
              <input
                value={newRule.pattern}
                onChange={(e) =>
                  setNewRule({ ...newRule, pattern: e.target.value })
                }
                placeholder="OOO AKME"
                className="w-full text-sm border rounded px-3 py-1.5 font-mono focus:outline-none focus:ring-1 focus:ring-blue-400"
              />
            </div>
            <div>
              <label className="text-xs text-slate-500 block mb-1">
                Замена
              </label>
              <input
                value={newRule.replacement}
                onChange={(e) =>
                  setNewRule({ ...newRule, replacement: e.target.value })
                }
                placeholder='ООО "АКМЕ"'
                className="w-full text-sm border rounded px-3 py-1.5 font-mono focus:outline-none focus:ring-1 focus:ring-blue-400"
              />
            </div>
          </div>
          <div className="flex items-center gap-4">
            <label className="flex items-center gap-1.5 text-sm">
              <input
                type="checkbox"
                checked={newRule.is_regex}
                onChange={(e) =>
                  setNewRule({ ...newRule, is_regex: e.target.checked })
                }
              />
              Regex
            </label>
            <div className="flex-1" />
            <button
              onClick={() => setShowCreate(false)}
              className="px-3 py-1.5 text-sm border rounded-md hover:bg-slate-50"
            >
              Отмена
            </button>
            <button
              onClick={handleCreate}
              disabled={
                !newRule.field_name || !newRule.pattern || !newRule.replacement
              }
              className="px-3 py-1.5 text-sm bg-blue-500 text-white rounded-md hover:bg-blue-600 disabled:opacity-50"
            >
              Создать
            </button>
          </div>
        </div>
      )}

      {/* Filters */}
      <div className="flex gap-2 mb-4">
        {filters.map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`px-3 py-1 text-sm rounded-full border transition-colors ${
              filter === f
                ? "bg-slate-800 text-white border-slate-800"
                : "bg-white text-slate-600 border-slate-200 hover:bg-slate-50"
            }`}
          >
            {filterLabels[f]}
          </button>
        ))}
      </div>

      {/* Rules list */}
      {loading ? (
        <div className="text-slate-400 py-8 text-center">Загрузка...</div>
      ) : !data || data.items.length === 0 ? (
        <div className="text-slate-400 py-8 text-center">
          Нет правил нормализации
        </div>
      ) : (
        <div className="space-y-2">
          {data.items.map((rule: NormRule) => (
            <div
              key={rule.id}
              className="bg-white border border-slate-200 rounded-lg p-4"
            >
              <div className="flex items-start justify-between">
                <div className="flex-1">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-sm font-medium">
                      {rule.field_name}
                    </span>
                    <span
                      className={`px-2 py-0.5 text-xs rounded-full ${statusBadge[rule.status] ?? "bg-slate-100"}`}
                    >
                      {statusLabel[rule.status] ?? rule.status}
                    </span>
                    {rule.is_regex && (
                      <span className="px-1.5 py-0.5 text-[10px] bg-purple-100 text-purple-600 rounded">
                        regex
                      </span>
                    )}
                  </div>
                  <div className="text-sm font-mono">
                    <span className="text-red-500 line-through">
                      {rule.pattern}
                    </span>
                    <span className="mx-2 text-slate-400">&rarr;</span>
                    <span className="text-green-600">{rule.replacement}</span>
                  </div>
                  {rule.description && (
                    <p className="text-xs text-slate-500 mt-1">
                      {rule.description}
                    </p>
                  )}
                  <div className="flex gap-4 mt-2 text-xs text-slate-400">
                    <span>Исправлений: {rule.source_corrections}</span>
                    <span>Применений: {rule.apply_count}</span>
                    <span>Автор: {rule.suggested_by}</span>
                    {rule.activated_by && (
                      <span>Активировал: {rule.activated_by}</span>
                    )}
                  </div>
                </div>
                <div className="flex gap-1.5 ml-4">
                  {rule.status === "proposed" && (
                    <button
                      onClick={() => handleActivate(rule.id)}
                      className="px-2.5 py-1 text-xs bg-green-500 text-white rounded hover:bg-green-600"
                    >
                      Активировать
                    </button>
                  )}
                  {rule.status === "active" && (
                    <button
                      onClick={() => handleDisable(rule.id)}
                      className="px-2.5 py-1 text-xs border border-slate-200 rounded hover:bg-slate-50"
                    >
                      Отключить
                    </button>
                  )}
                  {rule.status === "disabled" && (
                    <button
                      onClick={() => handleActivate(rule.id)}
                      className="px-2.5 py-1 text-xs border border-slate-200 rounded hover:bg-slate-50"
                    >
                      Включить
                    </button>
                  )}
                </div>
              </div>
            </div>
          ))}
          <div className="text-xs text-slate-400 text-right mt-2">
            {data.total} всего
          </div>
        </div>
      )}

      {/* Toast */}
      {toast && (
        <div className="fixed bottom-4 left-1/2 -translate-x-1/2 px-4 py-2 bg-slate-800 text-white text-sm rounded-lg shadow-lg z-50">
          {toast}
        </div>
      )}
    </div>
  );
}
