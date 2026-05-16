"use client";

import { useEffect, useState } from "react";

const STORAGE_KEY = "notification_prefs";

const PREF_LABELS = [
  {
    key: "notify_approval_assigned",
    label: "Новое согласование назначено мне",
  },
  { key: "notify_approval_decided", label: "Решение по моему согласованию" },
  { key: "notify_document_ready", label: "Документ распознан" },
  { key: "notify_mention", label: "Упоминание в чате" },
  { key: "notify_handover", label: "Документ передан мне" },
];

type Prefs = Record<string, boolean>;

function loadPrefs(): Prefs {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw) as Prefs;
  } catch {}
  return Object.fromEntries(PREF_LABELS.map((p) => [p.key, true]));
}

export default function NotificationsSettingsPage() {
  const [prefs, setPrefs] = useState<Prefs>(() =>
    Object.fromEntries(PREF_LABELS.map((p) => [p.key, true])),
  );
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setPrefs(loadPrefs());
  }, []);

  function toggle(key: string) {
    setPrefs((prev) => ({ ...prev, [key]: !prev[key] }));
    setSaved(false);
  }

  function save() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  }

  return (
    <div className="max-w-lg">
      <h2 className="text-base font-semibold mb-1">Уведомления</h2>
      <p className="text-sm text-muted-foreground mb-6">
        Выберите, о каких событиях получать уведомления.
      </p>

      <div className="border border-border rounded-lg divide-y divide-border">
        {PREF_LABELS.map(({ key, label }) => (
          <label
            key={key}
            className="flex items-center justify-between px-4 py-3 cursor-pointer hover:bg-muted/40 transition-colors"
          >
            <span className="text-sm">{label}</span>
            <button
              role="switch"
              aria-checked={prefs[key]}
              onClick={() => toggle(key)}
              className={`relative inline-flex h-5 w-9 shrink-0 rounded-full border-2 border-transparent transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                prefs[key] ? "bg-primary" : "bg-muted"
              }`}
            >
              <span
                className={`pointer-events-none inline-block h-4 w-4 rounded-full bg-background shadow transform transition-transform ${
                  prefs[key] ? "translate-x-4" : "translate-x-0"
                }`}
              />
            </button>
          </label>
        ))}
      </div>

      <div className="mt-4 flex items-center gap-3">
        <button
          onClick={save}
          className="px-4 py-2 text-sm font-medium bg-primary text-primary-foreground rounded-md hover:bg-primary/90 transition-colors"
        >
          Сохранить
        </button>
        {saved && (
          <span className="text-sm text-muted-foreground">Сохранено</span>
        )}
      </div>
    </div>
  );
}
