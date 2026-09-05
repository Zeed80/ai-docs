"use client";

import { useCallback, useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { useToast } from "@/components/ui/primitives/Toast";
import {
  btn,
  card,
  cardHeader as cardH,
  input,
} from "@/components/ui/primitives/tokens";
import { providerLabel } from "@/lib/models/labels";

const API = getApiBaseUrl();
const btnPrimary = `${btn} bg-blue-600 hover:bg-blue-700 text-white disabled:opacity-50`;
const btnSecondary = `${btn} bg-slate-700 hover:bg-slate-600 text-slate-200`;
const btnDanger = `${btn} bg-red-700 hover:bg-red-600 text-white`;

/** Профиль параметров инференса: температура, top_p и прочее. */
interface Profile {
  name: string;
  description?: string;
  builtin: boolean;
  temperature?: number;
  top_p?: number;
  top_k?: number;
  repeat_penalty?: number;
}

interface ProviderDefaults {
  defaults: Record<string, Record<string, unknown>>;
  total_vram_gb: number;
}

const PROFILE_LABELS: Record<string, string> = {
  anti_hallucination: "Без галлюцинаций",
  structured_reasoning: "Структ. рассуждение",
  balanced: "Баланс",
  creative: "Творческий",
};

export function ParametersPanel() {
  const toast = useToast();
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [selected, setSelected] = useState("anti_hallucination");
  const [editing, setEditing] = useState<Partial<Profile> | null>(null);
  const [newName, setNewName] = useState("");
  const [saving, setSaving] = useState(false);
  const [defaults, setDefaults] = useState<ProviderDefaults | null>(null);

  useEffect(() => {
    fetch(`${API}/api/local-models/parameter-profiles`, {
      credentials: "include",
    })
      .then((r) => r.json())
      .then((d) => setProfiles(d.profiles || []))
      .catch(() => {});
    fetch(`${API}/api/local-models/provider-defaults`, {
      credentials: "include",
    })
      .then((r) => r.json())
      .then((d) => setDefaults(d))
      .catch(() => {});
  }, []);

  const current = profiles.find((p) => p.name === selected);

  const saveCustom = async () => {
    if (!editing || !newName.trim()) return;
    setSaving(true);
    try {
      await fetch(
        `${API}/api/local-models/parameter-profiles/${encodeURIComponent(newName)}`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(await csrfHeaders()),
          },
          credentials: "include",
          body: JSON.stringify({
            params: {
              ...editing,
              description: `Кастомный профиль: ${newName}`,
            },
          }),
        },
      );
      const r = await fetch(`${API}/api/local-models/parameter-profiles`, {
        credentials: "include",
      });
      const d = await r.json();
      setProfiles(d.profiles || []);
      setSelected(newName);
      setEditing(null);
      setNewName("");
    } catch (e) {
      toast.error("Профиль не сохранён", String(e));
      /* ignore */
    }
    setSaving(false);
  };

  const deleteProfile = async (name: string) => {
    if (!confirm(`Удалить профиль «${name}»?`)) return;
    await fetch(
      `${API}/api/local-models/parameter-profiles/${encodeURIComponent(name)}`,
      {
        method: "DELETE",
        headers: await csrfHeaders(),
        credentials: "include",
      },
    );
    const r = await fetch(`${API}/api/local-models/parameter-profiles`, {
      credentials: "include",
    });
    const d = await r.json();
    setProfiles(d.profiles || []);
    setSelected("anti_hallucination");
  };

  return (
    <div className="space-y-6">
      {/* Profile selector */}
      <div className={card}>
        <div className={cardH}>
          <span className="text-sm font-medium text-slate-100">
            Профили параметров инференса
          </span>
        </div>
        <div className="p-4 space-y-4">
          <div className="flex gap-2 flex-wrap">
            {profiles.map((p) => (
              <button
                key={p.name}
                onClick={() => {
                  setSelected(p.name);
                  setEditing(null);
                }}
                className={`${btn} ${selected === p.name ? "bg-blue-600 text-white" : "bg-slate-700 text-slate-300"}`}
              >
                {PROFILE_LABELS[p.name] ?? p.name}
              </button>
            ))}
          </div>

          {current && (
            <div className="space-y-3 pt-2 border-t border-slate-700">
              {current.description && (
                <div className="text-xs text-slate-400">
                  {current.description}
                </div>
              )}
              {(
                [
                  {
                    key: "temperature",
                    label: "Temperature",
                    min: 0,
                    max: 2,
                    step: 0.05,
                    desc: "0 = детерминировано, >1 = случайно",
                  },
                  {
                    key: "top_p",
                    label: "Top-P",
                    min: 0,
                    max: 1,
                    step: 0.05,
                    desc: "Nucleus sampling threshold",
                  },
                  {
                    key: "top_k",
                    label: "Top-K",
                    min: 1,
                    max: 100,
                    step: 1,
                    desc: "Ограничение выборки по топ-K токенов",
                  },
                  {
                    key: "repeat_penalty",
                    label: "Repeat Penalty",
                    min: 1,
                    max: 2,
                    step: 0.05,
                    desc: "Штраф за повтор (1 = нет)",
                  },
                ] as const
              ).map(({ key, label, min, max, step, desc }) => {
                const val = (editing ?? current)[key as keyof Profile] as
                  number | undefined;
                return (
                  <div key={key} className="space-y-1">
                    <div className="flex justify-between text-xs">
                      <span className="text-slate-300">{label}</span>
                      <span className="text-slate-400 font-mono">
                        {val?.toFixed(2) ?? "—"}
                      </span>
                    </div>
                    <input
                      type="range"
                      min={min}
                      max={max}
                      step={step}
                      value={val ?? 0}
                      disabled={current.builtin && !editing}
                      onChange={(e) => {
                        const v = parseFloat(e.target.value);
                        setEditing((prev) => ({
                          ...(prev ?? current),
                          [key]: v,
                        }));
                      }}
                      className="w-full accent-blue-500"
                    />
                    <div className="text-xs text-slate-400">{desc}</div>
                  </div>
                );
              })}

              {editing && (
                <div className="pt-2 border-t border-slate-700 space-y-2">
                  <input
                    className={input}
                    placeholder="Название нового профиля..."
                    value={newName}
                    onChange={(e) => setNewName(e.target.value)}
                  />
                  <div className="flex gap-2">
                    <button
                      onClick={saveCustom}
                      disabled={saving || !newName.trim()}
                      className={btnPrimary}
                    >
                      {saving ? "Сохранение..." : "Сохранить профиль"}
                    </button>
                    <button
                      onClick={() => setEditing(null)}
                      className={btnSecondary}
                    >
                      Отмена
                    </button>
                  </div>
                </div>
              )}
              {!editing && current.builtin && (
                <button
                  onClick={() => setEditing({ ...current })}
                  className={btnSecondary}
                >
                  Создать на основе этого...
                </button>
              )}
              {!editing && !current.builtin && (
                <button
                  onClick={() => deleteProfile(current.name)}
                  className={btnDanger}
                >
                  Удалить профиль
                </button>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Provider hardware defaults */}
      {defaults && (
        <div className={card}>
          <div className={cardH}>
            <span className="text-sm font-medium text-slate-100">
              Параметры провайдеров (RTX 3090 · {defaults.total_vram_gb} GB)
            </span>
          </div>
          <div className="p-4 grid grid-cols-1 sm:grid-cols-3 gap-4">
            {Object.entries(defaults.defaults).map(([provName, params]) => (
              <div
                key={provName}
                className="bg-slate-900 rounded p-3 border border-slate-700"
              >
                <div className="text-sm font-medium text-slate-100 mb-2">
                  {providerLabel(provName)}
                </div>
                <div className="space-y-1">
                  {Object.entries(params as Record<string, unknown>).map(
                    ([k, v]) => (
                      <div key={k} className="flex justify-between text-xs">
                        <span className="text-slate-400">{k}</span>
                        <span className="text-slate-300 font-mono">
                          {String(v)}
                        </span>
                      </div>
                    ),
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
