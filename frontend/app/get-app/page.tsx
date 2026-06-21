"use client";

import { useEffect, useState } from "react";
import QRCode from "qrcode";

interface VersionInfo {
  versionName?: string;
  versionCode?: number;
  changelog?: string;
  minSdk?: number;
  sha256?: string;
  url?: string;
}

const APK_URL = "/download/latest.apk";
const VERSION_URL = "/download/version.json";

/**
 * Public landing page for installing the Android app: direct APK link + QR code.
 * Lives at /get-app (frontend); the machine paths /download/latest.apk and
 * /download/version.json are served by the backend and routed there by Traefik.
 * Excluded from the auth redirect in middleware.
 */
export default function DownloadPage() {
  const [qr, setQr] = useState<string | null>(null);
  const [serverQr, setServerQr] = useState<string | null>(null);
  const [version, setVersion] = useState<VersionInfo | null>(null);
  const [absUrl, setAbsUrl] = useState(APK_URL);
  const [origin, setOrigin] = useState("");

  useEffect(() => {
    const base = typeof window !== "undefined" ? window.location.origin : "";
    const url = `${base}${APK_URL}`;
    setAbsUrl(url);
    setOrigin(base);
    // QR #1 — download the APK.
    QRCode.toDataURL(url, { width: 240, margin: 1 })
      .then(setQr)
      .catch(() => setQr(null));
    // QR #2 — server address for the app's first-run setup screen.
    if (base) {
      QRCode.toDataURL(base, { width: 240, margin: 1 })
        .then(setServerQr)
        .catch(() => setServerQr(null));
    }
    fetch(VERSION_URL)
      .then((r) => (r.ok ? r.json() : null))
      .then((v) => v && setVersion(v))
      .catch(() => {});
  }, []);

  return (
    <div className="mx-auto flex min-h-screen max-w-md flex-col items-center gap-6 p-6 text-slate-100">
      <h1 className="mt-4 text-2xl font-semibold">AI-DOCS для Android</h1>

      {/* Шаг 1 — установка */}
      <div className="w-full">
        <h2 className="mb-1 text-sm font-semibold text-slate-200">
          Шаг 1. Установите приложение
        </h2>
        <p className="mb-3 text-center text-sm text-slate-400">
          Отсканируйте QR-код камерой телефона или нажмите кнопку. Требуется
          Android 9 и новее.
        </p>
        <div className="flex flex-col items-center gap-3">
          <div className="rounded-2xl bg-white p-4">
            {qr ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={qr}
                alt="QR для скачивания APK"
                width={240}
                height={240}
              />
            ) : (
              <div className="flex h-[240px] w-[240px] items-center justify-center text-slate-400">
                …
              </div>
            )}
          </div>
          <a
            href={APK_URL}
            className="w-full rounded-lg bg-sky-500 py-3 text-center text-sm font-medium text-white"
          >
            Скачать APK
          </a>
          <p className="break-all text-center text-xs text-slate-500">
            {absUrl}
          </p>
        </div>
      </div>

      {/* Шаг 2 — подключение к серверу */}
      <div className="w-full border-t border-slate-700 pt-6">
        <h2 className="mb-1 text-sm font-semibold text-slate-200">
          Шаг 2. Подключите приложение к серверу
        </h2>
        <p className="mb-3 text-center text-sm text-slate-400">
          При первом запуске приложение спросит адрес сервера. Отсканируйте этот
          QR-код на экране настройки — или введите адрес вручную.
        </p>
        <div className="flex flex-col items-center gap-3">
          <div className="rounded-2xl bg-white p-4">
            {serverQr ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={serverQr}
                alt="QR с адресом сервера"
                width={240}
                height={240}
              />
            ) : (
              <div className="flex h-[240px] w-[240px] items-center justify-center text-slate-400">
                …
              </div>
            )}
          </div>
          <p className="break-all text-center text-xs text-slate-500">
            {origin}
          </p>
        </div>
      </div>

      {version && (
        <div className="w-full rounded-lg border border-slate-700 bg-slate-800/50 p-4 text-sm">
          <div className="flex justify-between text-slate-300">
            <span>Версия</span>
            <span>
              {version.versionName ?? "—"}
              {version.versionCode ? ` (${version.versionCode})` : ""}
            </span>
          </div>
          {version.changelog && (
            <p className="mt-2 whitespace-pre-line text-slate-400">
              {version.changelog}
            </p>
          )}
        </div>
      )}

      <p className="text-center text-xs text-slate-500">
        После установки приложение само предложит обновление, когда выйдет новая
        версия — переустанавливать вручную не нужно.
      </p>
    </div>
  );
}
