"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { useEffect, useState, useMemo } from "react";
import Link from "next/link";

const API = getApiBaseUrl();

interface Skill {
  name: string;
  description: string;
  method: string;
  path: string;
  enabled: boolean;
  approval_required: boolean;
  gate_actions?: string[];
}

interface Plugin {
  id: string;
  plugin_key: string;
  name: string;
  version: string;
  description: string | null;
  enabled: boolean;
  risk_level: string;
}

const CATEGORY_MAP: Record<string, string> = {
  document: "Документы",
  invoice: "Счета",
  email: "Почта",
  supplier: "Поставщики",
  anomaly: "Аномалии",
  table: "Таблицы и экспорт",
  approval: "Согласования",
  calendar: "Календарь",
  collection: "Коллекции",
  normalization: "Нормализация",
  search: "Поиск",
  compare: "Сравнение КП",
  audit: "Аудит",
  payment: "Платежи",
  warehouse: "Склад",
  procurement: "Закупки",
};

const RISK_COLORS: Record<string, string> = {
  low: "bg-green-900/30 text-green-400 border-green-700/40",
  medium: "bg-amber-900/30 text-amber-400 border-amber-700/40",
  high: "bg-red-900/30 text-red-400 border-red-700/40",
};

function deriveCategory(name: string): string {
  const lower = name.toLowerCase();
  for (const [prefix, label] of Object.entries(CATEGORY_MAP)) {
    if (lower.startsWith(prefix + ".") || lower.startsWith(prefix + "_")) {
      return label;
    }
  }
  return "Прочее";
}

export default function SkillsMarketplacePage() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [plugins, setPlugins] = useState<Plugin[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");
  const [exposedSkills, setExposedSkills] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    Promise.all([
      fetch(`${API}/api/ai/agent-skills`).then((r) =>
        r.ok ? r.json() : { skills: [] },
      ),
      fetch(`${API}/api/agent/plugins`).then((r) => (r.ok ? r.json() : [])),
      fetch(`${API}/api/ai/settings`).then((r) => (r.ok ? r.json() : null)),
    ])
      .then(([skillsData, pluginsData, config]) => {
        setSkills(skillsData.skills ?? []);
        setPlugins(Array.isArray(pluginsData) ? pluginsData : []);
        if (config?.exposed_skills) {
          setExposedSkills(new Set(config.exposed_skills as string[]));
        } else {
          setExposedSkills(
            new Set(
              (skillsData.skills ?? [])
                .filter((s: Skill) => s.enabled)
                .map((s: Skill) => s.name),
            ),
          );
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const categories = useMemo(() => {
    const cats = new Set(skills.map((s) => deriveCategory(s.name)));
    return Array.from(cats).sort();
  }, [skills]);

  const filtered = useMemo(() => {
    const q = search.toLowerCase();
    return skills.filter((s) => {
      if (categoryFilter && deriveCategory(s.name) !== categoryFilter)
        return false;
      if (
        q &&
        !s.name.toLowerCase().includes(q) &&
        !s.description.toLowerCase().includes(q)
      )
        return false;
      return true;
    });
  }, [skills, search, categoryFilter]);

  const grouped = useMemo(() => {
    const map: Record<string, Skill[]> = {};
    for (const s of filtered) {
      const cat = deriveCategory(s.name);
      if (!map[cat]) map[cat] = [];
      map[cat].push(s);
    }
    return Object.entries(map).sort(([a], [b]) => a.localeCompare(b, "ru"));
  }, [filtered]);

  function toggleSkill(name: string) {
    setExposedSkills((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
    setDirty(true);
    setSaved(false);
  }

  async function saveExposed() {
    setSaving(true);
    try {
      const configR = await fetch(`${API}/api/ai/settings`);
      if (!configR.ok) return;
      const config = await configR.json();
      await fetch(`${API}/api/ai/settings`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...config,
          exposed_skills: Array.from(exposedSkills).sort(),
        }),
      });
      setDirty(false);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } finally {
      setSaving(false);
    }
  }

  async function togglePlugin(pluginKey: string, enable: boolean) {
    await fetch(
      `${API}/api/agent/plugins/${pluginKey}/${enable ? "enable" : "disable"}`,
      {
        method: "POST",
      },
    );
    setPlugins((prev) =>
      prev.map((p) =>
        p.plugin_key === pluginKey ? { ...p, enabled: enable } : p,
      ),
    );
  }

  if (loading) {
    return <div className="p-6 text-slate-400">Загрузка навыков...</div>;
  }

  const enabledCount = exposedSkills.size;
  const totalCount = skills.length;

  return (
    <div className="p-6 max-w-5xl mx-auto">
      {/* Header */}
      <div className="flex items-start justify-between mb-6 gap-4">
        <div>
          <div className="flex items-center gap-2 text-xs text-slate-500 mb-2">
            <Link href="/settings" className="hover:text-slate-300">
              Настройки
            </Link>
            <span>/</span>
            <span className="text-slate-400">Навыки агента</span>
          </div>
          <h1 className="text-2xl font-bold text-white">Маркетплейс навыков</h1>
          <p className="text-slate-400 text-sm mt-1">
            {enabledCount} из {totalCount} навыков активны
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {dirty && (
            <button
              onClick={() => void saveExposed()}
              disabled={saving}
              className="px-4 py-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white rounded-lg text-sm font-medium transition-colors"
            >
              {saving ? "Сохранение..." : "Сохранить изменения"}
            </button>
          )}
          {saved && <span className="text-xs text-green-400">✓ Сохранено</span>}
        </div>
      </div>

      {/* Filters */}
      <div className="flex gap-2 mb-5 flex-wrap">
        <input
          type="text"
          placeholder="Поиск навыка..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="flex-1 min-w-[180px] bg-zinc-800 border border-white/10 text-white rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500/60 placeholder-white/30"
        />
        <select
          value={categoryFilter}
          onChange={(e) => setCategoryFilter(e.target.value)}
          className="bg-zinc-800 border border-white/10 text-white/70 rounded-lg px-3 py-2 text-sm focus:outline-none"
        >
          <option value="">Все категории</option>
          {categories.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
        <button
          onClick={() => {
            setExposedSkills(new Set(skills.map((s) => s.name)));
            setDirty(true);
          }}
          className="px-3 py-2 text-xs bg-zinc-800 hover:bg-zinc-700 text-white/60 border border-white/10 rounded-lg"
        >
          Включить все
        </button>
        <button
          onClick={() => {
            setExposedSkills(new Set());
            setDirty(true);
          }}
          className="px-3 py-2 text-xs bg-zinc-800 hover:bg-zinc-700 text-white/60 border border-white/10 rounded-lg"
        >
          Отключить все
        </button>
      </div>

      {/* Skills by category */}
      {grouped.length === 0 ? (
        <div className="text-slate-400 text-sm py-12 text-center">
          Навыки не найдены
        </div>
      ) : (
        <div className="space-y-6">
          {grouped.map(([category, catSkills]) => (
            <div key={category}>
              <div className="flex items-center gap-2 mb-3">
                <h2 className="text-sm font-semibold text-white/60 uppercase tracking-wide">
                  {category}
                </h2>
                <span className="text-xs text-white/20">
                  {catSkills.filter((s) => exposedSkills.has(s.name)).length}/
                  {catSkills.length}
                </span>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
                {catSkills.map((skill) => {
                  const active = exposedSkills.has(skill.name);
                  return (
                    <div
                      key={skill.name}
                      onClick={() => toggleSkill(skill.name)}
                      className={`cursor-pointer rounded-xl border p-3 transition-all ${
                        active
                          ? "bg-blue-950/30 border-blue-600/40 hover:border-blue-500/60"
                          : "bg-zinc-900 border-white/10 hover:border-white/20 opacity-60 hover:opacity-80"
                      }`}
                    >
                      <div className="flex items-start justify-between gap-2 mb-1">
                        <span className="text-xs font-mono text-white/80 truncate">
                          {skill.name}
                        </span>
                        <div className="flex items-center gap-1 shrink-0">
                          {skill.approval_required && (
                            <span
                              className="text-[10px] px-1.5 py-0.5 bg-amber-900/30 text-amber-400 border border-amber-700/30 rounded"
                              title="Требует подтверждения"
                            >
                              gate
                            </span>
                          )}
                          <div
                            className={`w-3 h-3 rounded-full border transition-colors ${
                              active
                                ? "bg-blue-500 border-blue-400"
                                : "bg-zinc-700 border-zinc-600"
                            }`}
                          />
                        </div>
                      </div>
                      {skill.description && (
                        <p className="text-xs text-white/40 line-clamp-2">
                          {skill.description}
                        </p>
                      )}
                      <div className="mt-2 flex items-center gap-1">
                        <span className="text-[10px] font-mono text-white/20 bg-zinc-800 px-1.5 py-0.5 rounded">
                          {skill.method}
                        </span>
                        <span className="text-[10px] text-white/20 truncate">
                          {skill.path}
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Plugins section */}
      {plugins.length > 0 && (
        <div className="mt-10">
          <h2 className="text-lg font-bold text-white mb-4">Плагины</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {plugins.map((plugin) => (
              <div
                key={plugin.id}
                className={`rounded-xl border p-4 ${
                  plugin.enabled
                    ? "bg-zinc-900 border-white/10"
                    : "bg-zinc-900/50 border-white/5 opacity-60"
                }`}
              >
                <div className="flex items-start justify-between gap-2 mb-2">
                  <div>
                    <span className="text-sm font-medium text-white">
                      {plugin.name}
                    </span>
                    <span className="ml-2 text-xs text-white/30">
                      v{plugin.version}
                    </span>
                  </div>
                  <span
                    className={`text-[10px] px-1.5 py-0.5 rounded border ${RISK_COLORS[plugin.risk_level] ?? "bg-zinc-800 text-white/40 border-white/10"}`}
                  >
                    {plugin.risk_level}
                  </span>
                </div>
                {plugin.description && (
                  <p className="text-xs text-white/40 mb-3 line-clamp-2">
                    {plugin.description}
                  </p>
                )}
                <button
                  onClick={() =>
                    void togglePlugin(plugin.plugin_key, !plugin.enabled)
                  }
                  className={`w-full py-1.5 rounded text-xs font-medium transition-colors ${
                    plugin.enabled
                      ? "bg-red-900/30 hover:bg-red-900/50 text-red-300 border border-red-700/30"
                      : "bg-green-900/30 hover:bg-green-900/50 text-green-300 border border-green-700/30"
                  }`}
                >
                  {plugin.enabled ? "Отключить" : "Включить"}
                </button>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
