"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import QRCode from "qrcode";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { ProtectedRoute } from "@/components/auth/protected-route";

const API = getApiBaseUrl();
const APK_URL = "/download/latest.apk";

interface BuildStatus {
  state: "idle" | "building" | "success" | "failed";
  version_name: string | null;
  version_code: number | null;
  apk_available: boolean;
  log_tail: string | null;
}

const STATE_LABEL: Record<BuildStatus["state"], string> = {
  idle: "Готово",
  building: "Идёт сборка…",
  success: "Сборка завершена",
  failed: "Ошибка сборки",
};

function MobileAppContent() {
  const [status, setStatus] = useState<BuildStatus | null>(null);
  const [qr, setQr] = useState<string | null>(null);
  const [apkQr, setApkQr] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const loadStatus = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/mobile-build/status`, {
        credentials: "include",
      });
      if (res.ok) setStatus(await res.json());
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    void loadStatus();
    const origin = typeof window !== "undefined" ? window.location.origin : "";
    // QR to the install page (server config + APK) and a direct-APK QR.
    QRCode.toDataURL(`${origin}/get-app`, { width: 200, margin: 1 })
      .then(setQr)
      .catch(() => {});
    QRCode.toDataURL(`${origin}${APK_URL}`, { width: 200, margin: 1 })
      .then(setApkQr)
      .catch(() => {});
  }, [loadStatus]);

  // Poll while a build is running.
  useEffect(() => {
    if (status?.state === "building" && !pollRef.current) {
      pollRef.current = setInterval(loadStatus, 4000);
    } else if (status?.state !== "building" && pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [status?.state, loadStatus]);

  async function startBuild() {
    setStarting(true);
    setError(null);
    try {
      const res = await fetch(`${API}/api/mobile-build/build`, {
        method: "POST",
        credentials: "include",
        headers: csrfHeaders(),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${res.status}`);
      }
      setStatus(await res.json());
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setStarting(false);
    }
  }

  const building = status?.state === "building";

  return (
    <div className="max-w-3xl space-y-6">
      <div>
        <h2 className="text-base font-semibold mb-1">Мобильное приложение</h2>
        <p className="text-sm text-muted-foreground">
          Соберите Android-приложение «Света» на сервере и раздайте по ссылке
          или QR-коду. Приложение само обновится у пользователей при новой
          сборке.
        </p>
      </div>

      {/* Build */}
      <div className="rounded-lg border border-border p-4 space-y-3">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-sm font-medium">Сборка</div>
            <div className="text-xs text-muted-foreground">
              Текущая версия:{" "}
              {status?.version_name
                ? `${status.version_name} (${status.version_code})`
                : "ещё не собрана"}
              {" · "}
              {status ? STATE_LABEL[status.state] : "…"}
            </div>
          </div>
          <button
            onClick={startBuild}
            disabled={starting || building}
            className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-60"
          >
            {building
              ? "Сборка…"
              : starting
                ? "Запуск…"
                : "Собрать / пересобрать"}
          </button>
        </div>

        {error && <p className="text-xs text-destructive">Ошибка: {error}</p>}

        {status?.log_tail && (
          <pre className="max-h-48 overflow-auto rounded bg-muted p-2 text-[11px] leading-snug text-muted-foreground whitespace-pre-wrap">
            {status.log_tail}
          </pre>
        )}
        <p className="text-[11px] text-muted-foreground">
          Первая сборка дольше (скачиваются зависимости). Подписывается
          постоянным ключом, поэтому обновление ставится «поверх».
        </p>
      </div>

      {/* Download */}
      <div className="rounded-lg border border-border p-4">
        <div className="text-sm font-medium mb-3">Скачивание</div>
        {status?.apk_available ? (
          <div className="flex flex-wrap gap-6">
            <div className="flex flex-col items-center gap-2">
              <div className="rounded-lg bg-white p-2">
                {qr ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={qr}
                    alt="QR на страницу установки"
                    width={160}
                    height={160}
                  />
                ) : (
                  <div className="h-40 w-40" />
                )}
              </div>
              <span className="text-xs text-muted-foreground">
                Страница установки
              </span>
            </div>
            <div className="flex flex-col items-center gap-2">
              <div className="rounded-lg bg-white p-2">
                {apkQr ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={apkQr}
                    alt="QR прямой ссылки на APK"
                    width={160}
                    height={160}
                  />
                ) : (
                  <div className="h-40 w-40" />
                )}
              </div>
              <span className="text-xs text-muted-foreground">
                Прямая ссылка на APK
              </span>
            </div>
            <div className="flex flex-col justify-center gap-2 text-sm">
              <a href={APK_URL} className="text-primary hover:underline">
                Скачать APK ↓
              </a>
              <a
                href="/get-app"
                className="text-primary hover:underline"
                target="_blank"
              >
                Открыть страницу установки →
              </a>
            </div>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">
            APK ещё не собран. Нажмите «Собрать / пересобрать».
          </p>
        )}
      </div>
    </div>
  );
}

export default function MobileAppSettingsPage() {
  return (
    <ProtectedRoute requiredRoles={["admin"]}>
      <MobileAppContent />
    </ProtectedRoute>
  );
}
