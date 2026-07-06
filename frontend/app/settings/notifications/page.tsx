"use client";

import { useEffect, useState } from "react";
import {
  isNative,
  registerForPush,
  getServerConfig,
  clearServerConfig,
  checkForUpdate,
  installUpdate,
  getAppVersion,
} from "@/lib/native-bridge";
import AppLockSettings from "@/components/mobile/AppLockSettings";

const STORAGE_KEY = "notification_prefs";

const PREF_LABELS = [
  {
    key: "notify_approval_assigned",
    label: "Новое согласование назначено мне",
  },
  { key: "notify_approval_decided", label: "Решение по моему согласованию" },
  { key: "notify_document_ready", label: "Документ распознан" },
  { key: "notify_email_received", label: "Новое письмо в ящике" },
  { key: "notify_mention", label: "Упоминание в чате" },
  { key: "notify_handover", label: "Документ передан мне" },
];

interface DeviceOut {
  id: string;
  platform: string;
  app_version: string | null;
  enabled: boolean;
  created_at: string;
}

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
  const [native, setNative] = useState(false);
  const [devices, setDevices] = useState<DeviceOut[]>([]);
  const [registering, setRegistering] = useState(false);
  const [serverUrl, setServerUrl] = useState<string | null>(null);
  const [appVersion, setAppVersion] = useState<string | null>(null);
  const [updateState, setUpdateState] = useState<
    "idle" | "checking" | "latest" | "available" | "installing"
  >("idle");
  const [updateVersion, setUpdateVersion] = useState<string | null>(null);

  async function loadDevices() {
    try {
      const res = await fetch("/api/devices", { credentials: "include" });
      if (res.ok) setDevices(await res.json());
    } catch {
      /* ignore */
    }
  }

  useEffect(() => {
    setPrefs(loadPrefs());
    setNative(isNative());
    void loadDevices();
    void getServerConfig().then(setServerUrl);
    void getAppVersion().then((v) => v?.version && setAppVersion(v.version));
  }, []);

  async function checkUpdates() {
    setUpdateState("checking");
    try {
      const u = await checkForUpdate();
      if (u.available) {
        setUpdateVersion(u.versionName ?? null);
        setUpdateState("available");
      } else {
        setUpdateState("latest");
      }
    } catch {
      setUpdateState("idle");
    }
  }

  async function applyUpdate() {
    setUpdateState("installing");
    try {
      await installUpdate();
    } finally {
      setUpdateState("available");
    }
  }

  async function changeServer() {
    if (
      !confirm(
        "Сменить сервер? Приложение сбросит текущую сессию, push-настройки и вернётся к экрану ввода нового адреса.",
      )
    )
      return;
    await clearServerConfig();
  }

  async function enablePush() {
    setRegistering(true);
    try {
      await registerForPush();
      await loadDevices();
    } finally {
      setRegistering(false);
    }
  }

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

      {/* ── Mobile app ──────────────────────────────────────────────────── */}
      <h2 className="text-base font-semibold mt-10 mb-1">
        Мобильное приложение
      </h2>
      <p className="text-sm text-muted-foreground mb-4">
        Установите приложение «AI-DOCS» на Android, чтобы получать
        push-уведомления, снимать документы камерой и принимать файлы из других
        приложений.
      </p>

      <div className="border border-border rounded-lg p-4 space-y-4">
        <a
          href="/get-app"
          className="inline-flex items-center gap-2 text-sm font-medium text-primary hover:underline"
        >
          Открыть страницу установки (QR-код) →
        </a>

        {native && (
          <>
            <div className="flex items-center justify-between border-t border-border pt-4">
              <div className="min-w-0">
                <div className="text-sm font-medium">Сервер</div>
                <div className="truncate text-xs text-muted-foreground">
                  {serverUrl ?? "не задан"}
                </div>
              </div>
              <button
                onClick={changeServer}
                className="shrink-0 rounded-md border border-border px-3 py-1.5 text-sm font-medium hover:bg-muted/40"
              >
                Сменить
              </button>
            </div>

            <div className="flex items-center justify-between border-t border-border pt-4">
              <div>
                <div className="text-sm font-medium">
                  Push на этом устройстве
                </div>
                <div className="text-xs text-muted-foreground">
                  Зарегистрировано устройств: {devices.length}
                </div>
              </div>
              <button
                onClick={enablePush}
                disabled={registering}
                className="px-3 py-1.5 text-sm font-medium bg-primary text-primary-foreground rounded-md hover:bg-primary/90 disabled:opacity-60"
              >
                {registering ? "Включаю…" : "Включить push"}
              </button>
            </div>

            <AppLockSettings />

            <div className="flex items-center justify-between border-t border-border pt-4">
              <div className="min-w-0">
                <div className="text-sm font-medium">Обновления</div>
                <div className="truncate text-xs text-muted-foreground">
                  {appVersion
                    ? `Текущая версия: ${appVersion}`
                    : "Версия неизвестна"}
                  {updateState === "latest" && " · установлена последняя"}
                  {updateState === "available" &&
                    ` · доступна ${updateVersion ?? "новая"}`}
                </div>
              </div>
              {updateState === "available" || updateState === "installing" ? (
                <button
                  onClick={applyUpdate}
                  disabled={updateState === "installing"}
                  className="shrink-0 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-60"
                >
                  {updateState === "installing" ? "Загрузка…" : "Обновить"}
                </button>
              ) : (
                <button
                  onClick={checkUpdates}
                  disabled={updateState === "checking"}
                  className="shrink-0 rounded-md border border-border px-3 py-1.5 text-sm font-medium hover:bg-muted/40 disabled:opacity-60"
                >
                  {updateState === "checking" ? "Проверяю…" : "Проверить"}
                </button>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
